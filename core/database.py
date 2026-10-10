from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from core.config import settings

log = logging.getLogger("core.database")
log.setLevel(settings.log_level.upper())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ============================================================
# 1. strategies
# ============================================================
class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (
        UniqueConstraint("token_group", "role", "aggression_level", name="uq_strategy_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_group: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    aggression_level: Mapped[str] = mapped_column(String(16), nullable=False)

    active_version_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("strategy_versions.id", use_alter=True), nullable=True
    )
    current_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    versions: Mapped[list["StrategyVersion"]] = relationship(
        "StrategyVersion",
        back_populates="strategy",
        foreign_keys="StrategyVersion.strategy_id",
        cascade="all, delete-orphan",
    )
    active_version: Mapped[Optional["StrategyVersion"]] = relationship(
        "StrategyVersion",
        foreign_keys=[active_version_id],
        post_update=True,
    )


# ============================================================
# 2. word_stats
# ============================================================
class WordStat(Base):
    __tablename__ = "word_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pmi_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ============================================================
# 3. pmi_history
# ============================================================
class PMIHistory(Base):
    __tablename__ = "pmi_history"
    __table_args__ = (
        Index("ix_pmi_word_window", "word", "window_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    pmi: Mapped[float] = mapped_column(Float, nullable=False)
    cooccurrence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# ============================================================
# 4. strategy_versions
# ============================================================
class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    params_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    metrics_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="system", nullable=False)

    strategy: Mapped["Strategy"] = relationship(
        "Strategy", back_populates="versions", foreign_keys=[strategy_id]
    )


# ============================================================
# 5. connection_matrix (edge list)
# ============================================================
class ConnectionMatrix(Base):
    __tablename__ = "connection_matrix"
    __table_args__ = (
        UniqueConstraint("source_token", "target_token", name="uq_conn_tokens"),
        Index("ix_conn_source", "source_token"),
        Index("ix_conn_target", "target_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_token: Mapped[str] = mapped_column(String(64), nullable=False)
    target_token: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cooccurrence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ============================================================
# 6. bridges_history
# ============================================================
class BridgeHistory(Base):
    __tablename__ = "bridges_history"
    __table_args__ = (
        Index("ix_bridge_token_created", "bridge_token", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bridge_token: Mapped[str] = mapped_column(String(64), nullable=False)
    from_group: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    to_group: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dialogue_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weight_before: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    weight_after: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# ============================================================
# 7. processed_dialogues
# ============================================================
class ProcessedDialogue(Base):
    __tablename__ = "processed_dialogues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialogue_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    sender: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    seed_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="done", nullable=False)
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ============================================================
# 8. runs
# ============================================================
class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_type_started", "run_type", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    items_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ============================================================
# 9. operator_actions
# ============================================================
class OperatorAction(Base):
    __tablename__ = "operator_actions"
    __table_args__ = (
        Index("ix_op_action_dialogue", "dialogue_id"),
        Index("ix_op_action_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialogue_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# ============================================================
# Engine / Session
# ============================================================
def _make_engine():
    url = f"sqlite:///{settings.robot_db_path}"
    return create_engine(
        url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    try:
        settings.robot_db_path.parent.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(bind=engine)
        log.info("DB initialised at %s", settings.robot_db_path)
    except Exception as exc:
        log.exception("Failed to init DB: %s", exc)
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
