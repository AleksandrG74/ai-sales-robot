"""Сборщик промпта для dialogue-worker.

Читает конфиги (product, goal, token_groups), определяет текущий этап
диалога, находит активную версию стратегии в БД по ключу
(token_group, role, aggression_level) и собирает готовые messages
для LLM.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from core.config import settings
from core.database import Strategy, StrategyVersion, get_session

log = logging.getLogger("dialogue.prompt_builder")
log.setLevel(settings.log_level.upper())


# ============================================================
# Dataclass
# ============================================================
@dataclass
class BuiltPrompt:
    messages: list[dict[str, str]]
    system: str
    prompt_text: str
    strategy_id: Optional[int]
    version_id: Optional[int]
    stage: Optional[str]
    prev_stage: Optional[str]
    context: dict[str, Any] = field(default_factory=dict)


# ============================================================
# Default prompt template
# ============================================================
DEFAULT_PROMPT_TEMPLATE = (
    "Ты — опытный менеджер по продажам. Твоя задача — {goal}.\n"
    "\n"
    "## Что мы продаём\n"
    "Название: {product_name}\n"
    "Описание: {product_description}\n"
    "Цена: {product_price} {product_currency}\n"
    "{product_url_line}"
    "\n"
    "## Цель диалога\n"
    "{goal}\n"
    "Критерий успеха: {success_criteria}\n"
    "Тон общения: {tone}\n"
    "\n"
    "## Контекст\n"
    "Роль клиента: {role}\n"
    "Уровень агрессии: {aggression_level}\n"
    "Текущий этап диалога: {stage}\n"
    "Предыдущий этап: {prev_stage}\n"
    "\n"
    "## История диалога\n"
    "{dialogue_history}\n"
    "\n"
    "## Последнее сообщение клиента\n"
    "{last_client_message}\n"
    "\n"
    "## Твоя задача\n"
    "Ответь на последнее сообщение клиента. Учитывай текущий этап диалога. "
    "Отвечай кратко, по делу, в дружелюбном тоне. "
    "Не выдумывай факты о товаре, которых нет в описании."
)


# ============================================================
# Загрузка конфигов
# ============================================================
def _load_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("Config not found: %s", path)
        return {}
    except json.JSONDecodeError as exc:
        log.error("Bad JSON in %s: %s", path, exc)
        return {}


@lru_cache(maxsize=1)
def load_product() -> dict[str, Any]:
    return _load_json(settings.configs_dir / "product.json")


@lru_cache(maxsize=1)
def load_goal() -> dict[str, Any]:
    return _load_json(settings.configs_dir / "goal.json")


@lru_cache(maxsize=1)
def load_token_groups() -> dict[str, Any]:
    data = _load_json(settings.configs_dir / "token_groups.json")
    return data.get("groups", {})


def clear_config_cache() -> None:
    load_product.cache_clear()
    load_goal.cache_clear()
    load_token_groups.cache_clear()


# ============================================================
# Определение этапа диалога
# ============================================================
def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _tokens_in_text(text: str, tokens: list[str]) -> list[str]:
    """Возвращает токены, которые встречаются в тексте (после нормализации)."""
    low = _normalize(text)
    if not low:
        return []
    found: list[str] = []
    for t in tokens:
        tl = t.lower()
        if not tl:
            continue
        # простое вхождение подстроки с границами по не-буквам
        idx = low.find(tl)
        if idx == -1:
            continue
        # Проверим границы (не буква перед/после)
        before_ok = idx == 0 or not low[idx - 1].isalpha()
        after_ok = (idx + len(tl) >= len(low)) or not low[idx + len(tl)].isalpha()
        if before_ok and after_ok:
            found.append(t)
    return found


def detect_stage(messages: list[dict[str, Any]]) -> tuple[Optional[str], Optional[str], dict[str, list[str]]]:
    """Определяет текущий и предыдущий этап диалога по сообщениям.

    Возвращает (current_stage, prev_stage, per_group_found_tokens).
    Текущий этап — группа с максимальным order, где найдены токены.
    Предыдущий — вторая по order группа с найденными токенами.
    """
    groups = load_token_groups()
    if not groups:
        return None, None, {}

    # Собираем все тексты (и клиента, и всех — нам нужна общая картина)
    all_texts: list[str] = []
    for m in messages:
        txt = m.get("message") or ""
        if txt:
            all_texts.append(txt)
    joined = "\n".join(all_texts)

    per_group_found: dict[str, list[str]] = {}
    ordered_hits: list[tuple[int, str]] = []

    for name, spec in groups.items():
        tokens = spec.get("tokens", []) or []
        found = _tokens_in_text(joined, tokens)
        per_group_found[name] = found
        if found:
            order = int(spec.get("order", 0))
            ordered_hits.append((order, name))

    if not ordered_hits:
        return None, None, per_group_found

    ordered_hits.sort(key=lambda x: x[0])
    current = ordered_hits[-1][1]
    prev = ordered_hits[-2][1] if len(ordered_hits) >= 2 else None
    return current, prev, per_group_found


# ============================================================
# Стратегии
# ============================================================
def _get_or_create_strategy(
    session,
    token_group: str,
    role: str,
    aggression_level: str,
) -> Strategy:
    """Находит стратегию по ключу. Если нет — создаёт с версией v1."""
    st = (
        session.query(Strategy)
        .filter_by(token_group=token_group, role=role, aggression_level=aggression_level)
        .one_or_none()
    )
    if st is not None:
        return st

    name = f"{token_group}__{role}__{aggression_level}"
    log.info("Creating default strategy: %s", name)
    st = Strategy(
        name=name,
        token_group=token_group,
        role=role,
        aggression_level=aggression_level,
        current_weight=1.0,
    )
    session.add(st)
    session.flush()  # получить id

    v1 = StrategyVersion(
        strategy_id=st.id,
        version=1,
        prompt_template=DEFAULT_PROMPT_TEMPLATE,
        params_json={
            "temperature": 0.7,
            "max_tokens": 500,
        },
        metrics_json={
            "expected_response": None,
            "success_rate": 0.0,
            "runs": 0,
        },
        comment="Default v1 (auto-created)",
        created_by="system",
    )
    session.add(v1)
    session.flush()
    st.active_version_id = v1.id
    session.flush()
    return st


def _get_active_version(session, strategy: Strategy) -> Optional[StrategyVersion]:
    if strategy.active_version_id:
        v = session.get(StrategyVersion, strategy.active_version_id)
        if v is not None:
            return v
    # fallback — последняя версия
    v = (
        session.query(StrategyVersion)
        .filter_by(strategy_id=strategy.id)
        .order_by(StrategyVersion.version.desc())
        .first()
    )
    return v


# ============================================================
# Сборка промпта
# ============================================================
def _render_template(template: str, ctx: dict[str, Any]) -> str:
    """Простая подстановка {var}. Неизвестные переменные оставляем как есть."""
    out = template
    for k, v in ctx.items():
        placeholder = "{" + k + "}"
        if placeholder in out:
            out = out.replace(placeholder, str(v))
    return out


def _format_dialogue_history(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        author = (
            m.get("author_username")
            or m.get("author_first_name")
            or str(m.get("author_id") or "unknown")
        )
        txt = (m.get("message") or "").strip()
        if not txt:
            continue
        marker = " [target]" if m.get("is_target") else ""
        lines.append(f"{author}{marker}: {txt}")
    return "\n".join(lines) if lines else "(пусто)"


def _last_client_message(messages: list[dict[str, Any]]) -> str:
    """Последнее сообщение, помеченное is_target, либо просто последнее."""
    for m in reversed(messages):
        if m.get("is_target"):
            return (m.get("message") or "").strip()
    for m in reversed(messages):
        txt = (m.get("message") or "").strip()
        if txt:
            return txt
    return ""


def build_prompt(
    messages: list[dict[str, Any]],
    role: str,
    aggression_level: str,
    token_group: Optional[str] = None,
    *,
    stage: Optional[str] = None,
) -> BuiltPrompt:
    """Собирает промпт для LLM.

    Параметры:
        messages       — список сообщений треда (dict-и).
        role           — buyer | seller | curious | skeptic.
        aggression_level — low | medium | high.
        token_group    — если None, определяется автоматически как текущий этап.
        stage          — можно передать явно (иначе вычисляется).
    """
    product = load_product()
    goal = load_goal()
    groups = load_token_groups()

    if stage is None:
        stage, prev_stage, per_group_found = detect_stage(messages)
    else:
        _, prev_stage, per_group_found = detect_stage(messages)

    if token_group is None:
        token_group = stage or "greeting"

    # Проверим, что группа существует; если нет — fallback на greeting
    if token_group not in groups:
        log.warning("Unknown token_group '%s', fallback to 'greeting'", token_group)
        token_group = "greeting" if "greeting" in groups else next(iter(groups), "greeting")

    # Найдём или создадим стратегию + версию
    strategy_id: Optional[int] = None
    version_id: Optional[int] = None
    prompt_template = DEFAULT_PROMPT_TEMPLATE
    params: dict[str, Any] = {}

    try:
        with get_session() as s:
            st = _get_or_create_strategy(s, token_group, role, aggression_level)
            strategy_id = st.id
            v = _get_active_version(s, st)
            if v is not None:
                version_id = v.id
                if v.prompt_template:
                    prompt_template = v.prompt_template
                if v.params_json:
                    params = dict(v.params_json)
            # зафиксируем текущий этап как метаданные в params_json (без создания новой версии)
            # это нужно, чтобы statistics мог видеть stage
    except Exception as exc:
        log.exception("Failed to get/create strategy: %s", exc)
        # продолжим с дефолтным шаблоном

    # Контекст шаблона
    product_url = product.get("url") or ""
    product_url_line = f"URL: {product_url}\n" if product_url else ""
    ctx: dict[str, Any] = {
        "product_name": product.get("name") or "(без названия)",
        "product_description": product.get("description") or "(без описания)",
        "product_price": product.get("price", 0),
        "product_currency": product.get("currency", "RUB"),
        "product_url": product_url,
        "product_url_line": product_url_line,
        "goal": goal.get("goal") or "Продать товар",
        "success_criteria": goal.get("success_criteria") or "",
        "tone": goal.get("tone") or "friendly",
        "role": role,
        "aggression_level": aggression_level,
        "stage": stage or "(не определён)",
        "prev_stage": prev_stage or "(нет)",
        "dialogue_history": _format_dialogue_history(messages),
        "last_client_message": _last_client_message(messages),
    }

    system_prompt = _render_template(prompt_template, ctx)
    last_client = _last_client_message(messages)

    llm_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": last_client or "(пусто)"},
    ]

    prompt_text = "\n".join(
        f"[{m['role']}] {m['content']}" for m in llm_messages
    )

    return BuiltPrompt(
        messages=llm_messages,
        system=system_prompt,
        prompt_text=prompt_text,
        strategy_id=strategy_id,
        version_id=version_id,
        stage=stage,
        prev_stage=prev_stage,
        context={
            "token_group": token_group,
            "role": role,
            "aggression_level": aggression_level,
            "params": params,
            "per_group_found": per_group_found,
            "product": product,
            "goal": goal,
        },
    )


# ============================================================
# Тест
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sample_messages = [
        {"author_username": "client1", "message": "Привет, расскажите про товар", "is_target": False, "author_id": 1},
        {"author_username": "manager", "message": "Здравствуйте! Наш товар — ...", "is_target": False, "author_id": 2},
        {"author_username": "client1", "message": "Интересно, а сколько стоит?", "is_target": False, "author_id": 1},
        {"author_username": "client1", "message": "ок", "is_target": True, "author_id": 1},
    ]
    bp = build_prompt(sample_messages, role="buyer", aggression_level="low")
    print("=== system prompt ===")
    print(bp.system)
    print()
    print("=== messages ===")
    for m in bp.messages:
        print(f"[{m['role']}] {m['content'][:200]}...")
    print()
    print("stage:", bp.stage, "| prev:", bp.prev_stage)
    print("strategy_id:", bp.strategy_id, "| version_id:", bp.version_id)
