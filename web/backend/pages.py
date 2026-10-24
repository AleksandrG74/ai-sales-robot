# web/backend/pages.py
"""
Jinja2-страницы web-backend (FastAPI).

Страницы:
- GET /                    — дашборд
- GET /dialogues           — список диалогов (фильтры role/aggression, пагинация)
- GET /dialogues/{id}      — детали диалога
- GET /strategies          — список стратегий
- GET /bridges             — топ мостов
- GET /words               — топ токенов
- GET /runs                — история прогонов
- GET /live                — список live-чатов
- GET /live/{chat_id}      — диалог + форма ответа + кнопка LLM-подсказки
- GET /config              — редактирование product.json / goal.json

Все шаблоны находятся в web/backend/templates/.
Если какой-то шаблон отсутствует — возвращаем 500 с понятным текстом,
но не падаем всем приложением.
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from web.backend import queries as q
from auth import verify_credentials

logger = logging.getLogger("web-backend.pages")

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# === ВСПОМОГАТЕЛЬНОЕ ===

def _configs_dir() -> Path:
    base = Path("/app/configs")
    if not base.is_dir():
        base = Path("./configs")
    return base


def _read_json(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"value": data}
    except Exception as e:
        logger.exception("Не удалось прочитать %s: %s", path, e)
        return None


def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    """
    Единая точка рендера. Оборачивает в try/except, чтобы отсутствие шаблона
    давало понятную 500-ошибку, а не падение всего приложения.
    """
    try:
        return templates.TemplateResponse(
            request=request, name=name, context=ctx,
        )
    except Exception as e:
        logger.exception("Не удалось отрендерить %s: %s", name, e)
        raise HTTPException(status_code=500, detail=f"template render failed: {name}: {e}")


# === DASHBOARD ===

@router.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request) -> HTMLResponse:
    try:
        status = q.get_system_status()
        stats = q.get_overall_stats()
        runs = q.get_runs(limit=5)
        unread = q.count_unread_live_messages()
        chats_total = len(q.get_live_chats())
    except Exception as e:
        logger.exception("/ упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return _render(
        request, "dashboard.html",
        status=status, stats=stats, runs=runs,
        unread=unread, chats_total=chats_total,
    )


# === DIALOGUES ===

@router.get("/dialogues", response_class=HTMLResponse)
def page_dialogues(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    role: Optional[str] = Query(None),
    aggression: Optional[str] = Query(None),
) -> HTMLResponse:
    try:
        items = q.get_dialogues(
            limit=limit, offset=offset,
            role=role, aggression=aggression,
        )
        total = q.count_dialogues(role=role, aggression=aggression)
    except Exception as e:
        logger.exception("/dialogues упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return _render(
        request, "dialogues.html",
        items=items, total=total,
        limit=limit, offset=offset,
        role=role or "", aggression=aggression or "",
    )


@router.get("/dialogues/{dialogue_id}", response_class=HTMLResponse)
def page_dialogue(request: Request, dialogue_id: str) -> HTMLResponse:
    try:
        d = q.get_dialogue(dialogue_id)
    except Exception as e:
        logger.exception("/dialogues/{id} упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    if d is None:
        raise HTTPException(status_code=404, detail=f"dialogue {dialogue_id} not found")
    return _render(request, "dialogue.html", dialogue=d)


# === STRATEGIES ===

@router.get("/strategies", response_class=HTMLResponse)
def page_strategies(request: Request) -> HTMLResponse:
    try:
        strategies = q.get_strategies()
        # Собираем версии по каждой стратегии (SQLite — мгновенно).
        versions_by_strategy: dict = {}
        for s in strategies:
            sid = s.get("id")
            if sid is None:
                continue
            try:
                versions_by_strategy[sid] = q.get_versions(int(sid))
            except Exception as ve:
                logger.warning("get_versions(%s) упал: %s", sid, ve)
                versions_by_strategy[sid] = []
    except Exception as e:
        logger.exception("/strategies упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return _render(
        request, "strategies.html",
        strategies=strategies,
        versions_by_strategy=versions_by_strategy,
    )


# === BRIDGES ===

@router.get("/bridges", response_class=HTMLResponse)
def page_bridges(request: Request, limit: int = Query(50, ge=1, le=500)) -> HTMLResponse:
    try:
        bridges = q.get_bridges(limit=limit)
    except Exception as e:
        logger.exception("/bridges упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return _render(request, "bridges.html", bridges=bridges, limit=limit)


# === WORDS ===

@router.get("/words", response_class=HTMLResponse)
def page_words(request: Request, limit: int = Query(50, ge=1, le=500)) -> HTMLResponse:
    try:
        words = q.get_word_stats(limit=limit)
    except Exception as e:
        logger.exception("/words упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return _render(request, "words.html", words=words, limit=limit)


# === RUNS ===

@router.get("/runs", response_class=HTMLResponse)
def page_runs(request: Request, limit: int = Query(30, ge=1, le=200)) -> HTMLResponse:
    try:
        runs = q.get_runs(limit=limit)
    except Exception as e:
        logger.exception("/runs упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return _render(request, "runs.html", runs=runs, limit=limit)


# === LIVE ===

@router.get("/live", response_class=HTMLResponse)
def page_live(request: Request) -> HTMLResponse:
    try:
        chats = q.get_live_chats()
        unread = q.count_unread_live_messages()
    except Exception as e:
        logger.exception("/live упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return _render(request, "live.html", chats=chats, unread=unread)


@router.get("/live/{chat_id}", response_class=HTMLResponse)
def page_live_chat(
    request: Request,
    chat_id: int,
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    try:
        chat = q.get_live_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail=f"chat {chat_id} not monitored")
        messages = q.get_live_messages(
            chat_id=chat_id, limit=limit, offset=0, reverse=True,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("/live/{id} упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return _render(
        request, "live_chat.html",
        chat=chat, messages=messages,
    )


# === CONFIG ===

@router.get("/config", response_class=HTMLResponse)
def page_config(
    request: Request,
    saved: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
) -> HTMLResponse:
    try:
        product = _read_json(_configs_dir() / "product.json") or {}
        goal = _read_json(_configs_dir() / "goal.json") or {}
    except Exception as e:
        logger.exception("/config упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return _render(
        request, "config.html",
        product=product, goal=goal,
        saved=saved, error=error,
        product_json=json.dumps(product, ensure_ascii=False, indent=2),
        goal_json=json.dumps(goal, ensure_ascii=False, indent=2),
    )

@router.get("/clients", dependencies=[Depends(verify_credentials)])
async def clients_page(request: Request):
    from core.database import get_session
    from sqlalchemy import text

    clients = []
    try:
        with get_session() as session:
            result = session.execute(text("""
                SELECT
                    lm.chat_id,
                    lm.message_text AS last_message_text,
                    lm.author_first_name,
                    lm.sent_at
                FROM live_messages lm
                INNER JOIN (
                    SELECT chat_id, MAX(sent_at) AS max_sent
                    FROM live_messages
                    GROUP BY chat_id
                ) latest ON lm.chat_id = latest.chat_id AND lm.sent_at = latest.max_sent
                ORDER BY lm.sent_at DESC
                LIMIT 100
            """))
            for row in result.mappings():
                clients.append({
                    "chat_id": row["chat_id"],
                    "last_message_text": row["last_message_text"],
                    "author_first_name": row["author_first_name"],
                    "sent_at": row["sent_at"],
                })
    except Exception as e:
        print(f"[/clients] DB error: {e}")
        clients = []

    return templates.TemplateResponse(
        "clients.html",
        {"request": request, "clients": clients, "active_page": "clients"}
    )

@router.get("/live", dependencies=[Depends(verify_credentials)])
async def live_page(request: Request):
    from core.database import get_session
    from sqlalchemy import text

    chats = []
    try:
        with get_session() as session:
            result = session.execute(text("""
                SELECT chat_id, chat_title, enabled
                FROM monitored_chats
                WHERE enabled = 1
                ORDER BY chat_title
            """))
            for row in result.mappings():
                chats.append({
                    "chat_id": row["chat_id"],
                    "chat_title": row["chat_title"],
                    "enabled": row["enabled"],
                    "unread": 0,
                    "last_message": None,
                })
    except Exception as e:
        print(f"[/live] DB error: {e}")
        chats = []

    return templates.TemplateResponse(
        "live.html",
        {"request": request, "chats": chats, "active_page": "live"}
    )

ALLOWED_LOG_SERVICES = [
    "live-worker",
    "dialogue-worker",
    "telegram-worker",
    "web-backend",
    "orchestrator",
]


def _resolve_log_path(service: str):
    """Безопасно возвращает путь к файлу лога или None, если сервис не разрешён."""
    if service not in ALLOWED_LOG_SERVICES:
        return None
    base_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    candidate = (base_dir / f"{service}.log").resolve()
    # Защита от path traversal: файл должен лежать строго внутри base_dir
    try:
        candidate.relative_to(base_dir.resolve())
    except ValueError:
        return None
    return candidate


@router.get("/logs", dependencies=[Depends(verify_credentials)])
async def logs_page(request: Request, service: str = "live-worker"):
    from pathlib import Path as _Path  # локальный импорт, если Path не в scope

    current_service = service if service in ALLOWED_LOG_SERVICES else "live-worker"
    log_text = ""
    try:
        log_path = _resolve_log_path(current_service)
        if log_path and log_path.exists() and log_path.is_file():
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            log_text = "".join(lines[-100:])
    except Exception as e:
        print(f"[/logs] error reading log: {e}")
        log_text = ""

    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "log_text": log_text,
            "current_service": current_service,
            "services": ALLOWED_LOG_SERVICES,
            "active_page": "logs",
        },
    )