# ─────────────────────────────────────────────────────────────
# dialogues-miner — поисковик тредов-согласий
# Базовый образ: python 3.11 slim
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

LABEL maintainer="dialogues-miner"
LABEL description="Telegram dialogues miner + web dashboard"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Berlin \
    LANG=C.UTF-8 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        sqlite3 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Зависимости Python
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Код
COPY . .

# Папки для данных
RUN mkdir -p /app/data /app/logs /app/sessions

# Непривилегированный пользователь
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8001/ -u "${DASHBOARD_USER}:${DASHBOARD_PASS}" || exit 1

CMD ["python", "main.py", "--mode", "web"]