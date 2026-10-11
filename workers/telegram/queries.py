# workers/telegram/queries.py
"""
Запросы к robot.db для telegram-бота оператора.
Синхронные функции, возвращают чистые dict'ы (не ORM-объекты).
Не знают ничего про Telegram/aiogram.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import desc, func, select

from core.database import (
    get_session,
    Run,
    Strategy,
    StrategyVersion,
    WordStat,
    PMIHistory,
    BridgeHistory,
    BridgeWeight,
    ProcessedDialogue,
    OperatorAction,
)

logger = logging.getLogger(__name__)


# === УТИЛИТЫ ===

def _safe_json_loads(raw: Optional[str]) -> Optional[Any]:
    """Безопасный парсинг JSON-строки. Возвращает None при ошибке."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Не удалось распарсить JSON: %s", e)
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """datetime -> iso-строка, безопасно."""
    return dt.isoformat() if dt else None


# === СИСТЕМНЫЙ СТАТУС ===

def get_system_status() -> dict:
    """
    Общее состояние системы:
    - последний прогон (runs)
    - количество обработанных диалогов
    - количество стратегий / версий
    """
    result: dict = {
        "last_run": None,
        "dialogue_count": 0,
        "strategy_count": 0,
        "version_count": 0,
        "active_strategies": 0,
    }
    try:
        with get_session() as session:
            last_run = session.execute(
                select(Run).order_by(desc(Run.started_at)).limit(1)
            ).scalar_one_or_none()

            if last_run is not None:
                result["last_run"] = {
                    "id": last_run.id,
                    "run_type": last_run.run_type,
                    "status": last_run.status,
                    "started_at": _iso(last_run.started_at),
                    "finished_at": _iso(last_run.finished_at),
                    "items_processed": last_run.items_processed,
                    "error_text": last_run.error_text,
                }

            result["dialogue_count"] = session.execute(
                select(func.count(ProcessedDialogue.id))
            ).scalar_one()

            result["strategy_count"] = session.execute(
                select(func.count(Strategy.id))
            ).scalar_one()

            result["version_count"] = session.execute(
                select(func.count(StrategyVersion.id))
            ).scalar_one()

            result["active_strategies"] = session.execute(
                select(func.count(Strategy.id)).where(Strategy.active_version_id.isnot(None))
            ).scalar_one()

    except Exception as e:
        logger.exception("get_system_status упал: %s", e)

    return result


# === ОБЩИЕ МЕТРИКИ ===

def get_overall_stats() -> dict:
    """
    Агрегированные метрики по всем обработанным диалогам:
    - количество
    - avg score / avg cosine
    - распределение по ролям
    - распределение по уровням агрессии
    """
    stats: dict = {
        "total": 0,
        "with_result": 0,
        "avg_score": 0.0,
        "avg_cosine": 0.0,
        "roles": {},
        "aggression": {},
    }

    try:
        with get_session() as session:
            rows = session.execute(
                select(ProcessedDialogue.result_json).where(
                    ProcessedDialogue.result_json.isnot(None)
                )
            ).all()

        scores: list[float] = []
        cosines: list[float] = []
        roles: dict[str, int] = {}
        aggression: dict[str, int] = {}

        for (raw,) in rows:
            data = _safe_json_loads(raw)
            if not isinstance(data, dict):
                continue

            stats["with_result"] += 1

            sc = data.get("score")
            if isinstance(sc, (int, float)):
                scores.append(float(sc))

            cs = data.get("cosine")
            if isinstance(cs, (int, float)):
                cosines.append(float(cs))

            role = data.get("role")
            if isinstance(role, str) and role:
                roles[role] = roles.get(role, 0) + 1

            agg = data.get("aggression_level")
            if agg is None:
                agg = data.get("aggression")
            if isinstance(agg, (int, float)):
                key = str(int(agg))
                aggression[key] = aggression.get(key, 0) + 1
            elif isinstance(agg, str) and agg:
                aggression[agg] = aggression.get(agg, 0) + 1

        with get_session() as session:
            stats["total"] = session.execute(
                select(func.count(ProcessedDialogue.id))
            ).scalar_one()

        if scores:
            stats["avg_score"] = round(sum(scores) / len(scores), 4)
        if cosines:
            stats["avg_cosine"] = round(sum(cosines) / len(cosines), 4)

        stats["roles"] = dict(sorted(roles.items(), key=lambda kv: kv[1], reverse=True))
        stats["aggression"] = dict(sorted(aggression.items(), key=lambda kv: kv[0]))

    except Exception as e:
        logger.exception("get_overall_stats упал: %s", e)

    return stats


# === ДИАЛОГИ ===

