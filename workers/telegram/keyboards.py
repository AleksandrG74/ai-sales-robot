# workers/telegram/keyboards.py
"""
Inline-клавиатуры для бота оператора (aiogram 3.x).
Только построение клавиатур — никаких обработчиков и запросов к БД.
"""

from typing import Iterable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# === ДИАЛОГИ ===

def dialogues_keyboard(
    dialogues: list[dict],
    offset: int = 0,
    page_size: int = 10,
    total: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """
    Клавиатура для /dialogues:
    - по кнопке «Смотреть» на каждый диалог,
    - навигация «Предыдущие 10» / «Следующие 10».
    """
    rows: list[list[InlineKeyboardButton]] = []

    for d in dialogues:
        did = d.get("dialogue_id")
        if not did:
            continue
        # callback_data: "dlg:<dialogue_id>" — dialogue_id ~23 символа, уложимся в 64.
        label = f"🔎 {did}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"dlg:{did}")
        ])

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_off = max(0, offset - page_size)
        nav.append(InlineKeyboardButton(
            text="⬅️ Предыдущие 10",
            callback_data=f"dlgs:{prev_off}",
        ))
    # Кнопку «Следующие» показываем, если есть шанс, что данные есть.
    # Если total известен — проверяем точно; иначе — эвристика по размеру страницы.
    show_next = False
    if total is not None:
        show_next = (offset + page_size) < total
    else:
        show_next = len(dialogues) >= page_size

    if show_next:
        next_off = offset + page_size
        nav.append(InlineKeyboardButton(
            text="Следующие 10 ➡️",
            callback_data=f"dlgs:{next_off}",
        ))

    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"dlgs:{offset}"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def dialogue_detail_keyboard(dialogue_id: str) -> InlineKeyboardMarkup:
    """
    Клавиатура под карточкой диалога.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ К списку", callback_data="dlgs:0"),
        ],
        [
            InlineKeyboardButton(text="✔️ Пометить ok", callback_data=f"dlgok:{dialogue_id}"),
            InlineKeyboardButton(text="❌ Пометить fail", callback_data=f"dlgfail:{dialogue_id}"),
        ],
    ])


# === ВЕРСИИ ===

def strategies_keyboard(strategies: list[dict]) -> InlineKeyboardMarkup:
    """
    Клавиатура под /versions (без аргумента): по кнопке на стратегию.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for s in strategies:
        sid = s.get("id")
        if sid is None:
            continue
        name = s.get("name") or f"strategy {sid}"
        label = f"#{sid} {name}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"vers:{sid}")
        ])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="vers:0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def versions_keyboard(strategy_id: int, versions: list[dict]) -> InlineKeyboardMarkup:
    """
    Клавиатура под списком версий стратегии:
    - «Метрики» на каждую версию,
    - «Откатить» на каждую версию.
    callback_data: "vmet:<sid>:<ver>" и "vrb:<sid>:<ver>".
    """
    rows: list[list[InlineKeyboardButton]] = []
    for v in versions:
        ver = v.get("version")
        if ver is None:
            continue
        rows.append([
            InlineKeyboardButton(
                text=f"📈 Метрики v{ver}",
                callback_data=f"vmet:{strategy_id}:{ver}",
            ),
            InlineKeyboardButton(
                text=f"↩️ Откатить v{ver}",
                callback_data=f"vrb:{strategy_id}:{ver}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ К стратегиям", callback_data="vers:0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def version_metrics_keyboard(strategy_id: int, version: int) -> InlineKeyboardMarkup:
    """
    Клавиатура под карточкой метрик версии.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="↩️ Откатить на эту версию",
                callback_data=f"vrb:{strategy_id}:{version}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ К версиям",
                callback_data=f"vers:{strategy_id}",
            ),
        ],
    ])


def rollback_confirm_keyboard(strategy_id: int, version: int) -> InlineKeyboardMarkup:
    """
    Подтверждение отката.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Да, откатить",
                callback_data=f"vrbyes:{strategy_id}:{version}",
            ),
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=f"vers:{strategy_id}",
            ),
        ],
    ])


# === МОСТЫ ===

def bridges_keyboard(bridges: list[dict]) -> InlineKeyboardMarkup:
    """
    Клавиатура под /bridges: по кнопке «История» на каждый мост.
    callback_data: "brh:<bridge_token>".
    """
    rows: list[list[InlineKeyboardButton]] = []
    for b in bridges:
        token = b.get("bridge_token")
        if not token:
            continue
        label = f"📜 {token}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"brh:{token}")
        ])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="brs:0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bridge_history_keyboard(bridge_token: str) -> InlineKeyboardMarkup:
    """
    Клавиатура под историей моста.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ К мостам", callback_data="brs:0"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"brh:{bridge_token}"),
        ],
    ])


# === ОБЩИЕ ===

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Простая клавиатура «в меню».
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 Статус", callback_data="menu:status"),
            InlineKeyboardButton(text="📊 Метрики", callback_data="menu:stats"),
        ],
    ])


def confirm_cleanup_keyboard() -> InlineKeyboardMarkup:
    """
    Подтверждение /cleanup.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, очистить", callback_data="cleanup:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cleanup:no"),
        ],
    ])


def confirm_rollback_cancel_keyboard() -> InlineKeyboardMarkup:
    """
    Универсальная «Отмена».
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])
