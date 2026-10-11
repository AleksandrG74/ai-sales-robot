"""dialogue-worker: цикл обработки JSON-тредов."""
from __future__ import annotations

import asyncio
import json
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

LOG_FILE = settings.logs_dir / "dialogue-worker.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

SLEEP_BETWEEN_SCANS = int(os.getenv("DIALOGUE_WORKER_SLEEP", "30"))
WARMUP_LLM = os.getenv("DIALOGUE_WORKER_WARMUP_LLM", "1") == "1"


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
_shutdown = False
_current_run_id: Optional[int] = None


def _on_signal(signum, frame):  # type: ignore[no-untyped-def]
    global _shutdown
    log.info("Received signal %s, shutting down...", signum)
    _shutdown = True


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


def _load_processed_ids() -> set:
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
    return path.stem


def _list_raw_files(raw_dir: Path) -> list:
    """Читает список файлов из _index.json (обход бага overlayfs readdir)."""
    names = set()

    index_path = raw_dir / "_index.json"
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            for item in data.get("dialogues", []):
                fname = item.get("file")
                if fname and fname.endswith(".json"):
                    names.add(fname)
            if names:
                log.info("_list_raw_files: read %d names from _index.json", len(names))
        except Exception as exc:
            log.warning("_index.json read failed: %s", exc)

    try:
        out = subprocess.run(
            ["find", str(raw_dir), "-maxdepth", "1", "-name", "*.json", "-type", "f"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                line = line.strip()
                if line:
                    name = os.path.basename(line)
                    if name.endswith(".json"):
                        names.add(name)
    except Exception as exc:
        log.warning("subprocess find failed: %s", exc)

    try:
        with os.scandir(raw_dir) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".json"):
                    names.add(entry.name)
    except Exception as exc:
        log.warning("os.scandir failed: %s", exc)

    return sorted(raw_dir / n for n in names if not n.startswith("_index") and n.endswith(".json"))


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
        log.exception("LLM warmup failed: %s", exc)


async def _scan_once(processed_ids: set, run_id: Optional[int],
                     items_counter: dict) -> int:
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
