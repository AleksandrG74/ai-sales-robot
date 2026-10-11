# workers/live/main.py
"""
Точка входа live-worker (Telethon user-account).

Задачи:
- Подключиться к Telegram от имени user-account (тот же .session, что у miner).
- Слушать новые сообщения в monitored_chats.
- Писать их в live_messages.
- Отправлять исходящие (когда web-CRM положил задачу).
- Периодически проверять файл-флаг /app/data/sessions/.miner_running:
    * если флаг есть — pause_polling (события не обрабатываем),
    * если флага нет — resume_polling.

ВАЖНО: Telegram Bot API/Telethon доступен с сервера (Германия).
Локальный запуск на ПК не работает — здесь только синтаксические проверки.
Telethon-клиент создаётся ЛЕНИВО внутри async def _run().
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("live-worker")


# === ЛОГИРОВАНИЕ ===

def _setup_logging() -> None:
    """Логирование в /app/logs/live-worker.log + stdout."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    root.addHandler(sh)

    log_dir = Path("/app/logs")
    if not log_dir.is_dir():
        log_dir = Path("./logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "live-worker.log", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except Exception as e:
        logger.warning("Не удалось открыть файл лога в %s: %s", log_dir, e)


# === ПРОВЕРКА КОНФИГА ===

def _check_env() -> tuple[str, str, str, str]:
    """
    Возвращает (api_id, api_hash, phone, session_path).
    Падает с понятной ошибкой, если чего-то не хватает.
    """
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    phone = (os.getenv("TELEGRAM_PHONE") or "").strip()
    session_path = (os.getenv("LIVE_SESSION_PATH") or "").strip()

    missing = [k for k, v in {
        "TELEGRAM_API_ID": api_id,
        "TELEGRAM_API_HASH": api_hash,
        "TELEGRAM_PHONE": phone,
        "LIVE_SESSION_PATH": session_path,
    }.items() if not v]

    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
        )
    return api_id, api_hash, phone, session_path


# === ФЛАГ MINER'А ===

def _miner_flag_path() -> Path:
    return Path(os.getenv("MINER_FLAG_PATH", "/app/data/sessions/.miner_running"))


def _is_miner_running() -> bool:
    try:
        return _miner_flag_path().exists()
    except Exception:
        return False


async def _watch_miner_flag(client, stop_event: asyncio.Event, poll_sec: float = 5.0) -> None:
    """
    Раз в poll_sec проверяет флаг miner'а.
    Ставит/снимает паузу polling у live-клиента.
    Завершается по stop_event.
    """
    paused = False
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_sec)
            return  # stop_event взведён
        except asyncio.TimeoutError:
            pass

        try:
            running = _is_miner_running()
        except Exception as e:
            logger.warning("Ошибка проверки флага miner'а: %s", e)
            continue

        if running and not paused:
            logger.info("Флаг miner'а обнаружен — pause_polling у live-клиента.")
            try:
                await client.pause_polling()
                paused = True
            except Exception as e:
                logger.exception("pause_polling упал: %s", e)
        elif not running and paused:
            logger.info("Флаг miner'а снят — resume_polling у live-клиента.")
            try:
                await client.resume_polling()
                paused = False
            except Exception as e:
                logger.exception("resume_polling упал: %s", e)


# === СЕССИЯ В БД (live_sessions) ===

def _session_start() -> Optional[int]:
    """Создаёт запись в live_sessions со status='running'. Возвращает id."""
    try:
        from core.database import get_session, LiveSession
        with get_session() as session:
            row = LiveSession(
                started_at=datetime.now(timezone.utc),
                status="running",
                messages_received=0,
                messages_sent=0,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            sid = row.id
        logger.info("live_sessions: старт записи id=%s", sid)
        return sid
    except Exception as e:
        logger.exception("Не удалось создать live_sessions: %s", e)
        return None


def _session_finish(session_id: Optional[int], status: str = "stopped", error: Optional[str] = None) -> None:
    """Закрывает запись live_sessions."""
    if not session_id:
        return
    try:
        from core.database import get_session, LiveSession
        from sqlalchemy import select
        with get_session() as session:
            row = session.execute(
                select(LiveSession).where(LiveSession.id == session_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.stopped_at = datetime.now(timezone.utc)
            row.status = status
            if error:
                row.error_text = error[:1000]
            session.commit()
        logger.info("live_sessions: запись id=%s закрыта (status=%s)", session_id, status)
    except Exception as e:
        logger.exception("Не удалось закрыть live_sessions id=%s: %s", session_id, e)


# === ОСНОВНОЙ ЦИКЛ ===

async def _run() -> None:
    """
    Ленивая инициализация Telethon-клиента + event loop.
    """
    api_id, api_hash, phone, session_path = _check_env()

    # Ленивые импорты — модуль main.py должен импортироваться на ПК без сети
    from workers.live.telethon_client import LiveTelethonClient
    from workers.live.handler import register_handlers

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
            try:
                signal.signal(sig, lambda s, _f: _signal_handler(s))
            except Exception as e:
                logger.debug("Не удалось повесить обработчик %s: %s", sig_name, e)

    session_row_id = _session_start()
    client: Optional[LiveTelethonClient] = None
    watcher_task: Optional[asyncio.Task] = None

    try:
        client = LiveTelethonClient(
            api_id=int(api_id),
            api_hash=api_hash,
            phone=phone,
            session_path=session_path,
        )
        await client.connect()
        logger.info("Live-клиент подключён (session=%s)", session_path)

        # Регистрируем обработчики событий
        try:
            register_handlers(client)
            logger.info("Обработчики событий зарегистрированы.")
        except Exception as e:
            logger.exception("register_handlers упал: %s", e)

        # Запускаем watcher флага miner'а
        watcher_task = asyncio.create_task(
            _watch_miner_flag(client, stop_event),
            name="miner-flag-watcher",
        )

        logger.info("Live-worker в работе. Ожидаю сообщения…")
        await stop_event.wait()

    except Exception as e:
        logger.exception("Фатальная ошибка live-worker: %s", e)
        _session_finish(session_row_id, status="error", error=str(e))
        raise

    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except (asyncio.CancelledError, Exception):
                pass

        if client is not None:
            try:
                await client.disconnect()
                logger.info("Live-клиент отключён.")
            except Exception as e:
                logger.warning("disconnect упал: %s", e)

        _session_finish(session_row_id, status="stopped")
        logger.info("Live-worker остановлен.")


def main() -> None:
    _setup_logging()
    logger.info("Старт live-worker…")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Прервано с клавиатуры.")
    except Exception as e:
        logger.exception("Фатальная ошибка live-worker (верхний уровень): %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
