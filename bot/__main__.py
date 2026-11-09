"""Point d'entrée : python -m bot   (ou pythonw -m bot, sans console)."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, load_config


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw n'a pas de console
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    # Le bot n'entre jamais en vocal : les avertissements PyNaCl/davey sont sans objet.
    logging.getLogger("discord.client").addFilter(lambda r: "voice will NOT be supported" not in r.getMessage())


_MUTEX = None  # gardé ouvert tant que le processus vit


def already_running() -> bool:
    """Une seule instance à la fois : deux bots se disputeraient la connexion vocale (son
    haché) et les commandes. Mutex Windows nommé, libéré automatiquement à la sortie."""
    global _MUTEX
    if sys.platform != "win32":
        return False
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX = k32.CreateMutexW(None, False, "Global\\RadioCreepBot")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    setup_logging()
    if already_running():
        logging.getLogger("bot").warning("une autre instance du bot tourne déjà : arrêt de celle-ci")
        return  # code 0 : le planificateur ne relance pas en boucle
    config = load_config()
    from .client import RadioBot

    bot = RadioBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
