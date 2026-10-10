"""
search_engine.py — ядро поиска тредов-согласий.

Находит связные треды в Telegram-каналах, где:
  - ровно один автор дал точное слово-согласие из target_words.json
  - в треде >= 2 разных авторов (author_id)
  - в треде <= 200 слов
  - сообщения связаны через reply_to_msg_id внутри одного sender
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
TARGET_WORDS_PATH = PROJECT_ROOT / "target_words.json"
CONFIG_PATH = PROJECT_ROOT / "configs" / "mining_config.json"

DEFAULT_DB = PROJECT_ROOT / "data" / "dialogues.db"


def load_target_words(path: Path = TARGET_WORDS_PATH) -> set:
    """Читает target_words.json и возвращает множество слов в нижнем регистре."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    words = set()
    for group in data.get("groups", {}).values():
        for w in group:
            words.add(w.strip().lower())
    return words


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Читает configs/mining_config.json."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Message:
    id: int
    sender: str
    author_id: str
    author_username: Optional[str]
    author_first_name: Optional[str]
    author_last_name: Optional[str]
    reply_to_msg_id: Optional[int]
    timestamp: datetime
    message: str
    source_channel_id: Optional[str]
    source_channel_username: Optional[str]
    source_message_id: Optional[int]
    source_message_url: Optional[str]
    fwd_from_channel_id: Optional[str]
    fwd_from_channel_username: Optional[str]
    fwd_from_message_id: Optional[int]
    fwd_from_message_url: Optional[str]


@dataclass
class Dialogue:
    seed_id: int
    sender: str
    target_author: str
    target_author_username: Optional[str]
    source_message_url: Optional[str]
    authors: set
    messages: list
    words_count: int
    target_count: int


