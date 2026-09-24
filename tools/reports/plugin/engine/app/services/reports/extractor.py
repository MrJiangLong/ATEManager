"""工厂测试库只读取数器：各型号一个分库（库名 = 型号），同一台 PG。

仅执行 SELECT；连接按库名缓存。所有「取最新 PASS」查询共用同一模式：
ROW_NUMBER() OVER (PARTITION BY ... ORDER BY date DESC) 取 rn = 1，
与 TekReport 桌面工具的取数口径完全一致。
"""

from functools import lru_cache

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
