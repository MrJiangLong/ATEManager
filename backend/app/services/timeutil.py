"""时间工具：统一 UTC 语义与「距某时刻多久」的计算。

SQLite 存 naive UTC、PostgreSQL 存 aware TIMESTAMPTZ，两者相减会抛 TypeError，
因此任何读取出来的时间都要先经 `as_utc` 归一化再参与运算。
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from ..config import IS_SQLITE, settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sql_time(moment: datetime) -> datetime:
    """按方言归一化写入 SQL 的时间：SQLite 存 naive UTC，PG 存 aware TIMESTAMPTZ。"""
    return moment.replace(tzinfo=None) if IS_SQLITE else moment


def local_day_start(moment: Optional[datetime] = None) -> datetime:
    """`moment` 所在**本地自然日**的 00:00，返回 aware UTC 时刻。

    看板"今日良率"、日趋势分桶一律走这里：直接用 `utcnow().replace(hour=0)`
    得到的是 UTC 日界，东八区下等于本地 08:00，会把清晨之前的产出算到昨天。
    """
    base = as_utc(moment) or utcnow()
    local = base.astimezone(settings.TZ_INFO)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def local_day_start_of_date(day: date) -> datetime:
    """给定日期（本地日历日）的 00:00，返回 aware UTC 时刻。"""
    return (
        datetime.combine(day, time.min)
        .replace(tzinfo=settings.TZ_INFO)
        .astimezone(timezone.utc)
    )


def local_day_key(moment: Optional[datetime]) -> str:
    """时刻所属的本地日历日（YYYY-MM-DD）；空值返回空串。"""
    base = as_utc(moment)
    return base.astimezone(settings.TZ_INFO).date().isoformat() if base else ""


def as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    """naive 视为 UTC 并补时区；aware 原样返回；None 透传。"""
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def elapsed_sec(moment: Optional[datetime], now: Optional[datetime] = None) -> float:
    """距 moment 已过多少秒；moment 为空返回 -1（与「未开始」语义一致）。"""
    base = as_utc(moment)
    if base is None:
        return -1.0
    return ((now or utcnow()) - base).total_seconds()


def elapsed_int(moment: Optional[datetime], now: Optional[datetime] = None) -> int:
    """`elapsed_sec` 的整秒版本：空值 -1，负值截断为 0。"""
    value = elapsed_sec(moment, now)
    return -1 if value < 0 else round(value)


def client_online_since(now: Optional[datetime] = None) -> datetime:
    """机台在线阈值：最后心跳不早于该时刻即视为在线（Python 与 SQL 判定共用此阈值）。"""
    return (now or utcnow()) - timedelta(seconds=settings.CLIENT_ONLINE_WINDOW_SECONDS)


def is_client_online(last_seen_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    moment = as_utc(last_seen_at)
    return moment is not None and moment >= client_online_since(now)
