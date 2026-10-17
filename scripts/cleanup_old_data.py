# scripts/cleanup_old_data.py
"""
Очистка старых данных из robot.db.

Использование:
    python -m scripts.cleanup_old_data --days 90
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Добавляем корень проекта в sys.path для корректных импортов
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.database import get_session, PMIHistory, BridgeHistory, LiveMessage, Run
from sqlalchemy import delete

# === ЛОГИРОВАНИЕ ===
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("cleanup")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    log_dir = Path("/app/logs")
    if not log_dir.is_dir():
        log_dir = Path("./logs")
    
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "cleanup.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        print(f"Не удалось открыть logs/cleanup.log: {e}")

    logger.propagate = False
    return logger

log = setup_logging()


def cleanup(days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    log.info("Запуск очистки данных старше %d дней (до %s)", days, cutoff.isoformat())

    try:
        with get_session() as session:
            # 1. pmi_history
            res = session.execute(
                delete(PMIHistory).where(PMIHistory.created_at < cutoff)
            )
            pmi_count = res.rowcount or 0

            # 2. bridges_history
            res = session.execute(
                delete(BridgeHistory).where(BridgeHistory.created_at < cutoff)
            )
            bridge_count = res.rowcount or 0

            # 3. live_messages (ТОЛЬКО прочитанные!)
            res = session.execute(
                delete(LiveMessage)
                .where(LiveMessage.created_at < cutoff)
                .where(LiveMessage.read_by_operator == 1)
            )
            live_count = res.rowcount or 0

            # 4. runs
            res = session.execute(
                delete(Run).where(Run.started_at < cutoff)
            )
            run_count = res.rowcount or 0

            session.commit()

        log.info("Очистка завершена.")
        log.info("Удалено из pmi_history: %d", pmi_count)
        log.info("Удалено из bridges_history: %d", bridge_count)
        log.info("Удалено из live_messages (read): %d", live_count)
        log.info("Удалено из runs: %d", run_count)

        print(f"Удалено из pmi_history: {pmi_count}")
        print(f"Удалено из bridges_history: {bridge_count}")
        print(f"Удалено из live_messages (read): {live_count}")
        print(f"Удалено из runs: {run_count}")

    except Exception as e:
        log.exception("Ошибка при очистке данных: %s", e)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Очистка старых данных в robot.db")
    parser.add_argument("--days", type=int, default=90, help="Хранить данные за последние N дней (default: 90)")
    args = parser.parse_args()

    if args.days <= 0:
        print("Ошибка: --days должен быть > 0")
        sys.exit(1)

    cleanup(args.days)


if __name__ == "__main__":
    main()
