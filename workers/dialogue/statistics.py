"""Статистика для dialogue-worker.

Обновляет word_stats, pmi_history, connection_matrix.
PMI считается по processed_dialogues (fallback на raw_threads).
"""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.config import settings
from core.database import (
    ConnectionMatrix,
    PMIHistory,
    ProcessedDialogue,
    WordStat,
    get_session,
)

log = logging.getLogger("dialogue.statistics")
log.setLevel(settings.log_level.upper())


# ============================================================
# Внутренние счётчики (для снапшотов)
# ============================================================
_snapshot_lock = threading.Lock()
_dialogues_since_snapshot = 0
SNAPSHOT_EVERY_N = 10

TOP_TOKENS_LIMIT = 10


# ============================================================
# word_stats
# ============================================================
def update_word_stats(tokens: Iterable[str], success: bool) -> int:
    """Обновляет word_stats для списка токенов.

    Returns:
        Сколько токенов обновлено.
    """
    tokens = [t.strip().lower() for t in tokens if t and t.strip()]
    if not tokens:
        return 0

    updated = 0
    try:
        with get_session() as s:
            for t in tokens:
                ws = s.query(WordStat).filter_by(word=t).one_or_none()
                if ws is None:
                    ws = WordStat(word=t, total_count=0, success_count=0)
                    s.add(ws)
                    s.flush()
                ws.total_count = (ws.total_count or 0) + 1
                if success:
                    ws.success_count = (ws.success_count or 0) + 1
                total = ws.total_count or 1
                ws.success_rate = float(ws.success_count or 0) / float(total)
                updated += 1
    except Exception as exc:
        log.exception("update_word_stats failed: %s", exc)
        return 0

    log.debug("word_stats updated for %d tokens (success=%s)", updated, success)
    return updated


def get_top_tokens(
    n: int = TOP_TOKENS_LIMIT,
    min_count: int = 1,
    by: str = "success_rate",
) -> list[dict[str, Any]]:
    """Возвращает топ-N токенов.

    by: 'success_rate' | 'total_count' | 'pmi_score'
    """
    try:
        with get_session() as s:
            q = s.query(WordStat).filter(WordStat.total_count >= min_count)
            if by == "success_rate":
                q = q.order_by(WordStat.success_rate.desc(), WordStat.word.asc())
            elif by == "total_count":
                q = q.order_by(WordStat.total_count.desc(), WordStat.word.asc())
            elif by == "pmi_score":
                q = q.order_by(WordStat.pmi_score.desc(), WordStat.word.asc())
            else:
                q = q.order_by(WordStat.word.asc())
            rows = q.limit(n).all()
            return [
                {
                    "word": r.word,
                    "total_count": r.total_count,
                    "success_count": r.success_count,
                    "success_rate": r.success_rate,
                    "pmi_score": r.pmi_score,
                }
                for r in rows
            ]
    except Exception as exc:
        log.exception("get_top_tokens failed: %s", exc)
        return []


# ============================================================
# Загрузка корпуса токенов для PMI
# ============================================================
def _load_corpus() -> list[list[str]]:
    """Список "документов" — каждый документ = набор токенов из одного диалога.

    Сначала пробуем processed_dialogues.result_json.top_tokens.
    Если пусто — читаем raw_threads/*.json, собираем уникальные слова.
    """
    corpus: list[list[str]] = []

    try:
        with get_session() as s:
            rows = s.query(ProcessedDialogue).filter(
                ProcessedDialogue.status == "done"
            ).all()
            for r in rows:
                data = r.result_json or {}
                toks = data.get("top_tokens") or []
                if not toks:
                    # fallback — соберём токены из messages, если сохранены
                    msgs = data.get("messages") or []
                    text = " ".join((m.get("message") or "") for m in msgs)
                    toks = _split_words(text)
                toks = [t.lower() for t in toks if t]
                if toks:
                    corpus.append(sorted(set(toks)))
    except Exception as exc:
        log.exception("_load_corpus (processed) failed: %s", exc)

    if corpus:
        return corpus

    # fallback на raw_threads
    log.info("processed_dialogues пуст — читаем raw_threads для PMI")
    raw_dir = settings.raw_threads_dir
    try:
        for path in sorted(Path(raw_dir).glob("*.json")):
            if path.name.startswith("_"):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            text = " ".join((m.get("message") or "") for m in data.get("messages", []))
            toks = sorted(set(_split_words(text)))
            if toks:
                corpus.append(toks)
    except Exception as exc:
        log.exception("_load_corpus (raw_threads) failed: %s", exc)

    return corpus


_WORD_SPLIT_CHARS = " \t\n\r.,!?;:()[]{}\"'«»—–-…"

def _split_words(text: str) -> list[str]:
    """Простое разбиение на слова (lowercase, без пунктуации)."""
    if not text:
        return []
    text = text.lower()
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in _WORD_SPLIT_CHARS:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [w for w in out if len(w) >= 2]


