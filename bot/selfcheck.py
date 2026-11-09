"""Autodiagnostic sans Discord.

    python -m bot.selfcheck                     # lecture seule : SMTC, bibliothèque, interface, playlists
    python -m bot.selfcheck --search "booba"    # recherche dans l'index
    python -m bot.selfcheck --queue             # lit aussi la file Apple Music
    python -m bot.selfcheck --enqueue "A.C. Milan" "Booba"   # ⚠️ met VRAIMENT le morceau en « Play Next »
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from dotenv import load_dotenv

from .applemusic_ui import AppleMusicUI, UIError
from .config import DATA_DIR, ROOT
from .library import Library
from .smtc import AppleMusicMedia


def ok(msg: str) -> None:
    print(f"  [OK]  {msg}")


def ko(msg: str) -> None:
    print(f"  [!!]  {msg}")


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--search")
    ap.add_argument("--queue", action="store_true", help="lit aussi la file Apple Music (ouvre son panneau)")
    ap.add_argument("--enqueue", nargs="+", metavar=("TITRE", "ARTISTE"))
    ap.add_argument("--catalog", metavar="TEXTE", help="cherche dans le catalogue Apple Music (API)")
    ap.add_argument("--enqueue-catalog", nargs=2, metavar=("TITRE", "ARTISTE"))
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    import os
    from pathlib import Path

    failures = 0
    print("1. Now playing (SMTC)")
    media = AppleMusicMedia()
    np = await media.now_playing()
    if np:
        ok(f"{np.title} — {np.artist} [{np.album}] · {np.status} · {int(np.position)}/{int(np.duration)} s")
        thumb = await media.thumbnail()
        ok(f"pochette : {len(thumb)} octets") if thumb else ko("pas de pochette")
    else:
        failures += 1
        ko("aucune session Apple Music (l'appli est-elle ouverte et a-t-elle joué quelque chose ?)")

    print("2. Bibliothèque")
    xml = os.getenv("LIBRARY_XML", "").strip()
    dirs = tuple(Path(d.strip()) for d in os.getenv("LIBRARY_DIRS", "").split(";") if d.strip())
    lib = Library(Path(xml) if xml else None, dirs, DATA_DIR / "library_cache.json")
    t0 = time.monotonic()
    lib.load()
    if lib.tracks:
        ok(f"{len(lib.tracks)} morceaux · {lib.source} · {time.monotonic() - t0:.1f} s")
    else:
        failures += 1
        ko("bibliothèque vide : vérifie LIBRARY_XML / LIBRARY_DIRS dans .env")
    if args.search:
        for t in lib.search(args.search, 10):
            print(f"        - {t.label}  [{t.album}]")

    if args.catalog:
        from .catalog import Catalog, CatalogUnavailable

        cat = Catalog(os.getenv("CATALOG_COUNTRY", "").strip().lower() or "fr")
        t0 = time.monotonic()
        try:
            found = await cat.search(args.catalog, 10)
            ok(f"catalogue : {len(found)} résultat(s) en {time.monotonic() - t0:.1f} s")
            for t in found:
                print(f"        - {t.label}  [{t.album}]")
        except CatalogUnavailable as e:
            failures += 1
            ko(f"catalogue : {e}")
        finally:
            await cat.close()

    print("3. Interface Apple Music (UI Automation)")
    ui = AppleMusicUI()
    try:
        pls = await ui.run(ui.list_playlists)
        ok(f"{len(pls)} playlists : " + ", ".join(p.path for p in pls[:12]) + (" …" if len(pls) > 12 else ""))
        if args.queue:
            q = await ui.run(ui.read_queue)
            ok(f"file Apple Music : {len(q)} morceaux à suivre : " + ", ".join(e.title for e in q[:5]))
    except UIError as e:
        failures += 1
        ko(str(e))

    if args.enqueue:
        title, artist = args.enqueue[0], (args.enqueue[1] if len(args.enqueue) > 1 else "")
        print(f"4. Ajout à la file : {title} / {artist}")
        try:
            entry = await ui.run(ui.enqueue, title, artist, "", True)
            ok(f"ajouté : {entry.title} — {entry.artist} [{entry.album}]")
        except UIError as e:
            failures += 1
            ko(str(e))

    if args.enqueue_catalog:
        title, artist = args.enqueue_catalog
        print(f"5. Ajout depuis le catalogue : {title} / {artist}")
        try:
            await ui.run(ui.enqueue_catalog, title, artist, True)
            ok("ajouté en Play Next")
        except UIError as e:
            failures += 1
            ko(str(e))

    print("\nTout est bon." if not failures else f"\n{failures} problème(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
