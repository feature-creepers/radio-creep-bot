"""File des demandes (/request), persistée dans data/state.json."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .library import Track
from .state import JsonStore
from .textutil import artist_matches, same_title

# Cycle de vie d'une demande :
#   pending  → en attente d'un DJ (modes approve/manual, ou échec de l'ajout automatique)
#   approved → acceptée, attend son tour dans la file du bot
#   queued   → dans Apple Music : poussée par le bot en « Play Next » (pushed=True),
#              ou ajoutée à la main par un DJ (pushed=False)
#   played   → détectée en lecture (sort de la file)
#   rejected → refusée par un DJ
#   removed  → retirée par un DJ
#
# Le bot ne pousse qu'UNE demande à la fois en « Play Next » (juste après le morceau
# en cours) : quand elle démarre, il pousse la suivante. L'ordre est donc garanti,
# quel que soit l'état de la file d'Apple Music.
ACTIVE = ("pending", "approved", "queued")
MAX_PUSH_ATTEMPTS = 3


@dataclass
class Request:
    id: int
    track_id: str
    title: str
    artist: str
    album: str
    duration: float
    requester_id: int
    requester_name: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    note: str = ""
    pushed: bool = False  # poussée dans Apple Music par le bot
    push_key: list | None = None  # morceau qui jouait au moment du push
    attempts: int = 0
    skip_if_played: bool = False  # retirée alors qu'elle était déjà dans Apple Music
    hint: str = ""  # morceau en streaming : texte de recherche qui le trouve dans Apple Music
    artwork: str = ""  # pochette (morceaux du catalogue)

    @property
    def from_catalog(self) -> bool:
        return self.track_id.startswith("ct:")

    @property
    def label(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title


class RequestError(Exception):
    pass


class Jukebox:
    def __init__(self, store: JsonStore, max_pending_per_user: int, max_queue: int):
        self.store = store
        self.max_pending_per_user = max_pending_per_user
        self.max_queue = max_queue
        self.requests: list[Request] = [Request(**r) for r in store.get("requests", [])]
        self._next_id = max((r.id for r in self.requests), default=0) + 1
        self._prune()

    def _save(self) -> None:
        self.store.set("requests", [asdict(r) for r in self.requests])

    def _prune(self) -> None:
        """Garde les demandes actives + les 50 dernières terminées."""
        keep = [r for r in self.requests if r.status in ACTIVE or r.skip_if_played]
        done = [r for r in self.requests if r.status not in ACTIVE and not r.skip_if_played][-50:]
        self.requests = keep + done
        self.requests.sort(key=lambda r: r.id)

    # ------------------------------------------------------------ lecture
    def active(self) -> list[Request]:
        return [r for r in self.requests if r.status in ACTIVE]

    def get(self, rid: int) -> Request | None:
        return next((r for r in self.requests if r.id == rid), None)

    # ------------------------------------------------------------ écriture
    def add(self, track: Track, user_id: int, user_name: str, is_dj: bool = False) -> Request:
        active = self.active()
        if len(active) >= self.max_queue:
            raise RequestError(f"La file est pleine ({self.max_queue} demandes). Réessaie plus tard.")
        if any(r.track_id == track.id for r in active):
            raise RequestError(f"« {track.label} » est déjà dans la file.")
        mine = [r for r in active if r.requester_id == user_id]
        if not is_dj and len(mine) >= self.max_pending_per_user:
            raise RequestError(
                f"Tu as déjà {len(mine)} demande(s) en cours (max {self.max_pending_per_user}). "
                "Attends qu'elles passent !"
            )
        req = Request(
            id=self._next_id,
            track_id=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration=track.duration,
            requester_id=user_id,
            requester_name=user_name,
            hint=track.hint,
            artwork=track.artwork,
        )
        self._next_id += 1
        self.requests.append(req)
        self._save()
        return req

    def set_status(self, req: Request, status: str, note: str = "") -> None:
        req.status = status
        if note:
            req.note = note
        self._prune()
        self._save()

    def save(self) -> None:
        self._save()

    def in_flight(self) -> Request | None:
        """La demande actuellement poussée dans Apple Music par le bot (au plus une)."""
        return next((r for r in self.requests if r.status == "queued" and r.pushed), None)

    def next_approved(self) -> Request | None:
        return next((r for r in self.requests if r.status == "approved"), None)

    def position(self, req: Request) -> int:
        """Rang dans la file du bot (1 = la prochaine), demandes en attente de DJ exclues."""
        order = [r for r in self.requests if r.status == "queued"] + [r for r in self.requests if r.status == "approved"]
        return next((i for i, r in enumerate(order, 1) if r.id == req.id), 0)

    def match_skip(self, title: str, artist: str) -> Request | None:
        for r in self.requests:
            if r.skip_if_played and same_title(r.title, title) and artist_matches(r.artist, artist):
                return r
        return None

    def match_playing(self, title: str, artist: str) -> Request | None:
        """Le morceau qui démarre correspond-il à une demande active ? (la plus ancienne)"""
        for r in self.active():
            if same_title(r.title, title) and artist_matches(r.artist, artist):
                return r
        return None
