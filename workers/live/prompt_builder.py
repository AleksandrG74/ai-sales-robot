# SALEM_NAME_HINT = "Ты — Салем, умный цифровой продавец. Ты не называешь себя ботом.\n"
# workers/live/prompt_builder.py
"""
Сборка LLM-промптов для live-подсказок (suggest.py).

Переиспользует workers/dialogue/prompt_builder.py:
- load_product(), load_goal(), load_token_groups() — читаются оттуда.
- detect_stage() — оттуда.
- build_prompt(messages, role, aggression_level, token_group, stage) -> BuiltPrompt — оттуда.
- BuiltPrompt — оттуда (dataclass с полями messages, system, prompt_text, ...).
    "Твоё имя — Салем. Представляйся Салемом, если спрашивают имя.",

Переиспользует workers/dialogue/agent.py:
- detect_role_heuristic(messages)
- detect_aggression_heuristic(messages)
для быстрой оценки роли/агрессии без обращения к LLM.

Публичное API:
- build_suggest_prompt(chat_id, message_id, history) -> list[dict]
    Возвращает messages для LLMClient.chat(messages=...).
- build_suggest_prompt_full(chat_id, message_id, history) -> BuiltPrompt
    Полный объект (messages, system, prompt_text, stage, ...) — для логов и дебага.

Приведение формата:
    history из live_messages содержит поле "text".
    Функции dialogue-layer ожидают поле "message".
    Здесь выполняется нормализация — на входе text/role, на выходе message/role/name.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger("live-worker.prompt_builder")


# === НОРМАЛИЗАЦИЯ ===

def _normalize_history(history: list[dict]) -> list[dict]:
    """
    Приводит историю live_messages к формату, который ждут функции
    из workers.dialogue.prompt_builder (detect_stage, build_prompt,
    _format_dialogue_history, _last_client_message).

    На входе — dict'и с полями:
        message_id, text, is_outgoing, author_first_name, author_username, sent_at
    На выходе — dict'и с полями:
        message, role, name, timestamp, is_outgoing
    где role: "client" (входящее) | "manager" (исходящее).
    """
    out: list[dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        text = m.get("text") or m.get("message") or ""
        if not isinstance(text, str) or not text.strip():
            continue

        is_out = int(m.get("is_outgoing") or 0) == 1
        role = "manager" if is_out else "client"

        name = m.get("author_first_name") or m.get("author_username") or (
            "Мы" if is_out else "Клиент"
        )

        out.append({
            "message": text,
            "role": role,
            "name": name,
            "is_outgoing": 1 if is_out else 0,
            "timestamp": m.get("sent_at"),
            "message_id": m.get("message_id"),
        })
    return out


def _last_client_text(normalized: list[dict]) -> str:
    for m in reversed(normalized):
        if m.get("role") == "client" and m.get("message"):
            return m["message"]
    return ""


# === РОЛЬ / АГРЕССИЯ (быстрые эвристики) ===

def _detect_role_safe(normalized: list[dict]) -> str:
    """
    Пробует agent.detect_role_heuristic. Если что-то упало —
    возвращает 'curious' (самая частая роль в bootstrap).
    """
    try:
        from workers.dialogue import agent as dialogue_agent
    except Exception as e:
        logger.debug("dialogue.agent недоступен: %s", e)
        return "curious"

    fn = getattr(dialogue_agent, "detect_role_heuristic", None)
    if not callable(fn):
        return "curious"

    try:
        result = fn(normalized, client_key=None)
        # функция может вернуть str или кортеж (role, something)
        if isinstance(result, str) and result.strip():
            return result.strip()
        if isinstance(result, tuple) and result and isinstance(result[0], str):
            return result[0].strip()
    except Exception as e:
        logger.warning("detect_role_heuristic упал: %s", e)

    return "curious"


def _detect_aggression_safe(normalized: list[dict]) -> str:
    try:
        from workers.dialogue import agent as dialogue_agent
    except Exception as e:
        logger.debug("dialogue.agent недоступен: %s", e)
        return "low"

    fn = getattr(dialogue_agent, "detect_aggression_heuristic", None)
    if not callable(fn):
        return "low"

    try:
        result = fn(normalized, client_key=None)
        if isinstance(result, str) and result.strip():
            return result.strip()
        if isinstance(result, tuple) and result and isinstance(result[0], str):
            return result[0].strip()
    except Exception as e:
        logger.warning("detect_aggression_heuristic упал: %s", e)

    return "low"


def _detect_stage_safe(normalized: list[dict]) -> tuple[Optional[str], Optional[str]]:
    try:
        from workers.dialogue.prompt_builder import detect_stage
    except Exception as e:
        logger.debug("dialogue.prompt_builder.detect_stage недоступен: %s", e)
        return None, None

    try:
        cur, prev, _ = detect_stage(normalized)
        return cur, prev
    except Exception as e:
        logger.warning("detect_stage упал: %s", e)
        return None, None


# === СБОРКА ПОЛНОГО BuiltPrompt ===


def _inject_live_rules(messages: list[dict]) -> list[dict]:
    """
    Добавляет правила в system-промпт, чтобы агент не был навязчивым продавцом,
    если диалог не про продажи.
    """
    if not messages:
        return messages

    # Ищем system
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    if system_msg:
        extra_rules = (
            "\n\n## ВАЖНЫЕ ПРАВИЛА ДЛЯ ЖИВОГО ДИАЛОГА\n"
            "1. Если клиент обсуждает техническую проблему, задаёт вопрос по железу, софту или просит помощи — "
            "отвечай как полезный технический ассистент. НЕ пытайся продать товар, не предлагай купить, не используй скрипты продаж.\n"
            "2. Продажа уместна только если клиент сам проявил интерес к покупке (спросил цену, наличие, как купить).\n"
            "3. Твоя главная цель — быть полезным. Не будь навязчивым."
        )
        system_msg["content"] = str(system_msg.get("content", "")) + extra_rules
    return messages


def build_suggest_prompt_full(
    chat_id: int,
    message_id: int,
    history: list[dict],
) -> Optional[Any]:
    """
    Собирает BuiltPrompt через workers.dialogue.prompt_builder.build_prompt.
    Возвращает BuiltPrompt или None при ошибке.

    Параметры chat_id/message_id не используются в сборке, но передаются
    для логов и возможного использования в будущем (например, адресный
    промпт под конкретного клиента).
    """
    try:
        from workers.dialogue.prompt_builder import build_prompt
    except Exception as e:
        logger.exception("Не удалось импортировать dialogue.prompt_builder: %s", e)
        return None

    normalized = _normalize_history(history)
    if not normalized:
        logger.info("build_suggest_prompt_full: пустая история для chat=%s", chat_id)
        return None

    role = _detect_role_safe(normalized)
    aggression = _detect_aggression_safe(normalized)
    stage, _prev_stage = _detect_stage_safe(normalized)

    try:
        built = build_prompt(
            messages=normalized,
            role=role,
            aggression_level=aggression,
            token_group=None,
            stage=stage,
        )
        logger.info(
            "build_suggest_prompt_full: chat=%s msg=%s role=%s agg=%s stage=%s",
            chat_id, message_id, role, aggression, stage,
        )
        # 8.7: Добавляем правила live-режима
        built.messages = _inject_live_rules(built.messages)
        return built
    except Exception as e:
        logger.exception("build_prompt упал: %s", e)
        return None


# === ПУБЛИЧНОЕ API: list[dict] для LLMClient.chat ===

def build_suggest_prompt(
    chat_id: int,
    message_id: int,
    history: list[dict],
) -> list[dict]:
    """
    Возвращает messages (list[dict]) для LLMClient.chat(messages=...).

    При ошибке сборки BuiltPrompt — возвращает fallback messages
    (system + последнее сообщение клиента), чтобы suggest.py мог
    работать даже при проблемах с шаблонами/стратегиями.
    """
    built = build_suggest_prompt_full(chat_id, message_id, history)
    if built is not None:
        try:
            msgs = getattr(built, "messages", None)
            if isinstance(msgs, list) and msgs:
                # Проверим, что элементы — dict с role/content
                clean: list[dict[str, str]] = []
                for m in msgs:
                    if isinstance(m, dict) and "role" in m and "content" in m:
                        clean.append({"role": str(m["role"]), "content": str(m["content"])})
                if clean:
                    return clean
        except Exception as e:
            logger.warning("Не удалось извлечь messages из BuiltPrompt: %s", e)

    # Fallback
    logger.info("build_suggest_prompt: используется fallback messages.")
    normalized = _normalize_history(history)
    last_client = _last_client_text(normalized)
    return _fallback_messages(last_client)


# === FALLBACK ===

def _fallback_messages(last_client: str) -> list[dict]:
    """Простейшие messages, если dialogue.prompt_builder недоступен/упал."""
    system = (
        "Ты — опытный менеджер по продажам в Telegram. "
        "Отвечай кратко, по делу, в дружелюбном тоне. "
        "Не выдумывай факты о товаре. Верни только текст ответа, "
        "без пояснений и без кавычек."
    )
    user = last_client.strip() or "(пустое сообщение)"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# === ХЕЛПЕР: prompt_text для логов ===

def build_prompt_text(messages: list[dict]) -> str:
    """Собирает читаемую строку-промпт из messages — для логирования."""
    try:
        return "\n".join(
            f"[{m.get('role', '?')}] {m.get('content', '')}"
            for m in messages if isinstance(m, dict)
        )
    except Exception:
        return ""