# ============================================================
# PMI
# ============================================================
def _build_counts(corpus: list[list[str]]) -> tuple[Counter, Counter, int]:
    """Возвращает (word_counts, pair_counts, N).

    word_counts[word] — сколько документов содержит слово.
    pair_counts[(a,b)] — сколько документов содержит оба слова.
    N — общее число документов.
    """
    word_counts: Counter = Counter()
    pair_counts: Counter = Counter()
    for doc in corpus:
        uniq = sorted(set(doc))
        for w in uniq:
            word_counts[w] += 1
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pair_counts[(uniq[i], uniq[j])] += 1
    return word_counts, pair_counts, len(corpus)


def compute_pmi(a: str, b: str, *, corpus: Optional[list[list[str]]] = None) -> Optional[float]:
    """PMI(a, b) = log( P(a,b) / (P(a) * P(b)) )."""
    a = a.strip().lower()
    b = b.strip().lower()
    if not a or not b or a == b:
        return None

    if corpus is None:
        corpus = _load_corpus()
    if not corpus:
        return None

    word_counts, pair_counts, N = _build_counts(corpus)
    if N == 0:
        return None

    ca = word_counts.get(a, 0)
    cb = word_counts.get(b, 0)
    if ca == 0 or cb == 0:
        return None

    key = (a, b) if a < b else (b, a)
    cab = pair_counts.get(key, 0)
    if cab == 0:
        return None

    p_a = ca / N
    p_b = cb / N
    p_ab = cab / N
    if p_a <= 0 or p_b <= 0 or p_ab <= 0:
        return None

    return math.log(p_ab / (p_a * p_b))


def compute_all_pmi(top_n: int = 50) -> dict[str, float]:
    """Возвращает {word: pmi_avg} для top_n токенов.

    Для каждого токена усредняем PMI с остальными токенами из топа.
    """
    corpus = _load_corpus()
    if not corpus:
        return {}

    word_counts, pair_counts, N = _build_counts(corpus)
    if N == 0:
        return {}

    # Топ токенов по частоте
    top_words = [w for w, _ in word_counts.most_common(top_n)]
    result: dict[str, float] = {}

    for a in top_words:
        ca = word_counts.get(a, 0)
        if ca == 0:
            continue
        p_a = ca / N
        pmi_sum = 0.0
        pmi_n = 0
        for b in top_words:
            if b == a:
                continue
            cb = word_counts.get(b, 0)
            if cb == 0:
                continue
            key = (a, b) if a < b else (b, a)
            cab = pair_counts.get(key, 0)
            if cab == 0:
                continue
            p_b = cb / N
            p_ab = cab / N
            if p_a <= 0 or p_b <= 0 or p_ab <= 0:
                continue
            pmi_sum += math.log(p_ab / (p_a * p_b))
            pmi_n += 1
        if pmi_n > 0:
            result[a] = pmi_sum / pmi_n

    return result


def calculate_pmi_word_success(
    word: str,
    total_docs: int,
    word_count: int,
    success_count: int,
    total_success: int,
) -> float:
    """
    PMI(word, success) = log2( P(word, success) / (P(word) * P(success)) )

    Входы:
    - total_docs:    всего диалогов (знаменатель)
    - word_count:    в скольких диалогах встречается слово
    - success_count: в скольких из них диалог успешен
    - total_success: сколько всего успешных диалогов
    """
    if total_docs <= 0 or word_count <= 0 or success_count <= 0 or total_success <= 0:
        return 0.0

    p_ws = success_count / total_docs
    p_w = word_count / total_docs
    p_s = total_success / total_docs

    try:
        pmi = math.log2(p_ws / (p_w * p_s))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return round(pmi, 4)


def update_pmi_scores_word_success() -> int:
    """
    Обновляет word_stats.pmi_score по формуле PMI(word, success),
    используя счётчики из word_stats и processed_dialogues.

    Возвращает количество обновлённых слов.
    """
    try:
        from core.database import get_session, WordStat, ProcessedDialogue
        from sqlalchemy import select, func
        with get_session() as session:
            total_docs = int(session.execute(
                select(func.count(ProcessedDialogue.id))
            ).scalar_one() or 0)
            if total_docs == 0:
                return 0

            # total_success: считаем по статусу 'ok' (или success)
            total_success = int(session.execute(
                select(func.count(ProcessedDialogue.id))
                .where(ProcessedDialogue.status.in_(("ok", "success")))
            ).scalar_one() or 0)

            if total_success == 0:
                return 0

            rows = session.execute(select(WordStat)).scalars().all()
            updated = 0
            for r in rows:
                if not r.word:
                    continue
                wc = int(r.total_count or 0)
                sc = int(r.success_count or 0)
                pmi = calculate_pmi_word_success(
                    word=r.word,
                    total_docs=total_docs,
                    word_count=wc,
                    success_count=sc,
                    total_success=total_success,
                )
                r.pmi_score = pmi
                updated += 1
            session.commit()
            log.info("update_pmi_scores_word_success: обновлено %d слов.", updated)
            return updated
    except Exception as exc:
        log.exception("update_pmi_scores_word_success упал: %s", exc)
        return 0


