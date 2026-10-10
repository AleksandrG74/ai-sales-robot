"""
fetcher.py — качалка сообщений из Telegram-каналов.

Читает sources.json (122 канала), подключается через Telethon,
скачивает последние N дней, фильтрует короткие и ботов,
пишет в dialogs (SQLite).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import Channel, Chat, User

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

SOURCES_PATH = PROJECT_ROOT / "sources.json"
CONFIG_PATH = PROJECT_ROOT / "configs" / "mining_config.json"
DB_PATH = PROJECT_ROOT / "data" / "dialogues.db"
SESSION_DIR = PROJECT_ROOT / "sessions"
LOG_DIR = PROJECT_ROOT / "logs"

SESSION_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "fetcher.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("fetcher")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sources() -> list[dict]:
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [s for s in data["sources"] if s.get("enabled", True)]


def _extract_author(msg):
    """Возвращает (author_id, username, first_name, last_name)."""
    author_id = None
    username = None
    first_name = None
    last_name = None
    try:
        if msg.from_id and hasattr(msg.from_id, "user_id"):
            author_id = str(msg.from_id.user_id)
        sender = getattr(msg, "sender", None)
        if sender is not None:
            if getattr(sender, "username", None):
                username = sender.username
            if getattr(sender, "first_name", None):
                first_name = sender.first_name
            if getattr(sender, "last_name", None):
                last_name = sender.last_name
    except Exception:
        pass
    return author_id, username, first_name, last_name


def _extract_fwd_from(msg):
    """Возвращает (fwd_channel_id, fwd_channel_username, fwd_message_id, fwd_message_url)."""
    fwd_channel_id = None
    fwd_channel_username = None
    fwd_message_id = None
    fwd_message_url = None
    try:
        fwd = getattr(msg, "fwd_from", None)
        if fwd and getattr(fwd, "from_id", None):
            from_id = fwd.from_id
            if hasattr(from_id, "channel_id"):
                fwd_channel_id = str(from_id.channel_id)
                fwd_message_id = getattr(fwd, "channel_post", None)
    except Exception:
        pass
    return fwd_channel_id, fwd_channel_username, fwd_message_id, fwd_message_url


def _extract_source(msg, sender_name):
    """Возвращает (source_channel_id, source_channel_username, source_message_id, source_message_url)."""
    try:
        source_channel_username = sender_name.lstrip("@") if sender_name else None
        source_message_id = msg.id
        source_message_url = (
            f"https://t.me/{source_channel_username}/{source_message_id}"
            if source_channel_username and source_message_id
            else None
        )
        return None, source_channel_username, source_message_id, source_message_url
    except Exception:
        return None, None, None, None


def _extract_reply_to(msg) -> Optional[int]:
    """Возвращает ID сообщения, на которое отвечают, или None."""
    try:
        if msg.reply_to and hasattr(msg.reply_to, "reply_to_msg_id"):
            return msg.reply_to.reply_to_msg_id
    except Exception:
        pass
    return None


def _is_bot_message(msg) -> bool:
    """True, если сообщение отправлено ботом."""
    if getattr(msg, "via_bot_id", None):
        return True
    sender = getattr(msg, "sender", None)
    if sender and getattr(sender, "bot", False):
        return True
    return False


def _word_count(text: str) -> int:
    return len((text or "").split())


def init_db() -> sqlite3.Connection:
    """Открывает БД, создаёт схему, если её нет."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER,
            sender VARCHAR(100),
            message TEXT,
            timestamp DATETIME,
            strategy_used VARCHAR(50),
            tokens_used INTEGER,
            response_time FLOAT,
            author_id VARCHAR(50),
            reply_to_msg_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_dialogs_sender ON dialogs(sender);
        CREATE INDEX IF NOT EXISTS idx_dialogs_author ON dialogs(author_id);
        CREATE INDEX IF NOT EXISTS idx_dialogs_reply ON dialogs(reply_to_msg_id);
        CREATE INDEX IF NOT EXISTS idx_dialogs_message ON dialogs(message);
        CREATE INDEX IF NOT EXISTS idx_dialogs_timestamp ON dialogs(timestamp);

        CREATE TABLE IF NOT EXISTS channels (
            name VARCHAR(100) PRIMARY KEY,
            last_fetched_at DATETIME,
            last_message_id INTEGER,
            total_messages INTEGER DEFAULT 0
        );
    """)
    con.commit()
    return con


def save_batch(con: sqlite3.Connection, batch: list[tuple]) -> int:
    """Сохраняет батч сообщений в dialogs. Возвращает количество вставленных."""
    if not batch:
        return 0
    con.executemany(
        """
        INSERT OR IGNORE INTO dialogs
        (id, sender, message, timestamp,
         author_id, author_username, author_first_name, author_last_name,
         reply_to_msg_id,
         source_channel_id, source_channel_username, source_message_id, source_message_url,
         fwd_from_channel_id, fwd_from_channel_username, fwd_from_message_id, fwd_from_message_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )
    con.commit()
    return len(batch)


