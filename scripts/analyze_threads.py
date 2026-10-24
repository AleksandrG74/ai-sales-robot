# scripts/analyze_threads.py
"""
Анализ JSON-тредов из папки (экспорт miner'а).
Считает:
- частоту слов (топ-50),
- сопоставление со словами из token_groups.json,
- сколько тредов содержат токены каждой группы.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

# Путь к папке с тредами
THREADS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/Users/52602/Downloads/threads (3)")
TOKEN_GROUPS = Path("configs/token_groups.json")

def load_token_groups() -> dict:
    with open(TOKEN_GROUPS, "r", encoding="utf-8") as f:
        return json.load(f).get("groups", {})

def extract_words(text: str) -> list[str]:
    """Простейшая токенизация: по не-буквам."""
    text = (text or "").lower()
    return re.findall(r"[а-яёa-z]{2,}", text)

def main():
    if not THREADS_DIR.is_dir():
        print(f"Папка не найдена: {THREADS_DIR}")
        sys.exit(1)

    files = list(THREADS_DIR.glob("*.json"))
    print(f"Найдено файлов: {len(files)}")

    groups = load_token_groups()
    group_names = list(groups.keys())
    print(f"Группы: {group_names}\n")

    word_counter: Counter = Counter()
    # Счётчик: сколько тредов содержат хотя бы 1 токен группы
    group_thread_counts: dict[str, int] = {g: 0 for g in group_names}

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        messages = data.get("messages") or []
        full_text = " ".join((m.get("message") or "") for m in messages if isinstance(m, dict))
        words = extract_words(full_text)
        word_counter.update(words)

        # Проверяем, какие группы встречаются в этом треде
        lowered = full_text.lower()
        for gname, gspec in groups.items():
            tokens = gspec.get("tokens", [])
            if any(tok.lower() in lowered for tok in tokens):
                group_thread_counts[gname] += 1

    # Топ-50 слов
    print("=== Топ-50 слов по частоте ===")
    for word, count in word_counter.most_common(50):
        # К какой группе относится?
        g = "-"
        for gname, gspec in groups.items():
            if word in [t.lower() for t in gspec.get("tokens", [])]:
                g = gname
                break
        print(f"{word:20s} {count:5d}  [{g}]")

    # Покрытие группами
    print("\n=== Сколько тредов содержат токены каждой группы ===")
    for gname in group_names:
        cnt = group_thread_counts[gname]
        pct = cnt / max(len(files), 1) * 100
        print(f"{gname:12s}: {cnt:4d} тредов ({pct:5.1f}%)")

    print(f"\nВсего файлов: {len(files)}")

if __name__ == "__main__":
    main()
