from __future__ import annotations

import asyncio
import ctypes
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .applemusic_ui import AppleMusicUI, PlaylistInfo, UIError
from .catalog import Catalog
from .favorites import Favorites
from .config import DATA_DIR, Config
from .jukebox import MAX_PUSH_ATTEMPTS, Jukebox, Request
from .library import Library
from .smtc import AppleMusicMedia, NowPlaying
from .state import JsonStore
from .station import AutoStation
from .voice import VoiceStreamer

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
UI_TIMEOUT = 45.0  # une opération d'interface ne doit jamais bloquer le bot indéfiniment


class NotDJ(app_commands.CheckFailure):
    pass


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def seconds_since_last_input() -> float:
    """Temps écoulé depuis la dernière action clavier/souris de l'utilisateur du PC."""
    info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 1e9
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


class RadioBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True  # qui est dans le salon -radio
        intents.members = True  # (privilégié) pour connaître les membres du vocal dès le démarrage
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.config = config
        self.store = JsonStore(DATA_DIR / "state.json")
        self.media = AppleMusicMedia()
        self.ui = AppleMusicUI()
        self.library = Library(config.library_xml, config.library_dirs, DATA_DIR / "library_cache.json")
        self.catalog = Catalog(config.catalog_country, config.catalog)
        self.streamer = VoiceStreamer(self, config.stream_device, config.stream_bitrate)
        self._stream_started = False
        self.jukebox = Jukebox(self.store, config.max_pending_per_user, config.max_queue)
        self.favorites = Favorites(self.store)
        self.station = AutoStation(self)
        self.playlists: list[PlaylistInfo] = []
        self.now: NowPlaying | None = None
        self.request_mode: str = self.store.get("request_mode") or config.request_mode
        # demande → messages de notification DJ (pour les mettre à jour partout)
        self.notif_messages: dict[int, list[discord.Message]] = {}
        self._poll_task: asyncio.Task | None = None
        self._pump_lock = asyncio.Lock()

    # ------------------------------------------------------------ démarrage
    async def setup_hook(self) -> None:
        from .cogs import announcer, dj, favorites, radio, watch
        from .views import RequestButton

        self.add_dynamic_items(RequestButton, favorites.FavButton)
        await self.add_cog(radio.Radio(self))
        await self.add_cog(dj.DJ(self))
        await self.add_cog(announcer.Announcer(self))
        await self.add_cog(watch.Watch(self))
        await self.add_cog(favorites.Favoris(self))
        try:
            self.ui.clear_noactivate()  # au cas où un arrêt brutal l'aurait laissé
        except Exception:
            log.debug("nettoyage de la fenêtre Apple Music impossible", exc_info=True)
        self.tree.on_error = self.on_app_command_error

        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.Forbidden:
            msg = (
                "Discord refuse l'enregistrement des commandes slash (403 Missing Access). "
                "Soit le bot n'est pas sur le serveur GUILD_ID, soit il a été invité sans le scope "
                "« applications.commands ». Ré-invite-le avec l'URL du README (étape 2), sans le retirer "
                "du serveur, puis relance. Diagnostic : python -m bot.discordcheck"
            )
            log.critical(msg)
            raise SystemExit(msg)
        log.info("%d commandes slash synchronisées sur le serveur", len(synced))

        asyncio.create_task(self.reload_library())
        asyncio.create_task(self.refresh_playlists())
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        await self.catalog.close()
        await super().close()

    def get_track(self, track_id: str):
        """Morceau choisi dans un menu ou l'autocomplétion : fichier local, bibliothèque ou catalogue."""
        return self.library.get(track_id) or self.catalog.get(track_id)

    async def on_ready(self) -> None:
        log.info("connecté en tant que %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._stream_started:
            self._stream_started = True
            target = self.stream_target()
            if target:
                asyncio.create_task(self._start_stream(target))

    def stream_target(self) -> int | None:
        """Salon où le bot diffuse : choix de /diffusion (mémorisé), sinon STREAM_CHANNEL_ID."""
        stored = self.store.get("stream_channel_id", "env")
        return self.config.stream_channel_id if stored == "env" else stored

    async def _start_stream(self, channel_id: int) -> None:
        try:
            await self.streamer.start(channel_id)
        except Exception as e:  # le superviseur réessaie toutes les 15 s
            log.warning("diffusion : démarrage impossible (%s), nouvel essai bientôt", e)

    async def alert_owner(self, text: str) -> None:
        """MP au propriétaire (OWNER_ID)."""
        if not self.config.owner_id:
            log.warning("alerte sans destinataire (OWNER_ID) : %s", text)
            return
        try:
            owner = self.get_user(self.config.owner_id) or await self.fetch_user(self.config.owner_id)
            await owner.send(text)
        except Exception as e:  # réseau coupé compris : une alerte ratée ne doit pas tuer l'appelant
            log.warning("MP impossible au propriétaire : %s", e)

    # ------------------------------------------------------------ accès pratiques
    @property
    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.config.guild_id)

    def text_channel(self) -> discord.TextChannel | None:
        ch = self.get_channel(self.config.text_channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    def voice_channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        ch = self.get_channel(self.config.voice_channel_id)
        return ch if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else None

    def listeners(self) -> list[discord.Member]:
        """Humains présents (et non sourds) dans -radio, hors compte RADIO CREEP."""
        vc = self.voice_channel()
        if vc is None:
            return []
        out = []
        for m in vc.members:
            if m.bot or m.id == self.config.radio_account_id:
                continue
            if m.voice and (m.voice.self_deaf or m.voice.deaf):
                continue
            out.append(m)
        return out

    def is_dj(self, user: discord.abc.User) -> bool:
        if user.id == self.config.owner_id:
            return True
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            return any(r.id == self.config.dj_role_id for r in user.roles)
        return False

    async def ui_call(self, fn, *args, idle_wait: float | None = 20.0):
        """Opération dans l'interface d'Apple Music. Attend d'abord que le PC soit inactif
        (clavier/souris) depuis UI_IDLE_SECONDS, au plus `idle_wait` secondes (None = sans limite)."""
        await self.wait_user_idle(idle_wait)
        return await asyncio.wait_for(self.ui.run(fn, *args), UI_TIMEOUT)

    async def wait_user_idle(self, max_wait: float | None) -> None:
        need = self.config.ui_idle_seconds
        if need <= 0 or max_wait == 0:
            return
        start = asyncio.get_running_loop().time()
        while seconds_since_last_input() < need:
            if max_wait is not None and asyncio.get_running_loop().time() - start > max_wait:
                log.info("PC toujours utilisé après %.0f s : on agit quand même", max_wait)
                return
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ tâches de fond
    async def reload_library(self) -> None:
        try:
            await asyncio.to_thread(self.library.load)
        except Exception:
            log.exception("chargement de la bibliothèque impossible")

    async def refresh_playlists(self) -> str | None:
        try:
            self.playlists = await self.ui_call(self.ui.list_playlists)
            log.info("%d playlists trouvées dans Apple Music", len(self.playlists))
            return None
        except (UIError, asyncio.TimeoutError) as e:
            log.warning("liste des playlists indisponible : %s", e)
            return str(e) or "délai dépassé"
        except Exception as e:
            log.exception("liste des playlists : erreur")
            return str(e)

    async def _poll_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                np = await self.media.now_playing()
                prev = self.now
                self.now = np
                self.station.observe(np)
                if np is not None and (prev is None or np.key != prev.key):
                    log.info("à l'antenne : %s — %s", np.title, np.artist)
                    self.store.set("last_track", [np.title, np.artist])
                    req = await self._on_new_track(np)
                    self.dispatch("track_change", np, req)
                elif np is not None and prev is not None and np.playing != prev.playing:
                    self.dispatch("playback_state", np)
            except Exception:
                log.exception("lecture SMTC")
            await asyncio.sleep(POLL_SECONDS)

    # ------------------------------------------------------------ demandes
    async def submit_request(self, req: Request) -> str:
        """Applique le mode courant à une nouvelle demande. Renvoie le message pour le membre."""
        if self.request_mode == "auto":
            return await self.approve(req)
        await self.notify_djs(req)
        return f"⏳ Demande #{req.id} envoyée aux DJ : **{req.label}**."

    async def approve(self, req: Request) -> str:
        """Accepte une demande et la pousse tout de suite dans Apple Music : la confirmation ne
        part qu'une fois le morceau vu dans la file. S'il y a déjà une demande avant elle, elle
        entrera dès que celle-ci démarre (« Play Next » inverserait sinon l'ordre)."""
        req.attempts = 0
        self.jukebox.set_status(req, "approved")
        pos = self.jukebox.position(req)
        if pos <= 1:
            await self.pump(caller=req)
            if req.status == "queued":
                return f"✅ **{req.label}** est dans la file d'Apple Music : il passe juste après le morceau en cours (demande #{req.id})."
            if req.status != "approved":
                return f"⚠️ Je n'ai pas réussi à mettre **{req.label}** dans Apple Music ({req.note}) ; un DJ est prévenu."
            pos = max(2, self.jukebox.position(req))
        else:
            asyncio.create_task(self.pump())
        return f"✅ **{req.label}** est en position {pos} : il entrera dans Apple Music dès que le précédent démarre (demande #{req.id})."

    async def pump(self, caller: Request | None = None) -> None:
        """Pousse la prochaine demande acceptée dans Apple Music (« Play Next »), une seule à la
        fois et sans attendre : un délai laisserait passer un autre morceau avant elle.
        `caller` : demande dont l'appelant annonce lui-même le résultat."""
        async with self._pump_lock:
            if self.jukebox.in_flight() is not None:
                return
            req = self.jukebox.next_approved()
            if req is None:
                return
            err = ""
            try:
                await self.ui_call(
                    self.ui.push_next, req.title, req.artist, req.album, req.hint, req.from_catalog, idle_wait=0
                )
            except asyncio.TimeoutError:
                err = "Apple Music n'a pas répondu à temps"
            except UIError as e:
                err = str(e)
            except Exception as e:
                log.exception("enqueue")
                err = f"erreur inattendue : {e}"
            if not err:
                req.pushed = True
                req.push_key = list(self.now.key) if self.now else None
                self.jukebox.set_status(req, "queued")
                log.info("demande #%d poussée dans Apple Music : %s", req.id, req.label)
                return
            req.attempts += 1
            log.warning("push de la demande #%d impossible : %s", req.id, err)
            self.jukebox.set_status(req, "pending", note=err)
        await self.notify_djs(req, failure=err)
        if self.request_mode == "auto" and req is not caller:
            from .views import _tell_requester

            await _tell_requester(
                self, req, f"⚠️ Je n'ai pas réussi à mettre **{req.label}** dans Apple Music ; un DJ est prévenu."
            )
        asyncio.create_task(self.pump())  # passe à la demande suivante

    async def _on_new_track(self, np: NowPlaying) -> Request | None:
        """Suivi de la file quand un nouveau morceau démarre. Renvoie la demande jouée, le cas échéant."""
        # Demande retirée par un DJ alors qu'elle était déjà dans Apple Music : on la saute
        # (100 % en arrière-plan, alors que la retirer de la file d'Apple Music volerait le focus).
        to_skip = self.jukebox.match_skip(np.title, np.artist)
        if to_skip is not None:
            to_skip.skip_if_played = False
            self.jukebox.save()
            log.info("demande #%d retirée : on la saute", to_skip.id)
            await self.media.next()
            return None
        req = self.jukebox.match_playing(np.title, np.artist)
        if req is not None:
            self.jukebox.set_status(req, "played")
        flying = self.jukebox.in_flight()
        if flying is not None and flying is not req and flying.push_key != list(np.key):
            # Poussée en « Play Next » mais un autre morceau est passé : file d'Apple Music
            # remplacée (changement de playlist...) → on la repousse.
            flying.pushed = False
            flying.attempts += 1
            if flying.attempts >= MAX_PUSH_ATTEMPTS:
                self.jukebox.set_status(flying, "pending", note="perdue plusieurs fois dans la file Apple Music")
                asyncio.create_task(self.notify_djs(flying, failure="La demande a disparu de la file Apple Music plusieurs fois."))
            else:
                log.info("demande #%d sortie de la file Apple Music, on la repousse", flying.id)
                self.jukebox.set_status(flying, "approved")
        asyncio.create_task(self.pump())
        return req

    async def notify_djs(self, req: Request, failure: str = "") -> None:
        from .views import request_embed, request_view

        embed = request_embed(self, req, failure)
        view = request_view(self, req)
        targets: list[discord.abc.Messageable] = []
        if self.config.dj_channel_id:
            ch = self.get_channel(self.config.dj_channel_id)
            if isinstance(ch, discord.abc.Messageable):
                targets.append(ch)
        if self.config.owner_id:
            try:
                owner = self.get_user(self.config.owner_id) or await self.fetch_user(self.config.owner_id)
                targets.append(owner)
            except discord.HTTPException:
                log.warning("propriétaire introuvable (OWNER_ID)")
        msgs = []
        for t in targets:
            try:
                msgs.append(await t.send(embed=embed, view=view))
            except discord.HTTPException as e:
                log.warning("notification impossible vers %s : %s", t, e)
        if msgs:
            self.notif_messages[req.id] = msgs
        else:
            log.error("aucune cible de notification (configure OWNER_ID et/ou DJ_CHANNEL_ID)")

    async def refresh_notifications(self, req: Request, footer: str) -> None:
        from .views import request_embed

        embed = request_embed(self, req, footer=footer)
        for m in self.notif_messages.pop(req.id, []):
            try:
                await m.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------ erreurs
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # trace des commandes et clics (utile pour relier un incident à une action)
        data: dict = interaction.data or {}  # type: ignore[assignment]
        if interaction.type == discord.InteractionType.application_command:
            args = "".join(f" {o['name']}={o['value']!r}" for o in data.get("options", []) if "value" in o)
            log.info("commande /%s%s par %s", data.get("name", "?"), args, interaction.user)
        elif interaction.type == discord.InteractionType.component:
            log.info("clic %s %s par %s", data.get("custom_id", "?")[:40], data.get("values", ""), interaction.user)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, NotDJ):
            msg = "🔒 Commande réservée au rôle DJ."
        elif isinstance(error, app_commands.CheckFailure):
            msg = f"🚫 {error}"
        else:
            log.exception("erreur de commande", exc_info=error)
            msg = "💥 Oups, une erreur est survenue (voir les logs du bot)."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


def dj_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        bot: RadioBot = interaction.client  # type: ignore[assignment]
        if bot.is_dj(interaction.user):
            return True
        raise NotDJ()

    return app_commands.check(predicate)
