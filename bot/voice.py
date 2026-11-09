"""Diffusion directe (optionnelle) : le bot rejoint un salon vocal et y envoie la sortie
« Voicemeeter Out B2 », celle que le compte RADIO CREEP utilise comme micro dans Chrome.

Le routage audio ne change pas (Apple Music → AUX → B2) : le bot devient simplement un
auditeur de plus de B2, en capture partagée (Chrome peut continuer à l'utiliser en même
temps). Activée par /diffusion (DJ) ou STREAM_CHANNEL_ID dans .env.

Le bot se reconnecte tout seul (coupure réseau, redémarrage, déconnexion par un modo)."""
from __future__ import annotations

import array
import asyncio
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .client import RadioBot

log = logging.getLogger(__name__)

RATE = 48000  # Discord : 48 kHz stéréo 16 bits, trames de 20 ms
FRAME_BYTES = 3840
SAMPLE_BYTES = 4  # un échantillon stéréo 16 bits
# Réserve (« jitter buffer ») entre la capture et l'envoi à Discord. Une radio n'a pas besoin
# de faible latence : on garde une bonne marge pour encaisser les à-coups (bot occupé un
# instant, envoi qui rattrape son retard) sans trou dans le son.
TARGET_FRAMES = 6  # 120 ms visés
# 2 s : si l'envoi se fige un instant, discord.py rattrape ensuite d'un coup ; on garde donc
# tout ce qui a été capté pendant le gel (le rattrapage ramène pile à la cible). Au-delà,
# retour brutal à la cible (cas exceptionnel).
MAX_FRAMES = 100
STALL_WARN = 0.150  # un trou plus long dans la capture ou l'envoi est noté dans le log
DRIFT_MARGIN = 2 * FRAME_BYTES  # écart toléré avant de compenser la dérive d'horloge
STATS_EVERY = 4  # bilan dans les logs tous les 4 passages du superviseur (~1 min)
SUPERVISE_EVERY = 15.0
E2EE_GRACE = 20.0  # chiffrement de bout en bout (DAVE) pas prêt au-delà : on se reconnecte


class StreamError(Exception):
    pass


# PortAudio (sounddevice) initialise COM dans le thread qui l'appelle. Dans le thread de la
# boucle asyncio, ça bloque ensuite les appels WinRT (pochette SMTC : annonces et /now figés).
# Tout ce qui touche à PortAudio passe donc par ce thread dédié, y compris l'import.
_AUDIO = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio")


def _raise_priority() -> None:
    """Priorité « supérieure à la normale » : un jeu ou une appli gourmande ne doit pas
    priver la diffusion de processeur (sinon le son hache)."""
    try:
        import ctypes
        from ctypes import wintypes

        # Types explicites : sans eux, le pseudo-handle du processus est tronqué en 64 bits et
        # Windows répond « handle invalide » (erreur 6). Instance à part, pour ne pas toucher à
        # ctypes.windll, partagé avec le reste du processus.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        if k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00008000):  # ABOVE_NORMAL_PRIORITY_CLASS
            log.info("priorité du processus : supérieure à la normale")
        else:
            log.warning("priorité du processus non modifiée (erreur Windows %d)", ctypes.get_last_error())
    except Exception:
        log.warning("priorité du processus non modifiée", exc_info=True)


async def in_audio_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_AUDIO, fn, *args)


def find_device(name: str) -> tuple[int, str]:
    """Périphérique d'enregistrement dont le nom contient `name` (WASAPI de préférence)."""
    import sounddevice as sd

    apis = sd.query_hostapis()
    order = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2}
    found = []
    for i, d in enumerate(sd.query_devices()):
        api = apis[d["hostapi"]]["name"]
        if d["max_input_channels"] >= 2 and name.lower() in d["name"].lower() and api in order:
            found.append((order[api], i, f"{d['name']} ({api})"))
    if not found:
        raise StreamError(f"aucun périphérique d'enregistrement « {name} » (Voicemeeter est-il lancé ?)")
    found.sort()
    return found[0][1], found[0][2]


