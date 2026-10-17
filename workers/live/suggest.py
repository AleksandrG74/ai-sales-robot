# workers/live/suggest.py
"""
Генерация LLM-подсказок (llm_suggestion) для входящих live_messages.

Использует:
- core.llm.LLMClient — метод chat(messages=...) (принимает Iterable[ChatMessage|dict]).
- workers.live.prompt_builder.build_suggest_prompt(chat_id, message_id, history) -> list[dict].

Публичные функции:
- generate_suggestion(chat_id, message_id) -> Optional[str]
- suggest_for_message(chat_id, message_id) -> Optional[str]  (алиас)
- get_saved_suggestion(chat_id, message_id) -> Optional[str]
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("live-worker.suggest")

HISTORY_LIMIT = int(os.getenv("LIVE_SUGGEST_HISTORY", "15") or "15")


# === ЧТЕНИЕ ИСТОРИИ ===

def _get_chat_history(chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    """
    Последние N сообщений чата в хронологическом порядке (от старых к новым).
    Пустые тексты пропускаются.
    """
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import select, desc
        with get_session() as session:
            rows = session.execute(
                select(LiveMessage)
                .where(LiveMessage.chat_id == chat_id)
                .order_by(desc(LiveMessage.sent_at), desc(LiveMessage.id))
                .limit(limit)
            ).scalars().all()

            out: list[dict] = []
            # Разворачиваем хронологически (свежие были первыми)
            for r in reversed(rows):
                text = (r.text or "").strip()
                if not text:
                    continue
                out.append({
                    "message_id": r.message_id,
                    "text": text,
                    "is_outgoing": int(r.is_outgoing or 0),
                    "author_first_name": r.author_first_name,
                    "author_username": r.author_username,
                    "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                })
        return out
    except Exception as e:
        logger.exception("_get_chat_history(%s) упал: %s", chat_id, e)
        return []


def _save_suggestion(chat_id: int, message_id: int, suggestion: str) -> bool:
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(LiveMessage)
                .where(LiveMessage.chat_id == chat_id)
                .where(LiveMessage.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.llm_suggestion = suggestion
            row.llm_suggested_at = datetime.now(timezone.utc)
            session.commit()
        return True
    except Exception as e:
        logger.exception("_save_suggestion(%s, %s) упал: %s", chat_id, message_id, e)
        return False


# === ВЫЗОВ LLM ===

async def _call_llm(messages: list[dict]) -> Optional[str]:
    """
    Вызывает LLMClient.chat(messages=messages).
    messages — list[dict] с полями role/content (формат MessagesInput).
    """
    if not messages:
        logger.warning("_call_llm: пустой список messages.")
        return None

    try:
        from core.llm import LLMClient
    except Exception as e:
        logger.exception("Не удалось импортировать core.llm.LLMClient: %s", e)
        return None

    try:
        client = LLMClient()
    except Exception as e:
        logger.exception("LLMClient() упал: %s", e)
        return None

    fn = getattr(client, "chat", None)
    if not callable(fn):
        logger.error("У LLMClient нет метода chat().")
        return None

    try:
        res = await fn(messages=messages)
        text = _extract_text(res)
        if text:
            return text
        logger.warning("LLMClient.chat вернул пустой ответ.")
        return None
    except TypeError:
        # fallback: возможно, chat принимает позиционно
        try:
            res = await fn(messages)
            text = _extract_text(res)
            if text:
                return text
        except Exception as e:
            logger.exception("LLMClient.chat(messages) позиционно упал: %s", e)
        return None
    except Exception as e:
        logger.exception("LLMClient.chat упал: %s", e)
        return None


def _extract_text(res: Any) -> Optional[str]:
    """Извлекает текст из ответа LLMClient (LLMResponse, dict, str)."""
    if res is None:
        return None
    if isinstance(res, str):
        return res.strip() or None

    # LLMResponse — у него есть .content и __str__
    content = getattr(res, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()

    if isinstance(res, dict):
        for key in ("content", "text", "message", "answer"):
            v = res.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        ch = res.get("choices")
        if isinstance(ch, list) and ch:
            c0 = ch[0]
            if isinstance(c0, dict):
                msg = c0.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    return msg["content"].strip()
                if isinstance(c0.get("text"), str):
                    return c0["text"].strip()

    # Последний шанс — str()
    try:
        s = str(res).strip()
        return s or None
    except Exception:
        return None


# === ПУБЛИЧНОЕ API ===

async def generate_suggestion(chat_id: int, message_id: int) -> Optional[str]:
    """
    Собирает историю, строит messages через prompt_builder,
    вызывает LLMClient.chat(messages=...), сохраняет llm_suggestion.
    """
    try:
        history = _get_chat_history(chat_id, HISTORY_LIMIT)
        if not history:
            logger.debug("generate_suggestion: пустая история для chat=%s", chat_id)
            return None

        try:
            from workers.live.prompt_builder import (
                build_suggest_prompt,
                build_prompt_text,
            )
        except Exception as e:
            logger.exception("Не удалось импортировать live.prompt_builder: %s", e)
            return None

        messages = build_suggest_prompt(chat_id=chat_id, message_id=message_id, history=history)
        if not messages:
            logger.warning("generate_suggestion: build_suggest_prompt вернул пусто.")
            return None

        # Лог: prompt_text (для дебага)
        try:
            prompt_text = build_prompt_text(messages)
            if prompt_text:
                logger.debug(
                    "generate_suggestion: prompt для chat=%s msg=%s:\n%s",
                    chat_id, message_id, prompt_text[:2000],
                )
        except Exception:
            pass

        suggestion = await _call_llm(messages)
        if not suggestion:
            logger.warning("generate_suggestion: LLM вернул пусто.")
            return None

        _save_suggestion(chat_id, message_id, suggestion)
        logger.info(
            "generate_suggestion: chat=%s msg=%s, длина подсказки=%d",
            chat_id, message_id, len(suggestion),
        )
        return suggestion
    except Exception as e:
        logger.exception("generate_suggestion(%s, %s) упал: %s", chat_id, message_id, e)
        return None


async def suggest_for_message(chat_id: int, message_id: int) -> Optional[str]:
    """Алиас для совместимости с handler.py."""
    return await generate_suggestion(chat_id, message_id)


def get_saved_suggestion(chat_id: int, message_id: int) -> Optional[str]:
    """Читает уже сохранённую подсказку (без вызова LLM)."""
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(LiveMessage)
                .where(LiveMessage.chat_id == chat_id)
                .where(LiveMessage.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            s = row.llm_suggestion
            return s.strip() if isinstance(s, str) and s.strip() else None
    except Exception as e:
        logger.exception("get_saved_suggestion(%s, %s) упал: %s", chat_id, message_id, e)
        return None
