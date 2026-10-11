# workers/telegram/handlers.py
"""
Обработчики команд и callback'ов бота оператора (aiogram 3.x).
Все обращения к БД — через workers.telegram.queries (sync).
Все ответы форматируются через workers.telegram.formatters.
"""

import asyncio
import logging
import os
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message

from workers.telegram import formatters as fmt
from workers.telegram import keyboards as kb
from workers.telegram import queries as q

logger = logging.getLogger(__name__)

router = Router(name="operator")

# Лимиты, чтобы не заваливать оператора
DEFAULT_DIALOGUES_LIMIT = 10
DEFAULT_LIST_LIMIT = 10
MAX_LIMIT = 50


# === WHITELIST ===

def _is_allowed(message_or_cb) -> bool:
    """
    Whitelist по TELEGRAM_OPERATOR_CHAT_ID.
    Если переменная пуста/не задана — пускаем всех.
    """
    allowed_raw = os.getenv("TELEGRAM_OPERATOR_CHAT_ID", "").strip()
    if not allowed_raw:
        return True
    try:
        allowed_ids = {int(x.strip()) for x in allowed_raw.split(",") if x.strip()}
    except ValueError:
        logger.warning("TELEGRAM_OPERATOR_CHAT_ID содержит нечисловые значения: %r", allowed_raw)
        return True

    user = getattr(message_or_cb, "from_user", None)
    chat = getattr(message_or_cb, "chat", None) or getattr(getattr(message_or_cb, "message", None), "chat", None)

    if user is not None and user.id in allowed_ids:
        return True
    if chat is not None and chat.id in allowed_ids:
        return True
    return False


async def _deny(target) -> None:
    text = "⛔ Доступ запрещён."
    if isinstance(target, Message):
        await target.answer(text)
    elif isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)


def _parse_limit(args: Optional[str], default: int) -> int:
    if not args:
        return default
    try:
        n = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return default
    return max(1, min(MAX_LIMIT, n))


# === /start, /help ===

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    whitelist_enabled = bool(os.getenv("TELEGRAM_OPERATOR_CHAT_ID", "").strip())
    await message.answer(fmt.format_start(whitelist_enabled))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    await message.answer(fmt.format_help())


