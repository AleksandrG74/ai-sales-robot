# scripts/baseline_experiment.py
"""
Baseline-эксперимент: сравнение "голого" LLM и нашего агента (bootstrap).

Использование:
    python -m scripts.baseline_experiment
"""

import asyncio
import json
import logging
import random
import sys
from pathlib import Path
from typing import Optional

# Добавляем корень проекта в sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select, func
from core.database import get_session, ProcessedDialogue
from core.llm import LLMClient
from core.embeddings import Embedder

# === НАСТРОЙКИ ===
NUM_DIALOGUES = 20
BOOTSTRAP_AVG_COSINE = 0.589  # из предыдущих прогонов

# === ЛОГИРОВАНИЕ ===
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("baseline")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger

log = setup_logging()


def _safe_json_loads(raw):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def get_random_dialogues(limit: int) -> list[ProcessedDialogue]:
    """Берёт случайные диалоги с фазой bootstrap."""
    try:
        with get_session() as session:
            # Получаем все ID, где phase='bootstrap' и есть result_json
            rows = session.execute(
                select(ProcessedDialogue.id)
                .where(ProcessedDialogue.result_json.isnot(None))
                .order_by(func.random())
                .limit(limit * 3) # Берем с запасом, т.к. не все могут иметь phase='bootstrap'
            ).scalars().all()

            if not rows:
                return []

            # Теперь достаем сами объекты
            ds = session.execute(
                select(ProcessedDialogue).where(ProcessedDialogue.id.in_(rows))
            ).scalars().all()

            # Фильтруем по фазе bootstrap в Python (т.к. phase внутри JSON)
            valid = []
            for d in ds:
                data = _safe_json_loads(d.result_json) or {}
                if data.get("phase") == "bootstrap":
                    # Запоминаем распарсенный JSON в объекте, чтобы не парсить повторно
                    d._parsed_result = data
                    valid.append(d)
            
            return valid[:limit]
    except Exception as e:
        log.error("Ошибка получения диалогов: %s", e)
        return []


async def run_baseline():
    log.info("Запуск Baseline-эксперимента (голый LLM)...")
    
    dialogues = get_random_dialogues(NUM_DIALOGUES)
    if not dialogues:
        log.error("Нет диалогов с phase='bootstrap'. Эксперимент прерван.")
        return

    log.info("Выбрано %d диалогов для теста.", len(dialogues))

    llm = LLMClient()
    embedder = Embedder() # Инициализация может занять время (загрузка rubert-tiny2)

    results = []
    cosines = []

    for i, d in enumerate(dialogues, start=1):
        data = d._parsed_result
        expected_text = data.get("expected_text")
        
        # Извлекаем "последнее сообщение клиента" из промпта
        # (поле last_client_message мы не сохраняли в result_json)
        prompt_full = data.get("prompt") or ""
        last_client_msg = ""
        if "## Последнее сообщение клиента" in prompt_full:
            part = prompt_full.split("## Последнее сообщение клиента", 1)[1]
            # Обрезаем по следующему заголовку "## " если есть
            if "\n## " in part:
                part = part.split("\n## ", 1)[0]
            last_client_msg = part.strip().strip("-").strip()
        
        # Fallback, если по заголовку не нашли
        if not last_client_msg:
            last_client_msg = data.get("actual", "")[:300]

        if not last_client_msg or not expected_text:
            log.warning("[%d/%d] Пропуск %s: пустое сообщение или expected_text.", i, len(dialogues), d.dialogue_id)
            continue

        # Формируем простой промпт
        prompt = f"Ответь на последнее сообщение клиента: {last_client_msg}"
        messages = [{"role": "user", "content": prompt}]

        try:
            # Вызов LLM
            response = await llm.chat(messages=messages)
            llm_text = response.content if hasattr(response, 'content') else str(response)
            
            # Считаем cosine
            emb_llm = embedder.encode(llm_text)
            emb_expected = embedder.encode(expected_text)
            cosine = embedder.cosine(emb_llm, emb_expected)
            
            cosines.append(cosine)
            
            results.append({
                "dialogue_id": d.dialogue_id,
                "last_client_message": last_client_msg,
                "expected_text": expected_text,
                "llm_response": llm_text,
                "cosine": round(cosine, 4)
            })
            
            log.info("[%d/%d] %s | cosine: %.4f", i, len(dialogues), d.dialogue_id, cosine)

        except Exception as e:
            log.error("[%d/%d] Ошибка LLM на %s: %s", i, len(dialogues), d.dialogue_id, e)
            continue

    if not cosines:
        log.error("Не удалось получить ни одного ответа от LLM. Эксперимент провален.")
        return

    avg_baseline = sum(cosines) / len(cosines)

    print("\n" + "="*50)
    print(f"Baseline avg cosine: {avg_baseline:.3f} (на {len(cosines)} диалогах)")
    print(f"Bootstrap avg cosine: {BOOTSTRAP_AVG_COSINE:.3f} (из БД)")
    print("="*50 + "\n")

    # Сохраняем лог
    log_dir = Path("/app/logs")
    if not log_dir.is_dir():
        log_dir = Path("./logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    out_file = log_dir / "baseline_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "avg_baseline_cosine": round(avg_baseline, 4),
            "bootstrap_avg_cosine": BOOTSTRAP_AVG_COSINE,
            "num_dialogues": len(cosines),
            "details": results
        }, f, ensure_ascii=False, indent=2)

    log.info("Детальные результаты сохранены в %s", out_file)


if __name__ == "__main__":
    asyncio.run(run_baseline())
