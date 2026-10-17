# workers/live/handler.py
"""
Обработка входящих сообщений из Telegram (Telethon events.NewMessage).

Задачи:
- Регистрирует handler на событие NewMessage у LiveTelethonClient.
- Фильтрует сообщения: только из monitored_chats, только enabled=1.
- Сохраняет в live_messages (UNIQUE(chat_id, message_id) — идемпотентно).
- Обновляет monitored_chats.last_message_at.
- Если включён LIVE_LLM_SUGGEST=1 — генерирует llm_suggestion (в suggest.py).
- Опционально (LIVE_AUTO_REPLY=1, auto_reply=1 у чата) — авто-ответ через sender.

Модуль зависит от:
- workers.live.telethon_client.LiveTelethonClient
- workers.live.suggest (ленивый импорт — чтобы не падать, если модуль ещё не создан)
- workers.live.sender (ленивый импорт — по той же причине)
- core.database (get_session, MonitoredChat, LiveMessage)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("live-worker.handler")

# Внутренний singleton: id live_sessions, в рамках которого работаем.
# Устанавливается main.py, если захочет учитывать messages_in/out.
_CURRENT_SESSION_ID: Optional[int] = None


def set_session_id(session_id: Optional[int]) -> None:
    """Позволяет main.py передать id текущей live_sessions для счётчиков."""
    global _CURRENT_SESSION_ID
    _CURRENT_SESSION_ID = session_id


# === ХЕЛПЕРЫ БД ===

def _get_monitored_chat(chat_id: int) -> Optional[dict]:
    """
    Возвращает dict с данными monitored_chats по chat_id или None,
    если чат не отслеживается/выключен.
    """
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "chat_id": row.chat_id,
                "chat_title": row.chat_title,
                "chat_type": row.chat_type,
                "enabled": int(row.enabled or 0),
                "auto_reply": int(row.auto_reply or 0),
                "keywords": row.keywords or [],
            }
    except Exception as e:
        logger.exception("_get_monitored_chat(%s) упал: %s", chat_id, e)
        return None


def _save_live_message(
    chat_id: int,
    message_id: int,
    author_id: Optional[int],
    author_username: Optional[str],
    author_first_name: Optional[str],
    text: Optional[str],
    is_outgoing: int,
    reply_to_msg_id: Optional[int],
    sent_at: Optional[datetime],
    force_read: bool = False,
) -> bool:
    """
    Идемпотентно пишет сообщение в live_messages.
    Возвращает True, если строка реально вставлена (или уже была — считается True),
    False — при ошибке.
    """
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import select
        with get_session() as session:
            # Проверяем дубликат (UNIQUE(chat_id, message_id))
            existing = session.execute(
                select(LiveMessage)
                .where(LiveMessage.chat_id == chat_id)
                .where(LiveMessage.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return True

            row = LiveMessage(
                chat_id=chat_id,
                message_id=message_id,
                author_id=author_id,
                author_username=author_username,
                author_first_name=author_first_name,
                text=text,
                is_outgoing=int(is_outgoing),
                reply_to_msg_id=reply_to_msg_id,
                sent_at=sent_at or datetime.now(timezone.utc),
                imported_at=datetime.now(timezone.utc),
                read_by_operator=1 if (is_outgoing or force_read) else 0,
            )
            session.add(row)
            session.commit()
        return True
    except Exception as e:
        logger.exception(
            "Не удалось сохранить live_message chat=%s msg=%s: %s",
            chat_id, message_id, e,
        )
        return False


def _touch_monitored_chat(chat_id: int, when: Optional[datetime]) -> None:
    """Обновляет monitored_chats.last_message_at."""
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.last_message_at = when or datetime.now(timezone.utc)
            session.commit()
    except Exception as e:
        logger.warning("_touch_monitored_chat(%s) упал: %s", chat_id, e)


def _bump_session_received() -> None:
    """Инкремент messages_in в live_sessions (если задан session_id)."""
    if not _CURRENT_SESSION_ID:
        return
    try:
        from core.database import get_session, LiveSession
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(LiveSession).where(LiveSession.id == _CURRENT_SESSION_ID).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.messages_received = int(row.messages_received or 0) + 1
            session.commit()
    except Exception as e:
        logger.warning("_bump_session_received упал: %s", e)


# === ИЗВЛЕЧЕНИЕ ДАННЫХ ИЗ TELEGRAM-СООБЩЕНИЯ ===

def _extract(event: Any) -> Optional[dict]:
    """
    Приводит Telethon-объект NewMessage.Event к dict-представлению.
    Возвращает None, если событие не подходит (например, нет chat_id).
    """
    try:
        msg = event.message
        if msg is None:
            return None

        chat_id = event.chat_id
        if chat_id is None:
            return None

        sender = None
        try:
            sender = event.sender  # User/Channel/...
        except Exception:
            sender = None

        return {
            "chat_id": int(chat_id),
            "message_id": int(msg.id),
            "author_id": int(getattr(sender, "id", 0)) if sender is not None else None,
            "author_username": getattr(sender, "username", None),
            "author_first_name": getattr(sender, "first_name", None),
            "text": msg.message or "",
            "is_outgoing": 1 if getattr(msg, "out", False) else 0,
            "reply_to_msg_id": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
            "sent_at": getattr(msg, "date", None),
        }
    except Exception as e:
        logger.exception("_extract упал: %s", e)
        return None


# === ОСНОВНОЙ ОБРАБОТЧИК ===


# === ПРОВЕРКА KEYWORDS ===

def _check_keywords(text: str, keywords: list) -> bool:
    """
    Проверяет, содержит ли текст хотя бы одно из ключевых слов (case-insensitive).
    Если keywords пуст или None — возвращает True (пропускать всё).
    """
    if not keywords:
        return True
    if not text:
        return False
    text_lower = text.lower()
    for kw in keywords:
        if isinstance(kw, str) and kw.strip():
            if kw.strip().lower() in text_lower:
                return True
    return False


async def on_new_message(event: Any) -> None:
    """
    Реакция на новое сообщение.
    Порядок:
      1. Извлечь данные.
      2. Проверить, что чат в monitored_chats и enabled=1.
      3. Сохранить в live_messages.
      4. Обновить last_message_at.
      5. Опционально: LLM-подсказка.
      6. Опционально: авто-ответ.
    """
    try:
        data = _extract(event)
        if data is None:
            return

        chat_id = data["chat_id"]
        monitored = _get_monitored_chat(chat_id)
        if monitored is None:
            logger.debug("Сообщение из неотслеживаемого чата %s — игнорирую.", chat_id)
            return
        if not monitored.get("enabled"):
            logger.debug("Чат %s отключён (enabled=0) — игнорирую.", chat_id)
            return

        # --- Проверка keywords ---
        is_ignored = False
        if not data["is_outgoing"]:
            kws = monitored.get("keywords") or []
            if not _check_keywords(data["text"], kws):
                is_ignored = True

        # Пишем в live_messages
        ok = _save_live_message(
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            author_id=data["author_id"],
            author_username=data["author_username"],
            author_first_name=data["author_first_name"],
            text=data["text"],
            is_outgoing=data["is_outgoing"],
            reply_to_msg_id=data["reply_to_msg_id"],
            sent_at=data["sent_at"],
            force_read=is_ignored,  # <--- Новый параметр
        )
        if not ok:
            return

        _touch_monitored_chat(chat_id, data["sent_at"])

        if data["is_outgoing"]:
            logger.debug("Исходящее сообщение chat=%s msg=%s (не считаем messages_in).", chat_id, data["message_id"])
            return

        _bump_session_received()

        if is_ignored:
            logger.info(
                "Сообщение проигнорировано по keywords: chat=%s msg=%s (нет совпадений)",
                chat_id, data["message_id"],
            )
            return

        logger.info(
            "Новое входящее: chat=%s msg=%s author=@%s text=%r",
            chat_id, data["message_id"], data["author_username"], (data["text"] or "")[:80],
        )

        # Опционально: LLM-подсказка
        if os.getenv("LIVE_LLM_SUGGEST", "1").strip() == "1":
            await _try_generate_suggestion(chat_id, data)

        # Опционально: авто-ответ
        auto_reply_env = os.getenv("LIVE_AUTO_REPLY", "0").strip() == "1"
        if auto_reply_env and monitored.get("auto_reply"):
            await _try_auto_reply(chat_id, data)

    except Exception as e:
        logger.exception("on_new_message упал: %s", e)


async def _try_generate_suggestion(chat_id: int, data: dict) -> None:
    """
    Лениво дёргает suggest.py. Если модуля/функции нет — просто пропускает.
    """
    try:
        from workers.live import suggest  # type: ignore
    except Exception as e:
        logger.debug("suggest.py недоступен (%s) — пропускаю LLM-подсказку.", e)
        return

    fn = getattr(suggest, "generate_suggestion", None) or getattr(suggest, "suggest_for_message", None)
    if not callable(fn):
        logger.debug("В suggest.py нет generate_suggestion — пропускаю.")
        return

    try:
        await fn(chat_id=chat_id, message_id=data["message_id"])
        logger.info("LLM-подсказка для chat=%s msg=%s сгенерирована.", chat_id, data["message_id"])
    except Exception as e:
        logger.exception("generate_suggestion упал для chat=%s: %s", chat_id, e)


async def _try_auto_reply(chat_id: int, data: dict) -> None:
    """
    Лениво дёргает sender.py для авто-ответа.
    """
    try:
        from workers.live import sender  # type: ignore
    except Exception as e:
        logger.debug("sender.py недоступен (%s) — пропускаю авто-ответ.", e)
        return

    fn = getattr(sender, "auto_reply_to_message", None)
    if not callable(fn):
        logger.debug("В sender.py нет auto_reply_to_message — пропускаю.")
        return

    try:
        await fn(chat_id=chat_id, message_id=data["message_id"])
        logger.info("Авто-ответ отправлен в chat=%s msg=%s.", chat_id, data["message_id"])
    except Exception as e:
        logger.exception("auto_reply_to_message упал для chat=%s: %s", chat_id, e)


# === РЕГИСТРАЦИЯ ===

def register_handlers(client: Any) -> None:
    """
    Регистрирует обработчик NewMessage у LiveTelethonClient.
    Вызывается из main.py после connect().
    """
    try:
        from telethon import events  # type: ignore
    except Exception as e:
        logger.exception("Не удалось импортировать telethon.events: %s", e)
        return

    try:
        # incoming=True, outgoing=True — слушаем и то, и другое
        # (исходящие тоже фиксируем, чтобы видеть ответы оператора из Telegram-клиента).
        event_builder = events.NewMessage(incoming=True, outgoing=True)
        client.add_event_handler(event_builder, on_new_message)
        logger.info("handler.py: зарегистрирован on_new_message на events.NewMessage.")
    except Exception as e:
        logger.exception("register_handlers упал: %s", e)
