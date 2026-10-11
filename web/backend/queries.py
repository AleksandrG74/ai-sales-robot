# web/backend/queries.py
"""
Запросы к robot.db для web-backend.

Особенности:
- Синхронные функции (SQLAlchemy sync).
- Возвращают чистые dict'ы / list[dict] / скалярные значения — не ORM.
- Не знают ничего про FastAPI / HTTP.
- Пагинация: limit (default 50, max 500), offset.
- Фильтры для диалогов: role, aggression (применяются в Python после
  парсинга result_json), плюс SQL-фильтры sender / status / seed_id.
- POST-операции:
    * rollback_strategy  → workers.dialogue.versioning.rollback*
    * send_live_message  → workers.live.sender.send_operator_reply
    * suggest_live_reply → workers.live.suggest.generate_suggestion
    * log_operator_action → OperatorAction
- Не падает при пустой БД: возвращает [] / None / 0.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
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
    MonitoredChat,
    LiveMessage,
    LiveSession,
)

logger = logging.getLogger("web-backend.queries")

# Лимиты пагинации
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


# === УТИЛИТЫ ===

def _safe_json_loads(raw: Any) -> Any:
    """
    Безопасный парсинг JSON-поля.
    Принимает None / str / bytes / dict / list.
    При ошибке -> None.
    """
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception as e:
            logger.warning("Не удалось декодировать JSON-байты: %s", e)
            return None
    if isinstance(raw, str):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Не удалось распарсить JSON: %s", e)
            return None
    return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = DEFAULT_LIMIT
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    lim = max(1, min(MAX_LIMIT, lim))
    off = max(0, off)
    return lim, off


# === СИСТЕМНЫЙ СТАТУС ===

def get_system_status() -> dict:
    result: dict = {
        "last_run": None,
        "dialogue_count": 0,
        "strategy_count": 0,
        "version_count": 0,
        "active_strategies": 0,
        "monitored_chats": 0,
        "live_messages": 0,
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

            result["dialogue_count"] = int(session.execute(
                select(func.count(ProcessedDialogue.id))
            ).scalar_one())
            result["strategy_count"] = int(session.execute(
                select(func.count(Strategy.id))
            ).scalar_one())
            result["version_count"] = int(session.execute(
                select(func.count(StrategyVersion.id))
            ).scalar_one())
            result["active_strategies"] = int(session.execute(
                select(func.count(Strategy.id)).where(Strategy.active_version_id.isnot(None))
            ).scalar_one())

            try:
                result["monitored_chats"] = int(session.execute(
                    select(func.count(MonitoredChat.id))
                ).scalar_one())
            except Exception:
                result["monitored_chats"] = 0

            try:
                result["live_messages"] = int(session.execute(
                    select(func.count(LiveMessage.id))
                ).scalar_one())
            except Exception:
                result["live_messages"] = 0

    except Exception as e:
        logger.exception("get_system_status упал: %s", e)

    return result


# === ОБЩИЕ МЕТРИКИ ===

def get_overall_stats() -> dict:
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
            stats["total"] = int(session.execute(
                select(func.count(ProcessedDialogue.id))
            ).scalar_one())

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

def _row_to_dialogue_dict(row: ProcessedDialogue) -> dict:
    data = _safe_json_loads(row.result_json) or {}
    return {
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
    }


def get_dialogues(
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    role: Optional[str] = None,
    aggression: Optional[str] = None,
    sender: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """
    Список диалогов с пагинацией и фильтрами.
    role/aggression фильтруются в Python (после парсинга result_json),
    поэтому при их наличии читаем с запасом, потом срезаем offset/limit.
    """
    lim, off = _clamp(limit, offset)
    has_python_filter = bool(role or aggression)
    out: list[dict] = []

    try:
        with get_session() as session:
            stmt = select(ProcessedDialogue).order_by(desc(ProcessedDialogue.id))

            if sender:
                stmt = stmt.where(ProcessedDialogue.sender == sender)
            if status:
                stmt = stmt.where(ProcessedDialogue.status == status)

            if has_python_filter:
                stmt = stmt.limit(max(lim * 10, 200)).offset(0)
            else:
                stmt = stmt.limit(lim).offset(off)

            rows = session.execute(stmt).scalars().all()

            for row in rows:
                d = _row_to_dialogue_dict(row)
                if role and d.get("role") != role:
                    continue
                if aggression:
                    agg = d.get("aggression_level")
                    if agg is None or str(agg) != str(aggression):
                        continue
                out.append(d)

        if has_python_filter:
            out = out[off: off + lim]

    except Exception as e:
        logger.exception("get_dialogues упал: %s", e)

    return out


def count_dialogues(
    role: Optional[str] = None,
    aggression: Optional[str] = None,
    sender: Optional[str] = None,
    status: Optional[str] = None,
) -> int:
    try:
        if role or aggression:
            return len(get_dialogues(
                limit=MAX_LIMIT * 10, offset=0,
                role=role, aggression=aggression,
                sender=sender, status=status,
            ))

        with get_session() as session:
            stmt = select(func.count(ProcessedDialogue.id))
            if sender:
                stmt = stmt.where(ProcessedDialogue.sender == sender)
            if status:
                stmt = stmt.where(ProcessedDialogue.status == status)
            return int(session.execute(stmt).scalar_one())
    except Exception as e:
        logger.exception("count_dialogues упал: %s", e)
        return 0


def get_dialogue(dialogue_id: str) -> Optional[dict]:
    try:
        with get_session() as session:
            row = session.execute(
                select(ProcessedDialogue)
                .where(ProcessedDialogue.dialogue_id == dialogue_id)
                .limit(1)
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
                "result_raw": row.result_json if isinstance(row.result_json, str) else None,
            }
    except Exception as e:
        logger.exception("get_dialogue упал: %s", e)
        return None


def get_dialogue_count() -> int:
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
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(select(Strategy).order_by(Strategy.id)).scalars().all()
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


def get_strategy(strategy_id: int) -> Optional[dict]:
    try:
        with get_session() as session:
            s = session.execute(
                select(Strategy).where(Strategy.id == strategy_id).limit(1)
            ).scalar_one_or_none()
            if s is None:
                return None
            return {
                "id": s.id,
                "name": s.name,
                "token_group": s.token_group,
                "role": s.role,
                "aggression_level": s.aggression_level,
                "active_version_id": s.active_version_id,
                "current_weight": s.current_weight,
            }
    except Exception as e:
        logger.exception("get_strategy упал: %s", e)
        return None


def get_versions(strategy_id: int) -> list[dict]:
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
                    "created_at": _iso(getattr(v, "created_at", None)),
                })
    except Exception as e:
        logger.exception("get_versions упал: %s", e)
    return out


def rollback_strategy(strategy_id: int, version: int) -> bool:
    """
    Откат стратегии к указанной версии через workers.dialogue.versioning.
    """
    try:
        from workers.dialogue import versioning
    except Exception as e:
        logger.exception("Не удалось импортировать versioning: %s", e)
        return False
    try:
        if hasattr(versioning, "rollback_to_version"):
            return bool(versioning.rollback_to_version(strategy_id, version))
        if hasattr(versioning, "rollback"):
            return bool(versioning.rollback(strategy_id, version))
        logger.error("В versioning.py нет rollback*")
        return False
    except Exception as e:
        logger.exception("rollback_strategy(%s, %s) упал: %s", strategy_id, version, e)
        return False


# === СЛОВА / МОСТЫ / ПРОГОНЫ ===

def get_word_stats(limit: int = 20) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(WordStat)
                .where(WordStat.total_count > 0)
                .order_by(desc(WordStat.success_rate), desc(WordStat.total_count))
                .limit(lim)
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


def get_bridges(limit: int = 20) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(BridgeWeight).order_by(desc(BridgeWeight.weight)).limit(lim)
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


def get_bridge_history(bridge_token: str, limit: int = 50) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(BridgeHistory)
                .where(BridgeHistory.bridge_token == bridge_token)
                .order_by(desc(BridgeHistory.id))
                .limit(lim)
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


def get_runs(limit: int = 10) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(Run).order_by(desc(Run.started_at)).limit(lim)
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


def get_operator_actions(limit: int = 20) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(OperatorAction).order_by(desc(OperatorAction.id)).limit(lim)
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


# === PMI / ОЧИСТКА ===

def get_pmi_history(limit: int = 50) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(PMIHistory).order_by(desc(PMIHistory.id)).limit(lim)
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
    result = {"pmi_deleted": 0, "bridges_deleted": 0, "live_messages_deleted": 0}
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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
            except Exception as e:
                logger.warning("cleanup bridges_history пропущен: %s", e)

            try:
                lm_del = session.query(LiveMessage).filter(
                    LiveMessage.sent_at < cutoff
                ).delete(synchronize_session=False)
                result["live_messages_deleted"] = int(lm_del or 0)
            except Exception as e:
                logger.warning("cleanup live_messages пропущен: %s", e)

            session.commit()
    except Exception as e:
        logger.exception("cleanup_old_data упал: %s", e)
    return result


# === LIVE: ЧАТЫ ===

def get_live_chats(enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    try:
        with get_session() as session:
            stmt = select(MonitoredChat).order_by(
                desc(MonitoredChat.last_message_at),
                MonitoredChat.chat_id,
            )
            if enabled_only:
                stmt = stmt.where(MonitoredChat.enabled == 1)
            rows = session.execute(stmt).scalars().all()
            for c in rows:
                out.append({
                    "id": c.id,
                    "chat_id": c.chat_id,
                    "chat_title": c.chat_title,
                    "chat_type": c.chat_type,
                    "enabled": int(c.enabled or 0),
                    "auto_reply": int(c.auto_reply or 0),
                    "last_message_at": _iso(c.last_message_at),
                    "added_at": _iso(c.added_at),
                    "added_by": c.added_by,
                })
    except Exception as e:
        logger.exception("get_live_chats упал: %s", e)
    return out


def get_live_chat(chat_id: int) -> Optional[dict]:
    try:
        with get_session() as session:
            c = session.execute(
                select(MonitoredChat).where(MonitoredChat.chat_id == chat_id).limit(1)
            ).scalar_one_or_none()
            if c is None:
                return None
            return {
                "id": c.id,
                "chat_id": c.chat_id,
                "chat_title": c.chat_title,
                "chat_type": c.chat_type,
                "enabled": int(c.enabled or 0),
                "auto_reply": int(c.auto_reply or 0),
                "last_message_at": _iso(c.last_message_at),
                "added_at": _iso(c.added_at),
                "added_by": c.added_by,
            }
    except Exception as e:
        logger.exception("get_live_chat упал: %s", e)
        return None


def add_live_chat(
    chat_id: int,
    chat_title: Optional[str] = None,
    chat_type: Optional[str] = None,
    added_by: Optional[str] = None,
    enabled: int = 1,
    auto_reply: int = 0,
) -> Optional[dict]:
    """Через workers.live.monitor.add_chat."""
    try:
        from workers.live import monitor
        return monitor.add_chat(
            chat_id=chat_id,
            title=chat_title,
            chat_type=chat_type,
            added_by=added_by,
            enabled=enabled,
            auto_reply=auto_reply,
        )
    except Exception as e:
        logger.exception("add_live_chat упал: %s", e)
        return None


def remove_live_chat(chat_id: int) -> bool:
    try:
        from workers.live import monitor
        return bool(monitor.remove_chat(chat_id))
    except Exception as e:
        logger.exception("remove_live_chat упал: %s", e)
        return False


def set_live_chat_enabled(chat_id: int, enabled: bool) -> bool:
    try:
        from workers.live import monitor
        return bool(monitor.set_enabled(chat_id, enabled))
    except Exception as e:
        logger.exception("set_live_chat_enabled упал: %s", e)
        return False


def set_live_chat_auto_reply(chat_id: int, auto_reply: bool) -> bool:
    try:
        from workers.live import monitor
        return bool(monitor.set_auto_reply(chat_id, auto_reply))
    except Exception as e:
        logger.exception("set_live_chat_auto_reply упал: %s", e)
        return False


# === LIVE: СООБЩЕНИЯ ===

def _row_to_live_message(r: LiveMessage) -> dict:
    return {
        "id": r.id,
        "chat_id": r.chat_id,
        "message_id": r.message_id,
        "author_id": r.author_id,
        "author_username": r.author_username,
        "author_first_name": r.author_first_name,
        "text": r.text,
        "is_outgoing": int(r.is_outgoing or 0),
        "reply_to_msg_id": r.reply_to_msg_id,
        "sent_at": _iso(r.sent_at),
        "imported_at": _iso(r.imported_at),
        "read_by_operator": int(r.read_by_operator or 0),
        "llm_suggestion": r.llm_suggestion,
        "llm_suggested_at": _iso(r.llm_suggested_at),
        "operator_reply": r.operator_reply,
        "operator_replied_at": _iso(r.operator_replied_at),
    }


def get_live_messages(
    chat_id: int,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    reverse: bool = False,
) -> list[dict]:
    """
    История сообщений чата.
    reverse=False (по умолчанию) — свежие первыми (для API).
    reverse=True — хронологический порядок (для UI чата).
    """
    lim, off = _clamp(limit, offset)
    out: list[dict] = []
    try:
        with get_session() as session:
            if reverse:
                # Берём последние lim+off и разворачиваем
                stmt = (
                    select(LiveMessage)
                    .where(LiveMessage.chat_id == chat_id)
                    .order_by(desc(LiveMessage.sent_at), desc(LiveMessage.id))
                    .limit(lim + off)
                )
                rows = session.execute(stmt).scalars().all()
                rows = list(reversed(rows))[off: off + lim]
            else:
                stmt = (
                    select(LiveMessage)
                    .where(LiveMessage.chat_id == chat_id)
                    .order_by(desc(LiveMessage.sent_at), desc(LiveMessage.id))
                    .limit(lim)
                    .offset(off)
                )
                rows = session.execute(stmt).scalars().all()

            for r in rows:
                out.append(_row_to_live_message(r))
    except Exception as e:
        logger.exception("get_live_messages упал: %s", e)
    return out


def get_live_message(chat_id: int, message_id: int) -> Optional[dict]:
    try:
        with get_session() as session:
            r = session.execute(
                select(LiveMessage)
                .where(LiveMessage.chat_id == chat_id)
                .where(LiveMessage.message_id == message_id)
                .limit(1)
            ).scalar_one_or_none()
            return _row_to_live_message(r) if r is not None else None
    except Exception as e:
        logger.exception("get_live_message упал: %s", e)
        return None


def mark_live_chat_read(chat_id: int) -> int:
    """Помечает все входящие сообщения чата read_by_operator=1. Возвращает кол-во."""
    try:
        with get_session() as session:
            n = session.query(LiveMessage).filter(
                LiveMessage.chat_id == chat_id,
                LiveMessage.read_by_operator == 0,
                LiveMessage.is_outgoing == 0,
            ).update({LiveMessage.read_by_operator: 1}, synchronize_session=False)
            session.commit()
            return int(n or 0)
    except Exception as e:
        logger.exception("mark_live_chat_read упал: %s", e)
        return 0


def count_unread_live_messages() -> int:
    try:
        with get_session() as session:
            return int(session.execute(
                select(func.count(LiveMessage.id))
                .where(LiveMessage.read_by_operator == 0)
                .where(LiveMessage.is_outgoing == 0)
            ).scalar_one())
    except Exception as e:
        logger.exception("count_unread_live_messages упал: %s", e)
        return 0


# === LIVE: ОТПРАВКА / ПОДСКАЗКА (POST) ===

async def send_live_message(
    chat_id: int,
    text: str,
    reply_to_msg_id: Optional[int] = None,
    operator_id: Optional[int] = None,
) -> dict:
    """
    Отправка через workers.live.sender.send_operator_reply.
    Возвращает {"ok": bool, "message_id": int|None, "error": str|None}.
    """
    try:
        from workers.live import sender
    except Exception as e:
        logger.exception("Не удалось импортировать workers.live.sender: %s", e)
        return {"ok": False, "message_id": None, "error": f"sender unavailable: {e}"}

    try:
        return await sender.send_operator_reply(
            chat_id=chat_id,
            text=text,
            reply_to_msg_id=reply_to_msg_id,
            operator_id=operator_id,
        )
    except Exception as e:
        logger.exception("send_live_message упал: %s", e)
        return {"ok": False, "message_id": None, "error": str(e)}


async def suggest_live_reply(chat_id: int, message_id: int) -> Optional[str]:
    """
    Генерация LLM-подсказки через workers.live.suggest.generate_suggestion.
    """
    try:
        from workers.live import suggest
    except Exception as e:
        logger.exception("Не удалось импортировать workers.live.suggest: %s", e)
        return None
    try:
        return await suggest.generate_suggestion(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.exception("suggest_live_reply упал: %s", e)
        return None


# === LIVE: СЕССИИ ===

def get_live_sessions(limit: int = 10) -> list[dict]:
    lim, _ = _clamp(limit, 0)
    out: list[dict] = []
    try:
        with get_session() as session:
            rows = session.execute(
                select(LiveSession).order_by(desc(LiveSession.id)).limit(lim)
            ).scalars().all()
            for s in rows:
                out.append({
                    "id": s.id,
                    "started_at": _iso(s.started_at),
                    "stopped_at": _iso(s.stopped_at),
                    "status": s.status,
                    "messages_received": s.messages_received,
                    "messages_sent": s.messages_sent,
                    "error_text": s.error_text,
                })
    except Exception as e:
        logger.exception("get_live_sessions упал: %s", e)
    return out


# === СВОДКА ДЛЯ ДАШБОРДА ===

def get_dashboard_summary() -> dict:
    return {
        "status": get_system_status(),
        "stats": get_overall_stats(),
        "runs": get_runs(limit=5),
        "unread_live": count_unread_live_messages(),
    }
