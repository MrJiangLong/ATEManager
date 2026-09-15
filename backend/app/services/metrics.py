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

    sn_process = {p.sn: model_process.get(p.product_model) for p in products}
    if process_id:
        records = [(c, r) for c, r in records if sn_process.get(r.sn) == process_id]

    today = [(c, r) for c, r in records if c >= today_start]
    today_pass = sum(1 for _, r in today if r.overall_result == "PASS")
    today_fail = sum(1 for _, r in today if r.overall_result == "FAIL")

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

    # 窗口整体良率按量加权（与 trend 同源）：不能用"日良率的算术平均"，
    # 无产出的日期 pass_rate=0 会把均值整体拉低，
    # 出现"趋势图 100%、均值线 7%"这类与今日良率对不上的显示。
    window_total = sum(point.total for point in trend)
    window_passed = sum(point.passed for point in trend)
    window = schemas.WindowStat(
        total=window_total,
        passed=window_passed,
        pass_rate=_rate(window_passed, window_total),
    )

    def yield_by(extractor, with_fpy: bool = False) -> List[schemas.YieldRow]:
        """记录级良率分组（分母 = 窗口内的测试记录数）。

        with_fpy=True 时额外计算直通率（FPY）：每件（SN）在某键上的"首条记录"
        （窗口内最早一条）结果为 PASS 的件占比，分母是"件"不是记录。记录级良率
        会被重测稀释——返修后重测通过会多出一条 PASS 记录、分母同步变大，一次
        做好的比例看不出来；FPY 并列展示才能暴露"良率漂亮但重测多"的工位。
        口径与记录级良率同窗（含 MAX_RECORDS 上限约束）。
        """
        grouped: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
        first_outcome: Dict[tuple, tuple] = {}  # (sn, key) -> (created, overall_result)
        for created, r in records:
            key = extractor(r)
            if not key:
                continue
            bucket = grouped[key]
            bucket["total"] += 1
            if r.overall_result == "PASS":
                bucket["passed"] += 1
            if with_fpy:
                prev = first_outcome.get((r.sn, key))
                if prev is None or created < prev[0]:
                    first_outcome[(r.sn, key)] = (created, r.overall_result)

        fpy: Dict[str, Dict[str, int]] = defaultdict(lambda: {"sns": 0, "fpy": 0})
        if with_fpy:
            for (_sn, key), (_created, outcome) in first_outcome.items():
                fpy[key]["sns"] += 1
                if outcome == "PASS":
                    fpy[key]["fpy"] += 1

        rows = []
        for key, stat in sorted(grouped.items()):
            extra: dict = {}
            if with_fpy:
                extra = {
                    "first_pass": fpy[key]["fpy"],
                    "fpy_total": fpy[key]["sns"],
                    "first_pass_rate": _rate(fpy[key]["fpy"], fpy[key]["sns"]),
                }
            rows.append(
                schemas.YieldRow(key=key, **stat, pass_rate=_rate(stat["passed"], stat["total"]), **extra)
            )
        return rows

    def unit_yield_by_process():
        """按流程统计整件良率：分母只含已完结（走完全流程或报废）的件，在制不计入。

        与 process_yield（记录级）的差别：未完工的件没跑的工位不产生记录，失败没机会
        发生，只会把记录级良率往上抬，故本指标以"件"为单位。
        不良只看终态（仅报废算不良，中途 FAIL 但重测通过算合格），因此这是"最终良率"
        而非 FPY，需与 first_pass（整件无任何 FAIL 记录）并列才能看出返修成本。
        pending 只数窗口内有测试活动但未走完流程的件；从未进过站的件不计入。
        """
        active_sns = {r.sn for _, r in records}
        # FPY 按整件判定，不受时间窗截断影响：窗口外的失败同样算失败
        failed_sns = {
            sn
            for (sn,) in db.query(models.TestRecord.sn)
            .filter(models.TestRecord.overall_result == "FAIL")
            .distinct()
            .all()
        }
        grouped: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"total": 0, "passed": 0, "first_pass": 0}
        )
        pending = 0
        for p in products:
            if p.sn not in active_sns:
                continue
            pid = model_process.get(p.product_model)
            if not pid or (process_id and pid != process_id):
                continue
            graph = graph_of(p.product_model)
            if not graph:
                continue
            scrapped = p.current_status == models.STATUS_SCRAPPED
            if not scrapped and not is_completed(graph, set(_as_list(p.passed_stations))):
                pending += 1
                continue
            bucket = grouped[pid]
            bucket["total"] += 1
            if not scrapped:
                bucket["passed"] += 1
                if p.sn not in failed_sns:
                    bucket["first_pass"] += 1
        rows = [
            schemas.YieldRow(
                key=key,
                **stat,
                pass_rate=_rate(stat["passed"], stat["total"]),
                first_pass_rate=_rate(stat["first_pass"], stat["total"]),
            )
            for key, stat in sorted(grouped.items())
        ]
        return rows, pending

    unit_yield_rows, unit_yield_pending = unit_yield_by_process()

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
        station_yield=yield_by(lambda r: r.station_id, with_fpy=True),
        process_yield=yield_by(lambda r: sn_process.get(r.sn)),
        process_unit_yield=unit_yield_rows,
        process_unit_yield_pending=unit_yield_pending,
        top_failed_items=top_failed,
        clients=schemas.ClientStat(total=client_total, online=client_online),
        product_total=len(products),
        locks=lock_stat,
    )
