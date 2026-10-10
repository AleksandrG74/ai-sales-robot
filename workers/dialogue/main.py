"""dialogue-worker: ядро диалогового агента (заглушка).

На этапе 5 здесь будет настоящая логика:
- чтение JSON-тредов из data/raw_threads/
- определение роли клиента, уровня агрессии
- построение промпта, вызов LLM, оценка результата
- обновление PMI, весов, версий стратегий.

Пока — просто живой процесс с логированием и корректной
обработкой SIGTERM/SIGINT.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import settings

LOG_FILE = settings.logs_dir / "dialogue-worker.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("dialogue-worker")
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

    log.info("dialogue-worker started (pid=%s)", os.getpid())
    log.info("raw_threads_dir=%s", settings.raw_threads_dir)
    log.info("robot_db_path=%s", settings.robot_db_path)

    counter = 0
    while not _shutdown:
        counter += 1
        if counter % 12 == 0:  # раз в минуту при sleep=5
            log.debug("dialogue-worker alive, tick #%d", counter)
        time.sleep(5)

    log.info("dialogue-worker stopped")


if __name__ == "__main__":
    main()