class LineIn(discord.AudioSource):
    """Source audio PCM lue en continu sur un périphérique d'enregistrement.

    L'horloge de la carte son (Voicemeeter) et celle qui cadence l'envoi à Discord ne
    tournent jamais exactement à la même vitesse (mesuré : ~70 millionièmes). Plutôt que de
    couper des morceaux ou d'insérer des silences quand la réserve déborde ou se vide, on
    retire ou duplique UN échantillon par trame de 20 ms tant que la réserve moyenne
    s'écarte de sa cible : inaudible, et ça compense jusqu'à 0,1 % de dérive."""

    def __init__(self, device: int):
        import sounddevice as sd

        self._buf = bytearray()
        self._lock = threading.Lock()
        self._filling = True
        self._avg = float(TARGET_FRAMES * FRAME_BYTES)  # réserve moyenne (octets)
        self.underruns = 0
        self.dropped = 0  # débordements (retour brutal à la cible)
        self.corrections = 0
        self._win_min = 1 << 30
        self._win_max = 0
        self._last_cb = self._last_read = 0.0
        self._gap_cb = self._gap_read = 0.0  # plus longs trous (s) entre deux captures / deux lectures
        self.level = 0  # crête de la dernière seconde (0 à 32767)
        self._peak = 0
        self._blocks = 0
        self._stream = sd.RawInputStream(
            device=device, samplerate=RATE, channels=2, dtype="int16", blocksize=960, callback=self._callback
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        now = time.perf_counter()
        if self._last_cb:
            gap = now - self._last_cb
            self._gap_cb = max(self._gap_cb, gap)
            if gap > STALL_WARN:
                log.warning("diffusion : la capture s'est figée %.0f ms", gap * 1000)
        self._last_cb = now
        data = bytes(indata)
        a = array.array("h", data)[::16]  # sous-échantillonné : estimation de crête bon marché
        self._peak = max(self._peak, max(a, default=0), -min(a, default=0))
        self._blocks += 1
        if self._blocks >= 50:
            self.level, self._peak, self._blocks = self._peak, 0, 0
        with self._lock:
            self._buf += data
            if len(self._buf) > MAX_FRAMES * FRAME_BYTES:
                cut = len(self._buf) - TARGET_FRAMES * FRAME_BYTES
                cut -= cut % SAMPLE_BYTES
                del self._buf[:cut]
                self.dropped += 1

    def read(self) -> bytes:
        # Ne jamais renvoyer b"" : discord.py arrêterait la lecture. Silence en cas de manque.
        now = time.perf_counter()
        if self._last_read:
            gap = now - self._last_read
            self._gap_read = max(self._gap_read, gap)
            if gap > STALL_WARN:
                log.warning("diffusion : l'envoi à Discord s'est figé %.0f ms", gap * 1000)
        self._last_read = now
        with self._lock:
            depth = len(self._buf)
            if not self._filling:
                self._win_min = min(self._win_min, depth)
            self._win_max = max(self._win_max, depth)
            if self._filling:
                if depth < TARGET_FRAMES * FRAME_BYTES:
                    return b"\x00" * FRAME_BYTES
                self._filling = False
                self._avg = float(depth)
            if depth < FRAME_BYTES:
                self.underruns += 1
                self._filling = True  # on reconstitue la réserve avant de reprendre
                return b"\x00" * FRAME_BYTES
            self._avg += (depth - self._avg) * 0.005  # moyenne glissante (~4 s)
            target = TARGET_FRAMES * FRAME_BYTES
            if self._avg > target + DRIFT_MARGIN and depth >= FRAME_BYTES + SAMPLE_BYTES:
                # capture plus rapide que l'envoi : on consomme un échantillon de plus
                chunk = bytes(self._buf[: FRAME_BYTES + SAMPLE_BYTES])
                del self._buf[: FRAME_BYTES + SAMPLE_BYTES]
                mid = (FRAME_BYTES // 2) - (FRAME_BYTES // 2) % SAMPLE_BYTES
                self.corrections += 1
                return chunk[:mid] + chunk[mid + SAMPLE_BYTES :]
            if self._avg < target - DRIFT_MARGIN:
                # capture plus lente : on consomme un échantillon de moins et on en duplique un
                chunk = bytes(self._buf[: FRAME_BYTES - SAMPLE_BYTES])
                del self._buf[: FRAME_BYTES - SAMPLE_BYTES]
                mid = (FRAME_BYTES // 2) - (FRAME_BYTES // 2) % SAMPLE_BYTES
                self.corrections += 1
                return chunk[:mid] + chunk[mid - SAMPLE_BYTES : mid] + chunk[mid:]
            out = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            return out

    def window_stats(self) -> str:
        """Bilan depuis le dernier appel (réserve en ms), puis remise à zéro de la fenêtre."""
        with self._lock:
            ms = lambda b: b / (FRAME_BYTES / 20)  # noqa: E731
            lo = ms(self._win_min) if self._win_min < (1 << 30) else 0
            text = (
                f"réserve {ms(self._avg):.0f} ms (min {lo:.0f}, max {ms(self._win_max):.0f}) · "
                f"manques {self.underruns} · débordements {self.dropped} · corrections {self.corrections} · "
                f"plus long trou capture {self._gap_cb * 1000:.0f} ms, envoi {self._gap_read * 1000:.0f} ms"
            )
            self._win_min, self._win_max = 1 << 30, 0
            self._gap_cb = self._gap_read = 0.0
        return text

    def is_opus(self) -> bool:
        return False

    @property
    def level_db(self) -> float:
        return 20 * math.log10(self.level / 32768) if self.level > 0 else -120.0

    def cleanup(self) -> None:
        # appelé depuis le thread du lecteur discord.py : on ferme dans le thread audio
        if getattr(self, "_closed", False) or not hasattr(self, "_stream"):
            return
        self._closed = True
        try:
            _AUDIO.submit(self._close)
        except RuntimeError:  # arrêt de Python en cours
            self._close()

    def _close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class VoiceStreamer:
    def __init__(self, bot: RadioBot, device_name: str, bitrate: int):
        self.bot = bot
        self.device_name = device_name
        self.bitrate = bitrate
        self.channel_id: int | None = None
        self.device_label = ""
        self.source: LineIn | None = None
        self.last_error = ""
        self.failures = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._e2ee_bad_since: float | None = None

    @property
    def active(self) -> bool:
        return self.channel_id is not None

    def voice_client(self) -> discord.VoiceClient | None:
        g = self.bot.guild
        vc = g.voice_client if g else None
        return vc if isinstance(vc, discord.VoiceClient) else None

    async def start(self, channel_id: int) -> None:
        """Rejoint le salon et diffuse. Lève StreamError si c'est impossible tout de suite."""
        _raise_priority()
        self.channel_id = channel_id
        self.failures = 0
        await self._ensure()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self.channel_id = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        vc = self.voice_client()
        if vc is not None:
            vc.stop()
            await vc.disconnect(force=True)

    async def _supervise(self) -> None:
        n = 0
        while self.channel_id is not None:
            await asyncio.sleep(SUPERVISE_EVERY)
            n += 1
            if n % STATS_EVERY == 0 and self.source is not None:
                log.info("diffusion : %s", self.source.window_stats())
            try:
                await self._ensure()
                self.failures = 0
            except Exception as e:
                self.failures += 1
                self.last_error = str(e)
                log.warning("diffusion : %s (échec %d)", e, self.failures)
                if self.failures == 3:
                    await self.bot.alert_owner(f"🔇 La diffusion du bot n'arrive pas à reprendre : {e}")

    async def _ensure(self) -> None:
        async with self._lock:
            if self.channel_id is None:
                return
            ch = self.bot.get_channel(self.channel_id)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                raise StreamError(f"salon vocal {self.channel_id} introuvable")
            me = ch.guild.me
            perms = ch.permissions_for(me)
            if not (perms.connect and perms.speak):
                raise StreamError(f"il manque au bot les permissions « Se connecter » et « Parler » dans {ch.name}")
            vc = self.voice_client()
            if vc is None or not vc.is_connected():
                if vc is not None:
                    await vc.disconnect(force=True)
                log.info("diffusion : connexion à %s", ch.name)
                vc = await ch.connect(self_deaf=True, reconnect=True, timeout=30)
            elif vc.channel is None or vc.channel.id != ch.id:
                # Pas de move_to : après un déplacement, la voix ne se rétablit pas toujours
                # (son muet alors que la connexion semble OK). On repart d'une connexion neuve.
                log.info("diffusion : changement de salon → %s", ch.name)
                vc.stop()
                await vc.disconnect(force=True)
                await asyncio.sleep(1.0)
                vc = await ch.connect(self_deaf=True, reconnect=True, timeout=30)
            if not self.e2ee_ok(vc):
                # Sans chiffrement prêt, discord.py envoie le son en clair : les clients le jettent
                # (le bot « parle » mais personne ne l'entend). On repart d'une connexion neuve.
                now = asyncio.get_running_loop().time()
                if self._e2ee_bad_since is None:
                    self._e2ee_bad_since = now
                elif now - self._e2ee_bad_since > E2EE_GRACE:
                    log.warning("diffusion : chiffrement vocal pas prêt, reconnexion")
                    self._e2ee_bad_since = None
                    vc.stop()
                    await vc.disconnect(force=True)
                    await asyncio.sleep(1.0)
                    vc = await ch.connect(self_deaf=True, reconnect=True, timeout=30)
            else:
                self._e2ee_bad_since = None
            if not vc.is_playing():
                device, self.device_label = await in_audio_thread(find_device, self.device_name)
                self.source = await in_audio_thread(LineIn, device)
                vc.play(self.source, bitrate=self.bitrate, signal_type="music", after=self._after)
                log.info("diffusion : %s → %s (%d kbps)", self.device_label, ch.name, self.bitrate)
            self.last_error = ""

    @staticmethod
    def e2ee_ok(vc: discord.VoiceClient) -> bool:
        st = vc._connection  # discord.py 2.7 : état DAVE non exposé publiquement
        return getattr(st, "dave_protocol_version", 0) == 0 or bool(getattr(st, "can_encrypt", True))

    def _after(self, error: Exception | None) -> None:
        if error:
            log.warning("diffusion interrompue : %s", error)
            self.last_error = str(error)

    def status(self) -> str:
        if self.channel_id is None:
            return "arrêtée"
        vc = self.voice_client()
        ch = self.bot.get_channel(self.channel_id)
        where = getattr(ch, "mention", str(self.channel_id))
        if vc is None or not vc.is_connected():
            return f"en cours de connexion à {where}" + (f" ({self.last_error})" if self.last_error else "")
        s = self.source
        extra = ""
        if s:
            lvl = "silence !" if s.level < 30 else f"{s.level_db:.0f} dB"
            extra = (
                f" · son capté {lvl} · manques {s.underruns} · débordements {s.dropped}"
                f" · corrections de dérive {s.corrections}"
            )
        ping = f" · ping {vc.latency * 1000:.0f} ms" if vc.latency != float("inf") else ""
        ping += " · chiffrement OK" if self.e2ee_ok(vc) else " · ⚠️ chiffrement pas prêt (personne n'entend, reconnexion auto)"
        return f"en direct dans {where} · {self.device_label} · {self.bitrate} kbps{ping}{extra}"
