"""web-backend: FastAPI-приложение (заглушка).

На этапе 7 здесь будет полноценный backend:
- настройка товара/цели
- просмотр диалогов, версий, PMI, мостов
- откат версий.

Пока — минимальный сервис с /health и /.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI

from core.config import settings

LOG_FILE = settings.logs_dir / "web-backend.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("web-backend")
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

app = FastAPI(title="AI Sales Robot — web-backend", version="0.1.0")


@app.on_event("startup")
async def _on_startup() -> None:
    log.info("web-backend started")
    log.info("robot_db_path=%s", settings.robot_db_path)
    log.info("raw_threads_dir=%s", settings.raw_threads_dir)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    log.info("web-backend stopped")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "ai-sales-robot", "version": "0.1.0"}
