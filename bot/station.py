"""Station automatique : quand la file d'Apple Music se termine, le bot lance une station
(« Create Station ») dans le style du dernier morceau joué, pour que la radio ne s'arrête
jamais. Le morceau peut venir d'une demande, d'une playlist ou de n'importe qui.

Détection de « fin de file » (sans ouvrir le panneau File d'Apple Music, qui volerait
le focus) : le morceau arrivait à sa fin, puis la lecture s'arrête au moins 2 passages
de suite (~4 s), ET il n'y a plus de « suivant » (SMTC) ou la lecture est revenue au
début / s'est arrêtée. Un /pause de DJ ne déclenche rien."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .applemusic_ui import UIError
from .smtc import NowPlaying

if TYPE_CHECKING:
    from .client import RadioBot

log = logging.getLogger(__name__)

NEAR_END_SECONDS = 12.0  # « en fin de morceau »
STOPPED_POLLS = 2  # passages sans lecture avant de conclure (2 × 2 s)
COOLDOWN_SECONDS = 90.0
DJ_PAUSE_GRACE = 30.0


class AutoStation:
    def __init__(self, bot: RadioBot):
        self.bot = bot
        self.near_end: NowPlaying | None = None
        self.near_end_at = 0.0
        self.stopped = 0
        self.last_launch = 0.0
        self.last_seed: str = ""
        self.dj_paused_at = 0.0
        self._busy = False

    @property
    def enabled(self) -> bool:
        stored = self.bot.store.get("auto_station")
        return self.bot.config.auto_station if stored is None else bool(stored)

    def observe(self, np: NowPlaying | None) -> None:
        """Appelée à chaque passage de la boucle de lecture (toutes les 2 s)."""
        now = time.monotonic()
        if np is not None and np.playing:
            self.stopped = 0
            remaining = np.duration - np.position if np.duration > 0 else 1e9
            if remaining <= NEAR_END_SECONDS:
                self.near_end, self.near_end_at = np, now
            elif self.near_end is not None:
                self.near_end = None  # morceau suivant (ou retour en arrière) : la file continue
            return
        if self.near_end is None or now - self.near_end_at > 120:
            return
        self.stopped += 1
        if self.stopped < STOPPED_POLLS:
            return
        last = self.near_end
        ended = (
            np is None
            or not np.next_enabled
            or np.status in ("stopped", "closed")
            or np.position < 3
            or np.key != last.key
        )
        if not ended:
            return  # simple pause en fin de morceau, la file n'est pas finie
        self.near_end = None
        if now - self.dj_paused_at < DJ_PAUSE_GRACE:
            log.info("fin de file juste après un /pause : pas de station")
            return
        if not self.enabled or self._busy or now - self.last_launch < COOLDOWN_SECONDS:
            return
        log.info("file Apple Music terminée après « %s » : station automatique", last.title)
        asyncio.create_task(self.launch(last.title, last.artist, auto=True))

    async def launch(self, title: str, artist: str, auto: bool = False) -> str:
        """Crée la station et vérifie qu'elle joue. Renvoie "lcd"/"catalog". Lève UIError."""
        self._busy = True
        try:
            try:
                how = await self.bot.ui_call(self.bot.ui.create_station, title, artist, idle_wait=0)
            except asyncio.TimeoutError:
                raise UIError("Apple Music n'a pas répondu à temps")
            self.last_launch = time.monotonic()
            self.last_seed = f"{title} — {artist}" if artist else title
            playing = False
            for _ in range(10):
                await asyncio.sleep(1.0)
                try:
                    np = await self.bot.media.now_playing()
                except asyncio.TimeoutError:
                    continue
                if np is not None and np.playing:
                    playing = True
                    break
            if not playing:
                await self.bot.media.play()  # station chargée mais en pause : on relance
            if auto:
                ch = self.bot.text_channel()
                if ch is not None:
                    try:
                        await ch.send(
                            f"📻 Plus rien dans la file : je lance une **station** dans le style de "
                            f"**{self.last_seed}**. Lecture à l'infini, `/request` pour reprendre la main !",
                            silent=True,
                        )
                    except Exception as e:
                        log.warning("message de station impossible : %s", e)
            return how
        except UIError as e:
            log.warning("station impossible (%s) : %s", title, e)
            if auto:
                await self.bot.alert_owner(
                    f"📻 La file Apple Music est finie mais je n'ai pas pu lancer de station "
                    f"(« {title} ») : {e}. La radio est peut-être muette : `/station maintenant` ou relance une playlist."
                )
            raise
        finally:
            self._busy = False