def get_recent_dialogues(limit: int = 10, offset: int = 0) -> list[dict]:
    """
    Последние обработанные диалоги с кратким резюме.
    Возвращает список dict'ов: id, dialogue_id, sender, status, score, cosine, role.
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(ProcessedDialogue)
                .order_by(desc(ProcessedDialogue.id))
                .limit(limit)
                .offset(offset)
            ).scalars().all()

            for row in rows:
                data = _safe_json_loads(row.result_json) or {}
                out.append({
                    "id": row.id,
                    "dialogue_id": row.dialogue_id,
                    "sender": row.sender,
                    "seed_id": row.seed_id,
                    "status": row.status,
                    "error": row.error,
                    "score": data.get("score"),
                    "cosine": data.get("cosine"),
                    "role": data.get("role"),
                    "aggression_level": data.get("aggression_level", data.get("aggression")),
                })
    except Exception as e:
        logger.exception("get_recent_dialogues упал: %s", e)

    return out


def get_dialogue(dialogue_id: str) -> Optional[dict]:
    """
    Детали одного диалога по dialogue_id. Возвращает dict или None.
    """
    try:
        with get_session() as session:
            row = session.execute(
                select(ProcessedDialogue).where(ProcessedDialogue.dialogue_id == dialogue_id).limit(1)
            ).scalar_one_or_none()

            if row is None:
                return None

            data = _safe_json_loads(row.result_json)

            return {
                "id": row.id,
                "dialogue_id": row.dialogue_id,
                "sender": row.sender,
                "seed_id": row.seed_id,
                "status": row.status,
                "error": row.error,
                "result": data,
                "result_raw": row.result_json,
            }
    except Exception as e:
        logger.exception("get_dialogue упал: %s", e)
        return None


def get_dialogue_count() -> int:
    """Общее количество обработанных диалогов."""
    try:
        with get_session() as session:
            return int(session.execute(
                select(func.count(ProcessedDialogue.id))
            ).scalar_one())
    except Exception as e:
        logger.exception("get_dialogue_count упал: %s", e)
        return 0


# === СТРАТЕГИИ И ВЕРСИИ ===

def get_strategies() -> list[dict]:
    """
    Все стратегии с краткой сводкой. Без версий.
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(Strategy).order_by(Strategy.id)
            ).scalars().all()

            for s in rows:
                out.append({
                    "id": s.id,
                    "name": s.name,
                    "token_group": s.token_group,
                    "role": s.role,
                    "aggression_level": s.aggression_level,
                    "active_version_id": s.active_version_id,
                    "current_weight": s.current_weight,
                })
    except Exception as e:
        logger.exception("get_strategies упал: %s", e)

    return out


def get_versions(strategy_id: int) -> list[dict]:
    """
    Список версий конкретной стратегии (по strategy_id).
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(StrategyVersion)
                .where(StrategyVersion.strategy_id == strategy_id)
                .order_by(desc(StrategyVersion.version))
            ).scalars().all()

            for v in rows:
                out.append({
                    "id": v.id,
                    "strategy_id": v.strategy_id,
                    "version": v.version,
                    "prompt_template": v.prompt_template,
                    "params": _safe_json_loads(v.params_json),
                    "metrics": _safe_json_loads(v.metrics_json),
                    "comment": v.comment,
                    "created_by": v.created_by,
                })
    except Exception as e:
        logger.exception("get_versions упал: %s", e)

    return out


def rollback_strategy(strategy_id: int, version: int) -> bool:
    """
    Откат стратегии к указанной версии через versioning.py.
    Возвращает True при успехе, False при любой ошибке.
    """
    try:
        from workers.dialogue import versioning
    except Exception as e:
        logger.exception("Не удалось импортировать versioning: %s", e)
        return False

    try:
        if hasattr(versioning, "rollback_to_version"):
            ok = versioning.rollback_to_version(strategy_id, version)
            return bool(ok)
        if hasattr(versioning, "rollback"):
            ok = versioning.rollback(strategy_id, version)
            return bool(ok)
        logger.error("В versioning.py не найдена функция rollback*")
        return False
    except Exception as e:
        logger.exception("rollback_strategy(%s, %s) упал: %s", strategy_id, version, e)
        return False


# === ТОКЕНЫ / СТАТИСТИКА СЛОВ ===

def get_word_stats(limit: int = 10) -> list[dict]:
    """
    Топ-N слов по success_rate (среди тех, у кого есть попадания).
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(WordStat)
                .where(WordStat.total_count > 0)
                .order_by(desc(WordStat.success_rate), desc(WordStat.total_count))
                .limit(limit)
            ).scalars().all()

            for w in rows:
                out.append({
                    "id": w.id,
                    "word": w.word,
                    "total_count": w.total_count,
                    "success_count": w.success_count,
                    "success_rate": w.success_rate,
                    "pmi_score": w.pmi_score,
                })
    except Exception as e:
        logger.exception("get_word_stats упал: %s", e)

    return out


# === МОСТЫ ===

