"""Ядро диалогового агента.

Принимает JSON-тред от dialogues-miner, определяет роль клиента,
уровень агрессии, текущий этап диалога, строит промпт, вызывает LLM,
оценивает результат (score_dialogue + score_response), обновляет
статистику, мосты, версии стратегий, пишет processed_dialogues.

Фазы:
  - bootstrap: обучаемся на завершённых успешных тредах.
               score_dialogue = ступенчатая шкала от cosine(expected_text, actual)
  - online (позже): score_dialogue = этап клиента после нашего ответа
  - hybrid (позже): комбинированный
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.config import settings
from core.database import ProcessedDialogue, get_session
from core.embeddings import get_embedder
from core.llm import ChatMessage, get_llm_client
from workers.dialogue import bridges, statistics, versioning
from workers.dialogue.prompt_builder import (
    build_prompt,
    detect_stage,
    load_token_groups,
)

log = logging.getLogger("dialogue.agent")
log.setLevel(settings.log_level.upper())


# ============================================================
# Фаза
# ============================================================
def get_phase() -> str:
    """Возвращает текущую фазу.

    Приоритет:
      1. settings.phase (из .env, если задан)
      2. configs/phase.json
      3. "bootstrap" (дефолт)
    """
    if settings.phase:
        return settings.phase.strip().lower()

    try:
        path = settings.phase_file
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("phase") or "bootstrap").lower()
    except Exception as exc:
        log.warning("Failed to read phase.json: %s", exc)

    return "bootstrap"


# ============================================================
# Определение клиента / менеджера
# ============================================================
def _message_author_key(m: dict[str, Any]) -> str:
    """Стабильный идентификатор автора сообщения."""
    for k in ("author_id", "author_username", "author_first_name"):
        v = m.get(k)
        if v:
            return str(v)
    return "unknown"


def detect_client_author(messages: list[dict[str, Any]]) -> Optional[str]:
    """Клиент — тот, кто сказал target-слово. Возвращает author_key."""
    for m in reversed(messages):
        if m.get("is_target"):
            return _message_author_key(m)
    return None


def get_expected_text(
    messages: list[dict[str, Any]],
    client_key: Optional[str],
) -> str:
    """Последнее сообщение менеджера (не клиента) перед target-словом.

    Если менеджер не найден — пустая строка.
    """
    # Найдём индекс target-сообщения
    target_idx = -1
    for i, m in enumerate(messages):
        if m.get("is_target"):
            target_idx = i
            break

    # Идём назад от target_idx, ищем сообщение не от клиента
    if target_idx > 0:
        for i in range(target_idx - 1, -1, -1):
            m = messages[i]
            if client_key is not None and _message_author_key(m) == client_key:
                continue
            txt = (m.get("message") or "").strip()
            if txt:
                return txt

    # Если target не найден — просто последнее сообщение не от клиента
    for m in reversed(messages):
        if client_key is not None and _message_author_key(m) == client_key:
            continue
        txt = (m.get("message") or "").strip()
        if txt:
            return txt
    return ""


def get_target_word(messages: list[dict[str, Any]]) -> str:
    """Возвращает само target-слово (текст сообщения с is_target=True)."""
    for m in reversed(messages):
        if m.get("is_target"):
            return (m.get("message") or "").strip()
    return ""


# ============================================================
# Определение роли клиента
# ============================================================
_CURIOSITY_WORDS = ("как", "почему", "расскаж", "а что", "зачем", "интересно", "подробнее", "какой")
_OBJECTION_WORDS = ("дорого", "не верю", "сомнева", "не подходит", "нет", "вряд ли", "не увер")
_QUESTION_MARK = "?"


def detect_role_heuristic(messages: list[dict[str, Any]], client_key: Optional[str]) -> tuple[str, float]:
    """Эвристика определения роли. Возвращает (role, confidence 0..1)."""
    if client_key is None:
        return "buyer", 0.3

    client_msgs = [
        (m.get("message") or "").lower()
        for m in messages
        if _message_author_key(m) == client_key
    ]
    text = " ".join(client_msgs)

    has_curiosity = any(w in text for w in _CURIOSITY_WORDS) or _QUESTION_MARK in text
    has_objection = any(w in text for w in _OBJECTION_WORDS)
    client_msg_count = len(client_msgs)

    if has_objection and has_curiosity:
        return "skeptic", 0.6
    if has_objection:
        return "skeptic", 0.8
    if has_curiosity:
        return "curious", 0.8
    if client_msg_count <= 2:
        return "buyer", 0.5
    return "buyer", 0.6


def detect_role_llm(messages: list[dict[str, Any]], client_key: Optional[str]) -> Optional[str]:
    """LLM-fallback для определения роли. Возвращает role или None."""
    if client_key is None:
        return None
    client_text = " ".join(
        (m.get("message") or "")
        for m in messages
        if _message_author_key(m) == client_key
    ).strip()
    if not client_text:
        return None

    system = (
        "Ты классифицируешь роль клиента в диалоге по одному из четырёх типов: "
        "buyer (готов покупать), seller (продавец), curious (интересующийся, задаёт вопросы), "
        "skeptic (скептик, возражает). Отвечай ОДНИМ словом: buyer, seller, curious или skeptic."
    )
    try:
        llm = get_llm_client()
        resp = llm.chat_sync(
            messages=[ChatMessage(role="user", content=f"Реплики клиента:\n{client_text}")],
            system=system,
            temperature=0.0,
            max_tokens=5,
        )
        word = (resp.text or "").strip().lower().split()[0] if resp.text else ""
        word = word.strip(".,;:!?\"'")
        if word in ("buyer", "seller", "curious", "skeptic"):
            return word
    except Exception as exc:
        log.warning("detect_role_llm failed: %s", exc)
    return None


def detect_role(messages: list[dict[str, Any]], client_key: Optional[str]) -> str:
    role, conf = detect_role_heuristic(messages, client_key)
    if conf >= 0.7:
        return role
    llm_role = detect_role_llm(messages, client_key)
    if llm_role:
        return llm_role
    return role


# ============================================================
# Определение агрессии
# ============================================================
_NEGATIVE_WORDS = ("нет", "не ", "никогда", "ужасн", "отврат", "плохо", "обман", "развод")
_STRONG_WORDS = ("!", "должен", "обязан", "требую", "немедленно")


def detect_aggression_heuristic(messages: list[dict[str, Any]], client_key: Optional[str]) -> tuple[float, str]:
    """Возвращает (score 0..1, уровень low|medium|high)."""
    if client_key is None:
        return 0.3, "low"

    client_text = " ".join(
        (m.get("message") or "")
        for m in messages
        if _message_author_key(m) == client_key
    )
    if not client_text:
        return 0.3, "low"

    low = client_text.lower()
    score = 0.3

    # CAPS — если больше 40% букв заглавные
    letters = [c for c in client_text if c.isalpha()]
    if letters:
        caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if caps_ratio > 0.4:
            score += 0.3

    # Восклицательные знаки
    excl = client_text.count("!")
    if excl >= 2:
        score += 0.2
    elif excl == 1:
        score += 0.1

    # Негативные слова
    if any(w in low for w in _NEGATIVE_WORDS):
        score += 0.2

    # Сильные слова
    if any(w in low for w in _STRONG_WORDS):
        score += 0.2

    score = max(0.0, min(1.0, score))

    if score < 0.3:
        level = "low"
    elif score < 0.7:
        level = "medium"
    else:
        level = "high"
    return score, level


def detect_aggression_llm(messages: list[dict[str, Any]], client_key: Optional[str]) -> Optional[str]:
    """LLM-fallback. Возвращает low|medium|high или None."""
    if client_key is None:
        return None
    text = " ".join(
        (m.get("message") or "")
        for m in messages
        if _message_author_key(m) == client_key
    ).strip()
    if not text:
        return None

    system = (
        "Оцени уровень агрессии/негатива клиента в диалоге. "
        "Отвечай ОДНИМ словом: low (нет агрессии), medium (умеренное недовольство), "
        "high (явная агрессия, резкость)."
    )
    try:
        llm = get_llm_client()
        resp = llm.chat_sync(
            messages=[ChatMessage(role="user", content=text)],
            system=system,
            temperature=0.0,
            max_tokens=5,
        )
        word = (resp.text or "").strip().lower().split()[0] if resp.text else ""
        word = word.strip(".,;:!?\"'")
        if word in ("low", "medium", "high"):
            return word
    except Exception as exc:
        log.warning("detect_aggression_llm failed: %s", exc)
    return None


def detect_aggression(messages: list[dict[str, Any]], client_key: Optional[str]) -> str:
    score, level = detect_aggression_heuristic(messages, client_key)
    # Если score неоднозначный — уточняем через LLM
    if 0.3 <= score <= 0.7:
        llm_level = detect_aggression_llm(messages, client_key)
        if llm_level:
            return llm_level
    return level


# ============================================================
# Score
# ============================================================
REFUSAL_PATTERNS = ("не могу", "не знаю", "извините, я не", "как ии", "как языковая модель")


def compute_score_dialogue_bootstrap(cosine_value: Optional[float]) -> Optional[float]:
    """Bootstrap-фаза: score_dialogue = ступенчатая шкала от cosine."""
    if cosine_value is None:
        return None
    c = float(cosine_value)
    if c >= 0.85:
        return 1.0
    if c >= 0.70:
        return 0.7
    if c >= 0.50:
        return 0.5
    if c >= 0.30:
        return 0.3
    return 0.0


def compute_score_dialogue_online(stage_after: Optional[str]) -> Optional[float]:
    """Online-фаза: score_dialogue по этапу клиента после ответа."""
    if stage_after is None:
        return None
    if stage_after in ("agreement", "closing"):
        return 0.7
    if stage_after == "curiosity":
        return 0.5
    if stage_after == "objection":
        return 0.3
    if stage_after == "greeting":
        return 0.0
    return None


def compute_score_response(actual: str, last_client_message: str, finish_reason: Optional[str]) -> float:
    """Технический score ответа LLM. 0 / 0.5 / 1.0."""
    actual_low = (actual or "").lower()
    checks = [
        len(actual or "") >= 5,
        (actual or "").strip().lower() != (last_client_message or "").strip().lower(),
        not any(x in actual_low for x in REFUSAL_PATTERNS),
        finish_reason == "stop",
    ]
    passed = sum(1 for c in checks if c)
    if passed == 4:
        return 1.0
    if passed >= 2:
        return 0.5
    return 0.0


def combine_score(score_dialogue: Optional[float], score_response: float) -> float:
    """Комбинированный score."""
    if score_dialogue is not None:
        return 0.7 * float(score_dialogue) + 0.3 * float(score_response)
    return float(score_response)


# ============================================================
# Главная функция
# ============================================================
async def process_dialogue(
    dialogue_json: dict[str, Any],
    *,
    run_llm: bool = True,
) -> dict[str, Any]:
    """Обрабатывает один тред.

    Возвращает result_json (тот же, что пишется в processed_dialogues).
    """
    t_start = time.perf_counter()
    phase = get_phase()

    messages = dialogue_json.get("messages") or []
    seed_id = dialogue_json.get("seed_id")
    sender = dialogue_json.get("sender") or "unknown"
    dialogue_id = f"{sender}_{seed_id}" if seed_id is not None else sender

    log.info("process_dialogue: id=%s, phase=%s, msgs=%d", dialogue_id, phase, len(messages))

    # 1. Клиент
    client_key = detect_client_author(messages)
    if client_key is None:
        log.warning("process_dialogue: не нашли target-слово в %s", dialogue_id)

    # 2. Роль, агрессия
    role = detect_role(messages, client_key)
    aggression = detect_aggression(messages, client_key)

    # 3. Этап
    stage_before, prev_stage, per_group = detect_stage(messages)
    target_word = get_target_word(messages)
    expected_text = get_expected_text(messages, client_key)

    # 4. last_client_message = последнее сообщение клиента, НЕ считая target
    last_client_msg = ""
    for m in reversed(messages):
        if client_key is not None and _message_author_key(m) != client_key:
            continue
        if m.get("is_target"):
            continue
        txt = (m.get("message") or "").strip()
        if txt:
            last_client_msg = txt
            break
    if not last_client_msg:
        # если единственное сообщение клиента — это target, значит просто пусто
        last_client_msg = ""

    # 5. Промпт
    try:
        bp = build_prompt(
            messages,
            role=role,
            aggression_level=aggression,
            token_group=stage_before,
            stage=stage_before,
        )
    except Exception as exc:
        log.exception("build_prompt failed: %s", exc)
        return {
            "dialogue_id": dialogue_id,
            "error": f"build_prompt failed: {exc}",
            "phase": phase,
            "score": 0.0,
            "score_dialogue": None,
            "score_response": 0.0,
            "actual": None,
            "expected_text": expected_text,
            "expected_word": target_word,
        }

    prompt_text = bp.prompt_text
    version_id = bp.version_id
    token_group = bp.context.get("token_group") if bp.context else stage_before

    # 6. LLM
    actual = ""
    tokens_in = 0
    tokens_out = 0
    finish_reason: Optional[str] = None
    duration_ms = 0.0

    if run_llm:
        try:
            llm = get_llm_client()
            params = (bp.context or {}).get("params") or {}
            temperature = float(params.get("temperature", 0.7))
            max_tokens = params.get("max_tokens")
            resp = await llm.chat(
                messages=bp.messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            actual = resp.text or ""
            tokens_in = resp.tokens_in
            tokens_out = resp.tokens_out
            finish_reason = resp.finish_reason
            duration_ms = resp.latency_ms
        except Exception as exc:
            log.exception("LLM call failed: %s", exc)
            actual = ""
            finish_reason = "error"

    # 7. Метрики
    emb = get_embedder()
    cosine_value: Optional[float] = None
    similarity_to_target: Optional[float] = None

    try:
        if expected_text and actual:
            cosine_value = emb.cosine(emb.encode(expected_text), emb.encode(actual))
        if target_word and actual:
            similarity_to_target = emb.cosine(emb.encode(target_word), emb.encode(actual))
    except Exception as exc:
        log.exception("Embedding failed: %s", exc)

    # 8. score_dialogue по фазе
    if phase == "online":
        score_dialogue = compute_score_dialogue_online(stage_before)  # TODO: stage_after в online
    else:  # bootstrap или hybrid — используем bootstrap
        score_dialogue = compute_score_dialogue_bootstrap(cosine_value)

    # 9. score_response
    score_response = compute_score_response(actual, last_client_msg, finish_reason)

    # 10. Итоговый score
    score = combine_score(score_dialogue, score_response)

    # 11. word_stats + connection_matrix
    try:
        tokens_found: list[str] = []
        for grp in (per_group or {}).values():
            tokens_found.extend(grp)
        tokens_found = sorted(set(t.lower() for t in tokens_found))
        if tokens_found:
            statistics.update_word_stats(tokens_found, success=(score >= 0.7))
            statistics.update_connection_matrix(tokens_found)
    except Exception as exc:
        log.exception("statistics update failed: %s", exc)

    # 12. bridges
    try:
        transitions = bridges.detect_transitions(messages)
        if transitions:
            bridges.record_bridges(dialogue_id, transitions)
    except Exception as exc:
        log.exception("bridges update failed: %s", exc)

    # 13. top_tokens (из word_stats, топ 10)
    try:
        top_tokens_raw = statistics.get_top_tokens(10, by="success_rate")
        top_tokens = [r["word"] for r in top_tokens_raw]
    except Exception:
        top_tokens = []

    # 14. result_json
    duration_ms_total = (time.perf_counter() - t_start) * 1000.0
    result = {
        "prompt": prompt_text,
        "response": actual,
        "expected_text": expected_text,
        "expected_word": target_word,
        "actual": actual,
        "score": round(score, 4),
        "score_dialogue": round(score_dialogue, 4) if score_dialogue is not None else None,
        "score_response": round(score_response, 4),
        "phase": phase,
        "role": role,
        "aggression": aggression,
        "token_group": token_group,
        "stage_before": stage_before,
        "stage_after": stage_before,  # bootstrap: неизвестен, оставим тот же
        "top_tokens": top_tokens,
        "cosine": round(cosine_value, 4) if cosine_value is not None else None,
        "similarity_to_target": round(similarity_to_target, 4) if similarity_to_target is not None else None,
        "bridge_used": None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "finish_reason": finish_reason,
        "duration_ms": round(duration_ms_total, 1),
        "version_id": version_id,
    }

    # 15. Записать в processed_dialogues
    try:
        with get_session() as s:
            existing = s.query(ProcessedDialogue).filter_by(dialogue_id=dialogue_id).one_or_none()
            if existing is None:
                rec = ProcessedDialogue(
                    dialogue_id=dialogue_id,
                    sender=sender,
                    seed_id=seed_id,
                    status="done",
                    result_json=result,
                )
                s.add(rec)
            else:
                existing.status = "done"
                existing.result_json = result
                existing.error = None
    except Exception as exc:
        log.exception("Failed to save processed_dialogue: %s", exc)

    # 16. Триггеры snapshot / auto-version
    try:
        statistics.maybe_snapshot()
    except Exception as exc:
        log.exception("maybe_snapshot failed: %s", exc)

    try:
        versioning.maybe_auto_version()
    except Exception as exc:
        log.exception("maybe_auto_version failed: %s", exc)

    log.info(
        "process_dialogue done: id=%s score=%.3f dialogue=%.3f response=%.3f cosine=%s",
        dialogue_id, score,
        score_dialogue if score_dialogue is not None else -1.0,
        score_response,
        cosine_value,
    )
    return result


# ============================================================
# Обработка файла
# ============================================================
async def process_file(path: Path, *, run_llm: bool = True) -> dict[str, Any]:
    """Читает JSON-файл треда, обрабатывает, возвращает result_json."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.exception("Failed to read %s: %s", path, exc)
        return {"error": str(exc), "file": str(path)}

    try:
        return await process_dialogue(data, run_llm=run_llm)
    except Exception as exc:
        log.exception("process_file %s failed: %s", path, exc)
        return {"error": str(exc), "file": str(path)}


# ============================================================
# Тест
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sample = {
        "seed_id": 99999,
        "sender": "test_channel",
        "target_author_username": "client1",
        "messages": [
            {"author_id": 1, "author_username": "client1", "message": "Привет", "is_target": False},
            {"author_id": 2, "author_username": "manager", "message": "Здравствуйте! Готов помочь.", "is_target": False},
            {"author_id": 1, "author_username": "client1", "message": "Расскажите про товар, интересно", "is_target": False},
            {"author_id": 2, "author_username": "manager", "message": "Наш товар — ...", "is_target": False},
            {"author_id": 1, "author_username": "client1", "message": "Сколько стоит?", "is_target": False},
            {"author_id": 2, "author_username": "manager", "message": "Цена — 1000 руб.", "is_target": False},
            {"author_id": 1, "author_username": "client1", "message": "ок", "is_target": True},
        ],
    }
    result = asyncio.run(process_dialogue(sample))
    print("\n=== result_json ===")
    for k, v in result.items():
        if k == "prompt":
            print(f"  {k}: {str(v)[:80]}...")
        else:
            print(f"  {k}: {v}")
