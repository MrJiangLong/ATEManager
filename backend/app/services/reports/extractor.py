"""工厂测试库只读取数器：各型号一个分库（库名 = 型号），同一台 PG。

仅执行 SELECT；连接按库名缓存。所有「取最新 PASS」查询共用同一模式：
ROW_NUMBER() OVER (PARTITION BY ... ORDER BY date DESC) 取 rn = 1，
与 TekReport 桌面工具的取数口径完全一致。
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

def _qi(identifier: str) -> str:
    """标识符加引号并转义（防注入，表名/列名来自插件脚本，仍统一收口）。"""
    return '"' + str(identifier).replace('"', '""') + '"'

@lru_cache(maxsize=512)
def _table_exists(db_name: str, table: str) -> bool:
    with _engine(db_name).connect() as conn:
        return bool(conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).scalar())

def fetch_newest_pass(
    db_name: str,
    table: str,
    sn: str,
    select_cols: list,
    partition_by: list,
    filters: dict,
) -> list:
    """取每个分区键组合下 date 最新的一条记录（调用方在 filters 中声明 result=PASS）。

    表不存在时返回空列表（等价 TekReport.CheckTableAndSn 的 False 分支）——
    不同机型分库的测试项表集合不同，缺表是正常业务形态而非错误。"""
    if not _table_exists(db_name, table):
        return []
    cols = ", ".join(_qi(c) for c in select_cols)
    part = ", ".join(_qi(c) for c in partition_by)
    where = ["sn = :sn"]
    params: dict = {"sn": sn}
    for col, val in (filters or {}).items():
        where.append(f"{_qi(col)} = :f_{col}")
        params[f"f_{col}"] = val
    sql = (
        f"WITH ranked AS (SELECT {cols}, ROW_NUMBER() OVER "
        f"(PARTITION BY {part} ORDER BY {_qi('date')} DESC) AS rn "
        f"FROM {_qi(table)} WHERE {' AND '.join(where)}) "
        f"SELECT {cols} FROM ranked WHERE rn = 1"
    )
    with _engine(db_name).connect() as conn:
        result = conn.execute(sa.text(sql), params)
        return [dict(row._mapping) for row in result]

def _fetch_one(db_name: str, table: str, sn: str, column: str):
    with _engine(db_name).connect() as conn:
        row = conn.execute(
            sa.text(
                f"SELECT {_qi(column)} FROM {_qi(table)} "
                f"WHERE sn = :sn ORDER BY {_qi('date')} DESC LIMIT 1"
            ),
            {"sn": sn},
        ).first()
    return None if row is None else row[0]

def fetch_latest(db_name: str, table: str, sn: str, column: str) -> str:
    """该 SN 在表中最新一条记录的某列（date/version/tester 等通用入口）。"""
    return str(_fetch_one(db_name, table, sn, column) or "")


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