# === /status, /stats ===

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        data = await asyncio.to_thread(q.get_system_status)
        await message.answer(fmt.format_status(data))
    except Exception as e:
        logger.exception("/status упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        data = await asyncio.to_thread(q.get_overall_stats)
        await message.answer(fmt.format_stats(data))
    except Exception as e:
        logger.exception("/stats упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


# === /dialogues, /dialogue ===

async def _send_dialogues_page(message: Message, offset: int) -> None:
    limit = DEFAULT_DIALOGUES_LIMIT
    dialogues = await asyncio.to_thread(q.get_recent_dialogues, limit, offset)
    total = await asyncio.to_thread(q.get_dialogue_count)
    text = fmt.format_dialogues_list(dialogues, offset=offset)
    markup = kb.dialogues_keyboard(dialogues, offset=offset, page_size=limit, total=total)
    await message.answer(text, reply_markup=markup)


@router.message(Command("dialogues"))
async def cmd_dialogues(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        # /dialogues [N] — N влияет только на размер первой страницы,
        # но для упрощения всегда используем DEFAULT_DIALOGUES_LIMIT=10.
        _ = _parse_limit(command.args, DEFAULT_DIALOGUES_LIMIT)
        await _send_dialogues_page(message, offset=0)
    except Exception as e:
        logger.exception("/dialogues упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


@router.message(Command("dialogue"))
async def cmd_dialogue(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    did = (command.args or "").strip().split()[0] if command.args else ""
    if not did:
        await message.answer(fmt.format_error("Укажи id: /dialogue <dialogue_id>"))
        return
    try:
        d = await asyncio.to_thread(q.get_dialogue, did)
        if not d:
            await message.answer(fmt.format_info(f"Диалог {did} не найден."))
            return
        text = fmt.format_dialogue_detail(d)
        markup = kb.dialogue_detail_keyboard(did)
        await message.answer(text, reply_markup=markup)
    except Exception as e:
        logger.exception("/dialogue упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


# === /versions, /rollback ===

async def _send_strategies(message: Message) -> None:
    strategies = await asyncio.to_thread(q.get_strategies)
    text = fmt.format_strategies(strategies)
    markup = kb.strategies_keyboard(strategies)
    await message.answer(text, reply_markup=markup)


async def _send_versions(message: Message, strategy_id: int) -> None:
    versions = await asyncio.to_thread(q.get_versions, strategy_id)
    text = fmt.format_versions(strategy_id, versions)
    markup = kb.versions_keyboard(strategy_id, versions)
    await message.answer(text, reply_markup=markup)


@router.message(Command("versions"))
async def cmd_versions(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        args = (command.args or "").strip()
        if not args:
            await _send_strategies(message)
            return
        try:
            sid = int(args.split()[0])
        except ValueError:
            await message.answer(fmt.format_error("strategy_id должен быть числом."))
            return
        await _send_versions(message, sid)
    except Exception as e:
        logger.exception("/versions упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


@router.message(Command("rollback"))
async def cmd_rollback(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.answer(fmt.format_error("Использование: /rollback <strategy_id> <version>"))
        return
    try:
        sid = int(args[0])
        ver = int(args[1])
    except ValueError:
        await message.answer(fmt.format_error("strategy_id и version должны быть числами."))
        return
    await message.answer(
        f"⚠️ Откатить стратегию <b>#{sid}</b> к версии <b>v{ver}</b>?",
        reply_markup=kb.rollback_confirm_keyboard(sid, ver),
    )


# === /words, /bridges ===

@router.message(Command("words"))
async def cmd_words(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        n = _parse_limit(command.args, DEFAULT_LIST_LIMIT)
        words = await asyncio.to_thread(q.get_word_stats, n)
        await message.answer(fmt.format_word_stats(words))
    except Exception as e:
        logger.exception("/words упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


@router.message(Command("bridges"))
async def cmd_bridges(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        n = _parse_limit(command.args, DEFAULT_LIST_LIMIT)
        bridges = await asyncio.to_thread(q.get_bridges, n)
        text = fmt.format_bridges(bridges)
        markup = kb.bridges_keyboard(bridges)
        await message.answer(text, reply_markup=markup)
    except Exception as e:
        logger.exception("/bridges упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


# === /runs, /actions ===

@router.message(Command("runs"))
async def cmd_runs(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        n = _parse_limit(command.args, 5)
        runs = await asyncio.to_thread(q.get_runs, n)
        await message.answer(fmt.format_runs(runs))
    except Exception as e:
        logger.exception("/runs упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


@router.message(Command("actions"))
async def cmd_actions(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    try:
        n = _parse_limit(command.args, DEFAULT_LIST_LIMIT)
        actions = await asyncio.to_thread(q.get_operator_actions, n)
        await message.answer(fmt.format_operator_actions(actions))
    except Exception as e:
        logger.exception("/actions упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


# === /export ===

@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    await message.answer(fmt.format_info("Готовлю CSV, подожди…"))
    try:
        path = await asyncio.to_thread(_export_csv_sync)
        if not path or not os.path.exists(path):
            await message.answer(fmt.format_error("Не удалось сгенерировать CSV."))
            return
        file = FSInputFile(path)
        await message.answer_document(file, caption="📄 Экспорт метрик")
    except Exception as e:
        logger.exception("/export упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


def _export_csv_sync() -> Optional[str]:
    """
    Синхронно дёргает scripts/export_csv.py.
    Возвращает путь к сгенерированному файлу или None.
    """
    try:
        from scripts import export_csv  # type: ignore
    except Exception as e:
        logger.exception("Импорт scripts.export_csv упал: %s", e)
        return None

    # Пробуем разные возможные API модуля.
    for fn_name in ("export_to_csv", "export_all", "main", "generate_csv"):
        fn = getattr(export_csv, fn_name, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, str) and os.path.exists(result):
                    return result
                # Функция могла вернуть путь или записать файл в известное место
                default_path = getattr(export_csv, "DEFAULT_OUTPUT", None)
                if isinstance(default_path, str) and os.path.exists(default_path):
                    return default_path
            except Exception as e:
                logger.exception("export_csv.%s() упал: %s", fn_name, e)

    # Фолбэк — поискать свежий CSV в exports/
    try:
        exports_dir = os.path.join(os.getcwd(), "exports")
        if os.path.isdir(exports_dir):
            files = [os.path.join(exports_dir, f) for f in os.listdir(exports_dir) if f.endswith(".csv")]
            if files:
                return max(files, key=os.path.getmtime)
    except Exception as e:
        logger.warning("Фолбэк-поиск CSV в exports/ упал: %s", e)

    return None


# === /snapshot ===

@router.message(Command("snapshot"))
async def cmd_snapshot(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    await message.answer(fmt.format_info("Делаю PMI-снапшот…"))
    try:
        result = await asyncio.to_thread(_snapshot_sync)
        if result:
            await message.answer(fmt.format_ok(f"PMI-снапшот выполнен: {result}"))
        else:
            await message.answer(fmt.format_info("PMI-снапшот не дал изменений."))
    except Exception as e:
        logger.exception("/snapshot упал: %s", e)
        await message.answer(fmt.format_error(str(e)))


def _snapshot_sync() -> Optional[str]:
    """
    Ищет функцию снапшота в workers.dialogue.statistics или versioning.
    """
    candidates = [
        ("workers.dialogue.statistics", ("snapshot_pmi", "pmi_snapshot", "build_pmi_snapshot")),
        ("workers.dialogue.versioning", ("snapshot_pmi", "pmi_snapshot")),
    ]
    for module_name, fns in candidates:
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception:
            continue
        for fn_name in fns:
            fn = getattr(module, fn_name, None)
            if callable(fn):
                try:
                    res = fn()
                    return str(res) if res is not None else "ok"
                except Exception as e:
                    logger.exception("%s.%s() упал: %s", module_name, fn_name, e)
    logger.info("Не найдена функция снапшота — пропускаем.")
    return None


# === /cleanup ===

@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    await message.answer(
        "⚠️ Удалить pmi_history и bridges_history старше 90 дней?",
        reply_markup=kb.confirm_cleanup_keyboard(),
    )


# === CALLBACK'И ===

@router.callback_query(F.data.startswith("dlgs:"))
async def cb_dialogues(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        offset = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        offset = 0

    limit = DEFAULT_DIALOGUES_LIMIT
    dialogues = await asyncio.to_thread(q.get_recent_dialogues, limit, offset)
    total = await asyncio.to_thread(q.get_dialogue_count)
    text = fmt.format_dialogues_list(dialogues, offset=offset)
    markup = kb.dialogues_keyboard(dialogues, offset=offset, page_size=limit, total=total)

    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        # Если сообщение нельзя отредактировать (старое/идентичное) — просто шлём новое.
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("dlg:"))
async def cb_dialogue_detail(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    did = cb.data.split(":", 1)[1]
    d = await asyncio.to_thread(q.get_dialogue, did)
    if not d:
        await cb.answer("Не найден", show_alert=True)
        return
    text = fmt.format_dialogue_detail(d)
    markup = kb.dialogue_detail_keyboard(did)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("dlgok:"))
async def cb_dialogue_mark_ok(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    did = cb.data.split(":", 1)[1]
    operator_id = cb.from_user.id if cb.from_user else None
    await asyncio.to_thread(q.log_operator_action, did, "mark_ok", operator_id)
    await cb.answer("Помечено ok")


@router.callback_query(F.data.startswith("dlgfail:"))
async def cb_dialogue_mark_fail(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    did = cb.data.split(":", 1)[1]
    operator_id = cb.from_user.id if cb.from_user else None
    await asyncio.to_thread(q.log_operator_action, did, "mark_fail", operator_id)
    await cb.answer("Помечено fail")


@router.callback_query(F.data.startswith("vers:"))
async def cb_versions(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        sid = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        sid = 0

    if sid == 0:
        strategies = await asyncio.to_thread(q.get_strategies)
        text = fmt.format_strategies(strategies)
        markup = kb.strategies_keyboard(strategies)
    else:
        versions = await asyncio.to_thread(q.get_versions, sid)
        text = fmt.format_versions(sid, versions)
        markup = kb.versions_keyboard(sid, versions)

    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("vmet:"))
async def cb_version_metrics(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        _, sid_s, ver_s = cb.data.split(":", 2)
        sid = int(sid_s)
        ver = int(ver_s)
    except (ValueError, IndexError):
        await cb.answer("Некорректные данные", show_alert=True)
        return

    versions = await asyncio.to_thread(q.get_versions, sid)
    target = next((v for v in versions if v.get("version") == ver), None)
    if not target:
        await cb.answer("Версия не найдена", show_alert=True)
        return

    text = fmt.format_version_metrics(sid, target)
    markup = kb.version_metrics_keyboard(sid, ver)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("vrb:"))
async def cb_rollback_confirm(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        _, sid_s, ver_s = cb.data.split(":", 2)
        sid = int(sid_s)
        ver = int(ver_s)
    except (ValueError, IndexError):
        await cb.answer("Некорректные данные", show_alert=True)
        return
    await cb.message.answer(
        f"⚠️ Откатить стратегию <b>#{sid}</b> к версии <b>v{ver}</b>?",
        reply_markup=kb.rollback_confirm_keyboard(sid, ver),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("vrbyes:"))
async def cb_rollback_execute(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        _, sid_s, ver_s = cb.data.split(":", 2)
        sid = int(sid_s)
        ver = int(ver_s)
    except (ValueError, IndexError):
        await cb.answer("Некорректные данные", show_alert=True)
        return

    ok = await asyncio.to_thread(q.rollback_strategy, sid, ver)
    operator_id = cb.from_user.id if cb.from_user else None
    await asyncio.to_thread(q.log_operator_action, None, f"rollback:{sid}:{ver}", operator_id)

    if ok:
        await cb.message.answer(fmt.format_ok(f"Стратегия #{sid} откачена к v{ver}."))
    else:
        await cb.message.answer(fmt.format_error("Откат не удался (см. логи)."))
    await cb.answer()


@router.callback_query(F.data.startswith("brh:"))
async def cb_bridge_history(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    token = cb.data.split(":", 1)[1]
    history = await asyncio.to_thread(q.get_bridge_history, token, 20)
    text = fmt.format_bridge_history(token, history)
    markup = kb.bridge_history_keyboard(token)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("brs:"))
async def cb_bridges_list(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    bridges = await asyncio.to_thread(q.get_bridges, DEFAULT_LIST_LIMIT)
    text = fmt.format_bridges(bridges)
    markup = kb.bridges_keyboard(bridges)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    what = cb.data.split(":", 1)[1]
    if what == "status":
        data = await asyncio.to_thread(q.get_system_status)
        text = fmt.format_status(data)
    elif what == "stats":
        data = await asyncio.to_thread(q.get_overall_stats)
        text = fmt.format_stats(data)
    else:
        text = fmt.format_info("Меню")
    try:
        await cb.message.edit_text(text, reply_markup=kb.back_to_menu_keyboard())
    except Exception:
        await cb.message.answer(text, reply_markup=kb.back_to_menu_keyboard())
    await cb.answer()


@router.callback_query(F.data == "cleanup:yes")
async def cb_cleanup_yes(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    result = await asyncio.to_thread(q.cleanup_old_data, 90)
    text = fmt.format_ok(
        f"Очистка выполнена. pmi_history: {result.get('pmi_deleted', 0)}, "
        f"bridges_history: {result.get('bridges_deleted', 0)}."
    )
    try:
        await cb.message.edit_text(text)
    except Exception:
        await cb.message.answer(text)
    await cb.answer()


@router.callback_query(F.data == "cleanup:no")
async def cb_cleanup_no(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        await cb.message.edit_text(fmt.format_info("Очистка отменена."))
    except Exception:
        await cb.message.answer(fmt.format_info("Очистка отменена."))
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    try:
        await cb.message.edit_text(fmt.format_info("Отменено."))
    except Exception:
        await cb.message.answer(fmt.format_info("Отменено."))
    await cb.answer()


# === FALLBACK ===

@router.message()
async def fallback(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    await message.answer(fmt.format_info("Не знаю такой команды. /help — список."))


@router.callback_query()
async def fallback_cb(cb: CallbackQuery) -> None:
    if not _is_allowed(cb):
        await _deny(cb)
        return
    await cb.answer("Неизвестное действие", show_alert=False)