def get_bridges(limit: int = 10) -> list[dict]:
    """
    Топ-N мостов по весу из bridge_weights.
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(BridgeWeight)
                .order_by(desc(BridgeWeight.weight))
                .limit(limit)
            ).scalars().all()

            for b in rows:
                out.append({
                    "id": b.id,
                    "bridge_token": b.bridge_token,
                    "from_group": b.from_group,
                    "to_group": b.to_group,
                    "weight": b.weight,
                    "success_count": b.success_count,
                    "total_count": b.total_count,
                })
    except Exception as e:
        logger.exception("get_bridges упал: %s", e)

    return out


def get_bridge_history(bridge_token: str, limit: int = 20) -> list[dict]:
    """
    История использования конкретного моста.
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(BridgeHistory)
                .where(BridgeHistory.bridge_token == bridge_token)
                .order_by(desc(BridgeHistory.id))
                .limit(limit)
            ).scalars().all()

            for h in rows:
                out.append({
                    "id": h.id,
                    "bridge_token": h.bridge_token,
                    "from_group": h.from_group,
                    "to_group": h.to_group,
                    "dialogue_id": h.dialogue_id,
                    "success": h.success,
                    "weight_before": h.weight_before,
                    "weight_after": h.weight_after,
                })
    except Exception as e:
        logger.exception("get_bridge_history упал: %s", e)

    return out


# === ПРОГОНЫ ===

def get_runs(limit: int = 5) -> list[dict]:
    """
    Последние N прогонов.
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(Run).order_by(desc(Run.started_at)).limit(limit)
            ).scalars().all()

            for r in rows:
                duration = None
                if r.started_at and r.finished_at:
                    try:
                        duration = (r.finished_at - r.started_at).total_seconds()
                    except Exception:
                        duration = None

                out.append({
                    "id": r.id,
                    "run_type": r.run_type,
                    "status": r.status,
                    "started_at": _iso(r.started_at),
                    "finished_at": _iso(r.finished_at),
                    "duration_sec": duration,
                    "items_processed": r.items_processed,
                    "error_text": r.error_text,
                })
    except Exception as e:
        logger.exception("get_runs упал: %s", e)

    return out


# === ДЕЙСТВИЯ ОПЕРАТОРА ===

def get_operator_actions(limit: int = 10) -> list[dict]:
    """
    Последние действия оператора (аудит).
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(OperatorAction).order_by(desc(OperatorAction.id)).limit(limit)
            ).scalars().all()

            for a in rows:
                out.append({
                    "id": a.id,
                    "dialogue_id": a.dialogue_id,
                    "action": a.action,
                    "operator_id": a.operator_id,
                    "created_at": _iso(a.created_at),
                })
    except Exception as e:
        logger.exception("get_operator_actions упал: %s", e)

    return out


def log_operator_action(dialogue_id: Optional[str], action: str, operator_id: Optional[int] = None) -> bool:
    """
    Записать действие оператора в operator_actions.
    """
    try:
        with get_session() as session:
            row = OperatorAction(
                dialogue_id=dialogue_id,
                action=action,
                operator_id=operator_id,
            )
            session.add(row)
            session.commit()
        return True
    except Exception as e:
        logger.exception("log_operator_action упал: %s", e)
        return False


# === СНАПШОТ PMI / ОЧИСТКА ===

def get_pmi_history(limit: int = 20) -> list[dict]:
    """
    Последние записи pmi_history.
    """
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(PMIHistory).order_by(desc(PMIHistory.id)).limit(limit)
            ).scalars().all()
            for p in rows:
                out.append({
                    "id": p.id,
                    "word": p.word,
                    "window_start": _iso(getattr(p, "window_start", None)),
                    "window_end": _iso(getattr(p, "window_end", None)),
                    "pmi": getattr(p, "pmi", None),
                })
    except Exception as e:
        logger.exception("get_pmi_history упал: %s", e)
    return out


def cleanup_old_data(days: int = 90) -> dict:
    """
    Удаляет pmi_history и bridges_history старше N дней.
    Возвращает {'pmi_deleted': int, 'bridges_deleted': int}.
    """
    result = {"pmi_deleted": 0, "bridges_deleted": 0}
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_session() as session:
            try:
                pmi_del = session.query(PMIHistory).filter(
                    PMIHistory.window_end < cutoff
                ).delete(synchronize_session=False)
                result["pmi_deleted"] = int(pmi_del or 0)
            except Exception as e:
                logger.warning("cleanup pmi_history пропущен: %s", e)

            try:
                if hasattr(BridgeHistory, "created_at"):
                    br_del = session.query(BridgeHistory).filter(
                        BridgeHistory.created_at < cutoff
                    ).delete(synchronize_session=False)
                    result["bridges_deleted"] = int(br_del or 0)
                else:
                    logger.info("BridgeHistory без created_at — пропускаем cleanup.")
            except Exception as e:
                logger.warning("cleanup bridges_history пропущен: %s", e)

            session.commit()
    except Exception as e:
        logger.exception("cleanup_old_data упал: %s", e)

    return result
