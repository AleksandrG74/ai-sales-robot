# web/backend/api.py
"""
REST API web-backend (FastAPI).

Пока — минимальная заглушка: пустой APIRouter + базовый /api/ping.
Наполним эндпоинтами (stats, dialogues, strategies, live, config) далее.
"""

import logging

from fastapi import APIRouter

logger = logging.getLogger("web-backend.api")

router = APIRouter()


@router.get("/ping")
def ping() -> dict:
    """Простейший ping для проверки, что роутер подключён."""
    return {"ok": True, "service": "api"}
