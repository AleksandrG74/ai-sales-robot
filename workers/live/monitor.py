# workers/live/monitor.py
"""
Управление monitored_chats: CRUD-операции.

Публичные функции:
- list_chats(enabled_only=False) -> list[dict]
- get_chat(chat_id) -> dict | None
- add_chat(chat_id, title=None, chat_type=None, added_by=None, enabled=1, auto_reply=0) -> dict
- remove_chat(chat_id) -> bool
- toggle_enabled(chat_id) -> bool
- set_auto_reply(chat_id, auto_reply: bool) -> bool
- update_title(chat_id, title) -> bool

Все функции безопасны: не падают, возвращают None/False и логируют при ошибке.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("live-worker.monitor")


# === ХЕЛПЕР: dict-представление ===

def _to_dict(row) -> dict:
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "chat_title": row.chat_title,
        "chat_type": row.chat_type,
        "enabled": int(row.enabled or 0),
        "auto_reply": int(row.auto_reply or 0),
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
        "added_at": row.added_at.isoformat() if row.added_at else None,
        "added_by": row.added_by,
    }


# === СПИСОК ===

def list_chats(enabled_only: bool = False) -> list[dict]:
    """Возвращает список всех (или только enabled) monitored_chats."""
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select, desc
        with get_session() as session:
            stmt = select(MonitoredChat).order_by(desc(MonitoredChat.last_message_at), MonitoredChat.chat_id)
            if enabled_only:
                stmt = stmt.where(MonitoredChat.enabled == 1)
            rows = session.execute(stmt).scalars().all()
            return [_to_dict(r) for r in rows]
    except Exception as e:
        logger.exception("list_chats упал: %s", e)
        return []


def get_chat(chat_id: int) -> Optional[dict]:
    """Возвращает dict по chat_id или None."""
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            return _to_dict(row) if row is not None else None
    except Exception as e:
        logger.exception("get_chat(%s) упал: %s", chat_id, e)
        return None


# === ДОБАВЛЕНИЕ / УДАЛЕНИЕ ===

def add_chat(
    chat_id: int,
    title: Optional[str] = None,
    chat_type: Optional[str] = None,
    added_by: Optional[str] = None,
    enabled: int = 1,
    auto_reply: int = 0,
) -> Optional[dict]:
    """
    Добавляет чат в monitored_chats. Если уже есть — обновляет title/type
    и возвращает существующую запись.
    """
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()

            if row is not None:
                # Обновим метаданные, если переданы
                if title:
                    row.chat_title = title
                if chat_type:
                    row.chat_type = chat_type
                session.commit()
                session.refresh(row)
                logger.info("add_chat: чат %s уже существует, метаданные обновлены.", chat_id)
                return _to_dict(row)

            row = MonitoredChat(
                chat_id=int(chat_id),
                chat_title=title,
                chat_type=chat_type,
                enabled=int(enabled),
                auto_reply=int(auto_reply),
                added_at=datetime.now(timezone.utc),
                added_by=added_by,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info("add_chat: добавлен chat_id=%s title=%r", chat_id, title)
            return _to_dict(row)
    except Exception as e:
        logger.exception("add_chat(%s) упал: %s", chat_id, e)
        return None


def remove_chat(chat_id: int) -> bool:
    """
    Удаляет чат из monitored_chats. Связанные live_messages НЕ трогаем —
    они остаются историей (чтобы можно было вернуть чат без потери данных).
    """
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                logger.info("remove_chat: чат %s не найден.", chat_id)
                return False
            session.delete(row)
            session.commit()
            logger.info("remove_chat: чат %s удалён из мониторинга.", chat_id)
            return True
    except Exception as e:
        logger.exception("remove_chat(%s) упал: %s", chat_id, e)
        return False


# === ИЗМЕНЕНИЕ ФЛАГОВ ===

def toggle_enabled(chat_id: int) -> Optional[bool]:
    """
    Переключает enabled (0 <-> 1). Возвращает новое значение (0/1) или None.
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
            row.enabled = 0 if int(row.enabled or 0) == 1 else 1
            session.commit()
            logger.info("toggle_enabled: chat=%s enabled=%s", chat_id, row.enabled)
            return int(row.enabled)
    except Exception as e:
        logger.exception("toggle_enabled(%s) упал: %s", chat_id, e)
        return None


def set_enabled(chat_id: int, enabled: bool) -> bool:
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.enabled = 1 if enabled else 0
            session.commit()
            return True
    except Exception as e:
        logger.exception("set_enabled(%s, %s) упал: %s", chat_id, enabled, e)
        return False


def set_auto_reply(chat_id: int, auto_reply: bool) -> bool:
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.auto_reply = 1 if auto_reply else 0
            session.commit()
            logger.info("set_auto_reply: chat=%s auto_reply=%s", chat_id, row.auto_reply)
            return True
    except Exception as e:
        logger.exception("set_auto_reply(%s, %s) упал: %s", chat_id, auto_reply, e)
        return False


def update_title(chat_id: int, title: Optional[str]) -> bool:
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.chat_title = title
            session.commit()
            return True
    except Exception as e:
        logger.exception("update_title(%s) упал: %s", chat_id, e)
        return False


# === ПРОВЕРКА: ЕСТЬ ЛИ ЧАТ В МОНИТОРИНГЕ ===

def is_monitored(chat_id: int) -> bool:
    """True, если чат в monitored_chats и enabled=1."""
    try:
        from core.database import get_session, MonitoredChat
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            return bool(row and int(row.enabled or 0) == 1)
    except Exception as e:
        logger.exception("is_monitored(%s) упал: %s", chat_id, e)
        return False
