"""Мосты между группами токенов.

«Мост» — это токен, который переводит разговор из одной группы токенов
в другую. Например, из curiosity в objection: клиент спросил «сколько
стоит», услышал цену, сказал «дорого» — «дорого» это мост.

bridges_history — лог всех переходов (с флагом success).
bridge_weights — агрегированные веса мостов (success_rate).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from core.config import settings
from core.database import (
    BridgeHistory,
    BridgeWeight,
    get_session,
)
from workers.dialogue.prompt_builder import load_token_groups

log = logging.getLogger("dialogue.bridges")
log.setLevel(settings.log_level.upper())


# ============================================================
# Правила классификации успеха
# ============================================================
SUCCESS_GROUPS = {"agreement", "closing"}
FAIL_GROUPS = {"objection", "greeting"}
NEUTRAL_GROUPS = {"curiosity"}


@dataclass
class Transition:
    """Один переход между группами токенов."""
    bridge_token: str          # токен, который «сработал мостом»
    from_group: str
    to_group: str
    success: Optional[bool]    # True / False / None
    message_idx: int           # индекс сообщения, в котором найден токен


# ============================================================
# Определение группы токена
# ============================================================
def _group_of_token(token: str) -> Optional[str]:
    """Возвращает имя группы, к которой относится токен (lowercase)."""
    token = (token or "").strip().lower()
    if not token:
        return None
    groups = load_token_groups()
    for name, spec in groups.items():
        tokens = [t.lower() for t in (spec.get("tokens") or [])]
        if token in tokens:
            return name
    return None


def _tokens_in_text(text: str) -> list[str]:
    """Возвращает токены, найденные в тексте (по всем группам)."""
    text_low = (text or "").lower()
    if not text_low:
        return []
    found: list[str] = []
    groups = load_token_groups()
    for spec in groups.values():
        for t in (spec.get("tokens") or []):
            tl = t.lower()
            idx = text_low.find(tl)
            if idx == -1:
                continue
            before_ok = idx == 0 or not text_low[idx - 1].isalpha()
            after_ok = (idx + len(tl) >= len(text_low)) or not text_low[idx + len(tl)].isalpha()
            if before_ok and after_ok and t not in found:
                found.append(t)
    return found


# ============================================================
# Классификация успеха
# ============================================================
def classify_success(from_group: str, to_group: str) -> Optional[bool]:
    """Определяет, был ли переход успешным.

    → agreement / closing — успех (True)
    → objection / greeting — неудача (False)
    → curiosity — нейтрально (None)
    """
    if to_group in SUCCESS_GROUPS:
        return True
    if to_group in FAIL_GROUPS:
        return False
    if to_group in NEUTRAL_GROUPS:
        return None
    # неизвестная группа — нейтрально
    return None


# ============================================================
# Определение переходов
# ============================================================
def detect_transitions(messages: list[dict[str, Any]]) -> list[Transition]:
    """Проходит по сообщениям, определяет группу каждого «значимого» токена
    и фиксирует переходы, когда группа меняется.

    Возвращает список Transition.
    """
    transitions: list[Transition] = []
    last_group: Optional[str] = None
    last_idx: int = -1

    for idx, m in enumerate(messages):
        text = m.get("message") or ""
        tokens = _tokens_in_text(text)
        if not tokens:
            continue
        # Группа этого сообщения — по первому найденному токену
        # (или по максимальному order, если несколько)
        groups_map = load_token_groups()
        candidates: list[tuple[int, str, str]] = []  # (order, group, token)
        for t in tokens:
            g = _group_of_token(t)
            if g is None:
                continue
            order = int(groups_map.get(g, {}).get("order", 0))
            candidates.append((order, g, t))
        if not candidates:
            continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        order, group, token = candidates[0]

        if last_group is not None and group != last_group:
            success = classify_success(last_group, group)
            transitions.append(
                Transition(
                    bridge_token=token,
                    from_group=last_group,
                    to_group=group,
                    success=success,
                    message_idx=idx,
                )
            )
        last_group = group
        last_idx = idx

    return transitions


# ============================================================
# Запись и обновление весов
# ============================================================
def _get_or_create_bridge_weight(session, bridge_token: str,
                                 from_group: str, to_group: str) -> BridgeWeight:
    row = (
        session.query(BridgeWeight)
        .filter_by(bridge_token=bridge_token, from_group=from_group, to_group=to_group)
        .one_or_none()
    )
    if row is None:
        row = BridgeWeight(
            bridge_token=bridge_token,
            from_group=from_group,
            to_group=to_group,
            weight=0.0,
            success_count=0,
            total_count=0,
        )
        session.add(row)
        session.flush()
    return row


def update_bridge_weight(bridge_token: str, from_group: str, to_group: str,
                         success: Optional[bool]) -> tuple[float, float]:
    """Обновляет вес моста. Возвращает (weight_before, weight_after)."""
    try:
        with get_session() as s:
            row = _get_or_create_bridge_weight(s, bridge_token, from_group, to_group)
            before = float(row.weight or 0.0)
            row.total_count = (row.total_count or 0) + 1
            if success is True:
                row.success_count = (row.success_count or 0) + 1
            total = row.total_count or 1
            after = float(row.success_count or 0) / float(total)
            row.weight = after
            return before, after
    except Exception as exc:
        log.exception("update_bridge_weight failed: %s", exc)
        return 0.0, 0.0


def record_bridges(dialogue_id: str, transitions: Iterable[Transition]) -> int:
    """Записывает переходы в bridges_history и обновляет bridge_weights."""
    transitions = list(transitions)
    if not transitions:
        return 0

    written = 0
    try:
        with get_session() as s:
            for tr in transitions:
                # 1. Обновить агрегат bridge_weights
                row = _get_or_create_bridge_weight(s, tr.bridge_token, tr.from_group, tr.to_group)
                before = float(row.weight or 0.0)
                row.total_count = (row.total_count or 0) + 1
                if tr.success is True:
                    row.success_count = (row.success_count or 0) + 1
                total = row.total_count or 1
                after = float(row.success_count or 0) / float(total)
                row.weight = after

                # 2. Записать событие
                rec = BridgeHistory(
                    bridge_token=tr.bridge_token,
                    from_group=tr.from_group,
                    to_group=tr.to_group,
                    dialogue_id=dialogue_id,
                    success=tr.success,
                    weight_before=before,
                    weight_after=after,
                )
                s.add(rec)
                written += 1
    except Exception as exc:
        log.exception("record_bridges failed: %s", exc)
        return 0

    log.info("bridges recorded: %d transitions for dialogue %s", written, dialogue_id)
    return written


# ============================================================
# Отчёты
# ============================================================
def get_top_bridges(n: int = 20) -> list[dict[str, Any]]:
    """Топ мостов по весу, потом по total_count."""
    try:
        with get_session() as s:
            rows = (
                s.query(BridgeWeight)
                .order_by(BridgeWeight.weight.desc(), BridgeWeight.total_count.desc())
                .limit(n)
                .all()
            )
            return [
                {
                    "bridge_token": r.bridge_token,
                    "from_group": r.from_group,
                    "to_group": r.to_group,
                    "weight": r.weight,
                    "success_count": r.success_count,
                    "total_count": r.total_count,
                }
                for r in rows
            ]
    except Exception as exc:
        log.exception("get_top_bridges failed: %s", exc)
        return []


def get_bridges_from(group: str, n: int = 20) -> list[dict[str, Any]]:
    """Мосты, исходящие из указанной группы."""
    try:
        with get_session() as s:
            rows = (
                s.query(BridgeWeight)
                .filter(BridgeWeight.from_group == group)
                .order_by(BridgeWeight.weight.desc(), BridgeWeight.total_count.desc())
                .limit(n)
                .all()
            )
            return [
                {
                    "bridge_token": r.bridge_token,
                    "from_group": r.from_group,
                    "to_group": r.to_group,
                    "weight": r.weight,
                    "success_count": r.success_count,
                    "total_count": r.total_count,
                }
                for r in rows
            ]
    except Exception as exc:
        log.exception("get_bridges_from failed: %s", exc)
        return []


def cleanup_old_bridges(days: int = 90) -> int:
    """Удаляет bridges_history старше N дней. Возвращает число удалённых."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        with get_session() as s:
            deleted = (
                s.query(BridgeHistory)
                .filter(BridgeHistory.created_at < cutoff)
                .delete(synchronize_session=False)
            )
        log.info("cleanup_old_bridges: deleted %d records (older than %d days)", deleted, days)
        return int(deleted or 0)
    except Exception as exc:
        log.exception("cleanup_old_bridges failed: %s", exc)
        return 0


