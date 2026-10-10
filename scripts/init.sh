#!/usr/bin/env bash
# Инициализация ai-sales-robot.
# Запускается вручную из контейнера или при старте.
set -euo pipefail

cd "$(dirname "$0")/.."
echo "[init] PWD=$(pwd)"

# 1. Папки
mkdir -p data data/strategies data/raw_threads data/hf_cache logs configs
echo "[init] dirs created"

# 2. Инициализация БД
python -m core.database
echo "[init] db initialised"

# 3. Дефолтные configs
if [ ! -f configs/product.json ]; then
    cat > configs/product.json <<'JSON'
{
  "name": "Demo Product",
  "description": "Описание товара для демонстрации",
  "price": 0,
  "currency": "RUB",
  "url": "",
  "features": []
}
JSON
    echo "[init] configs/product.json created"
fi

if [ ! -f configs/goal.json ]; then
    cat > configs/goal.json <<'JSON'
{
  "goal": "Заинтересовать клиента и договориться о следующем шаге",
  "success_criteria": "Клиент согласился на звонок/встречу",
  "tone": "friendly",
  "max_dialogue_turns": 10
}
JSON
    echo "[init] configs/goal.json created"
fi

echo "[init] done"
