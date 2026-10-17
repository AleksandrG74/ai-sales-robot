# workers/telegram/main.py
"""
Точка входа бота оператора (aiogram 3.x).

ВАЖНО: Telegram Bot API доступен только с сервера (Германия).
Локальный запуск на ПК не работает (Telegram в РФ недоступен без прокси/VPN).
На ПК допустимы только статические проверки:
    python -c "import ast; ast.parse(open('workers/telegram/main.py', encoding='utf-8').read())"
    python -c "import workers.telegram.main"   # не создаёт Bot, не делает сетевых вызовов

Bot инициализируется лениво — внутри async def _run(), чтобы импорт модуля
на ПК не триггерил сетевые вызовы и не падал.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# Импортируем оба роутера (основные команды + управление live-мониторингом)
from workers.telegram.handlers import router as operator_router, router_live
from workers.telegram.notifications import NotificationPoller

logger = logging.getLogger("telegram-worker")


# === ЛОГИРОВАНИЕ ===

def _setup_logging() -> None:
    """
    Логирование в /app/logs/telegram-worker.log + stdout.
    Если /app/logs недоступен (локальный запуск) — пишем в ./logs/.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    # Чистим хендлеры, чтобы повторный вызов не дублировал строки
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    root.addHandler(sh)

    # файл
    log_dir = Path("/app/logs")
    if not log_dir.is_dir():
        log_dir = Path("./logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "telegram-worker.log", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except Exception as e:
        logger.warning("Не удалось открыть файл лога в %s: %s", log_dir, e)


# === ПОЛУЧЕНИЕ ТОКЕНА ===

def _get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан. "
            "Проверь .env (на сервере /opt/ai-sales-robot/.env) "
            "или переменные окружения контейнера."
        )
    return token


# === ОСНОВНОЙ ЦИКЛ ===

async def _run() -> None:
    """
    Ленивая инициализация Bot + Dispatcher, polling, корректное завершение.
    """
    token = _get_token()

    # Bot создаётся только здесь — импорт main.py на ПК не делает сетевых вызовов
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    
    dp = Dispatcher()
    # Регистрируем оба роутера: базовый (commands) и live-management (FSM)
    dp.include_router(operator_router)
    dp.include_router(router_live)

    stop_event = asyncio.Event()

    def _signal_handler(signum: int) -> None:
        logger.info("Получен сигнал %s — останавливаюсь…", signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except (NotImplementedError, RuntimeError):
            # Windows — add_signal_handler не поддерживается, используем signal.signal
            try:
                signal.signal(sig, lambda s, _f: _signal_handler(s))
            except Exception as e:
                logger.debug("Не удалось повесить обработчик %s: %s", sig_name, e)

    try:
        # Warmup: получаем @username, логируем
        me = await bot.get_me()
        logger.info(
            "Бот авторизован: @%s (id=%s, name=%s)",
            me.username, me.id, me.full_name,
        )

        # Сбрасываем вебхук и pending updates перед polling
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook сброшен, pending updates очищены.")
        except Exception as e:
            logger.warning("delete_webhook не удался (не критично): %s", e)

        # --- Запуск NotificationPoller ---
        operator_chat_id_str = os.getenv("TELEGRAM_OPERATOR_CHAT_ID", "").strip()
        notifier = None
        if operator_chat_id_str:
            try:
                op_id = int(operator_chat_id_str.split(",")[0].strip())
                notifier = NotificationPoller(bot=bot, operator_chat_id=op_id)
                notifier.start()
                logger.info("NotificationPoller запущен (operator_id=%s).", op_id)
            except Exception as e:
                logger.error("Не удалось запустить NotificationPoller: %s", e)
        else:
            logger.warning("TELEGRAM_OPERATOR_CHAT_ID не задан — уведомления отключены.")

        logger.info("Запускаю long polling…")

        # Параллельно: polling + ожидание stop_event
        polling_task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False),
            name="tg-polling",
        )
        stop_task = asyncio.create_task(stop_event.wait(), name="tg-stop")

        done, pending = await asyncio.wait(
            {polling_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Если polling упал сам — покажем ошибку
        if polling_task in done and not stop_event.is_set():
            exc = polling_task.exception()
            if exc is not None:
                logger.exception("Polling завершился с ошибкой: %s", exc)
            else:
                logger.info("Polling завершился штатно.")

        # Останавливаем NotificationPoller
        if notifier is not None:
            try:
                await notifier.stop()
                logger.info("NotificationPoller остановлен.")
            except Exception as e:
                logger.warning("Остановка NotificationPoller упала: %s", e)

        # Останавливаем polling
        try:
            await dp.stop_polling()
        except Exception as e:
            logger.warning("stop_polling упал: %s", e)

        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    finally:
        try:
            await bot.session.close()
            logger.info("Сессия бота закрыта.")
        except Exception as e:
            logger.warning("bot.session.close() упал: %s", e)
        logger.info("Telegram-worker остановлен.")


def main() -> None:
    _setup_logging()
    logger.info("Старт telegram-worker…")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Прервано с клавиатуры.")
    except Exception as e:
        logger.exception("Фатальная ошибка telegram-worker: %s", e)
        sys.exit(1)


# === ENTRY POINT ===

if __name__ == "__main__":
    main()