# ============================================================
# Тест
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sample_messages = [
        {"author_username": "client", "message": "Привет", "is_target": False, "author_id": 1},
        {"author_username": "manager", "message": "Здравствуйте! Чем могу помочь?", "is_target": False, "author_id": 2},
        {"author_username": "client", "message": "Расскажите про товар, интересно", "is_target": False, "author_id": 1},
        {"author_username": "manager", "message": "Наш товар — ...", "is_target": False, "author_id": 2},
        {"author_username": "client", "message": "Дорого как-то", "is_target": False, "author_id": 1},
        {"author_username": "manager", "message": "Есть скидка...", "is_target": False, "author_id": 2},
        {"author_username": "client", "message": "Ок, беру", "is_target": True, "author_id": 1},
    ]

    print("=== detect_transitions ===")
    transitions = detect_transitions(sample_messages)
    for tr in transitions:
        print(f"  {tr.from_group:10s} -> {tr.to_group:10s} | token='{tr.bridge_token}' | success={tr.success}")

    print("\n=== record_bridges ===")
    n = record_bridges("test_dialogue_001", transitions)
    print(f"written {n} records")

    print("\n=== top bridges ===")
    for row in get_top_bridges(10):
        print(row)

    print("\n=== bridges from 'objection' ===")
    for row in get_bridges_from("objection", 5):
        print(row)
