"""
main.py — CLI для dialogues-miner.

Режимы:
  --mode init    — создать БД
  --mode fetch   — скачать Telegram
  --mode mine    — найти треды (search_engine)
  --mode export  — сохранить JSON (dialogue_exporter)
  --mode all     — fetch + mine + export
  --mode web     — запустить веб-интерфейс
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "dialogues.db"


def cmd_init(args):
    from db_init import init_db
    init_db(DB_PATH)


def cmd_fetch(args):
    from fetcher import run as fetch_run
    # Просто вызываем main() fetcher с аргументами
    import fetcher
    sys.argv = ["fetcher.py"]
    if args.channel:
        sys.argv += ["--channel", args.channel]
    if args.days:
        sys.argv += ["--days", str(args.days)]
    if args.limit:
        sys.argv += ["--limit", str(args.limit)]
    if args.dry_run:
        sys.argv += ["--dry-run"]
    fetcher.main()


def cmd_mine(args):
    from dialogue_exporter import export_dialogues
    from search_engine import SearchEngine, load_config, load_target_words

    config = load_config()
    target_words = load_target_words()

    engine = SearchEngine(
        db_path=Path(args.db or DB_PATH),
        config=config,
        target_words=target_words,
    )
    dialogues = engine.find_agreement_dialogues()
    print()
    print(f"=== Найдено диалогов: {len(dialogues)} ===")

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = PROJECT_ROOT / "data" / "raw_threads"

    saved = export_dialogues(dialogues, out_dir, target_words)
    print(f"✅ Сохранено: {saved} файлов в {out_dir}")


def cmd_web(args):
    import uvicorn
    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=args.port or 8001,
        reload=False,
    )


def cmd_all(args):
    cmd_init(args)
    cmd_fetch(args)
    cmd_mine(args)


def main():
    parser = argparse.ArgumentParser(description="dialogues-miner CLI")
    parser.add_argument(
        "--mode",
        choices=["init", "fetch", "mine", "export", "all", "web"],
        required=True,
        help="Режим работы",
    )
    parser.add_argument("--db", help="Путь к БД (по умолчанию data/dialogues.db)")
    parser.add_argument("--out", help="Куда сохранять JSON")
    parser.add_argument("--channel", help="Качать только один канал")
    parser.add_argument("--days", type=int, help="Сколько дней назад")
    parser.add_argument("--limit", type=int, help="Лимит сообщений на канал")
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД")
    parser.add_argument("--port", type=int, default=8001, help="Порт для web")
    args = parser.parse_args()

    if args.mode == "init":
        cmd_init(args)
    elif args.mode == "fetch":
        cmd_fetch(args)
    elif args.mode == "mine":
        cmd_mine(args)
    elif args.mode == "export":
        # export = mine + save JSON
        args.out = args.out or str(PROJECT_ROOT / "data" / "raw_threads")
        cmd_mine(args)
    elif args.mode == "web":
        cmd_web(args)
    elif args.mode == "all":
        cmd_all(args)


if __name__ == "__main__":
    main()