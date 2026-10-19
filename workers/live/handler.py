# workers/live/handler.py
"""
Обработка входящих сообщений через Long Polling (iter_messages).
Жёсткая фильтрация: не обрабатываем свои сообщения (m.out == True) и сообщения от нашего аккаунта.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("live-worker.handler")

_CURRENT_SESSION_ID: Optional[int] = None


def set_session_id(session_id: Optional[int]) -> None:
    global _CURRENT_SESSION_ID
    _CURRENT_SESSION_ID = session_id


# === ХЕЛПЕРЫ БД ===

def _get_monitored_chats() -> list[dict]:
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            rows = session.execute(
                select(MonitoredChat).where(MonitoredChat.enabled == 1)
            ).scalars().all()
            return [
                {
                    "chat_id": r.chat_id,
                    "chat_title": r.chat_title,
                    "keywords": r.keywords or [],
                    "auto_reply": int(r.auto_reply or 0),
                }
                for r in rows
            ]
    except Exception as e:
        logger.exception("_get_monitored_chats упал: %s", e)
        return []


def _is_message_saved(chat_id: int, message_id: int) -> bool:
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(LiveMessage.id)
                .where(LiveMessage.chat_id == chat_id)
                .where(LiveMessage.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()
            return row is not None
    except Exception as e:
        logger.warning("_is_message_saved(%s, %s) упал: %s", chat_id, message_id, e)
        return False


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
        logger.exception("Не удалось сохранить live_message chat=%s msg=%s: %s", chat_id, message_id, e)
        return False


def _touch_monitored_chat(chat_id: int, when: Optional[datetime]) -> None:
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row:
                row.last_message_at = when or datetime.now(timezone.utc)
                session.commit()
    except Exception as e:
        logger.warning("_touch_monitored_chat(%s) упал: %s", chat_id, e)


def _bump_session_received() -> None:
    if not _CURRENT_SESSION_ID:
        return
    try:
        from core.database import get_session, LiveSession
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(LiveSession).where(LiveSession.id == _CURRENT_SESSION_ID).limit(1)
            ).scalar_one_or_none()
            if row:
                row.messages_received = int(row.messages_received or 0) + 1
                session.commit()
    except Exception as e:
        logger.warning("_bump_session_received упал: %s", e)


# === ЛОГИКА ОБРАБОТКИ ===

def _check_keywords(text: str, keywords: list) -> bool:
    if not keywords:
        return True
    if not text:
        return False
    t_lower = text.lower()
    return any(kw.strip().lower() in t_lower for kw in keywords if isinstance(kw, str) and kw.strip())


async def handle_new_message(msg, chat_meta: dict) -> None:
    """Обработка одного сообщения (LLM, авто-ответ)."""
    chat_id = msg.chat_id
    message_id = msg.id
    
    _bump_session_received()
    logger.info("polling: найдено новое сообщение id=%s (chat=%s)", message_id, chat_id)

    # LLM Suggest
    if os.getenv("LIVE_LLM_SUGGEST", "1").strip() == "1":
        try:
            from workers.live import suggest
            fn = getattr(suggest, "generate_suggestion", None) or getattr(suggest, "suggest_for_message", None)
            if callable(fn):
                await fn(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.exception("suggest упал для chat=%s: %s", chat_id, e)

    # Auto-reply
    auto_reply_env = os.getenv("LIVE_AUTO_REPLY", "0").strip() == "1"
    if auto_reply_env and chat_meta.get("auto_reply"):
        try:
            from workers.live import sender
            fn = getattr(sender, "auto_reply_to_message", None)
            if callable(fn):
                await fn(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.exception("auto_reply упал для chat=%s: %s", chat_id, e)


# === POLLING ===

async def polling_loop(client: Any, stop_event: asyncio.Event) -> None:
    """
    Основной цикл: раз в 5 секунд опрашивает iter_messages для всех enabled чатов.
    """
    logger.info("handler.polling_loop: запущен (интервал 5с).")
    
    # Получаем ID нашего аккаунта, чтобы отфильтровать свои сообщения
    me = await client.get_me()
    my_id = me.id if me else None
    logger.info("handler.polling_loop: my_id=%s", my_id)

    while not stop_event.is_set():
        try:
            chats = _get_monitored_chats()
            for chat in chats:
                if stop_event.is_set():
                    break
                    
                chat_id = chat["chat_id"]
                entity = await client.get_entity(chat_id)
                if entity is None:
                    continue
                
                # Получаем последние 5 сообщений через iter_messages (надёжнее)
                messages = []
                async for m in client.iter_messages(entity, limit=5):
                    messages.append(m)
                
                if not messages:
                    continue

                # В хронологическом порядке (старые первыми)
                for msg in reversed(messages):
                    if stop_event.is_set():
                        break
                    
                    # Жёсткие фильтры:
                    # 1. Не пустое
                    if not msg.message:
                        continue
                    # 2. Не наше сообщение (out=False)
                    if getattr(msg, "out", False):
                        continue
                    # 3. Не от нашего аккаунта
                    if my_id and getattr(msg, "sender_id", None) == my_id:
                        continue

                    # Проверяем, есть ли уже в БД
                    if _is_message_saved(chat_id, msg.id):
                        continue

                    # Проверяем keywords
                    if not _check_keywords(msg.message, chat.get("keywords")):
                        _save_live_message(
                            chat_id=chat_id, message_id=msg.id, author_id=None,
                            author_username=None, author_first_name=None,
                            text=msg.message, is_outgoing=0, reply_to_msg_id=None,
                            sent_at=msg.date, force_read=True
                        )
                        continue

                    # Сохраняем
                    sender = None
                    try:
                        sender = await msg.get_sender()
                    except Exception:
                        pass
                        
                    ok = _save_live_message(
                        chat_id=chat_id, message_id=msg.id,
                        author_id=getattr(sender, "id", None) if sender else getattr(msg, "sender_id", None),
                        author_username=getattr(sender, "username", None) if sender else None,
                        author_first_name=getattr(sender, "first_name", None) if sender else None,
                        text=msg.message, is_outgoing=0,
                        reply_to_msg_id=getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
                        sent_at=msg.date,
                    )
                    
                    if ok:
                        _touch_monitored_chat(chat_id, msg.date)
                        # Запускаем обработку в фоне
                        asyncio.create_task(handle_new_message(msg, chat))

        except Exception as e:
            logger.exception("Ошибка в polling_loop: %s", e)
            
        # Ожидаем 5 секунд или прерываемся при stop_event
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    logger.info("handler.polling_loop: остановлен.")
