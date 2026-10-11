# web/backend/pages.py
"""
Jinja2-страницы web-backend (FastAPI).

Пока — минимальная заглушка: пустой APIRouter.
Наполним страницами (/, /dialogues, /live, /strategies, /config) далее.
"""

import logging

from fastapi import APIRouter

logger = logging.getLogger("web-backend.pages")

router = APIRouter()
