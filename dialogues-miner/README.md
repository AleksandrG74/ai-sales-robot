# dialogues-miner

Поисковик успешных диалогов в Telegram-каналах.

## Задача

Найти треды, где:
- ровно один автор дал **точное** слово-согласие из списка 56 слов
- в треде **≥ 2 разных автора**
- в треде **≤ 200 слов**
- сообщения связаны через `reply_to_msg_id`

## Структура

- `main.py` — CLI (`--fetch`, `--mine`, `--export`)
- `search_engine.py` — ядро: построение тредов, фильтрация
- `db_writer.py` — Telethon → БД
- `dialogue_exporter.py` — JSON-выгрузка
- `target_words.json` — 56 слов-согласий
- `sources.json` — каналы Telegram
- `configs/mining_config.json` — параметры
- `web/` — веб-интерфейс (просмотр статистики)

## Запуск

### Локально

```bash
pip install -r requirements.txt
python main.py --mode mine --db path/to/database.db
