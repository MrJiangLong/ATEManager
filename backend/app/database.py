"""数据库引擎 / 会话 / 建表。"""

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import IS_SQLITE, settings

if IS_SQLITE:
    _db_path = make_url(settings.DATABASE_URL).database
    if _db_path and _db_path not in (":memory:", ""):
        Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

if IS_SQLITE:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_begin_immediate(dbapi_conn, _record):
        """SQLite 事务一律以 BEGIN IMMEDIATE 开始。

        pysqlite 默认 DEFERRED：读不取锁，两个事务可同时读到同一份快照再各自
        "读-改-写"提交，后提交者覆盖前者 → 更新丢失（并发 check-in 会双持锁）。
        IMMEDIATE 在事务开始即取 RESERVED 写锁，使并发写串行化；读仍可并发，
        代价可接受。timeout(30s) 用于缓解高并发下的锁等待。
        """
        dbapi_conn.isolation_level = "IMMEDIATE"


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI 依赖：每请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 存量库补齐列：create_all 不会 ALTER，故按方言探测后补列
_ADDED_COLUMNS = {
    "product_status": {
        "lock_token": "VARCHAR(64)",
        "lock_acquired_at": "TIMESTAMP" if IS_SQLITE else "TIMESTAMPTZ",
        "lock_last_seen_at": "TIMESTAMP" if IS_SQLITE else "TIMESTAMPTZ",
        "lock_heartbeat_count": "INTEGER DEFAULT 0",
        # 冗余列：支持列表页按"已完成/未完成"在 SQL 层精确过滤与分页
        "is_completed": "BOOLEAN DEFAULT 0" if IS_SQLITE else "BOOLEAN DEFAULT false",
    },
    "station_clients": {
        "app_version": "VARCHAR(50)",
        "created_at": "TIMESTAMP" if IS_SQLITE else "TIMESTAMPTZ",
        "client_name": "VARCHAR(128)",
    },
    "product_models": {
        "fw_match_rule": "VARCHAR(16) DEFAULT 'exact'",
    },
    "processes": {
        "is_active": "BOOLEAN DEFAULT 1" if IS_SQLITE else "BOOLEAN DEFAULT true",
    },
}


# 补列后需从既有列回填的（新列没有历史值，取最接近的口径兜底），仅在真正补列时执行一次
_BACKFILL_ON_ADD = {
    ("station_clients", "created_at"): "last_seen_at",
}


def _existing_columns(conn, table: str) -> set:
    if IS_SQLITE:
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    ).fetchall()
    return {row[0] for row in rows}


def _migrate_columns() -> None:
    """幂等补列：SQLite 与 PG 通用，失败仅告警不影响启动。"""
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = _existing_columns(conn, table)
            for name, ddl in columns.items():
                if name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                source = _BACKFILL_ON_ADD.get((table, name))
                if source:
                    conn.execute(text(f"UPDATE {table} SET {name} = {source} WHERE {name} IS NULL"))


def ensure_schema() -> None:
    """幂等建表 + 补列。"""
    from . import models  # noqa: F401  导入即注册表元数据

    Base.metadata.create_all(bind=engine)
    try:
        _migrate_columns()
    except Exception as exc:  # pragma: no cover - 迁移失败不应阻断启动
        from .logging import get_logger

        get_logger("startup").warning("列迁移跳过：%s", exc)
