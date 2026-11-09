"""Lecture du « now playing » et contrôle play/pause/suivant d'Apple Music
via les Windows Media Transport Controls (SMTC)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
)
from winrt.windows.storage.streams import Buffer, InputStreamOptions

log = logging.getLogger(__name__)

APP_ID_HINT = "AppleMusic"  # AUMID de la session : « AppleMusic.exe »
SEPARATOR = " — "  # Apple Music met « Artiste — Album » dans le champ artiste


@dataclass(frozen=True)
class NowPlaying:
    title: str
    artist: str
    album: str
    playing: bool
    status: str  # playing / paused / stopped / ...
    position: float  # secondes (estimée à l'instant de la lecture)
    duration: float
    next_enabled: bool = True  # Apple Music a un morceau après celui-ci (SMTC)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.title, self.artist, self.album)


_STATUS_NAMES = {
    Status.CLOSED: "closed",
    Status.OPENED: "opened",
    Status.CHANGING: "changing",
    Status.STOPPED: "stopped",
    Status.PLAYING: "playing",
    Status.PAUSED: "paused",
}


def split_artist_album(artist_field: str, album_field: str) -> tuple[str, str]:
    artist, album = artist_field or "", album_field or ""
    if not album and SEPARATOR in artist:
        artist, album = artist.split(SEPARATOR, 1)
    return artist.strip(), album.strip()


SMTC_TIMEOUT = 8.0  # un appel Windows qui ne répond pas ne doit jamais figer une commande


class AppleMusicMedia:
    def __init__(self) -> None:
        self._manager: SessionManager | None = None

    async def now_playing(self) -> NowPlaying | None:
        """Lève asyncio.TimeoutError si Windows ne répond pas (la boucle de lecture garde l'état)."""
        try:
            return await asyncio.wait_for(self._now_playing(), SMTC_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("SMTC : pas de réponse pour le morceau en cours, session réinitialisée")
            self._manager = None
            raise

    async def thumbnail(self) -> bytes | None:
        try:
            return await asyncio.wait_for(self._thumbnail(), SMTC_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("SMTC : pochette indisponible (pas de réponse)")
            self._manager = None
            return None

    async def _session(self):
        if self._manager is None:
            self._manager = await SessionManager.request_async()
        try:
            sessions = self._manager.get_sessions()
        except OSError:
            self._manager = await SessionManager.request_async()
            sessions = self._manager.get_sessions()
        for s in sessions:
            if APP_ID_HINT.lower() in (s.source_app_user_model_id or "").lower():
                return s
        return None

    async def _now_playing(self) -> NowPlaying | None:
        session = await self._session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        if props is None or not props.title:
            return None
        info = session.get_playback_info()
        tl = session.get_timeline_properties()
        if info is None:
            return None  # entre deux morceaux : on réessaie au prochain passage
        playing = info.playback_status == Status.PLAYING
        position = duration = 0.0
        if tl is not None:  # peut manquer brièvement pendant un changement de titre
            position = tl.position.total_seconds()
            if playing and tl.last_updated_time:
                # Apple Music ne met pas la position à jour en continu : on l'extrapole.
                elapsed = (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
                position += max(0.0, elapsed)
            duration = tl.end_time.total_seconds()
        if duration > 0:
            position = min(position, duration)
        artist, album = split_artist_album(props.artist, props.album_title)
        return NowPlaying(
            title=props.title,
            artist=artist,
            album=album,
            playing=playing,
            status=_STATUS_NAMES.get(info.playback_status, str(info.playback_status)),
            position=position,
            duration=duration,
            next_enabled=bool(info.controls.is_next_enabled) if info.controls is not None else True,
        )

    async def _thumbnail(self) -> bytes | None:
        """Pochette du morceau en cours (JPEG/PNG), ou None."""
        try:
            session = await self._session()
            if session is None:
                return None
            props = await session.try_get_media_properties_async()
            if props is None or props.thumbnail is None:
                return None
            stream = await props.thumbnail.open_read_async()
            size = stream.size
            if not size:
                return None
            buf = await stream.read_async(Buffer(size), size, InputStreamOptions.READ_AHEAD)
            return bytes(buf)
        except Exception:
            log.exception("lecture de la pochette impossible")
            return None

    async def _control(self, name: str) -> bool:
        async def go() -> bool:
            session = await self._session()
            if session is None:
                return False
            return bool(await getattr(session, name)())

        try:
            return await asyncio.wait_for(go(), SMTC_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("SMTC : pas de réponse à %s", name)
            self._manager = None
            return False

    async def play(self) -> bool:
        return await self._control("try_play_async")

    async def pause(self) -> bool:
        return await self._control("try_pause_async")

    async def next(self) -> bool:
        return await self._control("try_skip_next_async")
