"""数据库引擎与会话。"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

# SQLite: 默认 DEFERRED 事务，确认方案时需要 IMMEDIATE 立即拿写锁，
# 以保证并发确认不会产生冲突的已确认槽位占用。
if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # 并发确认时第二个写连接等待写锁（毫秒），避免立刻 SQLITE_BUSY
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()


def _apply_additive_migrations() -> None:
    """对既有数据库做仅增列/建表的轻量迁移（SQLite ALTER TABLE ADD COLUMN）。

    create_all 不会给已存在的表补列；这里幂等补齐实绩版本/首次登记原值列，
    旧行回填合理默认，保证旧库可直接以新版本启动。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "loading_actuals" not in inspector.get_table_names():
        return  # 全新库已由 create_all 建好，无需迁移
    actuals_cols = {c["name"] for c in inspector.get_columns("loading_actuals")}
    event_cols = {c["name"] for c in inspector.get_columns("loading_actual_events")}

    with engine.begin() as conn:
        if "version" not in actuals_cols:
            conn.execute(text(
                "ALTER TABLE loading_actuals ADD COLUMN version INTEGER NOT NULL "
                "DEFAULT 1"
            ))
            conn.execute(text("UPDATE loading_actuals SET version = 1"))
        if "first_crane_no" not in event_cols:
            conn.execute(text(
                "ALTER TABLE loading_actual_events ADD COLUMN first_crane_no INTEGER"
            ))
            conn.execute(text(
                "UPDATE loading_actual_events SET first_crane_no = crane_no"
            ))
        if "first_minute" not in event_cols:
            conn.execute(text(
                "ALTER TABLE loading_actual_events ADD COLUMN first_minute INTEGER"
            ))
            conn.execute(text(
                "UPDATE loading_actual_events SET first_minute = minute"
            ))
