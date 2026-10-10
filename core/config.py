from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Telegram (user-account, dialogues-miner) ===
    telegram_api_id: Optional[int] = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: Optional[str] = Field(default=None, alias="TELEGRAM_API_HASH")
    telegram_phone: Optional[str] = Field(default=None, alias="TELEGRAM_PHONE")

    # === Telegram Bot (telegram-worker, aiogram) ===
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_operator_chat_id: Optional[int] = Field(
        default=None, alias="TELEGRAM_OPERATOR_CHAT_ID"
    )

    # === DeepSeek LLM ===
    deepseek_api_key: Optional[str] = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_timeout: float = Field(default=60.0, alias="DEEPSEEK_TIMEOUT")
    deepseek_max_retries: int = Field(default=3, alias="DEEPSEEK_MAX_RETRIES")

    # === dialogues-miner web UI ===
    dashboard_user: Optional[str] = Field(default=None, alias="DASHBOARD_USER")
    dashboard_pass: Optional[str] = Field(default=None, alias="DASHBOARD_PASS")
    miner_web_port: int = Field(default=8001, alias="MINER_WEB_PORT")

    # === ai-sales-robot web-backend ===
    web_backend_port: int = Field(default=8000, alias="WEB_BACKEND_PORT")

    # === Orchestrator ===
    orchestrator_healthcheck_interval: int = Field(
        default=30, alias="ORCHESTRATOR_HEALTHCHECK_INTERVAL"
    )
    orchestrator_restart_delay: int = Field(
        default=5, alias="ORCHESTRATOR_RESTART_DELAY"
    )
    orchestrator_max_restarts: int = Field(default=0, alias="ORCHESTRATOR_MAX_RESTARTS")

    # === Общее ===
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    tz: str = Field(default="Europe/Moscow", alias="TZ")

    # === Пути (внутри контейнера) ===
    robot_db_path: Path = Field(
        default=BASE_DIR / "data" / "robot.db", alias="ROBOT_DB_PATH"
    )
    strategies_dir: Path = Field(
        default=BASE_DIR / "data" / "strategies", alias="STRATEGIES_DIR"
    )
    raw_threads_dir: Path = Field(
        default=BASE_DIR / "data" / "raw_threads", alias="RAW_THREADS_DIR"
    )
    configs_dir: Path = Field(default=BASE_DIR / "configs", alias="CONFIGS_DIR")
    logs_dir: Path = Field(default=BASE_DIR / "logs", alias="LOGS_DIR")

    # === Embeddings ===
    embeddings_model: str = Field(
        default="cointegrated/rubert-tiny2", alias="EMBEDDINGS_MODEL"
    )
    embeddings_cache: Path = Field(
        default=BASE_DIR / "data" / "hf_cache", alias="EMBEDDINGS_CACHE"
    )

    # === dialogues-miner internal ===
    miner_db_path: Path = Field(
        default=BASE_DIR / "dialogues-miner" / "data" / "dialogues.db",
        alias="MINER_DB_PATH",
    )
    miner_target_words: Path = Field(
        default=BASE_DIR / "dialogues-miner" / "target_words.json",
        alias="MINER_TARGET_WORDS",
    )

    def ensure_dirs(self) -> None:
        for p in (
            self.robot_db_path.parent,
            self.strategies_dir,
            self.raw_threads_dir,
            self.configs_dir,
            self.logs_dir,
            self.embeddings_cache,
        ):
            try:
                Path(p).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
