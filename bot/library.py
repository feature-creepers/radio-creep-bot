"""Index de recherche de la bibliothèque musicale.

Apple Music (Windows) stocke sa bibliothèque dans un format chiffré
(Library.musicdb) : on ne peut pas la lire. Deux sources possibles :
  1. LIBRARY_XML : un export XML (format iTunes) si tu en as un ;
  2. LIBRARY_DIRS : un scan des fichiers audio (tags lus avec mutagen),
     mis en cache dans data/library_cache.json (seuls les fichiers modifiés sont relus).

L'index sert à /search et à l'autocomplétion de /request (Discord exige une réponse
en moins de 3 s, impossible en pilotant l'interface d'Apple Music)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from rapidfuzz import fuzz, process

from .textutil import norm

log = logging.getLogger(__name__)

AUDIO_EXT = {".m4a", ".m4p", ".mp3", ".aac", ".flac", ".wav", ".aif", ".aiff", ".alac", ".ogg"}


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: str
    duration: float  # secondes
    path: str = ""
    # Morceau trouvé via la recherche d'Apple Music (streaming, pas de fichier local) :
    # l'artiste peut être inconnu ; « hint » garde le texte cherché pour le retrouver dans l'appli.
    hint: str = ""
    # Morceau du catalogue Apple Music (id « ct:<trackId> ») : pochette et contenu explicite.
    artwork: str = ""
    explicit: bool = False

    @property
    def from_apple(self) -> bool:
        return self.id.startswith("am:")

    @property
    def from_catalog(self) -> bool:
        return self.id.startswith("ct:")

    @property
    def label(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title


def _track_id(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


class Library:
    def __init__(self, xml_path: Path | None, dirs: tuple[Path, ...], cache_path: Path):
        self.xml_path = xml_path
        self.dirs = dirs
        self.cache_path = cache_path
        self.tracks: list[Track] = []
        self.by_id: dict[str, Track] = {}
        self._haystack: list[str] = []
        self._words: list[set[str]] = []
        self._vocab: list[str] = []
        self.extra: dict[str, Track] = {}  # morceaux trouvés via Apple Music (pour les menus de sélection)
        self.source = "vide"
        self.loaded_at: float = 0.0

    # ------------------------------------------------------------ chargement
    def load(self) -> None:
        """Bloquant : à appeler dans un thread (asyncio.to_thread)."""
        t0 = time.monotonic()
        if self.xml_path and self.xml_path.exists():
            tracks = self._load_xml(self.xml_path)
            self.source = f"export XML ({self.xml_path.name})"
        else:
            tracks = self._scan_dirs()
            self.source = f"scan de {len(self.dirs)} dossier(s)"
        # dédoublonnage titre+artiste (un même morceau en plusieurs formats)
        seen: set[tuple[str, str]] = set()
        uniq = []
        for t in sorted(tracks, key=lambda t: (norm(t.artist), norm(t.album), norm(t.title))):
            k = (norm(t.title), norm(t.artist))
            if k not in seen:
                seen.add(k)
                uniq.append(t)
        self.tracks = uniq
        self.by_id = {t.id: t for t in uniq}
        self._haystack = [norm(f"{t.title} {t.artist} {t.album}") for t in uniq]
        self._words = [set(h.split()) for h in self._haystack]
        self._vocab = sorted(set().union(*self._words)) if self._words else []
        self.loaded_at = time.time()
        log.info("bibliothèque : %d morceaux (%s) en %.1fs", len(uniq), self.source, time.monotonic() - t0)

    def _load_xml(self, path: Path) -> list[Track]:
        with path.open("rb") as f:
            data = plistlib.load(f)
        out = []
        for t in data.get("Tracks", {}).values():
            if t.get("Podcast") or t.get("Movie") or t.get("TV Show") or t.get("Has Video"):
                continue
            kind = (t.get("Kind") or "").lower()
            if "audiobook" in kind or "book" in kind:
                continue
            title = t.get("Name") or ""
            if not title:
                continue
            loc = t.get("Location") or ""
            p = unquote(urlparse(loc).path).lstrip("/") if loc else ""
            artist = t.get("Artist") or t.get("Album Artist") or ""
            out.append(
                Track(
                    id=_track_id(t.get("Persistent ID") or title, artist),
                    title=title,
                    artist=artist,
                    album=t.get("Album") or "",
                    duration=(t.get("Total Time") or 0) / 1000,
                    path=p,
                )
            )
        return out

    def _scan_dirs(self) -> list[Track]:
        import mutagen  # import local : lourd

        cache: dict[str, list] = {}
        if self.cache_path.exists():
            try:
                cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cache = {}
        new_cache: dict[str, list] = {}
        out: list[Track] = []
        read = 0
        for root_dir in self.dirs:
            if not root_dir.exists():
                log.warning("dossier introuvable : %s", root_dir)
                continue
            for dirpath, _, files in os.walk(root_dir):
                for name in files:
                    if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    sig = [st.st_mtime, st.st_size]
                    hit = cache.get(full)
                    if hit and hit[:2] == sig:
                        entry = hit
                    else:
                        read += 1
                        title = artist = album = ""
                        duration = 0.0
                        try:
                            mf = mutagen.File(full, easy=True)
                            if mf is not None:
                                tags = mf.tags or {}
                                title = (tags.get("title") or [""])[0]
                                artist = (tags.get("artist") or tags.get("albumartist") or [""])[0]
                                album = (tags.get("album") or [""])[0]
                                duration = float(getattr(mf.info, "length", 0) or 0)
                        except Exception:
                            log.debug("tags illisibles : %s", full)
                        if not title:
                            title = os.path.splitext(name)[0].lstrip("0123456789- ").strip() or name
                        entry = sig + [title, artist, album, duration]
                    new_cache[full] = entry
                    _, _, title, artist, album, duration = entry
                    out.append(Track(_track_id(full), title, artist, album, duration, full))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(new_cache, ensure_ascii=False), encoding="utf-8")
        log.info("scan : %d fichiers, %d relus", len(out), read)
        return out

    # ------------------------------------------------------------ recherche
    def search(self, query: str, limit: int = 10) -> list[Track]:
        q = norm(query)
        if not q or not self.tracks:
            return []
        tokens = q.split()
        # CHAQUE mot tapé doit correspondre à un mot du morceau (titre, artiste ou album) :
        # début de mot, contenu dans un mot (≥ 3 lettres), ou faute de frappe (≥ 4 lettres).
        # Pas de résultats « à peu près » : mieux vaut « rien trouvé » que du hors-sujet.
        # Les fautes de frappe ne sont tolérées que si la recherche exacte ne donne rien.
        hits: list[int] = []
        for fuzzy in (False, True):
            accepted: list[set[str]] = []
            for tok in tokens:
                ok = {w for w in self._vocab if w.startswith(tok) or (len(tok) >= 3 and tok in w)}
                if fuzzy and len(tok) >= 4:
                    ok |= {
                        w
                        for w, _, _ in process.extract(tok, self._vocab, scorer=fuzz.ratio, score_cutoff=80, limit=40)
                        if len(w) >= 4
                    }
                if not ok:
                    break
                accepted.append(ok)
            else:
                hits = [i for i, words in enumerate(self._words) if all(words & ok for ok in accepted)]
            if hits:
                break

        def rank(i: int):
            h = self._haystack[i]
            exact = all(tok in h for tok in tokens)
            return (not exact, -fuzz.WRatio(q, h), not norm(self.tracks[i].title).startswith(q))

        return [self.tracks[i] for i in sorted(hits, key=rank)[:limit]]

    def get(self, track_id: str) -> Track | None:
        return self.by_id.get(track_id) or self.extra.get(track_id)

    def remember_apple(self, title: str, artist: str, hint: str) -> Track:
        """Enregistre un morceau trouvé dans Apple Music (streaming) pour pouvoir le demander."""
        t = Track(id="am:" + _track_id(title, artist, hint), title=title, artist=artist, album="", duration=0, hint=hint)
        if len(self.extra) > 2000:
            self.extra.clear()
        self.extra[t.id] = t
        return t

    def find_local(self, title: str, artist: str = "") -> Track | None:
        nt, na = norm(title), norm(artist)
        return next(
            (t for t in self.tracks if norm(t.title) == nt and (not artist or norm(t.artist) == na)), None
        )

    def has_local(self, title: str, artist: str = "") -> bool:
        nt = norm(title)
        return any(norm(t.title) == nt and (not artist or norm(t.artist) == norm(artist)) for t in self.tracks)
