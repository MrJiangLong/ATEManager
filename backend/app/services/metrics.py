"""仪表盘统计聚合。

统一实现，不区分 PostgreSQL / SQLite：`SQL 圈定时间窗 + Python 聚合`。
"""

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from .gate import is_lock_lost
from .routing import ProcessGraph, _as_list, is_completed, load_process
from .timeutil import (
    as_utc,
    client_online_since,
    local_day_key,
    local_day_start,
    sql_time,
    utcnow,
)

# 单次统计最多拉取的事件数
MAX_RECORDS = 50_000


def _rate(passed: int, total: int) -> float:
    return round(passed / total * 100, 1) if total else 0.0


def build_overview(
    db: Session, *, days: int, process_id: Optional[str] = None
) -> schemas.MetricsOverview:
    window_days = days or settings.METRICS_WINDOW_DAYS
    now = utcnow()
    # 日界一律用本地时区（settings.TZ_INFO）：今日 = 本地 00:00 起，
    # 日趋势窗口 = 最近 window_days 个本地自然日（含今日）。
    today_start = local_day_start(now)
    since = today_start - timedelta(days=window_days - 1)

    # ---- 流程图缓存（按机型一次装载）----
    model_process = {
        m.product_model: m.process_id for m in db.query(models.ProductModel).all()
    }
    graphs: Dict[str, Optional[ProcessGraph]] = {}

    def graph_of(model_name: str) -> Optional[ProcessGraph]:
        pid = model_process.get(model_name)
        if not pid:
            return None
        if pid not in graphs:
            graphs[pid] = load_process(db, pid)
        return graphs[pid]

    # ---- 在制品 ----
    products = db.query(models.ProductStatus).all()
    wip = schemas.WipStat()
    for p in products:
        if process_id and model_process.get(p.product_model) != process_id:
            continue
        graph = graph_of(p.product_model)
        passed = set(_as_list(p.passed_stations))
        if p.current_status == models.STATUS_SCRAPPED:
            wip.scrapped += 1
        elif graph and is_completed(graph, passed):
            wip.completed += 1
        else:
            wip.in_process += 1

        if p.current_status == models.STATUS_IDLE:
            wip.idle += 1
        elif p.current_status == models.STATUS_TESTING:
            wip.testing += 1
        elif p.current_status == models.STATUS_LOCKED:
            wip.locked += 1
    wip.total = len(products)

    # ---- 窗口内事件 ----
    raw = (
        db.query(models.TestRecord)
        .filter(models.TestRecord.created_at >= sql_time(since))
        .order_by(models.TestRecord.record_id.desc())
        .limit(MAX_RECORDS)
        .all()
    )
    records = []
    for r in raw:
        created = as_utc(r.created_at)
        if created and created >= since:
            records.append((created, r))

    # ---- 机型 → 流程 映射（按 SN 归属）----
    sn_process = {p.sn: model_process.get(p.product_model) for p in products}
    if process_id:
        records = [(c, r) for c, r in records if sn_process.get(r.sn) == process_id]

    today = [(c, r) for c, r in records if c >= today_start]
    today_pass = sum(1 for _, r in today if r.overall_result == "PASS")
    today_fail = sum(1 for _, r in today if r.overall_result == "FAIL")

    # ---- 日趋势 ----
    per_day: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for created, r in records:
        bucket = per_day[local_day_key(created)]
        bucket["total"] += 1
        if r.overall_result == "PASS":
            bucket["passed"] += 1
    today_key = date.fromisoformat(local_day_key(now))
    trend = []
    for offset in range(window_days):
        day = (today_key - timedelta(days=window_days - 1 - offset)).isoformat()
        stat = per_day.get(day, {"total": 0, "passed": 0})
        trend.append(
            schemas.YieldPoint(
                date=day,
                total=stat["total"],
                passed=stat["passed"],
                pass_rate=_rate(stat["passed"], stat["total"]),
            )
        )

    # ---- 窗口整体良率（按量加权，与 trend 同源）----
    # 不能用"日良率的算术平均"：无产出的日期 pass_rate=0，会把均值整体拉低，
    # 出现"趋势图 100%、均值线 7%"这类与今日良率对不上的显示。
    window_total = sum(point.total for point in trend)
    window_passed = sum(point.passed for point in trend)
    window = schemas.WindowStat(
        total=window_total,
        passed=window_passed,
        pass_rate=_rate(window_passed, window_total),
    )

    # ---- 工位 / 流程良率 ----
    def yield_by(extractor) -> List[schemas.YieldRow]:
        grouped: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
        for _, r in records:
            key = extractor(r)
            if not key:
                continue
            bucket = grouped[key]
            bucket["total"] += 1
            if r.overall_result == "PASS":
                bucket["passed"] += 1
        return [
            schemas.YieldRow(key=key, **stat, pass_rate=_rate(stat["passed"], stat["total"]))
            for key, stat in sorted(grouped.items())
        ]

    # ---- TOP 失效用例ID ----
    fail_counter: Counter = Counter()
    fail_names: Dict[str, str] = {}
    for _, r in records:
        if r.overall_result != "FAIL":
            continue
        payload = r.executed_items if isinstance(r.executed_items, dict) else {}
        for item in payload.get("items") or []:
            if item.get("result") != "FAIL":
                continue
            case_id = item.get("case_id")
            if not case_id:
                continue
            fail_counter[case_id] += 1
            fail_names.setdefault(case_id, item.get("item_name") or case_id)
    top_failed = [
        schemas.FailedItemPoint(
            case_id=case_id, item_name=fail_names.get(case_id) or case_id, fail_count=count
        )
        for case_id, count in fail_counter.most_common(10)
    ]

    # ---- 租约锁概览 ----
    lock_stat = schemas.LockStat()
    for p in products:
        if p.current_status != models.STATUS_TESTING:
            continue
        lock_stat.active += 1
        if is_lock_lost(p):
            lock_stat.zombie += 1
    lock_stat.sessions_running = (
        db.query(func.count(models.TestSession.session_id))
        .filter(models.TestSession.status == models.SESSION_RUNNING)
        .scalar()
        or 0
    )
    lock_stat.sessions_abnormal = (
        db.query(func.count(models.TestSession.session_id))
        .filter(models.TestSession.status.in_(models.ABNORMAL_SESSION_STATUSES))
        .scalar()
        or 0
    )

    client_total = db.query(func.count(models.StationClient.client_id)).scalar() or 0
    client_online = (
        db.query(func.count(models.StationClient.client_id))
        .filter(
            models.StationClient.last_seen_at >= sql_time(client_online_since(now))
        )
        .scalar()
        or 0
    )

    return schemas.MetricsOverview(
        window_days=window_days,
        wip=wip,
        today=schemas.TodayStat(
            total=len(today),
            passed=today_pass,
            failed=today_fail,
            pass_rate=_rate(today_pass, len(today)),
        ),
        window=window,
        trend=trend,
        station_yield=yield_by(lambda r: r.station_id),
        process_yield=yield_by(lambda r: sn_process.get(r.sn)),
        top_failed_items=top_failed,
        clients=schemas.ClientStat(total=client_total, online=client_online),
        product_total=len(products),
        locks=lock_stat,
    )
