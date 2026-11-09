"""Favoris par membre (persistés dans data/state.json).

Chaque membre a sa liste (/favorite, bouton ⭐ des annonces, /favorites). Pour le
propriétaire (OWNER_ID), le morceau est aussi marqué « Favorite » dans Apple Music
(bouton de la barre de lecture de l'appli : seulement pour le morceau en cours)."""
from __future__ import annotations

import hashlib
import time

from .state import JsonStore
from .textutil import norm

MAX_PER_USER = 200
MAX_KEYS = 500


def track_key(title: str, artist: str) -> str:
    return hashlib.sha1(f"{norm(title)}\x1f{norm(artist)}".encode()).hexdigest()[:10]


class Favorites:
    def __init__(self, store: JsonStore):
        self.store = store

    def _all(self) -> dict[str, list[dict]]:
        return self.store.get("favorites") or {}

    def list(self, user_id: int) -> list[dict]:
        """Du plus récent au plus ancien. Chaque entrée : t (titre), a (artiste), al (album), ts."""
        return list(reversed(self._all().get(str(user_id), [])))

    def add(self, user_id: int, title: str, artist: str, album: str = "") -> bool:
        """False si déjà dans la liste."""
        data = self._all()
        items = data.setdefault(str(user_id), [])
        k = track_key(title, artist)
        if any(track_key(f["t"], f["a"]) == k for f in items):
            return False
        items.append({"t": title, "a": artist, "al": album, "ts": time.time()})
        del items[:-MAX_PER_USER]
        self.store.set("favorites", data)
        return True

    def remove(self, user_id: int, key: str) -> dict | None:
        data = self._all()
        items = data.get(str(user_id), [])
        for i, f in enumerate(items):
            if track_key(f["t"], f["a"]) == key:
                gone = items.pop(i)
                self.store.set("favorites", data)
                return gone
        return None

    # Boutons ⭐ des annonces : la clé du bouton renvoie au morceau annoncé
    def remember(self, title: str, artist: str, album: str) -> str:
        keys = self.store.get("fav_keys") or {}
        k = track_key(title, artist)
        keys.pop(k, None)
        keys[k] = [title, artist, album]
        while len(keys) > MAX_KEYS:
            keys.pop(next(iter(keys)))
        self.store.set("fav_keys", keys)
        return k

    def lookup(self, key: str) -> tuple[str, str, str] | None:
        v = (self.store.get("fav_keys") or {}).get(key)
        return tuple(v) if v else None  # type: ignore[return-value]
