"""telegram-worker: бот оператора (заглушка).

На этапе 6 здесь будет aiogram 3.x бот:
- /status, /stats, /dialogues, /versions, /rollback
- показ найденных лидов и версий стратегий.

Пока — просто живой процесс.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from core.config import settings

LOG_FILE = settings.logs_dir / "telegram-worker.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("telegram-worker")
    logger.setLevel(settings.log_level.upper())
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


log = setup_logging()
_shutdown = False


def _on_signal(signum, frame):  # type: ignore[no-untyped-def]
    global _shutdown
    log.info("Received signal %s, shutting down...", signum)
    _shutdown = True


def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log.info("telegram-worker started (pid=%s)", os.getpid())
    if settings.telegram_bot_token:
        log.info("bot token is set (prefix=%s...)", settings.telegram_bot_token[:8])
    else:
        log.warning("TELEGRAM_BOT_TOKEN is NOT set — bot would not start in real mode")

    counter = 0
    while not _shutdown:
        counter += 1
        if counter % 12 == 0:
            log.debug("telegram-worker alive, tick #%d", counter)
        time.sleep(5)

    log.info("telegram-worker stopped")


if __name__ == "__main__":
    main()
