# 🤖 AI Sales Robot

> Умный Telegram-ассистент для поиска лидов и автоматизации продаж.
> Transfer Learning без переобучения LLM: агент учится на истории диалогов и постепенно адаптирует стратегии.

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![aiogram](https://img.shields.io/badge/aiogram-3.7-2CA5E0?logo=telegram)](https://docs.aiogram.dev/)
[![Telethon](https://img.shields.io/badge/Telethon-1.34-0088cc)](https://docs.telethon.dev/)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-7c3aed)](https://www.deepseek.com/)
[![SQLite](https://img.shields.io/badge/DB-SQLite-003B57?logo=sqlite)](https://www.sqlite.org/)

---

## 📊 Результаты эксперимента

Сравнение «голого» LLM и агента с нашим пайплайном (Transfer Learning + PMI + стратегии).

| Метод | Avg Cosine (сходство с ответом менеджера) |
|---|---|
| **Baseline** (голый LLM, без промптов и таблиц) | `0.538` |
| **Bootstrap** (наш агент: стратегии + PMI + bridges) | `0.589` |
| **Прирост** | **+9.5%** ✅ |

> На выборке из 20 случайных диалогов bootstrap-фазы. Полный лог — `logs/baseline_results.json`.

---

## 🏗 Архитектура

Все 5 сервисов работают **в одном Docker-контейнере** под управлением `orchestrator`:

```text
┌─────────────────────────────────────────────────────────────┐
│ Docker-контейнер ai_sales_robot                             │
│                                                             │
│ ┌──────────────────┐                                        │
│ │ orchestrator     │ ← PID 1, следит за жизнью сервисов     │
│ └────────┬─────────┘                                        │
│          │                                                  │
│          ├── dialogues-miner (Telethon, парсит каналы)      │
│          ├── dialogue-worker (bootstrap-обучение, LLM)      │
│          ├── live-worker (user-account, real-time)          │
│          ├── telegram-worker (бот оператора, aiogram)       │
│          └── web-backend (FastAPI + Jinja2 UI)              │
│                                                             │
│ Общие ресурсы:                                              │
│   /app/data/robot.db     — SQLite (13 таблиц)               │
│   /app/data/raw_threads/ — JSON-треды от miner'а            │
│   /app/data/hf_cache/    — кэш rubert-tiny2                 │
│   /app/logs/             — логи всех сервисов               │
└─────────────────────────────────────────────────────────────┘
```

### Сервисы

| Сервис | Назначение |
|---|---|
| **dialogues-miner** | Парсит Telegram-каналы через Telethon, находит треды по target-словам, пишет JSON в `raw_threads/`. |
| **dialogue-worker** | Обрабатывает завершённые треды, определяет роль клиента и агрессию, генерирует ответы через DeepSeek, считает cosine, обновляет `word_stats`/`pmi_history`/`bridges`. |
| **live-worker** | Telethon-клиент user-account: слушает новые сообщения в `monitored_chats`, сохраняет в `live_messages`, опционально генерирует LLM-подсказки. |
| **telegram-worker** | aiogram 3.x бот оператора: команды `/status`, `/live`, `/monitor`, уведомления о новых сообщениях. |
| **web-backend** | FastAPI: REST API + Jinja2-страницы для оператора (дашборд, диалоги, live-чаты, конфиги). |
| **orchestrator** | Запускает 4 подпроцесса, следит за живостью, рестартит упавшие. |

---

## 🚀 Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/AleksandrG74/ai-sales-robot.git
cd ai-sales-robot
```

### 2. Настроить `.env`

```bash
cp .env.example .env
```

Заполните:

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота оператора (от `@BotFather`) |
| `TELEGRAM_OPERATOR_CHAT_ID` | Ваш `chat_id` (для whitelist) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | С https://my.telegram.org |
| `TELEGRAM_PHONE` | Номер user-account |
| `LIVE_SESSION_PATH` | Путь к `.session` файлу |
| `DEEPSEEK_API_KEY` | Ключ DeepSeek API |
| `WEB_BACKEND_URL` | Публичный адрес web-backend (для кнопок) |
| `LIVE_AUTO_REPLY` / `LIVE_LLM_SUGGEST` | Авто-ответ и LLM-подсказки (`0`/`1`) |

### 3. Запустить

```bash
docker compose build --no-cache ai-sales-robot
docker compose up -d
```

Проверить статус:

```bash
docker ps | grep ai_sales_robot
```

Открыть веб-интерфейс:

```text
http://localhost:8080/
```

Проверить health:

```bash
curl http://localhost:8080/health
```

---

## 🤖 Команды бота оператора

Бот `@sasa74bot` — только для оператора, с клиентами не общается (клиенты получают ответы через user-account из `live-worker`).

| Команда | Что делает |
|---|---|
| `/start` | Приветствие и список команд |
| `/help` | Полная справка |
| `/status` | Состояние системы (счётчики, последний прогон) |
| `/stats` | Метрики (avg score, avg cosine, роли, агрессия) |
| `/dialogues [N]` | Список последних N диалогов |
| `/dialogue <id>` | Детали одного диалога |
| `/versions` | Список стратегий и их версий |
| `/rollback <id> <ver>` | Откат стратегии к указанной версии |
| `/words [N]` | Топ-N токенов по `success_rate` |
| `/bridges [N]` | Топ-N мостов по `weight` |
| `/runs [N]` | Последние N прогонов |
| `/export` | Выгрузить CSV-метрики |
| `/cleanup` | Очистка старых данных (> 90 дней) |
| `/live` | Последние 10 входящих сообщений |
| `/monitor` | FSM: переслать сообщение из группы — добавить в мониторинг |
| `/unmonitor <chat_id>` | Удалить чат из мониторинга |
| `/keywords <chat_id> <kw1,kw2>` | Установить фильтр по ключевым словам |

### Уведомления

При новом входящем сообщении из `monitored_chats` бот автоматически присылает уведомление с inline-кнопками:

- **✅ Прочитано** — пометить как прочитанное
- **🔗 Открыть в вебе** — перейти на страницу `/live/{chat_id}`
- **🔇 Молчать** — отключить уведомления для чата

---

## 📁 Структура проекта

```text
ai-sales-robot/
├── core/                    # Ядро
│   ├── config.py            # pydantic-settings, читает .env
│   ├── database.py          # SQLAlchemy 2.0, 13 таблиц
│   ├── llm.py               # LLMClient (DeepSeek)
│   └── embeddings.py        # Embedder (rubert-tiny2)
│
├── workers/
│   ├── dialogue/            # Bootstrap-обучение
│   │   ├── agent.py         # Роль, агрессия, LLM, cosine
│   │   ├── prompt_builder.py
│   │   ├── statistics.py    # word_stats, PMI
│   │   ├── bridges.py       # Переходы между этапами
│   │   ├── versioning.py    # Версии стратегий
│   │   └── main.py          # Цикл сканирования raw_threads
│   │
│   ├── live/                # User-account (Telethon)
│   │   ├── telethon_client.py
│   │   ├── handler.py       # NewMessage → live_messages
│   │   ├── sender.py        # Отправка исходящих
│   │   ├── monitor.py       # CRUD monitored_chats
│   │   ├── suggest.py       # LLM-подсказки
│   │   ├── prompt_builder.py
│   │   ├── state_machine.py
│   │   └── main.py
│   │
│   └── telegram/            # Бот оператора (aiogram)
│       ├── queries.py
│       ├── formatters.py
│       ├── keyboards.py
│       ├── notifications.py # NotificationPoller
│       ├── handlers.py
│       └── main.py
│
├── web/
│   └── backend/
│       ├── main.py          # FastAPI app
│       ├── api.py           # REST API (20 эндпоинтов)
│       ├── pages.py         # Jinja2-страницы
│       ├── queries.py
│       ├── templates/       # base, dashboard, dialogues, live, ...
│       └── static/style.css
│
├── scripts/
│   ├── export_csv.py        # Выгрузка метрик
│   ├── cleanup_old_data.py  # Очистка старых данных
│   └── baseline_experiment.py
│
├── configs/
│   ├── product.json         # Что продаём
│   ├── goal.json            # Цель диалога
│   ├── token_groups.json    # 5 групп токенов
│   └── phase.json           # bootstrap / online / hybrid
│
├── orchestrator/main.py
├── docker-compose.yml
├── Dockerfile, Dockerfile.base
├── requirements.txt
└── .env.example
```

---

## 🧠 Как это работает (кратко)

1. **Bootstrap-фаза.** `dialogues-miner` собирает треды из Telegram-каналов → `dialogue-worker` обрабатывает 522 треда через DeepSeek, определяет роли/агрессию, генерирует ответы, считает cosine с реальными ответами менеджеров, обновляет `word_stats` (PMI), `bridges_history`.
2. **Transfer Learning.** На основе статистики слов и PMI строятся стратегии (`token_group × role × aggression`). Каждая стратегия версионируется; при прогоне агент выбирает активную версию и собирает промпт.
3. **Online-фаза.** `live-worker` слушает выбранные чаты (`monitored_chats`), сохраняет входящие в `live_messages`, опционально генерирует LLM-подсказки. Оператор видит их в боте (уведомления) и в web-CRM (`/live/{chat_id}`), отвечает через user-account.

---

## 🛠 Технологии

- **Python 3.11**
- **FastAPI 0.109** + Jinja2 3.1 + StaticFiles
- **aiogram 3.7** (бот)
- **Telethon 1.34** (user-account)
- **SQLAlchemy 2.0** + SQLite
- **DeepSeek API** (LLM)
- **rubert-tiny2** (sentence-transformers, эмбеддинги)
- **Docker** + docker-compose

---

## 📜 Лицензия

Проект создан в учебных целях (курсовая работа). Свободное использование с указанием автора.

---

## 👤 Автор

**Александр** — GitHub [@AleksandrG74](https://github.com/AleksandrG74)
