"""API publique iTunes Search : métadonnées du catalogue Apple Music.

Gratuite, sans clé ni compte développeur, assez rapide pour l'autocomplétion de /request
(Discord exige une réponse en moins de 3 s). Donne titre, artiste, album, durée et pochette.

Son index est INCOMPLET (mesuré le 2026-11-04) : « meet the grahams » de Kendrick Lamar, la
version originale de « The Less I Know the Better » ou « Push Ups » de Drake n'en sortent
jamais, alors qu'ils existent (lookup par identifiant) et que l'appli les trouve en premier.
Elle renvoie aussi des morceaux qui ne contiennent qu'une partie des mots cherchés. D'où :
- /search et le texte libre de /request interrogent d'abord l'appli (voir cogs/radio.py),
  l'API servant de complément et de source de pochettes ;
- on reclasse ici les résultats : ceux qui contiennent tous les mots d'abord ;
- un lien Apple Music désigne un morceau exact, sans passer par la recherche.

Limite d'Apple : environ 20 requêtes par minute et par adresse IP. On garde donc un
cache, un plafond d'appels par minute et une pause d'une minute si Apple refuse.
La lecture, elle, passe par l'appli Apple Music (recherche « Apple Music » ▸ Play Next)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict, deque
from urllib.parse import parse_qs, urlparse

import aiohttp

from .library import Track
from .textutil import norm, norm_title

log = logging.getLogger(__name__)

API_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
MAX_CALLS_PER_MINUTE = 18
CACHE_TTL = 1800.0
CACHE_SIZE = 500
FETCH_FAST = 50  # autocomplétion : ~0,8 s
FETCH_FULL = 200  # /search : ~2 s, en parallèle de la recherche dans l'appli

_APPLE_LINK = re.compile(r"https?://(?:[\w-]+\.)?(?:music|itunes)\.apple\.com/\S+", re.I)


def is_apple_link(text: str) -> bool:
    return bool(_APPLE_LINK.search(text or ""))


def link_id(text: str) -> str | None:
    """Identifiant d'un lien de morceau ou d'album (…/album/nom/123?i=456 → 456, …/album/nom/123 → 123)."""
    m = _APPLE_LINK.search(text or "")
    if not m:
        return None
    url = urlparse(m.group(0))
    track = (parse_qs(url.query).get("i") or [""])[0]
    if track.isdigit():
        return track
    parts = [x for x in url.path.split("/") if x]
    if not ({"album", "song"} & set(parts)):
        return None
    last = parts[-1].removeprefix("id")
    return last if last.isdigit() else None


def _matched(tokens: list[str], t: Track) -> int:
    """Nombre de mots de la recherche présents dans le morceau (début de mot : la frappe peut être en cours)."""
    words = norm(f"{t.title} {t.artist} {t.album}").split()
    return sum(any(w.startswith(tok) for w in words) for tok in tokens)


def full_match(query: str, t: Track) -> bool:
    tokens = norm(query).split()
    return bool(tokens) and _matched(tokens, t) == len(tokens)


def rank_tracks(query: str, tracks: list[Track]) -> list[Track]:
    """Tous les mots présents d'abord, puis les titres entièrement cités dans la recherche.
    À égalité, on garde l'ordre d'Apple (la popularité : utile pour « radiohead » seul)."""
    tokens = norm(query).split()
    given = set(tokens)

    def key(item: tuple[int, Track]):
        i, t = item
        return (
            -_matched(tokens, t),
            not set(norm(t.title).split()) <= given,
            not set(norm_title(t.title).split()) <= given,
            i,
        )

    return [t for _, t in sorted(enumerate(tracks), key=key)]


class CatalogUnavailable(Exception):
    pass


class Catalog:
    def __init__(self, country: str = "fr", enabled: bool = True):
        self.country = country
        self.enabled = enabled
        self.tracks: dict[str, Track] = {}  # tous les morceaux déjà vus (pour retrouver un id)
        self._cache: OrderedDict[str, tuple[float, list[Track], int]] = OrderedDict()  # date, résultats, nb demandés
        self._calls: deque[float] = deque()
        self._blocked_until = 0.0
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def get(self, track_id: str) -> Track | None:
        return self.tracks.get(track_id)

    def cached(self, query: str) -> list[Track] | None:
        hit = self._cache.get(self._key(query))
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        return None

    def _key(self, query: str) -> str:
        return norm(query)

    def _store(self, key: str, tracks: list[Track], fetched: int) -> None:
        self._cache[key] = (time.monotonic(), tracks, fetched)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def _can_call(self) -> bool:
        now = time.monotonic()
        if now < self._blocked_until:
            return False
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        return len(self._calls) < MAX_CALLS_PER_MINUTE

    async def _fetch(self, url: str, params: dict[str, str], timeout: float) -> list[dict]:
        if not self.enabled:
            raise CatalogUnavailable("catalogue désactivé (CATALOG=false)")
        if not self._can_call():
            raise CatalogUnavailable("trop de recherches dans le catalogue, réessaie dans une minute")
        self._calls.append(time.monotonic())
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"User-Agent": "radio-creep-bot/1.0"})
        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status in (403, 429):
                    self._blocked_until = time.monotonic() + 60
                    raise CatalogUnavailable("Apple limite les recherches, réessaie dans une minute")
                if r.status != 200:
                    raise CatalogUnavailable(f"catalogue indisponible (HTTP {r.status})")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise CatalogUnavailable(f"catalogue injoignable ({type(e).__name__})") from e
        return data.get("results", [])

    def _tracks_from(self, results: list[dict]) -> list[Track]:
        out: list[Track] = []
        seen: set[tuple[str, str]] = set()
        for it in results:
            if it.get("kind") != "song" or not it.get("trackId") or not it.get("trackName"):
                continue
            title, artist = it["trackName"], it.get("artistName", "")
            k = (norm(title), norm(artist))
            if k in seen:  # même morceau sur plusieurs albums (best-of…) : on garde le premier
                continue
            seen.add(k)
            art = it.get("artworkUrl100", "")
            t = Track(
                id=f"ct:{it['trackId']}",
                title=title,
                artist=artist,
                album=it.get("collectionName", ""),
                duration=(it.get("trackTimeMillis") or 0) / 1000,
                artwork=art.replace("100x100bb", "600x600bb") if art else "",
                explicit=it.get("trackExplicitness") == "explicit",
            )
            out.append(t)
            self.tracks[t.id] = t
        if len(self.tracks) > 5000:
            self.tracks = {t.id: t for t in out}
        return out

    async def search(self, query: str, limit: int = 25, timeout: float = 2.0, fetch: int = FETCH_FAST) -> list[Track]:
        """Morceaux du catalogue, les plus pertinents d'abord.
        Lève CatalogUnavailable (désactivé, limite atteinte, réseau)."""
        if not self.enabled:
            raise CatalogUnavailable("catalogue désactivé (CATALOG=false)")
        key = self._key(query)
        if not key:
            return []
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL and hit[2] >= fetch:
            return hit[1][:limit]
        params = {"term": query, "entity": "song", "media": "music", "country": self.country, "limit": str(fetch)}
        out = rank_tracks(query, self._tracks_from(await self._fetch(API_URL, params, timeout)))
        self._store(key, out, fetch)
        return out[:limit]

    async def from_link(self, text: str, timeout: float = 3.0) -> list[Track] | None:
        """Morceaux d'un lien Apple Music (un morceau, ou tout l'album). None si `text` n'est
        pas un lien Apple Music. Lève CatalogUnavailable si le lien ne peut pas être suivi."""
        if not is_apple_link(text):
            return None
        item = link_id(text)
        if item is None:
            raise CatalogUnavailable("seuls les liens de morceau ou d'album sont acceptés")
        key = f"lien:{item}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        params = {"id": item, "entity": "song", "country": self.country}
        out = self._tracks_from(await self._fetch(LOOKUP_URL, params, timeout))
        if not out:
            raise CatalogUnavailable(f"ce lien n'existe pas dans le catalogue Apple Music « {self.country} »")
        self._store(key, out, len(out))
        return out

    def from_app(self, query: str, rows: list[tuple[str, str, bool]], api: list[Track]) -> list[Track]:
        """Fusionne la recherche de l'appli (titre, artiste, explicite), fiable mais courte (une
        dizaine de lignes) et sans pochette, avec l'API : on garde l'ordre de l'appli, on complète
        avec les infos de l'API, puis on ajoute ceux de l'API qui contiennent TOUS les mots."""
        if not rows:
            return api
        known = {(norm(t.title), norm(t.artist)): t for t in api}
        out: list[Track] = []
        seen: set[tuple[str, str]] = set()
        for title, artist, explicit in rows:
            k = (norm(title), norm(artist))
            if k in seen:
                continue
            seen.add(k)
            t = known.get(k)
            if t is None:
                digest = hashlib.sha1(f"{k[0]}|{k[1]}".encode()).hexdigest()[:12]
                t = Track(id=f"ct:app:{digest}", title=title, artist=artist, album="", duration=0, explicit=explicit)
                self.tracks[t.id] = t
            out.append(t)
        out += [t for t in api if (norm(t.title), norm(t.artist)) not in seen and full_match(query, t)]
        return out
