"""dialogue-worker: цикл обработки JSON-тредов.

Сканирует data/raw_threads/*.json, обрабатывает каждый тред через
agent.process_dialogue, пишет processed_dialogues и runs.

Обрабатывает SIGTERM/SIGINT: делает финальный PMI-снапшот, закрывает
ресурсы, ставит runs.finished_at.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from core.config import settings
from core.database import ProcessedDialogue, Run, get_session, init_db
from core.embeddings import get_embedder
from core.llm import get_llm_client
from workers.dialogue import statistics
from workers.dialogue.agent import process_file

# ============================================================
# Настройки
# ============================================================
LOG_FILE = settings.logs_dir / "dialogue-worker.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

SLEEP_BETWEEN_SCANS = int(os.getenv("DIALOGUE_WORKER_SLEEP", "30"))
WARMUP_LLM = os.getenv("DIALOGUE_WORKER_WARMUP_LLM", "1") == "1"


# ============================================================
# Логирование
# ============================================================
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("dialogue-worker")
    logger.setLevel(settings.log_level.upper())
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


log = setup_logging()


# ============================================================
# Глобальные флаги
# ============================================================
_shutdown = False
_current_run_id: Optional[int] = None


def _on_signal(signum, frame):  # type: ignore[no-untyped-def]
    global _shutdown
    log.info("Received signal %s, shutting down...", signum)
    _shutdown = True


# ============================================================
# runs
# ============================================================
def _start_run() -> Optional[int]:
    try:
        with get_session() as s:
            rec = Run(
                run_type="agent",
                started_at=datetime.now(timezone.utc),
                status="running",
                items_processed=0,
            )
            s.add(rec)
            s.flush()
            log.info("run started: id=%s", rec.id)
            return rec.id
    except Exception as exc:
        log.exception("Failed to start run: %s", exc)
        return None


def _finish_run(run_id: Optional[int], items: int, status: str = "ok",
                error_text: Optional[str] = None) -> None:
    if run_id is None:
        return
    try:
        with get_session() as s:
            rec = s.get(Run, run_id)
            if rec is None:
                return
            rec.finished_at = datetime.now(timezone.utc)
            rec.status = status
            rec.items_processed = items
            rec.error_text = error_text
            log.info("run finished: id=%s status=%s items=%d", run_id, status, items)
    except Exception as exc:
        log.exception("Failed to finish run %s: %s", run_id, exc)


def _update_run_progress(run_id: Optional[int], items: int) -> None:
    if run_id is None:
        return
    try:
        with get_session() as s:
            rec = s.get(Run, run_id)
            if rec is not None:
                rec.items_processed = items
    except Exception as exc:
        log.exception("Failed to update run %s: %s", run_id, exc)


# ============================================================
# Уже обработанные
# ============================================================
def _load_processed_ids() -> set:
    """Возвращает набор dialogue_id из processed_dialogues (status='done')."""
    try:
        with get_session() as s:
            rows = s.query(ProcessedDialogue.dialogue_id).filter(
                ProcessedDialogue.status == "done"
            ).all()
        return {r[0] for r in rows if r[0]}
    except Exception as exc:
        log.exception("_load_processed_ids failed: %s", exc)
        return set()


def _dialogue_id_from_path(path: Path) -> str:
    """dialogue_id = имя файла без .json."""
    return path.stem


# ============================================================
# Сканирование папки (обходит баг overlayfs с readdir)
# ============================================================
def _list_raw_files(raw_dir: Path) -> list:
    """Возвращает список JSON-файлов в raw_dir.

    Собирает имена из двух источников: os.scandir и subprocess ls.
    Даже если один syscall (readdir) возвращает неполный список,
    второй компенсирует. Это обходит известный баг overlayfs/bind-mount,
    при котором Python видит меньше файлов, чем есть на диске.
    """
    names: set = set()

    # 1) os.scandir
    try:
        with os.scandir(raw_dir) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".json"):
                    names.add(entry.name)
    except Exception as exc:
        log.warning("os.scandir failed: %s", exc)

    # 2) subprocess ls — fallback
    try:
        out = subprocess.run(
            ["ls", "-1", str(raw_dir)],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            for name in out.stdout.splitlines():
                name = name.strip()
                if name.endswith(".json"):
                    names.add(name)
    except Exception as exc:
        log.warning("subprocess ls failed: %s", exc)

    return sorted(raw_dir / n for n in names if not n.startswith("_"))


# ============================================================
# Warmup
# ============================================================
def _warmup_embedder() -> None:
    log.info("Warming up Embedder...")
    try:
        emb = get_embedder()
        t0 = time.perf_counter()
        _ = emb.encode("warmup")
        log.info("Embedder ready in %.2fs (dim=%d)", time.perf_counter() - t0, emb.dim)
    except Exception as exc:
        log.exception("Embedder warmup failed: %s", exc)


def _warmup_llm() -> None:
    if not WARMUP_LLM:
        return
    log.info("Warming up LLM...")
    try:
        llm = get_llm_client()
        t0 = time.perf_counter()
        resp = llm.chat_sync(
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0,
            max_tokens=3,
        )
        log.info("LLM ready in %.2fs (model=%s, reply=%r)",
                 time.perf_counter() - t0, resp.model, resp.text[:30])
    except Exception as exc:
        log.exception("LLM warmup failed (это не критично): %s", exc)


# ============================================================
# Обработка папки
# ============================================================
async def _scan_once(processed_ids: set, run_id: Optional[int],
                     items_counter: dict) -> int:
    """Один проход по raw_threads. Возвращает количество обработанных за проход."""
    raw_dir = settings.raw_threads_dir
    if not raw_dir.exists():
        log.debug("raw_threads dir не существует: %s", raw_dir)
        return 0

    files = _list_raw_files(raw_dir)
    log.info("scan: found %d json files in %s", len(files), raw_dir)
    if not files:
        return 0

    processed_this_run = 0
    for path in files:
        if _shutdown:
            break
        dialogue_id = _dialogue_id_from_path(path)
        if dialogue_id in processed_ids:
            continue

        log.info("processing file: %s (id=%s)", path.name, dialogue_id)
        try:
            result = await process_file(path)
        except Exception as exc:
            log.exception("process_file failed for %s: %s", path, exc)
            result = {"error": str(exc)}

        if "error" in result:
            log.error("dialogue %s FAILED: %s", dialogue_id, result.get("error"))
        else:
            processed_ids.add(dialogue_id)
            processed_this_run += 1
            items_counter["total"] += 1
            _update_run_progress(run_id, items_counter["total"])

    return processed_this_run


# ============================================================
# Главный цикл
# ============================================================
async def run_loop() -> None:
    global _current_run_id

    log.info("dialogue-worker starting (pid=%s)", os.getpid())
    log.info("raw_threads_dir=%s", settings.raw_threads_dir)
    log.info("robot_db_path=%s", settings.robot_db_path)
    log.info("sleep_between_scans=%ds", SLEEP_BETWEEN_SCANS)

    try:
        init_db()
    except Exception as exc:
        log.exception("init_db failed: %s", exc)

    _warmup_embedder()
    _warmup_llm()

    _current_run_id = _start_run()
    items_counter = {"total": 0}

    processed_ids = _load_processed_ids()
    log.info("already processed: %d dialogues", len(processed_ids))

    while not _shutdown:
        try:
            n = await _scan_once(processed_ids, _current_run_id, items_counter)
            if n > 0:
                log.info("scan complete: +%d new dialogues (total=%d)",
                         n, items_counter["total"])
            else:
                log.info("scan complete: no new dialogues (total in db=%d)",
                         len(processed_ids))
        except Exception as exc:
            log.exception("scan iteration failed: %s", exc)

        for _ in range(SLEEP_BETWEEN_SCANS):
            if _shutdown:
                break
            await asyncio.sleep(1)

    log.info("shutdown: making final PMI snapshot...")
    try:
        statistics.maybe_snapshot(force=True)
    except Exception as exc:
        log.exception("final snapshot failed: %s", exc)

    try:
        llm = get_llm_client()
        await llm.aclose()
    except Exception as exc:
        log.warning("llm.aclose failed: %s", exc)

    _finish_run(_current_run_id, items_counter["total"], status="ok")
    log.info("dialogue-worker stopped (total items=%d)", items_counter["total"])


# ============================================================
# Точка входа
# ============================================================
def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        asyncio.run(run_loop())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt")
    except Exception as exc:
        log.exception("dialogue-worker crashed: %s", exc)
        _finish_run(_current_run_id, 0, status="fail", error_text=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
