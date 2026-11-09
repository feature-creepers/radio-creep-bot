from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

REQUEST_MODES = ("auto", "approve", "manual")


def _int(name: str, default: int | None = None, required: bool = False) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        if required:
            raise SystemExit(f"Variable {name} manquante dans .env")
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Variable {name} doit être un nombre (reçu : {raw!r})")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "oui", "on")


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    dj_role_id: int
    owner_id: int | None
    radio_account_id: int | None
    dj_channel_id: int | None
    request_mode: str
    max_pending_per_user: int
    max_queue: int
    show_apple_queue: bool
    announce_delete_previous: bool
    ui_idle_seconds: int
    library_xml: Path | None
    library_dirs: tuple[Path, ...]
    catalog: bool = True
    catalog_country: str = "fr"
    stream_channel_id: int | None = None
    stream_device: str = "Voicemeeter Out B2"
    stream_bitrate: int = 256
    auto_station: bool = True


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN manquant dans .env (voir README)")
    mode = os.getenv("REQUEST_MODE", "approve").strip().lower()
    if mode not in REQUEST_MODES:
        raise SystemExit(f"REQUEST_MODE doit valoir {', '.join(REQUEST_MODES)} (reçu : {mode!r})")
    xml = os.getenv("LIBRARY_XML", "").strip()
    dirs = tuple(Path(d.strip()) for d in os.getenv("LIBRARY_DIRS", "").split(";") if d.strip())
    return Config(
        token=token,
        guild_id=_int("GUILD_ID", required=True),
        voice_channel_id=_int("VOICE_CHANNEL_ID", required=True),
        text_channel_id=_int("TEXT_CHANNEL_ID", required=True),
        dj_role_id=_int("DJ_ROLE_ID", required=True),
        owner_id=_int("OWNER_ID"),
        radio_account_id=_int("RADIO_ACCOUNT_ID"),
        dj_channel_id=_int("DJ_CHANNEL_ID"),
        request_mode=mode,
        max_pending_per_user=_int("MAX_PENDING_PER_USER", 2),
        max_queue=_int("MAX_QUEUE", 25),
        show_apple_queue=_bool("SHOW_APPLE_QUEUE", False),
        announce_delete_previous=_bool("ANNOUNCE_DELETE_PREVIOUS", False),
        ui_idle_seconds=_int("UI_IDLE_SECONDS", 5),
        library_xml=Path(xml) if xml else None,
        library_dirs=dirs,
        catalog=_bool("CATALOG", True),
        catalog_country=(os.getenv("CATALOG_COUNTRY", "").strip().lower() or "fr"),
        stream_channel_id=_int("STREAM_CHANNEL_ID"),
        stream_device=os.getenv("STREAM_DEVICE", "").strip() or "Voicemeeter Out B2",
        stream_bitrate=max(32, min(512, _int("STREAM_BITRATE", 256))),
        auto_station=_bool("AUTO_STATION", True),
    )
