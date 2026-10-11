"""Выгрузка метрик из robot.db в CSV для анализа / курсовой.

Использование:
    python -m scripts.export_csv
    python -m scripts.export_csv --out-dir /app/data/exports
    python -m scripts.export_csv --since 2026-10-01

Файлы пишутся в <out-dir>/YYYY-MM-DD/ с timestamp в имени.
Также кладётся _meta_<timestamp>.json с описанием.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.config import settings
from core.database import (
    BridgeHistory,
    BridgeWeight,
    ProcessedDialogue,
    WordStat,
    get_session,
)
from workers.dialogue.prompt_builder import load_token_groups

log = logging.getLogger("scripts.export_csv")
log.setLevel(settings.log_level.upper())
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(h)


# ============================================================
# Утилиты
# ============================================================
def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _out_dir(base: Path) -> Path:
    """Возвращает /app/data/exports/YYYY-MM-DD/."""
    day = datetime.now().strftime("%Y-%m-%d")
    d = base / day
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_csv(path: Path, rows: Iterable[dict[str, Any]],
               fieldnames: list[str]) -> int:
    """Пишет список словарей в CSV. Возвращает число строк."""
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
            count += 1
    return count


def _token_to_group() -> dict[str, str]:
    """Возвращает {token: group_name}."""
    mapping: dict[str, str] = {}
    try:
        groups = load_token_groups()
        for gname, spec in groups.items():
            for t in (spec.get("tokens") or []):
                mapping[t.lower()] = gname
    except Exception as exc:
        log.warning("_token_to_group failed: %s", exc)
    return mapping


# ============================================================
# Экспортёры
# ============================================================
def export_metrics_per_dialogue(session, out: Path, ts: str) -> tuple[Path, int]:
    rows: list[dict[str, Any]] = []
    dialogues = (
        session.query(ProcessedDialogue)
        .filter(ProcessedDialogue.status == "done")
        .order_by(ProcessedDialogue.processed_at.asc())
        .all()
    )
    for d in dialogues:
        rj = d.result_json or {}
        rows.append({
            "dialogue_id": d.dialogue_id,
            "sender": d.sender or "",
            "seed_id": d.seed_id or "",
            "phase": rj.get("phase", ""),
            "role": rj.get("role", ""),
            "role_source": rj.get("role_source", ""),
            "aggression": rj.get("aggression", ""),
            "aggression_source": rj.get("aggression_source", ""),
            "token_group": rj.get("token_group", ""),
            "stage_before": rj.get("stage_before", ""),
            "stage_after": rj.get("stage_after", ""),
            "score": rj.get("score", ""),
            "score_dialogue": rj.get("score_dialogue", ""),
            "score_response": rj.get("score_response", ""),
            "cosine": rj.get("cosine", ""),
            "similarity_to_target": rj.get("similarity_to_target", ""),
            "expected_word": rj.get("expected_word", ""),
            "tokens_in": rj.get("tokens_in", ""),
            "tokens_out": rj.get("tokens_out", ""),
            "finish_reason": rj.get("finish_reason", ""),
            "duration_ms": rj.get("duration_ms", ""),
            "llm_total_ms": rj.get("llm_total_ms", ""),
            "embedding_load_ms": rj.get("embedding_load_ms", ""),
            "version_id": rj.get("version_id", ""),
            "top_tokens": ";".join(rj.get("top_tokens") or []),
        })
    fname = out / f"metrics_per_dialogue_{ts}.csv"
    fields = [
        "dialogue_id", "sender", "seed_id", "phase", "role", "role_source",
        "aggression", "aggression_source", "token_group",
        "stage_before", "stage_after",
        "score", "score_dialogue", "score_response",
        "cosine", "similarity_to_target", "expected_word",
        "tokens_in", "tokens_out", "finish_reason",
        "duration_ms", "llm_total_ms", "embedding_load_ms",
        "version_id", "top_tokens",
    ]
    n = _write_csv(fname, rows, fields)
    return fname, n


def export_word_stats(session, out: Path, ts: str) -> tuple[Path, int]:
    token_to_group = _token_to_group()
    rows: list[dict[str, Any]] = []
    for w in session.query(WordStat).order_by(WordStat.total_count.desc()).all():
        rows.append({
            "word": w.word,
            "group": token_to_group.get(w.word.lower(), ""),
            "total_count": w.total_count,
            "success_count": w.success_count,
            "success_rate": round(w.success_rate, 4) if w.success_rate is not None else 0,
            "pmi_score": round(w.pmi_score, 4) if w.pmi_score is not None else 0,
            "updated_at": w.updated_at.isoformat() if w.updated_at else "",
        })
    fname = out / f"word_stats_{ts}.csv"
    fields = ["word", "group", "total_count", "success_count",
              "success_rate", "pmi_score", "updated_at"]
    n = _write_csv(fname, rows, fields)
    return fname, n


def export_bridges_weights(session, out: Path, ts: str) -> tuple[Path, int]:
    rows: list[dict[str, Any]] = []
    for b in (
        session.query(BridgeWeight)
        .order_by(BridgeWeight.weight.desc(), BridgeWeight.total_count.desc())
        .all()
    ):
        rows.append({
            "bridge_token": b.bridge_token,
            "from_group": b.from_group,
            "to_group": b.to_group,
            "weight": round(b.weight, 4) if b.weight is not None else 0,
            "success_count": b.success_count,
            "total_count": b.total_count,
            "updated_at": b.updated_at.isoformat() if b.updated_at else "",
        })
    fname = out / f"bridges_weights_{ts}.csv"
    fields = ["bridge_token", "from_group", "to_group",
              "weight", "success_count", "total_count", "updated_at"]
    n = _write_csv(fname, rows, fields)
    return fname, n


def export_bridges_history(session, out: Path, ts: str) -> tuple[Path, int]:
    rows: list[dict[str, Any]] = []
    for b in (
        session.query(BridgeHistory)
        .order_by(BridgeHistory.created_at.asc())
        .all()
    ):
        rows.append({
            "bridge_token": b.bridge_token,
            "from_group": b.from_group or "",
            "to_group": b.to_group or "",
            "dialogue_id": b.dialogue_id or "",
            "success": "" if b.success is None else int(b.success),
            "weight_before": round(b.weight_before, 4) if b.weight_before is not None else 0,
            "weight_after": round(b.weight_after, 4) if b.weight_after is not None else 0,
            "created_at": b.created_at.isoformat() if b.created_at else "",
        })
    fname = out / f"bridges_history_{ts}.csv"
    fields = ["bridge_token", "from_group", "to_group", "dialogue_id",
              "success", "weight_before", "weight_after", "created_at"]
    n = _write_csv(fname, rows, fields)
    return fname, n


def export_roles_summary(session, out: Path, ts: str) -> tuple[Path, int]:
    dialogues = (
        session.query(ProcessedDialogue)
        .filter(ProcessedDialogue.status == "done")
        .all()
    )
    total = len(dialogues)
    dims = {
        "role": {},
        "aggression": {},
        "stage_before": {},
        "stage_after": {},
        "token_group": {},
        "phase": {},
        "finish_reason": {},
    }
    for d in dialogues:
        rj = d.result_json or {}
        for dim in dims:
            v = rj.get(dim)
            if v is None or v == "":
                v = "(none)"
            dims[dim][v] = dims[dim].get(v, 0) + 1

    rows: list[dict[str, Any]] = []
    for dim, counts in dims.items():
        for value, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
            share = (cnt / total) if total else 0
            rows.append({
                "dimension": dim,
                "value": value,
                "count": cnt,
                "share": round(share, 4),
            })
    fname = out / f"roles_summary_{ts}.csv"
    fields = ["dimension", "value", "count", "share"]
    n = _write_csv(fname, rows, fields)
    return fname, n


# ============================================================
# Главная
# ============================================================
def run_export(out_base: Path | None = None) -> dict[str, Any]:
    """Выгружает все CSV. Возвращает meta-словарь."""
    base = Path(out_base) if out_base else (settings.robot_db_path.parent / "exports")
    out = _out_dir(base)
    ts = _now_ts()

    log.info("export_csv: out dir = %s", out)
    meta: dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "out_dir": str(out),
        "timestamp": ts,
        "files": {},
    }

    with get_session() as s:
        for name, fn in [
            ("metrics_per_dialogue", export_metrics_per_dialogue),
            ("word_stats", export_word_stats),
            ("bridges_weights", export_bridges_weights),
            ("bridges_history", export_bridges_history),
            ("roles_summary", export_roles_summary),
        ]:
            try:
                path, n = fn(s, out, ts)
                meta["files"][name] = {"path": str(path), "rows": n}
                log.info("export_csv: %s -> %d rows", path.name, n)
            except Exception as exc:
                log.exception("export_csv: %s failed: %s", name, exc)
                meta["files"][name] = {"error": str(exc)}

    meta_path = out / f"_meta_{ts}.json"
    try:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("export_csv: meta -> %s", meta_path.name)
    except Exception as exc:
        log.exception("export_csv: meta write failed: %s", exc)

    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="Выгрузка метрик robot.db в CSV")
    ap.add_argument("--out-dir", type=str, default=None,
                    help="Базовая папка для экспорта (по умолчанию data/exports)")
    args = ap.parse_args()

    try:
        meta = run_export(Path(args.out_dir) if args.out_dir else None)
    except Exception as exc:
        log.exception("export_csv failed: %s", exc)
        return 1

    print()
    print("=== Экспорт завершён ===")
    print(f"Папка: {meta.get('out_dir')}")
    for name, info in (meta.get("files") or {}).items():
        if "error" in info:
            print(f"  {name}: ERROR — {info['error']}")
        else:
            print(f"  {name}: {info['rows']} строк -> {Path(info['path']).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
