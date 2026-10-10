"""
dialogue_exporter.py — сохранение найденных тредов в JSON.

Берёт Dialogue из search_engine и сохраняет:
  - по одному JSON на тред в data/raw_threads/
  - сводный индекс data/raw_threads/_index.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from search_engine import (
    Dialogue,
    SearchEngine,
    load_config,
    load_target_words,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = PROJECT_ROOT / "data" / "raw_threads"


def dialogue_to_dict(d: Dialogue, target_words: set) -> dict:
    """Преобразует Dialogue в JSON-совместимый словарь."""
    return {
        "seed_id": d.seed_id,
        "sender": d.sender,
        "target_author": d.target_author,
        "target_author_username": d.target_author_username,
        "source_message_url": d.source_message_url,
        "authors": sorted(d.authors),
        "authors_count": len(d.authors),
        "words_count": d.words_count,
        "target_count": d.target_count,
        "messages": [
            {
                "id": m.id,
                "timestamp": m.timestamp if m.timestamp else None,
                "author_id": m.author_id,
                "author_username": m.author_username,
                "author_first_name": m.author_first_name,
                "author_last_name": m.author_last_name,
                "message": m.message,
                "is_target": (m.message or "").strip().lower() in target_words,
                "reply_to_msg_id": m.reply_to_msg_id,
                "source_message_url": m.source_message_url,
                "fwd_from_channel_username": m.fwd_from_channel_username,
                "fwd_from_message_url": m.fwd_from_message_url,
            }
            for m in d.messages
        ],
    }


def safe_filename(sender: str, seed_id: int) -> str:
    """Готовит безопасное имя файла."""
    safe_sender = "".join(c if c.isalnum() or c in "-_" else "_" for c in sender)
    return f"{safe_sender}_{seed_id}.json"


def export_dialogues(
    dialogues: list[Dialogue],
    out_dir: Path,
    target_words: set,
) -> int:
    """Сохраняет все диалоги + индекс. Возвращает количество сохранённых."""
    out_dir.mkdir(parents=True, exist_ok=True)

    index = []
    saved = 0

    for d in dialogues:
        fname = safe_filename(d.sender, d.seed_id)
        fpath = out_dir / fname

        payload = dialogue_to_dict(d, target_words)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        index.append({
            "file": fname,
            "seed_id": d.seed_id,
            "sender": d.sender,
            "authors_count": len(d.authors),
            "words_count": d.words_count,
            "target_count": d.target_count,
            "target_author": d.target_author,
            "target_author_username": d.target_author_username,
            "source_message_url": d.source_message_url,
        })
        saved += 1

    # Сводный индекс
    with open(out_dir / "_index.json", "w", encoding="utf-8") as f:
        json.dump({
            "total": saved,
            "dialogues": sorted(index, key=lambda x: (x["sender"], x["seed_id"])),
        }, f, ensure_ascii=False, indent=2)

    return saved


def main():
    parser = argparse.ArgumentParser(description="dialogues-miner: экспорт тредов в JSON")
    parser.add_argument("--db", required=True, help="Путь к SQLite БД")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Куда сохранять JSON")
    args = parser.parse_args()

    config = load_config()
    target_words = load_target_words()

    engine = SearchEngine(
        db_path=Path(args.db),
        config=config,
        target_words=target_words,
    )
    dialogues = engine.find_agreement_dialogues()

    print()
    print(f"Найдено диалогов: {len(dialogues)}")

    out_dir = Path(args.out)
    saved = export_dialogues(dialogues, out_dir, target_words)

    print(f"✅ Сохранено в {out_dir}: {saved} файлов")
    print(f"   Индекс: {out_dir / '_index.json'}")


if __name__ == "__main__":
    main()
