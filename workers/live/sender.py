# workers/live/sender.py
"""
Отправка исходящих сообщений через Telethon user-account.

Используется:
- web-CRM: оператор нажал «Отправить» в браузере.
- auto_reply_to_message(): авто-ответ при LIVE_AUTO_REPLY=1.

Публичная функция:
- send_operator_reply(chat_id, text, reply_to_msg_id=None, operator_id=None) -> dict
    Отправляет сообщение, сохраняет в live_messages (is_outgoing=1),
    логирует в operator_actions, инкрементит live_sessions.messages_sent.

- auto_reply_to_message(chat_id, message_id) -> bool
    Достаёт llm_suggestion из live_messages и, если он есть, отправляет.

Требует глобально установленного клиента через set_client().
Модуль main.py обязан вызвать set_client(client) после connect().
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("live-worker.sender")

# Глобальный клиент LiveTelethonClient. Устанавливается main.py.
_CLIENT: Optional[Any] = None

# id текущей live_sessions (для счётчика messages_sent).
_CURRENT_SESSION_ID: Optional[int] = None


def set_client(client: Any) -> None:
    """Сохраняет ссылку на LiveTelethonClient для использования в sender."""
    global _CLIENT
    _CLIENT = client
    logger.info("sender: клиент установлен (%s).", type(client).__name__ if client else None)


def set_session_id(session_id: Optional[int]) -> None:
    global _CURRENT_SESSION_ID
    _CURRENT_SESSION_ID = session_id


def _bump_session_sent() -> None:
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
            row.messages_sent = int(row.messages_sent or 0) + 1
            session.commit()
    except Exception as e:
        logger.warning("_bump_session_sent упал: %s", e)


def _save_outgoing_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_to_msg_id: Optional[int],
    sent_at: Optional[datetime],
) -> bool:
    """
    Сохраняет исходящее сообщение в live_messages.
    Идемпотентно по UNIQUE(chat_id, message_id).
    """
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import select
        with get_session() as session:
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
                author_id=None,
                author_username=None,
                author_first_name=None,
                text=text,
                is_outgoing=1,
                reply_to_msg_id=reply_to_msg_id,
                sent_at=sent_at or datetime.now(timezone.utc),
                imported_at=datetime.now(timezone.utc),
                read_by_operator=1,
            )
            session.add(row)
            session.commit()
        return True
    except Exception as e:
        logger.exception(
            "Не удалось сохранить исходящее live_message chat=%s msg=%s: %s",
            chat_id, message_id, e,
        )
        return False


def _log_operator_action(chat_id: int, action: str, operator_id: Optional[int]) -> None:
    try:
        from core.database import get_session, OperatorAction
        with get_session() as session:
            row = OperatorAction(
                dialogue_id=f"live:{chat_id}",
                action=action,
                operator_id=operator_id,
            )
            session.add(row)
            session.commit()
    except Exception as e:
        logger.warning("_log_operator_action упал: %s", e)


async def send_operator_reply(
    chat_id: int,
    text: str,
    reply_to_msg_id: Optional[int] = None,
    operator_id: Optional[int] = None,
) -> dict:
    """
    Отправляет сообщение от имени user-account в чат chat_id.

    Возвращает dict:
      {"ok": bool, "message_id": int|None, "error": str|None}
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message_id": None, "error": "Пустой текст"}

    if _CLIENT is None:
        logger.error("send_operator_reply: клиент не установлен.")
        return {"ok": False, "message_id": None, "error": "клиент не установлен"}

    entity = await _CLIENT.get_entity(chat_id)
    if entity is None:
        logger.error("send_operator_reply: не удалось получить entity для chat_id=%s", chat_id)
        return {"ok": False, "message_id": None, "error": f"entity not found: {chat_id}"}

    sent = await _CLIENT.send_message(entity, text, reply_to=reply_to_msg_id)
    if sent is None:
        return {"ok": False, "message_id": None, "error": "send_message вернул None"}

    msg_id = int(getattr(sent, "id", 0) or 0)
    sent_at = getattr(sent, "date", None) or datetime.now(timezone.utc)

    if msg_id:
        _save_outgoing_message(chat_id, msg_id, text, reply_to_msg_id, sent_at)

    _bump_session_sent()
    _log_operator_action(chat_id, "send_operator_reply", operator_id)

    logger.info(
        "send_operator_reply: chat=%s msg=%s operator=%s len=%d",
        chat_id, msg_id, operator_id, len(text),
    )
    return {"ok": True, "message_id": msg_id, "error": None}


async def auto_reply_to_message(client, chat_id: int, message_id: int) -> bool:
    """
    Авто-ответ: достаёт llm_suggestion у указанного сообщения
    и отправляет его как ответ (reply_to=message_id).

    Возвращает True, если сообщение было отправлено.
    """
    suggestion = _get_suggestion(chat_id, message_id)
    if not suggestion:
        logger.debug("auto_reply_to_message: нет llm_suggestion для chat=%s msg=%s", chat_id, message_id)
        return False

    if client is None:
        logger.error("auto_reply_to_message: клиент не передан.")
        return False

    entity = await client.get_entity(chat_id)
    if entity is None:
        return False

    sent = await client.send_message(entity, suggestion, reply_to=message_id)
    if sent is None:
        return False

    msg_id = int(getattr(sent, "id", 0) or 0)
    sent_at = getattr(sent, "date", None) or datetime.now(timezone.utc)
    if msg_id:
        _save_outgoing_message(chat_id, msg_id, suggestion, message_id, sent_at)

    _bump_session_sent()
    _log_operator_action(chat_id, "auto_reply", operator_id=None)
    logger.info("auto_reply_to_message: отправлено в chat=%s (reply_to=%s)", chat_id, message_id)
    return True


def _get_suggestion(chat_id: int, message_id: int) -> Optional[str]:
    """Читает llm_suggestion у конкретного сообщения."""
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
        logger.exception("_get_suggestion(%s, %s) упал: %s", chat_id, message_id, e)
        return None


def _env_auto_reply_enabled() -> bool:
    return os.getenv("LIVE_AUTO_REPLY", "0").strip() == "1"
