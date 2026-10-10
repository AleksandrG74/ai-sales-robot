"""
db_init.py — инициализация БД dialogues-miner.

Создаёт SQLite-БД со структурой, совместимой с search_engine.py:
  - dialogs — сообщения из Telegram
  - leads — минимальная (для совместимости)
  - channels — какие каналы уже скачаны
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "data" / "dialogues.db"


SCHEMA = """
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
    author_username VARCHAR(100),
    author_first_name VARCHAR(100),
    author_last_name VARCHAR(100),
    reply_to_msg_id INTEGER,
    source_channel_id VARCHAR(50),
    source_channel_username VARCHAR(100),
    source_message_id INTEGER,
    source_message_url TEXT,
    fwd_from_channel_id VARCHAR(50),
    fwd_from_channel_username VARCHAR(100),
    fwd_from_message_id INTEGER,
    fwd_from_message_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_dialogs_sender ON dialogs(sender);
CREATE INDEX IF NOT EXISTS idx_dialogs_author ON dialogs(author_id);
CREATE INDEX IF NOT EXISTS idx_dialogs_reply ON dialogs(reply_to_msg_id);
CREATE INDEX IF NOT EXISTS idx_dialogs_message ON dialogs(message);
CREATE INDEX IF NOT EXISTS idx_dialogs_timestamp ON dialogs(timestamp);
CREATE INDEX IF NOT EXISTS idx_dialogs_author_username ON dialogs(author_username);
CREATE INDEX IF NOT EXISTS idx_dialogs_source_url ON dialogs(source_message_url);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id VARCHAR(100),
    username VARCHAR(100),
    message TEXT,
    source VARCHAR(50),
    region VARCHAR(50),
    intent VARCHAR(50),
    intent_score INTEGER,
    product VARCHAR(100),
    status VARCHAR(20),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    contacted_at DATETIME,
    converted_at DATETIME,
    profile_json TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    name VARCHAR(100) PRIMARY KEY,
    last_fetched_at DATETIME,
    last_message_id INTEGER,
    total_messages INTEGER DEFAULT 0
);
"""


def init_db(db_path: Path) -> None:
    """Создаёт БД и таблицы, если их нет."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(SCHEMA)
        con.commit()
        print(f"✅ БД инициализирована: {db_path}")

        # Показать, что создано
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall()]
        print(f"   Таблицы: {', '.join(tables)}")

        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
        )
        indexes = [row[0] for row in cur.fetchall()]
        print(f"   Индексы: {len(indexes)} шт.")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Инициализация БД dialogues-miner")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Путь к SQLite БД")
    args = parser.parse_args()

    init_db(Path(args.db))


if __name__ == "__main__":
    main()