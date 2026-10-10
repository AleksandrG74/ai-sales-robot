FROM ai-sales-robot-base:latest

WORKDIR /app

# Копируем код проекта
COPY core/        /app/core/
COPY workers/     /app/workers/
COPY web/         /app/web/
COPY scripts/     /app/scripts/
COPY configs/     /app/configs/
COPY orchestrator/ /app/orchestrator/

# Копируем dialogues-miner as-is
COPY dialogues-miner/ /app/dialogues-miner/

# Создаём папки (на случай если volume ещё не примонтирован)
RUN mkdir -p /app/data /app/logs /app/data/hf_cache /app/data/strategies /app/data/raw_threads

# Инициализация
RUN chmod +x /app/scripts/init.sh || true

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python /app/scripts/healthcheck.py || exit 1

CMD ["python", "-u", "-m", "orchestrator.main"]