class SearchEngine:
    def __init__(self, db_path, config, target_words):
        self.db_path = Path(db_path)
        self.config = config
        self.target_words = target_words
        self.max_words = config.get("max_words", 200)
        self.min_authors = config.get("min_authors", 2)
        self.target_authors = config.get("target_authors", 1)

    def _connect(self):
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _load_seeds(self, con):
        if not self.target_words:
            return []
        placeholders = ",".join("?" * len(self.target_words))
        sql = f"""
            SELECT id, sender, author_id, author_username, author_first_name, author_last_name,
                   reply_to_msg_id, timestamp, message,
                   source_channel_id, source_channel_username, source_message_id, source_message_url,
                   fwd_from_channel_id, fwd_from_channel_username, fwd_from_message_id, fwd_from_message_url
            FROM dialogs
            WHERE LOWER(TRIM(message)) IN ({placeholders})
              AND author_id IS NOT NULL
              AND TRIM(author_id) != ''
        """
        rows = con.execute(sql, list(self.target_words)).fetchall()
        return [Message(*row) for row in rows]

    def _load_message(self, con, msg_id):
        row = con.execute(
            "SELECT id, sender, author_id, author_username, author_first_name, author_last_name, "
            "reply_to_msg_id, timestamp, message, "
            "source_channel_id, source_channel_username, source_message_id, source_message_url, "
            "fwd_from_channel_id, fwd_from_channel_username, fwd_from_message_id, fwd_from_message_url "
            "FROM dialogs WHERE id = ?",
            (msg_id,),
        ).fetchone()
        return Message(*row) if row else None

    def _load_replies(self, con, msg_id, sender):
        rows = con.execute(
            "SELECT id, sender, author_id, author_username, author_first_name, author_last_name, "
            "reply_to_msg_id, timestamp, message, "
            "source_channel_id, source_channel_username, source_message_id, source_message_url, "
            "fwd_from_channel_id, fwd_from_channel_username, fwd_from_message_id, fwd_from_message_url "
            "FROM dialogs WHERE reply_to_msg_id = ? AND sender = ?",
            (msg_id, sender),
        ).fetchall()
        return [Message(*row) for row in rows]

    def _build_thread(self, con, seed):
        parents = []
        seen = {seed.id}
        cur = seed
        while cur.reply_to_msg_id is not None:
            parent = self._load_message(con, cur.reply_to_msg_id)
            if parent is None or parent.id in seen or parent.sender != seed.sender:
                break
            parents.append(parent)
            seen.add(parent.id)
            cur = parent

        thread = list(reversed(parents)) + [seed]
        queue = [seed.id]
        while queue:
            mid = queue.pop(0)
            for reply in self._load_replies(con, mid, seed.sender):
                if reply.id in seen:
                    continue
                seen.add(reply.id)
                thread.append(reply)
                queue.append(reply.id)

        thread.sort(key=lambda m: m.timestamp)
        return thread

    @staticmethod
    def _count_words(messages):
        return sum(len((m.message or "").split()) for m in messages)

    def _check_dialogue(self, seed, thread):
        target_authors = {
            m.author_id
            for m in thread
            if m.author_id
            and (m.message or "").strip().lower() in self.target_words
        }
        if len(target_authors) != self.target_authors:
            return None

        authors = {m.author_id for m in thread if m.author_id and m.author_id.strip()}
        if len(authors) < self.min_authors:
            return None

        wc = self._count_words(thread)
        if wc > self.max_words:
            return None

        if seed.id not in {m.id for m in thread}:
            return None

        target_author = next(iter(target_authors))
        target_author_username = next(
            (m.author_username for m in thread if m.author_id == target_author),
            None,
        )
        source_message_url = seed.source_message_url or seed.fwd_from_message_url

        return Dialogue(
            seed_id=seed.id,
            sender=seed.sender,
            target_author=target_author,
            target_author_username=target_author_username,
            source_message_url=source_message_url,
            authors=authors,
            messages=thread,
            words_count=wc,
            target_count=sum(
                1
                for m in thread
                if m.author_id and (m.message or "").strip().lower() in self.target_words
            ),
        )

    def find_agreement_dialogues(self):
        con = self._connect()
        try:
            seeds = self._load_seeds(con)
            print(f"Найдено семян: {len(seeds)}")
            results = []
            for i, seed in enumerate(seeds, 1):
                thread = self._build_thread(con, seed)
                dialogue = self._check_dialogue(seed, thread)
                if dialogue is not None:
                    results.append(dialogue)
                if i % 200 == 0:
                    print(f"  обработано {i}/{len(seeds)}, найдено: {len(results)}")
            return results
        finally:
            con.close()


def main():
    parser = argparse.ArgumentParser(description="dialogues-miner: поиск тредов-согласий")
    parser.add_argument("--db", required=True, help="Путь к SQLite БД")
    parser.add_argument("--limit", type=int, default=0, help="Максимум семян (0 = все)")
    args = parser.parse_args()

    config = load_config()
    target_words = load_target_words()

    print(f"target_words: {len(target_words)}")
    print(f"max_words: {config.get('max_words')}")
    print(f"min_authors: {config.get('min_authors')}")
    print(f"target_authors: {config.get('target_authors')}")
    print()

    engine = SearchEngine(
        db_path=Path(args.db),
        config=config,
        target_words=target_words,
    )
    dialogues = engine.find_agreement_dialogues()

    print()
    print(f"=== ИТОГО диалогов: {len(dialogues)} ===")
    for d in dialogues[:10]:
        print(f"--- seed={d.seed_id} sender={d.sender} ---")
        print(f"    авторов: {len(d.authors)}, слов: {d.words_count}, "
              f"целевых: {d.target_count}, автор согласия: {d.target_author}")
        for m in d.messages[:5]:
            tag = "★" if (m.message or "").strip().lower() in target_words else " "
            print(f"    {tag} [{m.timestamp}] {m.author_id}: {(m.message or '')[:70]}")


if __name__ == "__main__":
    main()