# workers/live/telethon_client.py
"""
Обёртка над Telethon TelegramClient для live-worker.

Возможности:
- connect / disconnect (владение .session файлом).
- check_session_file — проверка существования .session перед стартом.
- pause_polling / resume_polling — приостановка обработки новых событий
  на время работы miner'а (тот же user-account, одна сессия).
- Методы-хелперы: get_me, get_entity, iter_messages, send_message.

Telethon-клиент создаётся внутри конструктора, но реальное подключение —
в connect(). На ПК импорт модуля не делает сетевых вызовов.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger("live-worker.telethon_client")


class SessionInUseError(RuntimeError):
    """Сессия занята другим процессом (например, miner'ом)."""


class LiveTelethonClient:
    """
    Обёртка над TelegramClient.

    Регистрируемые обработчики (NewMessage и т.п.) хранятся в self._event_handlers.
    pause_polling временно снимает их; resume_polling возвращает обратно.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_path: str,
        flood_sleep_threshold: int = 60,
    ) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.phone = phone
        self.session_path = session_path
        self.flood_sleep_threshold = flood_sleep_threshold

        self._client: Optional[Any] = None
        self._connected: bool = False
        self._paused: bool = False

        # (event_builder, callback) — то, что регистрируем через add_event_handler
        self._event_handlers: list[tuple[Any, Any]] = []
        # Хендлеры, временно снятые при pause_polling (чтобы вернуть при resume)
        self._paused_handlers: list[tuple[Any, Any]] = []

    # === ПРОВЕРКА ФАЙЛА СЕССИИ ===

    @staticmethod
    def check_session_file(session_path: str) -> bool:
        """
        Проверяет наличие .session файла (без .session в конце пути).
        Telethon хранит файл как <session_path>.session.
        Возвращает True, если файл существует.
        """
        try:
            base = Path(session_path)
            candidates = [
                base,                            # если путь уже с .session
                Path(str(base) + ".session"),    # Telethon добавляет .session
                base.with_suffix(".session"),    # если base — без расширения
            ]
            for c in candidates:
                try:
                    if c.is_file() and c.stat().st_size > 0:
                        logger.info("Найден файл сессии: %s", c)
                        return True
                except OSError:
                    continue
            logger.warning(
                "Файл сессии не найден (проверены: %s)",
                ", ".join(str(c) for c in candidates),
            )
            return False
        except Exception as e:
            logger.exception("check_session_file упал: %s", e)
            return False

    # === CONNECT / DISCONNECT ===

    async def connect(self) -> None:
        """
        Создаёт TelegramClient (если нужно) и подключается.
        Если сессия занята — поднимает SessionInUseError.
        """
        if self._connected:
            logger.debug("connect: уже подключены — пропускаю.")
            return

        try:
            from telethon import TelegramClient
            from telethon.errors import (
                SessionPasswordNeededError,
                AuthKeyDuplicatedError,
            )
        except Exception as e:
            raise RuntimeError(f"Не удалось импортировать Telethon: {e}") from e

        # Гарантируем, что директория для .session существует
        try:
            Path(self.session_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("Не удалось создать папку для .session (%s): %s", self.session_path, e)

        # Ленивая инициализация клиента
        if self._client is None:
            self._client = TelegramClient(
                self.session_path,
                self.api_id,
                self.api_hash,
                flood_sleep_threshold=self.flood_sleep_threshold,
            )

        try:
            await self._client.connect()
        except AuthKeyDuplicatedError as e:
            raise SessionInUseError(
                "AuthKeyDuplicatedError: сессия используется в другом месте "
                "(вероятно, запущен miner). Останови miner и попробуй снова."
            ) from e
        except Exception as e:
            logger.exception("Не удалось подключиться к Telegram: %s", e)
            raise

        # Проверяем авторизацию
        try:
            if not await self._client.is_user_authorized():
                logger.warning(
                    "Пользователь не авторизован в сессии %s. "
                    "Возможно, потребуется интерактивный вход (код из SMS).",
                    self.session_path,
                )
                # Пробуем стандартный вход по номеру
                try:
                    await self._client.start(phone=self.phone)
                except SessionPasswordNeededError:
                    raise RuntimeError(
                        "Облачный пароль (2FA) требуется для входа. "
                        "Авторизуйся вручную: python -m workers.live.auth (или аналог)."
                    )
        except Exception as e:
            logger.exception("Проверка авторизации упала: %s", e)
            raise

        me = None
        try:
            me = await self._client.get_me()
        except Exception as e:
            logger.warning("get_me упал: %s", e)

        self._connected = True
        if me is not None:
            logger.info(
                "Live-клиент подключён: id=%s, username=@%s, phone=%s",
                getattr(me, "id", None),
                getattr(me, "username", None),
                getattr(me, "phone", None),
            )
        else:
            logger.info("Live-клиент подключён (данные о себе недоступны).")

    async def disconnect(self) -> None:
        """Отключается и закрывает клиент."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception as e:
            logger.warning("disconnect упал: %s", e)
        finally:
            self._connected = False
            self._paused = False
            self._event_handlers.clear()
            self._paused_handlers.clear()

    # === EVENT HANDLERS ===

    def add_event_handler(self, event_builder: Any, callback: Any) -> None:
        """
        Регистрирует обработчик события Telethon.
        event_builder — например telethon.events.NewMessage.
        callback — async-функция.
        """
        if self._client is None:
            raise RuntimeError("Клиент не создан — вызови connect() сначала.")
        try:
            self._client.add_event_handler(callback, event_builder)
            self._event_handlers.append((event_builder, callback))
            logger.debug(
                "Зарегистрирован обработчик: %s -> %s",
                getattr(event_builder, "__name__", event_builder),
                getattr(callback, "__name__", callback),
            )
        except Exception as e:
            logger.exception("add_event_handler упал: %s", e)

    # === PAUSE / RESUME (для сосуществования с miner) ===

    async def pause_polling(self) -> None:
        """
        Снимает зарегистрированные event-handlers, чтобы не обрабатывать
        новые события. Само соединение с Telegram остаётся живым.
        """
        if self._client is None or self._paused:
            return
        try:
            for event_builder, callback in list(self._event_handlers):
                try:
                    self._client.remove_event_handler(callback, event_builder)
                    self._paused_handlers.append((event_builder, callback))
                except Exception as e:
                    logger.warning("Не удалось снять handler %s: %s", callback, e)
            self._paused = True
            logger.info("polling приостановлен (снято %d обработчиков).", len(self._paused_handlers))
        except Exception as e:
            logger.exception("pause_polling упал: %s", e)

    async def resume_polling(self) -> None:
        """Возвращает снятые event-handlers обратно."""
        if self._client is None or not self._paused:
            return
        try:
            for event_builder, callback in list(self._paused_handlers):
                try:
                    self._client.add_event_handler(callback, event_builder)
                except Exception as e:
                    logger.warning("Не удалось вернуть handler %s: %s", callback, e)
            self._event_handlers.extend(self._paused_handlers)
            self._paused_handlers.clear()
            self._paused = False
            logger.info("polling возобновлён.")
        except Exception as e:
            logger.exception("resume_polling упал: %s", e)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def raw(self) -> Optional[Any]:
        """Доступ к «сырому» TelegramClient — для низкоуровневых задач."""
        return self._client

    # === ХЕЛПЕРЫ ===

    async def get_me(self) -> Optional[Any]:
        if self._client is None:
            return None
        try:
            return await self._client.get_me()
        except Exception as e:
            logger.warning("get_me упал: %s", e)
            return None

    async def get_entity(self, chat_id: int) -> Optional[Any]:
        """Возвращает entity (chat/user/channel) по id."""
        if self._client is None:
            return None
        try:
            return await self._client.get_entity(chat_id)
        except Exception as e:
            logger.warning("get_entity(%s) упал: %s", chat_id, e)
            return None

    async def iter_messages(
        self,
        entity: Any,
        limit: int = 50,
        offset_id: int = 0,
        reverse: bool = False,
    ) -> AsyncIterator[Any]:
        """
        Итератор по сообщениям чата (свежие по умолчанию).
        Кидает исключения наружу — вызывающая сторона решает, что делать.
        """
        if self._client is None:
            return
        try:
            async for msg in self._client.iter_messages(
                entity, limit=limit, offset_id=offset_id, reverse=reverse,
            ):
                yield msg
        except Exception as e:
            logger.exception("iter_messages(%s) упал: %s", entity, e)
            raise

    async def send_message(
        self,
        entity: Any,
        text: str,
        reply_to: Optional[int] = None,
    ) -> Optional[Any]:
        """
        Отправляет сообщение. Возвращает объект отправленного сообщения
        (или None при ошибке). Логирует ошибки, но не падает.
        """
        if self._client is None:
            logger.error("send_message: клиент не создан.")
            return None
        try:
            msg = await self._client.send_message(entity, text, reply_to=reply_to)
            logger.info("Отправлено сообщение chat=%s, msg_id=%s", entity, getattr(msg, "id", None))
            return msg
        except Exception as e:
            logger.exception("send_message(%s) упал: %s", entity, e)
            return None