def update_pmi_scores(scores: dict[str, float]) -> int:
    """Обновляет word_stats.pmi_score для всех слов из scores."""
    if not scores:
        return 0
    updated = 0
    try:
        with get_session() as s:
            for word, pmi in scores.items():
                ws = s.query(WordStat).filter_by(word=word).one_or_none()
                if ws is None:
                    ws = WordStat(word=word)
                    s.add(ws)
                ws.pmi_score = float(pmi)
                updated += 1
    except Exception as exc:
        log.exception("update_pmi_scores failed: %s", exc)
        return 0
    return updated


# ============================================================
# pmi_history (снапшоты)
# ============================================================
def record_pmi_snapshot(window_start: Optional[datetime] = None,
                        window_end: Optional[datetime] = None) -> int:
    """Записывает снапшот PMI в pmi_history. Возвращает число записей."""
    if window_end is None:
        window_end = datetime.now(timezone.utc)
    if window_start is None:
        window_start = window_end - timedelta(days=1)

    scores = compute_all_pmi()
    if not scores:
        log.info("PMI snapshot: пусто, пропускаем")
        return 0

    # положим pmi в word_stats
    update_pmi_scores(scores)

    records = 0
    try:
        with get_session() as s:
            for word, pmi in scores.items():
                rec = PMIHistory(
                    word=word,
                    window_start=window_start,
                    window_end=window_end,
                    pmi=float(pmi),
                    cooccurrence_count=0,
                    total_count=0,
                )
                s.add(rec)
                records += 1
    except Exception as exc:
        log.exception("record_pmi_snapshot failed: %s", exc)
        return 0

    log.info("PMI snapshot saved: %d words", records)
    return records


def maybe_snapshot(force: bool = False) -> bool:
    """Делает снапшот раз в SNAPSHOT_EVERY_N диалогов (или при force=True).

    Возвращает True, если снапшот был сделан.
    """
    global _dialogues_since_snapshot
    with _snapshot_lock:
        _dialogues_since_snapshot += 1
        should = force or (_dialogues_since_snapshot >= SNAPSHOT_EVERY_N)
        if should:
            _dialogues_since_snapshot = 0

    if should:
        try:
            record_pmi_snapshot()
            return True
        except Exception as exc:
            log.exception("maybe_snapshot failed: %s", exc)
    return False


def cleanup_old_pmi(days: int = 90) -> int:
    """Удаляет pmi_history старше N дней. Возвращает число удалённых записей."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        with get_session() as s:
            deleted = (
                s.query(PMIHistory)
                .filter(PMIHistory.window_end < cutoff)
                .delete(synchronize_session=False)
            )
        log.info("cleanup_old_pmi: deleted %d records (older than %d days)", deleted, days)
        return int(deleted or 0)
    except Exception as exc:
        log.exception("cleanup_old_pmi failed: %s", exc)
        return 0


# ============================================================
# connection_matrix
# ============================================================
def update_connection_matrix(tokens: Iterable[str], *, weight_step: float = 0.1) -> int:
    """Попарно обновляет рёбра для токенов одного диалога.

    weight_step — прирост weight за каждый совместный диалог.
    """
    uniq = sorted({t.strip().lower() for t in tokens if t and t.strip()})
    if len(uniq) < 2:
        return 0

    updated = 0
    try:
        with get_session() as s:
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    a, b = uniq[i], uniq[j]
                    row = (
                        s.query(ConnectionMatrix)
                        .filter_by(source_token=a, target_token=b)
                        .one_or_none()
                    )
                    if row is None:
                        row = ConnectionMatrix(
                            source_token=a,
                            target_token=b,
                            weight=0.0,
                            cooccurrence_count=0,
                        )
                        s.add(row)
                        s.flush()
                    row.cooccurrence_count = (row.cooccurrence_count or 0) + 1
                    row.weight = float(row.weight or 0.0) + float(weight_step)
                    updated += 1
    except Exception as exc:
        log.exception("update_connection_matrix failed: %s", exc)
        return 0
    return updated


def get_top_connections(n: int = 20, min_count: int = 1) -> list[dict[str, Any]]:
    try:
        with get_session() as s:
            rows = (
                s.query(ConnectionMatrix)
                .filter(ConnectionMatrix.cooccurrence_count >= min_count)
                .order_by(ConnectionMatrix.weight.desc(), ConnectionMatrix.source_token.asc())
                .limit(n)
                .all()
            )
            return [
                {
                    "source": r.source_token,
                    "target": r.target_token,
                    "weight": r.weight,
                    "cooccurrence_count": r.cooccurrence_count,
                }
                for r in rows
            ]
    except Exception as exc:
        log.exception("get_top_connections failed: %s", exc)
        return []


# ============================================================
# Тест
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("=== update_word_stats ===")
    n = update_word_stats(["да", "ок", "факт"], success=True)
    print(f"updated {n} tokens")

    print("\n=== top tokens ===")
    for row in get_top_tokens(5):
        print(row)

    print("\n=== connection matrix ===")
    n = update_connection_matrix(["да", "ок", "факт"])
    print(f"updated {n} edges")
    for row in get_top_connections(5):
        print(row)

    print("\n=== PMI snapshot ===")
    cnt = record_pmi_snapshot()
    print(f"snapshot wrote {cnt} records")
