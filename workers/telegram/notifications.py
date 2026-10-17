# workers/telegram/notifications.py
"""
NotificationPoller — опрос live_messages и отправка уведомлений оператору.

Работает внутри процесса telegram-worker (там же, где aiogram).
Каждые NOTIFICATIONS_POLL_INTERVAL секунд (по умолчанию 30):
- Читает live_messages, где is_outgoing=0 AND read_by_operator=0.
- Отправляет уведомление в TELEGRAM_OPERATOR_CHAT_ID.
- Помечает read_by_operator=1.

Graceful shutdown — по stop() (await task).
"""

import asyncio
import logging
import os
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger("telegram-worker.notifications")

# Настройки по умолчанию
DEFAULT_POLL_INTERVAL = 30
DEFAULT_BATCH_SIZE = 50
DEFAULT_ERR_SLEEP = 60


# === ЧТЕНИЕ И ОБНОВЛЕНИЕ БД (синхронно, через to_thread) ===

def _fetch_unread_messages(limit: int = DEFAULT_BATCH_SIZE) -> list[dict]:
    """
    Возвращает пачку непрочитанных входящих live_messages.
    """
    try:
        from core.database import get_session, LiveMessage, MonitoredChat
        from sqlalchemy import select, asc

        out: list[dict] = []
        with get_session() as session:
            rows = session.execute(
                select(LiveMessage)
                .where(LiveMessage.is_outgoing == 0)
                .where(LiveMessage.read_by_operator == 0)
                .order_by(asc(LiveMessage.sent_at), asc(LiveMessage.id))
                .limit(limit)
            ).scalars().all()

            for r in rows:
                # Заодно подтянем название чата
                chat_title = None
                try:
                    mc = session.execute(
                        select(MonitoredChat)
                        .where(MonitoredChat.chat_id == r.chat_id)
                        .limit(1)
                    ).scalar_one_or_none()
                    if mc is not None:
                        chat_title = mc.chat_title
                except Exception:
                    pass

                out.append({
                    "id": r.id,
                    "chat_id": r.chat_id,
                    "message_id": r.message_id,
                    "author_id": r.author_id,
                    "author_username": r.author_username,
                    "author_first_name": r.author_first_name,
                    "text": r.text,
                    "is_outgoing": int(r.is_outgoing or 0),
                    "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                    "chat_title": chat_title,
                })
        return out
    except Exception as e:
        logger.exception("_fetch_unread_messages упал: %s", e)
        return []


def _mark_read(ids: list[int]) -> bool:
    """Помечает пачку сообщений read_by_operator=1."""
    if not ids:
        return True
    try:
        from core.database import get_session, LiveMessage
        from sqlalchemy import update

        with get_session() as session:
            session.execute(
                update(LiveMessage)
                .where(LiveMessage.id.in_(ids))
                .values(read_by_operator=1)
            )
            session.commit()
        return True
    except Exception as e:
        logger.exception("_mark_read упал: %s", e)
        return False


# === FALLBACK-ФОРМАТТЕР (если в formatters.py нет format_live_notification) ===

def _fallback_format(msg: dict) -> str:
    chat_title = msg.get("chat_title") or f"chat {msg.get('chat_id')}"
    author = msg.get("author_first_name") or (
        f"@{msg['author_username']}" if msg.get("author_username") else
        f"id {msg.get('author_id') or '?'}"
    )
    text = (msg.get("text") or "")[:200]
    return (
        f"🆕 <b>{chat_title}</b>\n"
        f"👤 {author}: {text}\n"
        f"🆔 chat_id=<code>{msg.get('chat_id')}</code>"
    )


# === POLLER ===

class NotificationPoller:
    """
    Асинхронный поллер live_messages -> уведомления оператору.
    """

    def __init__(
        self,
        bot: Bot,
        operator_chat_id: int,
        poll_interval: Optional[int] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if not operator_chat_id:
            raise ValueError(
                "TELEGRAM_OPERATOR_CHAT_ID не задан. "
                "Уведомления отправлять некуда."
            )
        self.bot = bot
        self.operator_chat_id = int(operator_chat_id)
        self.batch_size = int(batch_size)
        self.poll_interval = int(
            poll_interval or
            os.getenv("NOTIFICATIONS_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL))
        )

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # --- lifecycle ---

    def start(self) -> None:
        """Запускает фоновую задачу-поллер (идемпотентно)."""
        if self._task is not None and not self._task.done():
            logger.debug("NotificationPoller уже запущен.")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._poll_loop(), name="notification-poller"
        )
        logger.info(
            "NotificationPoller запущен (interval=%ss, batch=%s, operator=%s).",
            self.poll_interval, self.batch_size, self.operator_chat_id,
        )

    async def stop(self) -> None:
        """Graceful shutdown: останавливает цикл и ждёт завершения."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("NotificationPoller: таймаут остановки, отменяю task.")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception as e:
                logger.warning("NotificationPoller: остановка с ошибкой: %s", e)
        logger.info("NotificationPoller остановлен.")

    # --- внутреннее ---

    async def _poll_loop(self) -> None:
        """Основной цикл: fetch -> format -> send -> mark_read -> sleep."""
        while not self._stop_event.is_set():
            try:
                await self._process_batch()
            except Exception as e:
                logger.exception("_process_batch упал: %s", e)

            # Спим poll_interval, но прерываемся на stop_event
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval
                )
                # если дошли сюда — stop_event взведён
                return
            except asyncio.TimeoutError:
                continue

    async def _process_batch(self) -> None:
        """
        Одна итерация обработки: читает пачку, шлёт, помечает.
        """
        # Синхронные БД-операции в отдельный поток, чтобы не блокировать loop
        messages = await asyncio.to_thread(_fetch_unread_messages, self.batch_size)
        if not messages:
            return

        logger.info("NotificationPoller: %d новых входящих.", len(messages))

        sent_ids: list[int] = []
        for m in messages:
            text = await self._format_message(m)
            try:
                await self.bot.send_message(
                    chat_id=self.operator_chat_id,
                    text=text,
                    parse_mode="HTML",
                )
                sent_ids.append(int(m["id"]))
            except TelegramAPIError as e:
                # Бот заблокирован или чат недоступен — логируем и спим подольше
                logger.error(
                    "TelegramAPIError при отправке уведомления (chat=%s): %s. Спим %ss.",
                    m.get("chat_id"), e, DEFAULT_ERR_SLEEP,
                )
                await asyncio.sleep(DEFAULT_ERR_SLEEP)
                continue
            except Exception as e:
                logger.exception(
                    "Не удалось отправить уведомление (chat=%s): %s",
                    m.get("chat_id"), e,
                )
                continue

        if sent_ids:
            await asyncio.to_thread(_mark_read, sent_ids)
            logger.info("NotificationPoller: помечено прочитанными %d.", len(sent_ids))

    async def _format_message(self, msg: dict) -> str:
        """
        Форматирует уведомление: сначала пробует formatters.format_live_notification,
        затем fallback.
        """
        try:
            from workers.telegram import formatters
            fn = getattr(formatters, "format_live_notification", None)
            if callable(fn):
                try:
                    text = fn(msg)
                    if isinstance(text, str) and text.strip():
                        return text
                except Exception as e:
                    logger.warning("format_live_notification упал: %s", e)
        except Exception as e:
            logger.debug("formatters недоступен: %s", e)

        return _fallback_format(msg)
