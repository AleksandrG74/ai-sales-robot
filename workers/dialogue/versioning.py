"""Версионирование стратегий.

Оценивает метрики активной версии по последним N диалогам,
создаёт пассивный снапшот новой версии, если метрики значимо
изменились, и умеет откатывать на предыдущую версию.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import settings
from core.database import (
    ProcessedDialogue,
    Strategy,
    StrategyVersion,
    get_session,
)

log = logging.getLogger("dialogue.versioning")
log.setLevel(settings.log_level.upper())


# ============================================================
# Пороги
# ============================================================
SUCCESS_SCORE_THRESHOLD = 0.7
DELTA_SUCCESS_RATE = 0.10      # 10 п.п.
DELTA_AVG_SCORE = 0.15         # 15%
LOOKBACK_DEFAULT = 50
AUTO_VERSION_EVERY_N = 50


# ============================================================
# Dataclass с метриками
# ============================================================
@dataclass
class StrategyMetrics:
    runs: int
    success_count: int
    success_rate: float
    avg_score: float
    avg_tokens_out: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "success_count": self.success_count,
            "success_rate": round(self.success_rate, 4),
            "avg_score": round(self.avg_score, 4),
            "avg_tokens_out": round(self.avg_tokens_out, 2),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }


# ============================================================
# Счётчик для auto-version
# ============================================================
_auto_lock = threading.Lock()
_dialogues_since_version = 0


# ============================================================
# Оценка стратегии
# ============================================================
def _metrics_from_rows(rows: list[ProcessedDialogue]) -> StrategyMetrics:
    total = 0
    success = 0
    score_sum = 0.0
    tokens_sum = 0
    tokens_n = 0

    for r in rows:
        data = r.result_json or {}
        score = data.get("score")
        if score is None:
            continue
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            continue
        total += 1
        score_sum += score_f
        if score_f >= SUCCESS_SCORE_THRESHOLD:
            success += 1
        toks = data.get("tokens_out")
        if isinstance(toks, (int, float)):
            tokens_sum += int(toks)
            tokens_n += 1

    if total == 0:
        return StrategyMetrics(0, 0, 0.0, 0.0, 0.0)

    return StrategyMetrics(
        runs=total,
        success_count=success,
        success_rate=success / total,
        avg_score=score_sum / total,
        avg_tokens_out=(tokens_sum / tokens_n) if tokens_n else 0.0,
    )


def evaluate_strategy(strategy_id: int, lookback: int = LOOKBACK_DEFAULT) -> Optional[StrategyMetrics]:
    """Считает метрики стратегии по последним N диалогам.

    Как определяем, что диалог относится к стратегии:
      в result_json есть version_id, принадлежащий этой стратегии.
    """
    try:
        with get_session() as s:
            st = s.get(Strategy, strategy_id)
            if st is None:
                log.warning("Strategy %s not found", strategy_id)
                return None

            version_ids = [v.id for v in
                           s.query(StrategyVersion).filter_by(strategy_id=strategy_id).all()]
            if not version_ids:
                return StrategyMetrics(0, 0, 0.0, 0.0, 0.0)

            rows = (
                s.query(ProcessedDialogue)
                .filter(ProcessedDialogue.status == "done")
                .order_by(ProcessedDialogue.processed_at.desc())
                .limit(lookback * 5)  # с запасом — часть отфильтруется
                .all()
            )
            # оставим только те, где version_id из нашей стратегии
            relevant: list[ProcessedDialogue] = []
            for r in rows:
                data = r.result_json or {}
                vid = data.get("version_id")
                if vid in version_ids:
                    relevant.append(r)
                if len(relevant) >= lookback:
                    break

            return _metrics_from_rows(relevant)
    except Exception as exc:
        log.exception("evaluate_strategy failed: %s", exc)
        return None


def evaluate_all(lookback: int = LOOKBACK_DEFAULT,
                 auto_create: bool = False) -> dict[int, StrategyMetrics]:
    """Прогоняет evaluate_strategy по всем стратегиям.

    Если auto_create=True и should_create_version() → True, создаёт версию.
    """
    result: dict[int, StrategyMetrics] = {}
    try:
        with get_session() as s:
            ids = [st.id for st in s.query(Strategy).all()]
    except Exception as exc:
        log.exception("evaluate_all: не смогли получить список стратегий: %s", exc)
        return result

    for sid in ids:
        m = evaluate_strategy(sid, lookback=lookback)
        if m is None:
            continue
        result[sid] = m
        if auto_create:
            try:
                if should_create_version(sid, lookback=lookback):
                    create_version(
                        strategy_id=sid,
                        comment=f"auto: success_rate={m.success_rate:.2f}, avg_score={m.avg_score:.2f}",
                        metrics_json=m.to_dict(),
                        created_by="system",
                    )
            except Exception as exc:
                log.exception("auto create_version for strategy %s failed: %s", sid, exc)
    return result


# ============================================================
# Триггер создания версии
# ============================================================
def _last_version_metrics(strategy_id: int) -> Optional[dict[str, Any]]:
    try:
        with get_session() as s:
            v = (
                s.query(StrategyVersion)
                .filter_by(strategy_id=strategy_id)
                .order_by(StrategyVersion.version.desc())
                .first()
            )
            if v is None:
                return None
            return dict(v.metrics_json or {})
    except Exception as exc:
        log.exception("_last_version_metrics failed: %s", exc)
        return None


def should_create_version(strategy_id: int, lookback: int = LOOKBACK_DEFAULT) -> bool:
    """Решает, пора ли создавать новую версию."""
    current = evaluate_strategy(strategy_id, lookback=lookback)
    if current is None or current.runs < 5:
        # слишком мало данных
        return False

    prev = _last_version_metrics(strategy_id) or {}
    prev_sr = float(prev.get("success_rate") or 0.0)
    prev_as = float(prev.get("avg_score") or 0.0)

    d_sr = abs(current.success_rate - prev_sr)
    d_as = abs(current.avg_score - prev_as)

    if d_sr > DELTA_SUCCESS_RATE or d_as > DELTA_AVG_SCORE:
        log.info(
            "should_create_version: strategy=%s Δsr=%.3f Δas=%.3f (current: sr=%.3f, as=%.3f)",
            strategy_id, d_sr, d_as, current.success_rate, current.avg_score,
        )
        return True
    return False


# ============================================================
# Создание версии
# ============================================================
def _next_version_number(session, strategy_id: int) -> int:
    last = (
        session.query(StrategyVersion)
        .filter_by(strategy_id=strategy_id)
        .order_by(StrategyVersion.version.desc())
        .first()
    )
    return (last.version + 1) if last else 1


def create_version(
    strategy_id: int,
    *,
    prompt_template: Optional[str] = None,
    params_json: Optional[dict[str, Any]] = None,
    metrics_json: Optional[dict[str, Any]] = None,
    comment: Optional[str] = None,
    created_by: str = "system",
) -> Optional[int]:
    """Создаёт новую версию стратегии.

    Если prompt_template/params_json не переданы — копирует из активной.
    Возвращает id новой версии.
    """
    try:
        with get_session() as s:
            st = s.get(Strategy, strategy_id)
            if st is None:
                log.warning("create_version: strategy %s not found", strategy_id)
                return None

            active = None
            if st.active_version_id:
                active = s.get(StrategyVersion, st.active_version_id)

            new_template = prompt_template
            new_params = params_json
            if active is not None:
                if new_template is None:
                    new_template = active.prompt_template
                if new_params is None:
                    new_params = dict(active.params_json or {})

            if new_template is None:
                log.warning("create_version: no template to copy for strategy %s", strategy_id)
                return None

            version_number = _next_version_number(s, strategy_id)
            v = StrategyVersion(
                strategy_id=strategy_id,
                version=version_number,
                prompt_template=new_template,
                params_json=new_params,
                metrics_json=metrics_json or {},
                comment=comment,
                created_by=created_by,
            )
            s.add(v)
            s.flush()
            st.active_version_id = v.id
            s.flush()
            log.info("create_version: strategy=%s new version=%s (id=%s)",
                     strategy_id, version_number, v.id)
            return v.id
    except Exception as exc:
        log.exception("create_version failed: %s", exc)
        return None


# ============================================================
# Откат
# ============================================================
def rollback(strategy_id: int, to_version_number: int) -> bool:
    """Переключает active_version_id на указанную версию (по номеру)."""
    try:
        with get_session() as s:
            st = s.get(Strategy, strategy_id)
            if st is None:
                log.warning("rollback: strategy %s not found", strategy_id)
                return False
            v = (
                s.query(StrategyVersion)
                .filter_by(strategy_id=strategy_id, version=to_version_number)
                .one_or_none()
            )
            if v is None:
                log.warning("rollback: version %s not found for strategy %s",
                            to_version_number, strategy_id)
                return False
            st.active_version_id = v.id
            log.info("rollback: strategy=%s -> version=%s", strategy_id, to_version_number)
            return True
    except Exception as exc:
        log.exception("rollback failed: %s", exc)
        return False


# ============================================================
# Отчёты
# ============================================================
def get_versions(strategy_id: int) -> list[dict[str, Any]]:
    try:
        with get_session() as s:
            rows = (
                s.query(StrategyVersion)
                .filter_by(strategy_id=strategy_id)
                .order_by(StrategyVersion.version.asc())
                .all()
            )
            st = s.get(Strategy, strategy_id)
            active_id = st.active_version_id if st else None
            return [
                {
                    "id": r.id,
                    "version": r.version,
                    "is_active": (r.id == active_id),
                    "comment": r.comment,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "created_by": r.created_by,
                    "metrics_json": r.metrics_json or {},
                }
                for r in rows
            ]
    except Exception as exc:
        log.exception("get_versions failed: %s", exc)
        return []


def get_active_version(strategy_id: int) -> Optional[dict[str, Any]]:
    try:
        with get_session() as s:
            st = s.get(Strategy, strategy_id)
            if st is None or st.active_version_id is None:
                return None
            v = s.get(StrategyVersion, st.active_version_id)
            if v is None:
                return None
            return {
                "id": v.id,
                "version": v.version,
                "prompt_template": v.prompt_template,
                "params_json": v.params_json or {},
                "metrics_json": v.metrics_json or {},
                "comment": v.comment,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "created_by": v.created_by,
            }
    except Exception as exc:
        log.exception("get_active_version failed: %s", exc)
        return None


# ============================================================
# Авто-версионирование
# ============================================================
def maybe_auto_version(force: bool = False) -> bool:
    """Раз в AUTO_VERSION_EVERY_N диалогов запускает evaluate_all(auto_create=True)."""
    global _dialogues_since_version
    with _auto_lock:
        _dialogues_since_version += 1
        should = force or (_dialogues_since_version >= AUTO_VERSION_EVERY_N)
        if should:
            _dialogues_since_version = 0

    if should:
        try:
            result = evaluate_all(auto_create=True)
            log.info("maybe_auto_version: evaluated %d strategies", len(result))
            return True
        except Exception as exc:
            log.exception("maybe_auto_version failed: %s", exc)
    return False


# ============================================================
# Тест
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("=== стратегии в БД ===")
    try:
        with get_session() as s:
            strategies = s.query(Strategy).all()
            for st in strategies:
                print(f"  id={st.id} name={st.name} active_version_id={st.active_version_id}")
    except Exception as exc:
        print("  (нет стратегий или БД пуста):", exc)

    print("\n=== создаём тестовую стратегию ===")
    from workers.dialogue.prompt_builder import build_prompt
    sample_messages = [
        {"author_username": "client", "message": "Привет, расскажите", "is_target": False, "author_id": 1},
        {"author_username": "client", "message": "ок", "is_target": True, "author_id": 1},
    ]
    bp = build_prompt(sample_messages, role="buyer", aggression_level="low")
    print(f"  strategy_id={bp.strategy_id} version_id={bp.version_id}")

    print("\n=== версии стратегии ===")
    for v in get_versions(bp.strategy_id or 0):
        print(f"  v{v['version']} (id={v['id']}) is_active={v['is_active']} comment={v['comment']!r}")

    print("\n=== создаём новую версию (симуляция) ===")
    new_id = create_version(
        bp.strategy_id or 0,
        comment="test: manual snapshot",
        metrics_json={"runs": 10, "success_rate": 0.8, "avg_score": 0.75},
        created_by="system",
    )
    print(f"  new version_id={new_id}")

    print("\n=== версии после создания ===")
    for v in get_versions(bp.strategy_id or 0):
        print(f"  v{v['version']} (id={v['id']}) is_active={v['is_active']} comment={v['comment']!r}")

    print("\n=== откат на v1 ===")
    ok = rollback(bp.strategy_id or 0, 1)
    print(f"  rollback ok={ok}")

    print("\n=== активная версия сейчас ===")
    av = get_active_version(bp.strategy_id or 0)
    print(f"  {av}")
