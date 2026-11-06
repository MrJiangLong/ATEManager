"""工厂测试库只读取数器：各型号一个分库（库名 = 型号），同一台 PG。

只暴露三件事给插件：api.sql 的任意参数化只读查询、api.tables / api.describe 的
schema 自助探查。SQL 由插件自己编写，本模块只负责连接、强制只读、
事务级超时与结果行数上限；连接按库名缓存（NullPool，用完即断）。
"""

from functools import lru_cache
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from ...config import settings

def configured() -> bool:
    return bool(settings.FACTORY_DB_HOST and settings.FACTORY_DB_USER)

@lru_cache(maxsize=64)
def _engine_cached(db_name: str, host: str, port: int, user: str, password: str):
    """NullPool：用完即断、零常驻连接 —— 型号分库多达数十个，
    常驻池会耗尽工厂 PG 的 max_connections（实测触发 53300）。

    只读强制：每个连接 SET default_transaction_read_only = on，
    即使代码出现 bug 试图写入，数据库层也会直接拒绝（用户硬性要求）。"""
    url = f"postgresql+pg8000://{user}:{password}@{host}:{port}/{db_name}"
    engine = sa.create_engine(url, poolclass=NullPool)

    @sa.event.listens_for(engine, "connect")
    def _force_read_only(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("SET default_transaction_read_only = on")
        cursor.close()

    return engine

def _engine(db_name: str):
    return _engine_cached(
        db_name, settings.FACTORY_DB_HOST, settings.FACTORY_DB_PORT,
        settings.FACTORY_DB_USER, settings.FACTORY_DB_PASSWORD,
    )

# =====================================================================
# 插件任意查询（api.sql / api.tables / api.describe）
# 设计：脚本是受信任代码，这里不防注入，只防「误伤生产库」与「不知道有哪些表」。
# 写操作的硬边界是连接级 SET default_transaction_read_only = on，不是下面的关键字检查。
# =====================================================================
_READ_STATEMENTS = ("SELECT", "WITH", "TABLE", "VALUES", "SHOW", "EXPLAIN")


def _leading_keyword(sql: str) -> str:
    """取 SQL 首个关键字（跳过前导空白/行注释/块注释），仅用于友好报错。"""
    s = sql or ""
    while True:
        stripped = s.lstrip()
        if stripped.startswith("--"):
            s = stripped.split("\n", 1)[-1]
            continue
        if stripped.startswith("/*"):
            end = stripped.find("*/")
            s = stripped[end + 2:] if end >= 0 else ""
            continue
        s = stripped
        break
    return s.split(None, 1)[0].upper().rstrip(";") if s else ""


def _set_query_timeout(conn, timeout_ms: int) -> None:
    """事务级 statement_timeout：set_config(..., true) 支持参数绑定（SET LOCAL 不支持）。"""
    conn.execute(
        sa.text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(int(timeout_ms))},
    )


def fetch_sql(
    db_name: str,
    sql: str,
    params: Optional[dict] = None,
    *,
    timeout_ms: int = 30000,
    max_rows: int = 50000,
) -> list:
    """插件任意只读查询：复用强制只读连接，带事务级超时与结果行数上限。

    - 只查询当前型号分库（db_name 由 api.model 决定，不支持跨库）；
    - 非读语句给出友好报错，但**真正的写防护来自只读事务**（PG 层拒绝）；
    - 值必须走 params 绑定；标识符由插件自行用双引号书写；
    - 返回 list[dict]，numeric 列仍为 Decimal。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("api.sql: SQL 不能为空")
    leading = _leading_keyword(sql)
    if leading not in _READ_STATEMENTS:
        raise ValueError(
            f"api.sql 仅用于读取（SELECT / WITH ...），收到 {leading or '空语句'}；"
            "写操作由只读事务直接拒绝"
        )
    with _engine(db_name).connect() as conn:
        _set_query_timeout(conn, timeout_ms)
        result = conn.execute(sa.text(sql), params or {})
        rows = result.fetchmany(int(max_rows) + 1)
        if len(rows) > int(max_rows):
            raise RuntimeError(
                f"api.sql 结果超过上限 {max_rows} 行，请加 WHERE / LIMIT / 聚合"
            )
        return [dict(r._mapping) for r in rows]


def list_tables(db_name: str, *, timeout_ms: int = 30000) -> list:
    """当前型号库 public schema 下的表与视图清单（供 api.tables() 自助探查）。"""
    with _engine(db_name).connect() as conn:
        _set_query_timeout(conn, timeout_ms)
        rows = conn.execute(
            sa.text(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        return [dict(r._mapping) for r in rows]


def describe_table(db_name: str, table: str, *, timeout_ms: int = 30000) -> list:
    """列清单（名称/类型/可空/序号），表名走参数绑定；供 api.describe() 使用。"""
    with _engine(db_name).connect() as conn:
        _set_query_timeout(conn, timeout_ms)
        rows = conn.execute(
            sa.text(
                "SELECT column_name, data_type, is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "ORDER BY ordinal_position"
            ),
            {"t": table},
        )
        return [dict(r._mapping) for r in rows]
