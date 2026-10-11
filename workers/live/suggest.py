# workers/live/suggest.py
"""
Генерация LLM-подсказок (llm_suggestion) для входящих live_messages.

Использует:
- core.llm.LLMClient (DeepSeek) — через chat_sync или chat.
- workers.live.prompt_builder.build_suggest_prompt (ленивый импорт).
- configs/product.json, configs/goal.json — читает prompt_builder.

Публичные функции:
- generate_suggestion(chat_id, message_id) -> Optional[str]
    Читает историю чата (последние N сообщений), собирает промпт,
    зовёт LLM, сохраняет llm_suggestion + llm_suggested_at в live_messages.
    Возвращает текст подсказки (или None при ошибке).

- suggest_for_message(chat_id, message_id) -> Optional[str]
    Алиас для generate_suggestion (для совместимости с handler.py).

Если LIVE_LLM_SUGGEST != "1" — функция всё равно работает (её вызывает
web-CRM вручную), но handler.py не будет её дёргать на каждое сообщение.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("live-worker.suggest")

# Сколько последних сообщений чата подаём в LLM как контекст.
HISTORY_LIMIT = int(os.getenv("LIVE_SUGGEST_HISTORY", "15") or "15")


# === ЧТЕНИЕ ИСТОРИИ ===

def _get_chat_history(chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    """
    Возвращает последние N сообщений чата в хронологическом порядке
    (от старых к новым). Только непустой text.
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

        rows = list(reversed(rows))
        out: list[dict] = []
        for r in rows:
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


# === СБОРКА ПРОМПТА ===

def _build_prompt(chat_id: int, message_id: int, history: list[dict]) -> Optional[str]:
    """
    Пробует:
    1. workers.live.prompt_builder.build_suggest_prompt — если есть.
    2. Иначе — встроенный минимальный промпт.
    """
    # Попытка №1 — внешний prompt_builder
    try:
        from workers.live import prompt_builder  # type: ignore
        fn = getattr(prompt_builder, "build_suggest_prompt", None)
        if callable(fn):
            try:
                p = fn(chat_id=chat_id, message_id=message_id, history=history)
                if isinstance(p, str) and p.strip():
                    return p
            except TypeError:
                # другая сигнатура
                try:
                    p = fn(history)
                    if isinstance(p, str) and p.strip():
                        return p
                except Exception as e:
                    logger.warning("prompt_builder.build_suggest_prompt (alt) упал: %s", e)
    except Exception as e:
        logger.debug("prompt_builder недоступен: %s", e)

    # Попытка №2 — встроенный промпт
    return _fallback_prompt(chat_id, history)


def _fallback_prompt(chat_id: int, history: list[dict]) -> str:
    """
    Минимальный промпт «на случай, если prompt_builder ещё не готов».
    Включает цель и товар из configs/, если они читаются.
    """
    lines: list[str] = []

    # Товар / цель — читаем мягко
    product, goal = _read_configs()
    if product:
        lines.append(f"Товар: {product.get('name') or '—'}")
        if product.get("description"):
            lines.append(f"Описание: {product['description']}")
        if product.get("price"):
            lines.append(f"Цена: {product['price']} {product.get('currency', '')}".strip())
    if goal:
        if goal.get("goal"):
            lines.append(f"Цель диалога: {goal['goal']}")
        if goal.get("tone"):
            lines.append(f"Тон: {goal['tone']}")

    lines.append("")
    lines.append("Ниже — история переписки (клиент / менеджер):")
    for m in history:
        who = "Мы" if m["is_outgoing"] else (m.get("author_first_name") or m.get("author_username") or "Клиент")
        lines.append(f"{who}: {m['text']}")
    lines.append("")
    lines.append(
        "Задача: предложи ОДИН короткий ответ менеджера (1–3 предложения), "
        "который продвигает диалог к цели. Без пояснений, только текст ответа."
    )
    return "\n".join(lines)


def _read_configs() -> tuple[dict, dict]:
    """Читает configs/product.json и configs/goal.json. Возвращает (product, goal)."""
    import json
    from pathlib import Path
    base = Path("/app/configs")
    if not base.is_dir():
        base = Path("./configs")

    product: dict = {}
    goal: dict = {}
    for name, target in (("product.json", "product"), ("goal.json", "goal")):
        try:
            p = base / name
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if target == "product":
                        product = data
                    else:
                        goal = data
        except Exception as e:
            logger.warning("Не удалось прочитать %s: %s", name, e)
    return product, goal


# === ВЫЗОВ LLM ===

async def _call_llm(prompt: str) -> Optional[str]:
    """
    Пробует LLMClient.chat (async). Если LLMClient sync-only — через to_thread(chat_sync).
    """
    try:
        from core.llm import LLMClient
    except Exception as e:
        logger.exception("Не удалось импортировать core.llm.LLMClient: %s", e)
        return None

    client = None
    try:
        client = LLMClient()
    except Exception as e:
        logger.exception("LLMClient() упал: %s", e)
        return None

    # Вариант 1: async chat
    fn = getattr(client, "chat", None)
    if callable(fn):
        try:
            res = await fn(prompt)
            text = _extract_text(res)
            if text:
                return text
        except TypeError:
            # Возможно, chat принимает messages-список
            try:
                res = await fn([{"role": "user", "content": prompt}])
                text = _extract_text(res)
                if text:
                    return text
            except Exception as e:
                logger.warning("LLMClient.chat(list) упал: %s", e)
        except Exception as e:
            logger.warning("LLMClient.chat(str) упал: %s", e)

    # Вариант 2: sync chat_sync через to_thread
    fn_sync = getattr(client, "chat_sync", None)
    if callable(fn_sync):
        import asyncio
        try:
            res = await asyncio.to_thread(fn_sync, prompt)
            text = _extract_text(res)
            if text:
                return text
        except Exception as e:
            logger.warning("LLMClient.chat_sync упал: %s", e)

    logger.error("Ни один метод LLMClient не сработал.")
    return None


def _extract_text(res) -> Optional[str]:
    """Извлекает текст из ответа LLMClient (str, dict, объект)."""
    if res is None:
        return None
    if isinstance(res, str):
        return res.strip() or None
    if isinstance(res, dict):
        for key in ("content", "text", "message", "answer"):
            v = res.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # choices-формат
        ch = res.get("choices")
        if isinstance(ch, list) and ch:
            c0 = ch[0]
            if isinstance(c0, dict):
                msg = c0.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    return msg["content"].strip()
                if isinstance(c0.get("text"), str):
                    return c0["text"].strip()
    # объект с .content
    v = getattr(res, "content", None)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


# === ПУБЛИЧНОЕ API ===

async def generate_suggestion(chat_id: int, message_id: int) -> Optional[str]:
    """
    Основная функция: собирает историю, строит промпт, зовёт LLM,
    сохраняет llm_suggestion. Возвращает текст или None.
    """
    try:
        history = _get_chat_history(chat_id, HISTORY_LIMIT)
        if not history:
            logger.debug("generate_suggestion: пустая история для chat=%s", chat_id)
            return None

        prompt = _build_prompt(chat_id, message_id, history)
        if not prompt:
            logger.warning("generate_suggestion: не удалось собрать промпт.")
            return None

        suggestion = await _call_llm(prompt)
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
