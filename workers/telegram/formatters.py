# workers/telegram/formatters.py
"""
Форматирование ответов бота для Telegram (HTML).
Функции принимают чистые dict'ы из queries.py, возвращают HTML-строки.
Никаких обращений к БД / Telegram API здесь нет.
"""

import html
import json
from datetime import datetime
from typing import Any, Optional

# Telegram: max длина одного сообщения — 4096 символов.
# Оставляем запас для безопасного отсечения.
MAX_MESSAGE_LEN = 4000


# === УТИЛИТЫ ===

def _esc(x: Any) -> str:
    """HTML-экранирование произвольного значения."""
    if x is None:
        return "—"
    return html.escape(str(x), quote=False)


def _fmt_num(x: Any, digits: int = 3) -> str:
    """Формат числа: округление до digits, безопасно для None/нечисла."""
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "да" if x else "нет"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return _esc(x)


def _fmt_dt(iso: Optional[str]) -> str:
    """ISO-строка -> 'YYYY-MM-DD HH:MM:SS' (локально, без tz)."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return _esc(iso)


def _fmt_duration(sec: Optional[float]) -> str:
    """Секунды -> 'Xс' / 'Xм Yс' / 'Xч Yм'."""
    if sec is None:
        return "—"
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "—"
    if sec < 60:
        return f"{sec:.1f}с"
    if sec < 3600:
        m = int(sec // 60)
        s = int(sec % 60)
        return f"{m}м {s}с"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    return f"{h}ч {m}м"


def _truncate(text: str, limit: int = MAX_MESSAGE_LEN) -> str:
    """Обрезает длинное сообщение, добавляя маркер."""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n<i>…обрезано</i>"


def _bar(value: float, total: float, width: int = 12) -> str:
    """Простой текстовый бар для распределений."""
    if total <= 0:
        return ""
    filled = int(round(width * (value / total)))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


# === СТАТУС ===

def format_status(status: dict) -> str:
    """
    /status — состояние системы.
    """
    lines: list[str] = []
    lines.append("<b>🤖 Состояние системы</b>")
    lines.append("")

    dlg = status.get("dialogue_count", 0)
    strat = status.get("strategy_count", 0)
    vers = status.get("version_count", 0)
    active = status.get("active_strategies", 0)

    lines.append(f"<b>Обработано диалогов:</b> <code>{_esc(dlg)}</code>")
    lines.append(f"<b>Стратегий:</b> <code>{_esc(strat)}</code> (активных: <code>{_esc(active)}</code>)")
    lines.append(f"<b>Версий стратегий:</b> <code>{_esc(vers)}</code>")
    lines.append("")

    lr = status.get("last_run")
    if not lr:
        lines.append("<i>Прогонов пока не было.</i>")
    else:
        status_icon = {
            "ok": "✅",
            "success": "✅",
            "failed": "❌",
            "error": "❌",
            "running": "⏳",
        }.get(str(lr.get("status", "")).lower(), "❔")

        lines.append("<b>Последний прогон:</b>")
        lines.append(f"  • ID: <code>{_esc(lr.get('id'))}</code> ({_esc(lr.get('run_type'))})")
        lines.append(f"  • Статус: {status_icon} {_esc(lr.get('status'))}")
        lines.append(f"  • Старт: {_fmt_dt(lr.get('started_at'))}")
        lines.append(f"  • Финиш: {_fmt_dt(lr.get('finished_at'))}")
        lines.append(f"  • Обработано: <code>{_esc(lr.get('items_processed'))}</code>")

        if lr.get("error_text"):
            lines.append(f"  • ❗ Ошибка: <code>{_esc(lr['error_text'])[:300]}</code>")

    return _truncate("\n".join(lines))


# === ОБЩИЕ МЕТРИКИ ===

def format_stats(stats: dict) -> str:
    """
    /stats — агрегированные метрики.
    """
    lines: list[str] = []
    lines.append("<b>📊 Общие метрики</b>")
    lines.append("")

    total = stats.get("total", 0)
    with_result = stats.get("with_result", 0)
    lines.append(f"<b>Всего диалогов:</b> <code>{_esc(total)}</code>")
    lines.append(f"<b>С результатом:</b> <code>{_esc(with_result)}</code>")
    lines.append(f"<b>Avg score:</b> <code>{_fmt_num(stats.get('avg_score'), 4)}</code>")
    lines.append(f"<b>Avg cosine:</b> <code>{_fmt_num(stats.get('avg_cosine'), 4)}</code>")
    lines.append("")

    roles = stats.get("roles") or {}
    if roles:
        lines.append("<b>Роли:</b>")
        for role, cnt in roles.items():
            lines.append(f"  • {_esc(role)}: <code>{_esc(cnt)}</code>")
        lines.append("")

    aggression = stats.get("aggression") or {}
    if aggression:
        lines.append("<b>Агрессия:</b>")
        max_cnt = max(aggression.values()) if aggression.values() else 0
        for lvl, cnt in aggression.items():
            bar = _bar(cnt, max_cnt)
            lines.append(f"  • ур.{_esc(lvl)}: <code>{_esc(cnt)}</code> {bar}")

    return _truncate("\n".join(lines))


# === ДИАЛОГИ ===

def format_dialogues_list(dialogues: list[dict], offset: int = 0) -> str:
    """
    /dialogues — список последних диалогов.
    offset нужен только для заголовка.
    """
    lines: list[str] = []
    lines.append(f"<b>💬 Диалоги</b> (с {offset + 1})")
    lines.append("")

    if not dialogues:
        lines.append("<i>Пусто.</i>")
        return _truncate("\n".join(lines))

    for i, d in enumerate(dialogues, start=offset + 1):
        did = d.get("dialogue_id") or "?"
        role = d.get("role") or "—"
        score = _fmt_num(d.get("score"), 3)
        cosine = _fmt_num(d.get("cosine"), 3)
        agg = d.get("aggression_level")
        status = d.get("status") or "—"

        status_icon = {
            "ok": "✅",
            "success": "✅",
            "failed": "❌",
            "error": "❌",
        }.get(str(status).lower(), "•")

        agg_part = f" | agg: <code>{_esc(agg)}</code>" if agg is not None else ""

        lines.append(
            f"{i}. {status_icon} <code>{_esc(did)}</code>\n"
            f"     роль: <b>{_esc(role)}</b> | score: <code>{score}</code> | cos: <code>{cosine}</code>{agg_part}"
        )

    return _truncate("\n".join(lines))


def format_dialogue_detail(d: Optional[dict]) -> str:
    """
    /dialogue <id> — детали одного диалога.
    """
    if not d:
        return "❌ Диалог не найден."

    lines: list[str] = []
    lines.append(f"<b>💬 Диалог</b> <code>{_esc(d.get('dialogue_id'))}</code>")
    lines.append("")
    lines.append(f"<b>ID записи:</b> <code>{_esc(d.get('id'))}</code>")
    lines.append(f"<b>Sender:</b> <code>{_esc(d.get('sender'))}</code>")
    lines.append(f"<b>Seed:</b> <code>{_esc(d.get('seed_id'))}</code>")
    lines.append(f"<b>Статус:</b> {_esc(d.get('status'))}")

    if d.get("error"):
        lines.append(f"<b>❗ Ошибка:</b> <code>{_esc(d['error'])[:300]}</code>")

    result = d.get("result")
    if isinstance(result, dict):
        lines.append("")
        lines.append("<b>Метрики:</b>")
        lines.append(f"  • score: <code>{_fmt_num(result.get('score'), 4)}</code>")
        lines.append(f"  • cosine: <code>{_fmt_num(result.get('cosine'), 4)}</code>")
        lines.append(f"  • role: <b>{_esc(result.get('role'))}</b>")
        agg = result.get("aggression_level", result.get("aggression"))
        if agg is not None:
            lines.append(f"  • aggression: <code>{_esc(agg)}</code>")

        extra_keys = {k: v for k, v in result.items()
                      if k not in {"score", "cosine", "role", "aggression_level", "aggression"}}
        if extra_keys:
            lines.append("")
            lines.append("<b>Прочее:</b>")
            try:
                extra_json = json.dumps(extra_keys, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                extra_json = str(extra_keys)
            if len(extra_json) > 1500:
                extra_json = extra_json[:1500] + "\n…"
            lines.append(f"<pre>{_esc(extra_json)}</pre>")
    elif d.get("result_raw"):
        lines.append("")
        lines.append("<b>result_json (raw):</b>")
        raw = str(d["result_raw"])
        if len(raw) > 1500:
            raw = raw[:1500] + "…"
        lines.append(f"<pre>{_esc(raw)}</pre>")
    else:
        lines.append("")
        lines.append("<i>Нет result_json.</i>")

    return _truncate("\n".join(lines))


# === СТРАТЕГИИ ===

def format_strategies(strategies: list[dict]) -> str:
    """
    /versions без аргумента — список стратегий.
    """
    lines: list[str] = []
    lines.append("<b>🧠 Стратегии</b>")
    lines.append("")

    if not strategies:
        lines.append("<i>Стратегий нет.</i>")
        return _truncate("\n".join(lines))

    for s in strategies:
        lines.append(
            f"<b>#{_esc(s.get('id'))}</b> {_esc(s.get('name'))}\n"
            f"     группа: <code>{_esc(s.get('token_group'))}</code> | "
            f"роль: <b>{_esc(s.get('role'))}</b> | "
            f"agg: <code>{_esc(s.get('aggression_level'))}</code>\n"
            f"     weight: <code>{_fmt_num(s.get('current_weight'), 4)}</code> | "
            f"active_ver_id: <code>{_esc(s.get('active_version_id'))}</code>"
        )

    return _truncate("\n".join(lines))


def format_versions(strategy_id: int, versions: list[dict]) -> str:
    """
    /versions <strategy_id> — версии стратегии.
    """
    lines: list[str] = []
    lines.append(f"<b>📚 Версии стратегии #{_esc(strategy_id)}</b>")
    lines.append("")

    if not versions:
        lines.append("<i>Версий нет.</i>")
        return _truncate("\n".join(lines))

    for v in versions:
        metrics = v.get("metrics") or {}
        score = metrics.get("avg_score") if isinstance(metrics, dict) else None
        cosine = metrics.get("avg_cosine") if isinstance(metrics, dict) else None
        score_s = _fmt_num(score, 4) if score is not None else "—"
        cosine_s = _fmt_num(cosine, 4) if cosine is not None else "—"

        lines.append(
            f"<b>v{_esc(v.get('version'))}</b> (id: <code>{_esc(v.get('id'))}</code>)\n"
            f"     avg_score: <code>{score_s}</code> | avg_cosine: <code>{cosine_s}</code>\n"
            f"     автор: <code>{_esc(v.get('created_by'))}</code>\n"
            f"     комментарий: {_esc(v.get('comment'))}"
        )

    return _truncate("\n".join(lines))


def format_version_metrics(strategy_id: int, version: dict) -> str:
    """
    Callback «Метрики» — детали одной версии.
    """
    lines: list[str] = []
    lines.append(f"<b>📈 Метрики</b> стратегия #{_esc(strategy_id)}, v{_esc(version.get('version'))}")
    lines.append("")

    params = version.get("params")
    metrics = version.get("metrics")

    if isinstance(params, dict) and params:
        lines.append("<b>Параметры:</b>")
        try:
            lines.append(f"<pre>{_esc(json.dumps(params, ensure_ascii=False, indent=2))[:1500]}</pre>")
        except (TypeError, ValueError):
            lines.append(f"<pre>{_esc(str(params))[:1500]}</pre>")
        lines.append("")

    if isinstance(metrics, dict) and metrics:
        lines.append("<b>Метрики:</b>")
        try:
            lines.append(f"<pre>{_esc(json.dumps(metrics, ensure_ascii=False, indent=2))[:1500]}</pre>")
        except (TypeError, ValueError):
            lines.append(f"<pre>{_esc(str(metrics))[:1500]}</pre>")
    elif not params:
        lines.append("<i>Нет данных.</i>")

    return _truncate("\n".join(lines))


# === ТОКЕНЫ ===

def format_word_stats(words: list[dict]) -> str:
    """
    /words — топ токенов по success_rate.
    """
    lines: list[str] = []
    lines.append("<b>🔤 Топ токенов по success_rate</b>")
    lines.append("")

    if not words:
        lines.append("<i>Данных нет.</i>")
        return _truncate("\n".join(lines))

    for i, w in enumerate(words, start=1):
        lines.append(
            f"{i}. <b>{_esc(w.get('word'))}</b>\n"
            f"     sr: <code>{_fmt_num(w.get('success_rate'), 3)}</code> "
            f"({_esc(w.get('success_count'))}/{_esc(w.get('total_count'))}) | "
            f"pmi: <code>{_fmt_num(w.get('pmi_score'), 3)}</code>"
        )

    return _truncate("\n".join(lines))


# === МОСТЫ ===

def format_bridges(bridges: list[dict]) -> str:
    """
    /bridges — топ мостов.
    """
    lines: list[str] = []
    lines.append("<b>🌉 Топ мостов по weight</b>")
    lines.append("")

    if not bridges:
        lines.append("<i>Мостов нет.</i>")
        return _truncate("\n".join(lines))

    for i, b in enumerate(bridges, start=1):
        lines.append(
            f"{i}. <b>{_esc(b.get('bridge_token'))}</b>\n"
            f"     {_esc(b.get('from_group'))} → {_esc(b.get('to_group'))}\n"
            f"     weight: <code>{_fmt_num(b.get('weight'), 4)}</code> | "
            f"успехов: <code>{_esc(b.get('success_count'))}/{_esc(b.get('total_count'))}</code>"
        )

    return _truncate("\n".join(lines))


def format_bridge_history(bridge_token: str, history: list[dict]) -> str:
    """
    Callback «Смотреть историю» — история одного моста.
    """
    lines: list[str] = []
    lines.append(f"<b>📜 История моста</b> <code>{_esc(bridge_token)}</code>")
    lines.append("")

    if not history:
        lines.append("<i>Пусто.</i>")
        return _truncate("\n".join(lines))

    for h in history:
        ok_icon = "✅" if h.get("success") else "❌"
        lines.append(
            f"{ok_icon} <code>{_esc(h.get('dialogue_id'))}</code> | "
            f"{_esc(h.get('from_group'))} → {_esc(h.get('to_group'))}\n"
            f"     w: <code>{_fmt_num(h.get('weight_before'), 3)}</code> → "
            f"<code>{_fmt_num(h.get('weight_after'), 3)}</code>"
        )

    return _truncate("\n".join(lines))


# === ПРОГОНЫ ===

def format_runs(runs: list[dict]) -> str:
    """
    /runs — последние прогоны.
    """
    lines: list[str] = []
    lines.append("<b>🏃 Последние прогоны</b>")
    lines.append("")

    if not runs:
        lines.append("<i>Прогонов нет.</i>")
        return _truncate("\n".join(lines))

    for r in runs:
        icon = {
            "ok": "✅",
            "success": "✅",
            "failed": "❌",
            "error": "❌",
            "running": "⏳",
        }.get(str(r.get("status", "")).lower(), "❔")

        lines.append(
            f"{icon} <b>#{_esc(r.get('id'))}</b> {_esc(r.get('run_type'))} — "
            f"<code>{_esc(r.get('status'))}</code>\n"
            f"     старт: {_fmt_dt(r.get('started_at'))}\n"
            f"     финиш: {_fmt_dt(r.get('finished_at'))} "
            f"(<code>{_fmt_duration(r.get('duration_sec'))}</code>)\n"
            f"     обработано: <code>{_esc(r.get('items_processed'))}</code>"
        )
        if r.get("error_text"):
            lines.append(f"     ❗ <code>{_esc(r['error_text'])[:200]}</code>")

    return _truncate("\n".join(lines))


# === ДЕЙСТВИЯ ОПЕРАТОРА ===

def format_operator_actions(actions: list[dict]) -> str:
    """
    Аудит действий оператора.
    """
    lines: list[str] = []
    lines.append("<b>📝 Последние действия оператора</b>")
    lines.append("")

    if not actions:
        lines.append("<i>Действий нет.</i>")
        return _truncate("\n".join(lines))

    for a in actions:
        lines.append(
            f"• <code>{_fmt_dt(a.get('created_at'))}</code> "
            f"<b>{_esc(a.get('action'))}</b> "
            f"диалог <code>{_esc(a.get('dialogue_id'))}</code> "
            f"(op: <code>{_esc(a.get('operator_id'))}</code>)"
        )

    return _truncate("\n".join(lines))


# === HELP / START ===

def format_start(whitelist_enabled: bool) -> str:
    """
    /start — приветствие.
    """
    lines: list[str] = []
    lines.append("<b>👋 AI Sales Robot — бот оператора</b>")
    lines.append("")
    lines.append("Управление и мониторинг системы.")
    if whitelist_enabled:
        lines.append("<i>Режим: whitelist (только разрешённые чаты).</i>")
    else:
        lines.append("<i>Режим: все чаты.</i>")
    lines.append("")
    lines.append("Основные команды: /status, /stats, /dialogues, /versions, /words, /bridges, /runs")
    lines.append("Полный список: /help")
    return _truncate("\n".join(lines))


def format_help() -> str:
    """
    /help — справка.
    """
    lines: list[str] = []
    lines.append("<b>📖 Справка</b>")
    lines.append("")
    lines.append("<b>Мониторинг:</b>")
    lines.append("  /status — состояние системы")
    lines.append("  /stats — агрегированные метрики")
    lines.append("  /runs [N] — последние N прогонов (по умолч. 5)")
    lines.append("")
    lines.append("<b>Диалоги:</b>")
    lines.append("  /dialogues [N] — последние N диалогов (по умолч. 10)")
    lines.append("  /dialogue &lt;id&gt; — детали диалога")
    lines.append("")
    lines.append("<b>Стратегии:</b>")
    lines.append("  /versions — список стратегий")
    lines.append("  /versions &lt;strategy_id&gt; — версии стратегии")
    lines.append("  /rollback &lt;strategy_id&gt; &lt;version&gt; — откат версии")
    lines.append("")
    lines.append("<b>Данные:</b>")
    lines.append("  /words [N] — топ токенов по success_rate")
    lines.append("  /bridges [N] — топ мостов по weight")
    lines.append("  /export — выгрузить CSV")
    lines.append("  /snapshot — PMI-снапшот")
    lines.append("  /cleanup — очистка старых данных (&gt;90 дней)")
    lines.append("")
    lines.append("<b>Прочее:</b>")
    lines.append("  /actions [N] — последние действия оператора")
    lines.append("  /help — эта справка")
    return _truncate("\n".join(lines))


# === СЛУЖЕБНЫЕ ОТВЕТЫ ===

def format_error(message: str) -> str:
    """Универсальное сообщение об ошибке."""
    return f"❌ <b>Ошибка:</b> {_esc(message)}"


def format_info(message: str) -> str:
    """Универсальное информационное сообщение."""
    return f"ℹ️ {_esc(message)}"


def format_ok(message: str) -> str:
    """Универсальное сообщение об успехе."""
    return f"✅ {_esc(message)}"
