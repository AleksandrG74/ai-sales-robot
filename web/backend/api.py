# web/backend/api.py
"""
REST API web-backend (FastAPI).

Все GET-эндпоинты возвращают JSON, при ошибках — HTTPException(500).
Для несуществующих объектов — 404.
POST-эндпоинты используют ленивые импорты telethon-зависимых модулей
(workers.live.sender, workers.live.suggest), чтобы api.py можно было
импортировать без Telegram.

Покрытие:
- /api/ping, /api/stats, /api/status
- /api/dialogues, /api/dialogues/{id}
- /api/strategies, /api/strategies/{id}/versions, /api/strategies/{id}/rollback
- /api/bridges, /api/words, /api/runs
- /api/live/chats, /api/live/chats/{chat_id}/messages
- /api/live/unread_count, /api/live/chats/{chat_id}/send, /api/live/chats/{chat_id}/suggest
- /api/config/product, /api/config/goal
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Form, HTTPException, Query, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from web.backend import queries as q
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import traceback
from web.backend.auth import verify_credentials

logger = logging.getLogger("web-backend.api")

router = APIRouter()


# === КОНФИГ-ДИРЕКТОРИЯ ===

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


def _write_json(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.exception("Не удалось записать %s: %s", path, e)
        return False


# === PYDANTIC MODELS ===

class RollbackRequest(BaseModel):
    version: int = Field(..., ge=1)


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    reply_to: Optional[int] = None


class ConfigUpdateRequest(BaseModel):
    data: dict


# === СЛУЖЕБНОЕ ===

@router.get("/ping")
def ping() -> dict:
    return {"ok": True, "service": "api"}


# === STATUS / STATS ===

@router.get("/status", dependencies=[Depends(verify_credentials)])
def api_status() -> dict:
    try:
        return q.get_system_status()
    except Exception as e:
        logger.exception("/api/status упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats", dependencies=[Depends(verify_credentials)])
def api_stats() -> dict:
    try:
        return q.get_overall_stats()
    except Exception as e:
        logger.exception("/api/stats упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# === DIALOGUES ===

@router.get("/dialogues", dependencies=[Depends(verify_credentials)])
def api_dialogues(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    role: Optional[str] = Query(None),
    aggression: Optional[str] = Query(None),
    sender: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
) -> dict:
    try:
        items = q.get_dialogues(
            limit=limit, offset=offset,
            role=role, aggression=aggression,
            sender=sender, status=status,
        )
        total = q.count_dialogues(role=role, aggression=aggression, sender=sender, status=status)
        return {"items": items, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.exception("/api/dialogues упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dialogues/{dialogue_id}", dependencies=[Depends(verify_credentials)])
def api_dialogue(dialogue_id: str) -> dict:
    try:
        d = q.get_dialogue(dialogue_id)
    except Exception as e:
        logger.exception("/api/dialogues/{id} упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    if d is None:
        raise HTTPException(status_code=404, detail=f"dialogue {dialogue_id} not found")
    return d


# === STRATEGIES ===

@router.get("/strategies", dependencies=[Depends(verify_credentials)])
def api_strategies() -> dict:
    try:
        items = q.get_strategies()
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("/api/strategies упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategies/{strategy_id}", dependencies=[Depends(verify_credentials)])
def api_strategy(strategy_id: int) -> dict:
    try:
        s = q.get_strategy(strategy_id)
    except Exception as e:
        logger.exception("/api/strategies/{id} упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    if s is None:
        raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
    return s


@router.get("/strategies/{strategy_id}/versions", dependencies=[Depends(verify_credentials)])
def api_strategy_versions(strategy_id: int) -> dict:
    try:
        # Проверим, что сама стратегия существует
        s = q.get_strategy(strategy_id)
        if s is None:
            raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
        versions = q.get_versions(strategy_id)
        return {"strategy_id": strategy_id, "items": versions, "total": len(versions)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("/api/strategies/{id}/versions упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/{strategy_id}/rollback", dependencies=[Depends(verify_credentials)])
def api_strategy_rollback(strategy_id: int, body: RollbackRequest) -> dict:
    try:
        s = q.get_strategy(strategy_id)
        if s is None:
            raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")

        versions = q.get_versions(strategy_id)
        if not any(v.get("version") == body.version for v in versions):
            raise HTTPException(
                status_code=400,
                detail=f"version {body.version} not found for strategy {strategy_id}",
            )

        ok = q.rollback_strategy(strategy_id, body.version)
        if not ok:
            raise HTTPException(status_code=400, detail="rollback failed (see logs)")

        # Считаем active_version_id после отката
        updated = q.get_strategy(strategy_id) or {}
        logger.info(
            "rollback: strategy=%s -> v%s (active_version_id=%s)",
            strategy_id, body.version, updated.get("active_version_id"),
        )
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "version": body.version,
            "active_version_id": updated.get("active_version_id"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("/api/strategies/{id}/rollback упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# === BRIDGES / WORDS / RUNS ===

@router.get("/bridges", dependencies=[Depends(verify_credentials)])
def api_bridges(limit: int = Query(20, ge=1, le=500)) -> dict:
    try:
        items = q.get_bridges(limit=limit)
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("/api/bridges упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/words", dependencies=[Depends(verify_credentials)])
def api_words(limit: int = Query(20, ge=1, le=500)) -> dict:
    try:
        items = q.get_word_stats(limit=limit)
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("/api/words упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/runs", dependencies=[Depends(verify_credentials)])
def api_runs(limit: int = Query(10, ge=1, le=200)) -> dict:
    try:
        items = q.get_runs(limit=limit)
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("/api/runs упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# === LIVE ===

@router.get("/live/chats", dependencies=[Depends(verify_credentials)])
def api_live_chats(enabled_only: bool = Query(False)) -> dict:
    try:
        items = q.get_live_chats(enabled_only=enabled_only)
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("/api/live/chats упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/chats/{chat_id}", dependencies=[Depends(verify_credentials)])
def api_live_chat(chat_id: int) -> dict:
    try:
        c = q.get_live_chat(chat_id)
    except Exception as e:
        logger.exception("/api/live/chats/{id} упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    if c is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not monitored")
    return c


@router.get("/live/chats/{chat_id}/messages", dependencies=[Depends(verify_credentials)])
def api_live_messages(
    chat_id: int,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    reverse: bool = Query(False),
) -> dict:
    try:
        items = q.get_live_messages(chat_id=chat_id, limit=limit, offset=offset, reverse=reverse)
        return {"chat_id": chat_id, "items": items, "limit": limit, "offset": offset}
    except Exception as e:
        logger.exception("/api/live/chats/{id}/messages упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live/unread_count", dependencies=[Depends(verify_credentials)])
def api_live_unread_count() -> dict:
    try:
        return {"unread": q.count_unread_live_messages()}
    except Exception as e:
        logger.exception("/api/live/unread_count упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/live/chats/{chat_id}/send", dependencies=[Depends(verify_credentials)])
async def api_live_send(chat_id: int, body: SendMessageRequest) -> dict:
    """
    Отправка сообщения от имени user-account через workers.live.sender.
    Ленивый импорт — чтобы не тянуть telethon при импорте api.py.
    """
    chat = q.get_live_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not monitored")

    try:
        from workers.live import sender
    except Exception as e:
        logger.exception("Не удалось импортировать workers.live.sender: %s", e)
        raise HTTPException(status_code=503, detail=f"sender unavailable: {e}")

    try:
        result = await sender.send_operator_reply(
            chat_id=chat_id,
            text=body.text,
            reply_to_msg_id=body.reply_to,
            operator_id=None,
        )
    except Exception as e:
        logger.exception("send_operator_reply упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "send failed")

    logger.info("send: chat=%s msg_id=%s len=%d", chat_id, result.get("message_id"), len(body.text))
    return result


@router.post("/live/chats/{chat_id}/suggest", dependencies=[Depends(verify_credentials)])
async def api_live_suggest(chat_id: int, message_id: int = Query(..., ge=1)) -> dict:
    """
    Генерация LLM-подсказки для указанного сообщения.
    Ленивый импорт suggest — чтобы не тянуть LLM/embeddings при импорте api.py.
    """
    chat = q.get_live_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not monitored")

    msg = q.get_live_message(chat_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail=f"message {message_id} not found")

    try:
        from workers.live import suggest
    except Exception as e:
        logger.exception("Не удалось импортировать workers.live.suggest: %s", e)
        raise HTTPException(status_code=503, detail=f"suggest unavailable: {e}")

    try:
        text = await suggest.generate_suggestion(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.exception("generate_suggestion упал: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    if not text:
        raise HTTPException(status_code=500, detail="suggest returned empty")

    logger.info("suggest: chat=%s msg=%s len=%d", chat_id, message_id, len(text))
    return {"chat_id": chat_id, "message_id": message_id, "suggestion": text}


# === CONFIG: product ===

@router.get("/config/product", dependencies=[Depends(verify_credentials)])
def api_config_product_get() -> dict:
    path = _configs_dir() / "product.json"
    data = _read_json(path)
    if data is None:
        raise HTTPException(status_code=404, detail=f"product.json not found at {path}")
    return data


@router.post("/config/product", dependencies=[Depends(verify_credentials)])
def api_config_product_set(body: ConfigUpdateRequest) -> dict:
    path = _configs_dir() / "product.json"
    if not _write_json(path, body.data):
        raise HTTPException(status_code=500, detail="failed to write product.json")
    logger.info("product.json обновлён (%d ключей).", len(body.data))
    return {"ok": True, "path": str(path)}


# === CONFIG: goal ===

@router.get("/config/goal", dependencies=[Depends(verify_credentials)])
def api_config_goal_get() -> dict:
    path = _configs_dir() / "goal.json"
    data = _read_json(path)
    if data is None:
        raise HTTPException(status_code=404, detail=f"goal.json not found at {path}")
    return data


@router.post("/config/goal", dependencies=[Depends(verify_credentials)])
def api_config_goal_set(body: ConfigUpdateRequest) -> dict:
    path = _configs_dir() / "goal.json"
    if not _write_json(path, body.data):
        raise HTTPException(status_code=500, detail="failed to write goal.json")
    logger.info("goal.json обновлён (%d ключей).", len(body.data))
    return {"ok": True, "path": str(path)}


# === FORM-варианты (SSR, Post/Redirect/Get) ===

def _parse_features(raw: str) -> list:
    """Строка (одна фича на строку) -> список непустых строк."""
    if not raw:
        return []
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


@router.post("/config/product/form", dependencies=[Depends(verify_credentials)])
def api_config_product_form(
    name: str = Form(""),
    description: str = Form(""),
    price: float = Form(0.0),
    currency: str = Form("RUB"),
    url: str = Form(""),
    features: str = Form(""),
) -> RedirectResponse:
    """
    Принимает форму из /config, сохраняет product.json, редиректит на /config?saved=product.
    Ошибки — через /config?error=...
    """
    try:
        data = {
            "name": (name or "").strip(),
            "description": (description or "").strip(),
            "price": float(price or 0),
            "currency": (currency or "RUB").strip() or "RUB",
            "url": (url or "").strip(),
            "features": _parse_features(features),
        }
        path = _configs_dir() / "product.json"
        if not _write_json(path, data):
            return RedirectResponse(url="/config?error=Не+удалось+записать+product.json", status_code=303)
        logger.info("product.json (form) обновлён: %s ключей.", len(data))
        return RedirectResponse(url="/config?saved=product", status_code=303)
    except Exception as e:
        logger.exception("product form save упал: %s", e)
        return RedirectResponse(url="/config?error=product+save+failed", status_code=303)


@router.post("/config/goal/form", dependencies=[Depends(verify_credentials)])
def api_config_goal_form(
    goal: str = Form(""),
    success_criteria: str = Form(""),
    tone: str = Form("friendly"),
    max_dialogue_turns: int = Form(0),
) -> RedirectResponse:
    try:
        data = {
            "goal": (goal or "").strip(),
            "success_criteria": (success_criteria or "").strip(),
            "tone": (tone or "friendly").strip() or "friendly",
            "max_dialogue_turns": int(max_dialogue_turns or 0),
        }
        path = _configs_dir() / "goal.json"
        if not _write_json(path, data):
            return RedirectResponse(url="/config?error=Не+удалось+записать+goal.json", status_code=303)
        logger.info("goal.json (form) обновлён: %s ключей.", len(data))
        return RedirectResponse(url="/config?saved=goal", status_code=303)
    except Exception as e:
        logger.exception("goal form save упал: %s", e)
        return RedirectResponse(url="/config?error=goal+save+failed", status_code=303)


# === DEMO CHAT (открытый эндпоинт, без Basic Auth) ===
class DemoChatRequest(BaseModel):
    message: str


_DEMO_SYSTEM_PROMPT_CACHE = {"text": None}


def _load_demo_system_prompt() -> str:
    """Собирает упрощённый системный промпт из configs/product.json и configs/goal.json."""
    if _DEMO_SYSTEM_PROMPT_CACHE["text"] is not None:
        return _DEMO_SYSTEM_PROMPT_CACHE["text"]

    base_dir = Path(__file__).resolve().parent.parent.parent  # /c/Projects/ai-sales-robot
    product_path = base_dir / "configs" / "product.json"
    goal_path = base_dir / "configs" / "goal.json"

    product_data = {}
    goal_data = {}
    try:
        if product_path.exists():
            product_data = json.loads(product_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[demo/chat] product.json read error: {e}")

    try:
        if goal_path.exists():
            goal_data = json.loads(goal_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[demo/chat] goal.json read error: {e}")

    parts = [
            "Ты — Салем, умный цифровой продавец. Отвечай от первого лица. Никогда не упоминай слово 'бот'.",
            "Твоё имя — Салем. Представляйся Салемом, если спрашивают имя.",
        "Ты — вежливый и профессиональный ассистент по продажам компании.",
        "Отвечай кратко, по делу, дружелюбно. Не выдумывай факты о продукте, которых нет ниже.",
    ]

    if product_data:
        name = product_data.get("name") or product_data.get("title")
        desc = product_data.get("description") or product_data.get("short_description")
        features = product_data.get("features") or product_data.get("advantages")
        price = product_data.get("price")
        if name:
            parts.append(f"Продукт: {name}.")
        if desc:
            parts.append(f"Описание: {desc}")
        if features:
            if isinstance(features, (list, tuple)):
                parts.append("Ключевые преимущества: " + "; ".join(str(x) for x in features) + ".")
            else:
                parts.append(f"Ключевые преимущества: {features}")
        if price:
            parts.append(f"Цена: {price}")

    if goal_data:
        goal = goal_data.get("goal") or goal_data.get("objective") or goal_data.get("target")
        tone = goal_data.get("tone") or goal_data.get("style")
        if goal:
            parts.append(f"Цель диалога: {goal}")
        if tone:
            parts.append(f"Тон общения: {tone}")

    parts.append("Если пользователь спрашивает о том, чего ты не знаешь — вежливо предложи уточнить у менеджера.")

    prompt = "\n".join(parts)
    _DEMO_SYSTEM_PROMPT_CACHE["text"] = prompt
    return prompt



@router.post("/demo/chat")
async def demo_chat(body: DemoChatRequest):
    """Демо-эндпоинт: сохраняет диалог в БД (Demo Sandbox) и отвечает через LLM."""
    import traceback
    from fastapi.responses import JSONResponse
    
    DEMO_CHAT_ID = -999999999
    DEMO_CHAT_TITLE = "Demo Sandbox"
    
    try:
        from core.llm import LLMClient
        from core.database import get_session, MonitoredChat, LiveMessage
        from sqlalchemy import select
        
        # 1. Сохраняем сообщение пользователя и создаём чат (если надо)
        with get_session() as session:
            chat = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == DEMO_CHAT_ID).limit(1)
            ).scalar_one_or_none()
            if not chat:
                chat = MonitoredChat(chat_id=DEMO_CHAT_ID, chat_title=DEMO_CHAT_TITLE, enabled=1)
                session.add(chat)
                session.commit()
            
            user_msg = LiveMessage(
                chat_id=DEMO_CHAT_ID,
                message_id=int(__import__('time').time() * 1000) % 1000000000,
                text=body.message,
                is_outgoing=0,
                read_by_operator=0
            )
            session.add(user_msg)
            session.commit()
        
        # 2. Формируем промпт
        prompt_text = "Ты — Салем, умный ассистент по продажам. Твоя задача — помогать клиентам и продавать товар, который описан ниже. Представляйся Салемом, если тебя спрашивают, кто ты. ВАЖНО: Отвечай ОЧЕНЬ КРАТКО. Максимум 2-3 предложения. Не пиши длинные списки и абзацы."
        try:
            from workers.dialogue.prompt_builder import load_product, load_goal
            product = load_product() or {}
            goal = load_goal() or {}
            prod_name = product.get("name", "")
            if prod_name:
                prompt_text += f"\nМы продаём: {prod_name}."
            goal_text = goal.get("goal", "")
            if goal_text:
                prompt_text += f"\nЦель диалога: {goal_text}."
        except Exception:
            pass
        
        messages = [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": body.message}
        ]
        
        # 3. Вызов LLM
        llm = LLMClient()
        response = await llm.chat(messages=messages)
        reply_text = response.content if hasattr(response, "content") else str(response)
        
        # 4. Сохраняем ответ бота
        with get_session() as session:
            bot_msg = LiveMessage(
                chat_id=DEMO_CHAT_ID,
                message_id=(int(__import__('time').time() * 1000) % 1000000000) + 1,
                text=reply_text,
                is_outgoing=1,
                read_by_operator=1
            )
            session.add(bot_msg)
            session.commit()
            
        return {"reply": reply_text}
        
    except Exception as e:
        err_str = f"Ошибка: {e}\n{traceback.format_exc()}"
        return JSONResponse(status_code=500, content={"detail": err_str})


TONE_MAP_RU_TO_EN = {
    "Дружелюбный": "friendly",
    "Профессиональный": "professional",
    "Непринуждённый": "casual",
    "Непринужденный": "casual",
    "Официальный": "formal",
}

def normalize_tone(value: str) -> str:
    """Приводит русское название тона к английскому ключу."""
    if not value:
        return "friendly"
    v = value.strip()
    if v in TONE_MAP_RU_TO_EN:
        return TONE_MAP_RU_TO_EN[v]
    allowed = {"friendly", "professional", "casual", "formal"}
    if v.lower() in allowed:
        return v.lower()
    return "friendly"