def update_channel_stats(con: sqlite3.Connection, name: str, last_id: Optional[int], total: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO channels (name, last_fetched_at, last_message_id, total_messages)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_fetched_at = excluded.last_fetched_at,
            last_message_id = COALESCE(excluded.last_message_id, channels.last_message_id),
            total_messages = channels.total_messages + excluded.total_messages
        """,
        (name, now, last_id, total),
    )
    con.commit()


async def fetch_channel(
    client: TelegramClient,
    con: sqlite3.Connection,
    source: dict,
    cutoff: datetime,
    limit: int,
    min_words: int,
    skip_bots: bool,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Качает один канал. Возвращает (сохранено, пропущено).
    """
    name = source["name"]
    logger.info(f"📡 {name}")

    try:
        entity = await client.get_entity(name)
    except (UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError, ValueError) as e:
        logger.warning(f"  ⚠️  Канал недоступен: {e}")
        return 0, 0
    except Exception as e:
        logger.warning(f"  ⚠️  Ошибка получения entity: {e}")
        return 0, 0

    saved = 0
    skipped = 0
    batch: list[tuple] = []
    last_id: Optional[int] = None

    try:
        async for msg in client.iter_messages(entity, limit=limit):
            if msg.date and msg.date < cutoff:
                break
            if not msg.message or not msg.message.strip():
                skipped += 1
                continue
            if _word_count(msg.message) < min_words:
                skipped += 1
                continue
            if skip_bots and _is_bot_message(msg):
                skipped += 1
                continue

            author_id, author_username, author_first_name, author_last_name = _extract_author(msg)
            reply_to_msg_id = _extract_reply_to(msg)
            source_channel_id, source_channel_username, source_message_id, source_message_url = _extract_source(msg, name)
            fwd_channel_id, fwd_channel_username, fwd_message_id, fwd_message_url = _extract_fwd_from(msg)

            row = (
                msg.id, name, msg.message[:2000],
                msg.date.isoformat() if msg.date else None,
                author_id, author_username, author_first_name, author_last_name,
                reply_to_msg_id,
                source_channel_id, source_channel_username, source_message_id, source_message_url,
                fwd_channel_id, fwd_channel_username, fwd_message_id, fwd_message_url,
            )
            batch.append(row)
            last_id = msg.id

            if len(batch) >= 50:
                if not dry_run:
                    save_batch(con, batch)
                saved += len(batch)
                batch.clear()

        if batch:
            if not dry_run:
                save_batch(con, batch)
            saved += len(batch)

        if not dry_run:
            update_channel_stats(con, name, last_id, saved)

        logger.info(f"  ✅ сохранено: {saved}, пропущено: {skipped}")

    except FloodWaitError as e:
        logger.warning(f"  ⏳ Flood wait: {e.seconds} сек")
        await asyncio.sleep(e.seconds + 5)
    except Exception as e:
        logger.error(f"  ❌ Ошибка: {e}")

    return saved, skipped


async def run(args) -> None:
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")

    if not all([api_id, api_hash, phone]):
        logger.error("❌ Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE в .env")
        sys.exit(1)

    config = load_config()
    sources = load_sources()

    if args.channel:
        sources = [s for s in sources if s["name"] == args.channel]
        if not sources:
            logger.error(f"❌ Канал '{args.channel}' не найден в sources.json")
            sys.exit(1)

    min_words = config.get("min_message_words", 5)
    skip_bots = config.get("skip_bots", True)
    days = args.days or config["fetch"]["days_back"]
    limit = args.limit or config["fetch"]["limit_per_channel"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    logger.info(f"Каналов: {len(sources)}")
    logger.info(f"Дней: {days}, лимит на канал: {limit}")
    logger.info(f"Фильтры: min_words={min_words}, skip_bots={skip_bots}")
    if args.dry_run:
        logger.info("🧪 DRY-RUN: запись в БД отключена")
    logger.info("")

    session_path = str(SESSION_DIR / "telegram_session")
    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start(phone=phone)

    me = await client.get_me()
    logger.info(f"✅ Подключен: {me.first_name} (@{me.username})")
    logger.info("")

    con = init_db()
    total_saved = 0
    total_skipped = 0

    try:
        for i, source in enumerate(sources, 1):
            logger.info(f"[{i}/{len(sources)}]")
            saved, skipped = await fetch_channel(
                client, con, source, cutoff, limit,
                min_words, skip_bots, dry_run=args.dry_run,
            )
            total_saved += saved
            total_skipped += skipped
    finally:
        con.close()
        await client.disconnect()

    logger.info("")
    logger.info(f"=== ИТОГО ===")
    logger.info(f"Сохранено: {total_saved}")
    logger.info(f"Пропущено: {total_skipped}")
    logger.info("🔌 Отключено")


def main():
    parser = argparse.ArgumentParser(description="dialogues-miner: качалка Telegram")
    parser.add_argument("--channel", help="Качать только один канал (для теста)")
    parser.add_argument("--days", type=int, help="Сколько дней назад (по умолчанию 30)")
    parser.add_argument("--limit", type=int, help="Лимит сообщений на канал (по умолчанию 5000)")
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
