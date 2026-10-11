# web/backend/main.py
"""
Точка входа web-backend (FastAPI + Jinja2 + StaticFiles).

Разворачивается в контейнере ai_sales_robot, порт 8000 (наружу — 8080).
Логи — в /app/logs/web-backend.log + stdout.

При старте:
- инициализирует logging,
- создаёт FastAPI-приложение,
- монтирует /static,
- подключает Jinja2Templates,
- подключает роутеры api и pages,
- оставляет /health для healthcheck.
"""

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Пути внутри контейнера
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


# === ЛОГИРОВАНИЕ ===

def setup_logging() -> logging.Logger:
    """Логирование в /app/logs/web-backend.log + stdout."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    root.addHandler(sh)

    log_dir = Path("/app/logs")
    if not log_dir.is_dir():
        log_dir = Path("./logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "web-backend.log", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except Exception as e:
        logging.getLogger("web-backend").warning(
            "Не удалось открыть файл лога в %s: %s", log_dir, e
        )

    logger = logging.getLogger("web-backend")
    logger.propagate = False
    return logger


logger = setup_logging()


# === FASTAPI APP ===

app = FastAPI(
    title="AI Sales Robot — Web CRM",
    description="REST API и Jinja2-страницы для оператора.",
    version="0.2.0",
)


# === STATIC FILES ===

try:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    logger.info("StaticFiles смонтированы: %s -> /static", STATIC_DIR)
except Exception as e:
    logger.warning("Не удалось смонтировать StaticFiles (%s): %s", STATIC_DIR, e)


# === JINJA2 TEMPLATES ===

try:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    logger.info("Jinja2Templates инициализированы: %s", TEMPLATES_DIR)
except Exception as e:
    logger.warning("Не удалось инициализировать Jinja2Templates (%s): %s", TEMPLATES_DIR, e)
    templates = None


# === ROUTERS ===

try:
    from web.backend import api, pages
    app.include_router(api.router, prefix="/api", tags=["api"])
    app.include_router(pages.router, tags=["pages"])
    logger.info("Роутеры api и pages подключены.")
except Exception as e:
    logger.exception("Не удалось подключить роутеры api/pages: %s", e)


# === HEALTHCHECK ===

@app.get("/health")
def health() -> dict:
    """Простейший healthcheck для docker/orchestrator."""
    return {"status": "ok"}


@app.get("/")
def root() -> dict:
    """
    Временный корень: пока pages.py не перехватил /.
    Как только pages.py начнёт отдавать HTML на /, этот эндпоинт
    будет перекрыт — FastAPI выберет первый зарегистрированный роутер,
    поэтому pages.router должен быть подключён ДО этого корня.
    Пока — оставляем JSON, чтобы healthcheck/CLI могли проверить,
    что сервис жив.
    """
    return {"service": "ai-sales-robot", "version": app.version}


# === STARTUP / SHUTDOWN ===

@app.on_event("startup")
async def on_startup() -> None:
    logger.info("web-backend стартовал (version=%s).", app.version)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("web-backend останавливается…")


# === ENTRY POINT ===

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEB_BACKEND_PORT", "8000"))
    uvicorn.run("web.backend.main:app", host="0.0.0.0", port=port, reload=False)
