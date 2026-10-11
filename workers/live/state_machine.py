# workers/live/state_machine.py
"""
Лёгкий in-memory FSM для live-worker.

Назначение:
- Хранить состояние каждого чата (chat_id) в памяти процесса.
- Автоматически сбрасывать «зависшие» состояния по TTL.
- Давать API: get, set, clear, snapshot, cleanup_stale.
- Thread-safe через asyncio.Lock.
- Без зависимостей от aiogram / telethon — чистый Python.

Состояния:
- "idle"              — ничего не происходит (по умолчанию).
- "generating"        — идёт генерация LLM-подсказки.
- "awaiting_operator" — подсказка готова, ждём оператора.
- "replied"           — ответ отправлен.

Не персистится в БД. При рестарте процесса всё теряется — это ок,
FSM отражает именно текущие операции в рантайме.

Использование в будущем:
- handler.py: set(chat_id, "generating") перед suggest, 
              set(chat_id, "awaiting_operator") после suggest.
- sender.py:  set(chat_id, "replied") после успешной отправки.
Сейчас handler/sender не трогаем.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("live-worker.state_machine")


# === КОНСТАНТЫ ===

STATE_IDLE = "idle"
STATE_GENERATING = "generating"
STATE_AWAITING_OPERATOR = "awaiting_operator"
STATE_REPLIED = "replied"

# Таймаут «зависших» состояний (сек). Настраивается через env.
DEFAULT_TIMEOUT_SEC = 600

# Состояния, которые НЕ считаются stale даже если долго не обновлялись.
_STICKY_STATES = frozenset({STATE_IDLE, STATE_AWAITING_OPERATOR})


def _read_timeout() -> int:
    raw = os.getenv("LIVE_STATE_TIMEOUT_SEC", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SEC
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_TIMEOUT_SEC
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


# === ЗАПИСЬ ===

@dataclass
class ChatState:
    state: str = STATE_IDLE
    data: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "state": self.state,
            "data": dict(self.data),
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "age_sec": round(now - self.updated_at, 2),
        }


# === FSM ===

class ChatStateMachine:
    """
    Thread-safe in-memory FSM.
    Синхронизация — asyncio.Lock (предполагается, что все вызовы из одного
    event loop). Для защиты от гонок между корутинами — этого достаточно.
    """

    def __init__(self, timeout_sec: Optional[int] = None) -> None:
        self._states: dict[int, ChatState] = {}
        self._lock = asyncio.Lock()
        self._timeout = int(timeout_sec) if timeout_sec else _read_timeout()

    # --- внутреннее (без блокировки) ---

    def _is_stale(self, st: ChatState) -> bool:
        if st.state in _STICKY_STATES:
            return False
        if self._timeout <= 0:
            return False
        return (time.time() - st.updated_at) > self._timeout

    def _reset_to_idle(self, st: ChatState, reason: str) -> None:
        st.state = STATE_IDLE
        st.data = {"reason": reason}
        st.updated_at = time.time()

    # --- публичное API ---

    async def get(self, chat_id: int) -> dict:
        """Возвращает состояние чата (dict). Если нет — idle."""
        cid = int(chat_id)
        async with self._lock:
            st = self._states.get(cid)
            if st is None:
                return ChatState().to_dict()
            if self._is_stale(st):
                logger.info(
                    "FSM: chat=%s state=%s stale (>%sс) — сброс в idle.",
                    cid, st.state, self._timeout,
                )
                self._reset_to_idle(st, reason="ttl_expired")
            return st.to_dict()

    async def set(self, chat_id: int, state: str, **data: Any) -> dict:
        """Устанавливает состояние. Переход логируется INFO."""
        cid = int(chat_id)
        new_state = str(state)
        async with self._lock:
            st = self._states.get(cid)
            old_state = st.state if st else None
            if st is None:
                st = ChatState()
                self._states[cid] = st
            st.state = new_state
            st.data = dict(data or {})
            st.updated_at = time.time()
            result = st.to_dict()

        if old_state != new_state:
            logger.info(
                "FSM: chat=%s %s -> %s%s",
                cid, old_state or "(none)", new_state,
                f" ({data})" if data else "",
            )
        return result

    async def clear(self, chat_id: int) -> None:
        """Удаляет запись о чате."""
        cid = int(chat_id)
        async with self._lock:
            existed = self._states.pop(cid, None)
        if existed is not None:
            logger.info("FSM: chat=%s очищен (был state=%s).", cid, existed.state)

    async def snapshot(self) -> dict:
        """Возвращает все состояния (для логов / диагностики / web-CRM)."""
        async with self._lock:
            now = time.time()
            return {
                str(cid): {
                    **st.to_dict(),
                    "age_sec": round(now - st.updated_at, 2),
                }
                for cid, st in self._states.items()
            }

    async def cleanup_stale(self) -> int:
        """
        Помечает (или сбрасывает) stale-чаты в idle.
        Возвращает количество сброшенных.
        """
        reset = 0
        async with self._lock:
            for cid, st in self._states.items():
                if self._is_stale(st):
                    logger.info(
                        "FSM.cleanup_stale: chat=%s state=%s -> idle.",
                        cid, st.state,
                    )
                    self._reset_to_idle(st, reason="cleanup_stale")
                    reset += 1
        if reset:
            logger.info("FSM.cleanup_stale: сброшено %d чатов.", reset)
        return reset

    async def is_busy(self, chat_id: int) -> bool:
        """True, если чат в активной операции (generating/replied)."""
        st = await self.get(chat_id)
        return st["state"] in (STATE_GENERATING, STATE_REPLIED)

    @property
    def timeout_sec(self) -> int:
        return self._timeout


# === SINGLETON ===

_FSM_SINGLETON: Optional[ChatStateMachine] = None
_FSM_INIT_LOCK = asyncio.Lock()


async def get_fsm() -> ChatStateMachine:
    """Единый экземпляр FSM на процесс (ленивая async-инициализация)."""
    global _FSM_SINGLETON
    if _FSM_SINGLETON is None:
        async with _FSM_INIT_LOCK:
            if _FSM_SINGLETON is None:
                _FSM_SINGLETON = ChatStateMachine()
                logger.info(
                    "FSM: создан singleton ChatStateMachine (timeout=%ss).",
                    _FSM_SINGLETON.timeout_sec,
                )
    return _FSM_SINGLETON


def get_fsm_sync() -> Optional[ChatStateMachine]:
    """
    Возвращает уже созданный singleton без await (может быть None,
    если get_fsm() ещё ни разу не вызывали).
    """
    return _FSM_SINGLETON
