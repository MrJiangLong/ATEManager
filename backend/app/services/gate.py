"""运行时防呆状态机：进站 / 保活 / 断点 / 出站 ACK / 释放 / 维修处置。

并发锁模型（v1.0 租约锁）
    持锁    = current_status == 'TESTING' AND current_client == <client_id> AND lock_token
    失联    = now - lock_last_seen_at > LOCK_HEARTBEAT_GRACE_SEC   → 快通道，可被接管
    硬超时  = now - lock_acquired_at  > stations.timeout_sec        → 计一次失败
    释放    = 出站 / 主动释放 / 强制解锁 / 回收任务

    关键区分：心跳只刷新 lock_last_seen_at，**不再篡改 lock_acquired_at**，
    因此长测试可跑满 30min 不被接管，而崩溃机台 120s 内即释放锁。

    防脏写（fencing）：每次进站/接管换发新 lock_token；旧持锁方持旧 token
    出站/上报断点一律 403 lock_invalid，避免已接管会话被僵尸机台覆盖。

续测（test_sessions）
    上位机增量上报 checkpoint（按 case_id 去重覆盖，幂等）；
    崩溃重启后进站携带 resume_session_id（同机台自动续），attempt 累加，
    服务端回传已完成用例清单；出站时 checkpoint 补齐未提交用例，避免漏测误判。

出站幂等
    checkout_id 存放于 test_records.executed_items JSONB 内部（不改动表结构），
    网络重试命中同一 checkout_id 时直接回放既有回执，避免重复记账。
"""

import uuid
from typing import Any, List, Optional, Set, Tuple, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import IS_SQLITE, settings
from ..errors import (
    EXIT_CASE_ID_MISMATCH,
    EXIT_GATE_BLOCKED,
    EXIT_LOCK_CONFLICT,
    EXIT_LOCK_EXPIRED,
    EXIT_MISSING_MANDATORY,
    EXIT_PRODUCT_LOCKED,
    bad_request,
    conflict_error,
    forbidden,
    not_found,
)
from .firmware import fw_matches
from .routing import (
    _as_list,
    is_completed,
    load_process,
    mandatory_case_ids,
    missing_prereq,
    next_stations,
    station_rules,
)
from .timeutil import elapsed_sec, utcnow

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_SKIP = "SKIP"

DEFAULT_TIMEOUT_SEC = 1800


def _timeout_of(db: Session, station_id: str) -> int:
    station = db.get(models.Station, station_id)
    return station.timeout_sec if station and station.timeout_sec else DEFAULT_TIMEOUT_SEC


def _passed(product: models.ProductStatus) -> Set[str]:
    return set(_as_list(product.passed_stations))


def _write_passed(product: models.ProductStatus, passed: Set[str], graph=None) -> None:
    """写入已盖章工位集合，并同步维护冗余列 is_completed（冗余理由见 models.ProductStatus）。

    改动 passed_stations 必须走本函数，否则冗余列会漂移。
    """
    product.passed_stations = sorted(passed)
    if graph is not None:
        product.is_completed = is_completed(graph, passed)


# =====================================================================
# 机台
# =====================================================================
def get_client(
    db: Session,
    client_id: str,
    *,
    ip: Optional[str] = None,
    app_version: Optional[str] = None,
    create: bool = False,
) -> Optional[models.StationClient]:
    """读取机台；create=True 时首次调用自动注册（station_id 置 NULL，需由 Web 端补录）。

    顺带刷新 ip / app_version：两者都是机台自报的运行期信息，由本函数统一收口，
    调用方无需各自赋值（改动会随调用方后续的 commit 落库）。
    """
    client = db.get(models.StationClient, client_id)
    if client is None and create:
        client = models.StationClient(
            client_id=client_id, station_id=None, ip_address=ip, app_version=app_version
        )
        db.add(client)
        try:
            db.commit()
        except IntegrityError:  # 并发注册，回读即可
            db.rollback()
            client = db.get(models.StationClient, client_id)
    if client is not None:
        if ip:
            client.ip_address = ip
        if app_version:
            client.app_version = app_version
    return client


def touch_client(db: Session, client_id: str) -> Optional[models.StationClient]:
    """刷新机台在线时间。"""
    client = db.get(models.StationClient, client_id)
    if client is not None:
        client.last_seen_at = utcnow()
        db.commit()
    return client


# =====================================================================
# 在制品行锁
# =====================================================================
def _locked_product(db: Session, sn: str) -> Optional[models.ProductStatus]:
    """按 sn 取在制品并加行锁（PG: FOR UPDATE；SQLite 无行锁）。

    此处绝不能 commit：那会立刻释放刚获得的 FOR UPDATE 行锁，
    使后续的接管判定与加锁失去保护（并发 check-in 同一 SN 会双双成功）。
    锁必须一直保持到本次进站事务结束。
    """
    query = db.query(models.ProductStatus).filter(models.ProductStatus.sn == sn)
    if not IS_SQLITE:
        query = query.with_for_update()
    return query.first()


def _release_lock(product: models.ProductStatus) -> None:
    """释放租约：清 token 与时间戳（testing_started_at 保留兼容既有查询）。"""
    product.testing_started_at = None
    product.lock_token = None
    product.lock_acquired_at = None
    product.lock_last_seen_at = None
    product.lock_heartbeat_count = 0


def _apply_fail(product: models.ProductStatus, reason: str) -> None:
    product.fail_count = (product.fail_count or 0) + 1
    if product.fail_count >= settings.FAIL_LIMIT:
        product.current_status = models.STATUS_LOCKED
        product.locked_at = utcnow()
        product.locked_reason = reason


# =====================================================================
# 租约锁：失联 / 硬超时 / fencing
# =====================================================================
def new_lock_token() -> str:
    return uuid.uuid4().hex


def lock_held_sec(product: models.ProductStatus) -> int:
    """锁已持有时长（自 lock_acquired_at 起）。"""
    base = product.lock_acquired_at or product.testing_started_at
    return max(0, round(elapsed_sec(base)))


def lock_idle_sec(product: models.ProductStatus) -> int:
    """距上次心跳时长；无基线时返回 -1。"""
    base = product.lock_last_seen_at or product.lock_acquired_at or product.testing_started_at
    if base is None:
        return -1
    return max(0, round(elapsed_sec(base)))


def is_lock_lost(product: models.ProductStatus) -> bool:
    """失联判定：心跳断流超过宽限窗口 → 锁可被接管（不计产品失败）。"""
    idle = lock_idle_sec(product)
    return idle >= 0 and idle > settings.LOCK_HEARTBEAT_GRACE_SEC


def is_lock_expired(product: models.ProductStatus, timeout_sec: int) -> bool:
    """硬超时判定：自持锁开始计时，心跳不续期。"""
    return lock_held_sec(product) > timeout_sec


def _claimed_rowcount(db: Session, statement) -> int:
    """执行抢占式 UPDATE，返回命中行数（0 = 抢锁失败）。

    SQLAlchemy 2.0 的 `Session.execute()` 静态类型标注为 `Result`，而 `rowcount`
    只有 DML 返回的 `CursorResult` 才具备，故此处显式窄化，避免误判为属性错误。
    """
    result = cast("CursorResult[Any]", db.execute(statement))
    return int(result.rowcount or 0)


def _token_mismatch(product: models.ProductStatus, token: Optional[str]) -> bool:
    """fencing 校验：token 不匹配一律拒绝（不受 STRICT_LOCK_TOKEN 开关影响）。

    兼容策略：
        - 存量锁无 token       → 退化为按 client_id 匹配
        - 请求未带 token       → STRICT_LOCK_TOKEN=True 时拒绝（老上位机需升级）
        - 请求带错 token       → 始终拒绝（锁已被接管）
    """
    stored = product.lock_token
    if not stored:
        return False
    if not token:
        return settings.STRICT_LOCK_TOKEN
    return token != stored


# =====================================================================
# 测试会话（续测载体）
# =====================================================================
def _checkpoint_items(session: models.TestSession) -> List[dict]:
    payload = session.checkpoint if isinstance(session.checkpoint, dict) else {}
    items = payload.get("items") or []
    return [it for it in items if isinstance(it, dict)]


def _completed_case_ids(session: models.TestSession) -> List[str]:
    """断点中已完成的用例ID清单（保序去重），下发给上位机用于跳过已跑用例。"""
    case_ids: List[str] = []
    for item in _checkpoint_items(session):
        case_id = item.get("case_id")
        if case_id and case_id not in case_ids:
            case_ids.append(str(case_id))
    return case_ids


def _merge_into_checkpoint(session: models.TestSession, items: List[dict]) -> int:
    """按 case_id 去重覆盖合并，返回合并后总数。"""
    payload = session.checkpoint if isinstance(session.checkpoint, dict) else {}
    merged = {it.get("case_id"): it for it in _checkpoint_items(session)}
    seq_base = len(merged)
    for offset, item in enumerate(items):
        case_id = item.get("case_id")
        if not case_id:
            continue
        record = dict(item)
        record["seq"] = merged.get(case_id, {}).get("seq", seq_base + offset)
        merged[case_id] = record
    payload["items"] = [merged[k] for k in merged]
    session.checkpoint = payload
    # JSONB 原地修改需显式标记，否则 SQLAlchemy 不会 flush
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(session, "checkpoint")
    return len(merged)


def _close_session(
    session: models.TestSession,
    status: str,
    reason: str = "",
    ended_by: Optional[str] = None,
) -> None:
    session.status = status
    session.end_reason = reason or None
    session.ended_by = ended_by
    session.ended_at = utcnow()


def _running_session(
    db: Session, sn: str, station_id: str
) -> Optional[models.TestSession]:
    return (
        db.query(models.TestSession)
        .filter(
            models.TestSession.sn == sn,
            models.TestSession.station_id == station_id,
            models.TestSession.status == models.SESSION_RUNNING,
        )
        .order_by(models.TestSession.started_at.desc())
        .first()
    )


def _count_consecutive_lost(db: Session, sn: str, station_id: str) -> int:
    """统计最近连续的「失联终止」会话数（用于宽限 N 次后才计失败）。"""
    rows = (
        db.query(models.TestSession)
        .filter(
            models.TestSession.sn == sn,
            models.TestSession.station_id == station_id,
            models.TestSession.status == models.SESSION_ABORTED,
        )
        .order_by(models.TestSession.started_at.desc())
        .limit(settings.LOST_LOCK_FAIL_THRESHOLD)
        .all()
    )
    count = 0
    for row in rows:
        if (row.end_reason or "").startswith(LOST_REASON_PREFIX):
            count += 1
        else:
            break
    return count


LOST_REASON_PREFIX = "client_lost"


# =====================================================================
# 进站：需求 2 跳站卡控
# =====================================================================
def check_in(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    product_model: str,
    firmware: str,
    case_ids: Optional[List[str]] = None,
    resume_session_id: Optional[str] = None,
) -> schemas.CheckInData:
    # 0) 机型 → 专属流程（先于工位解析：一台机台可绑定多个流程的不同工位）
    model_row = db.get(models.ProductModel, product_model)
    if model_row is None:
        raise forbidden(
            "model_not_registered",
            f"model_not_registered: {product_model} is not registered",
        )
    graph = load_process(db, model_row.process_id)
    if graph is None:
        raise not_found("process_not_found", f"process_not_found: {model_row.process_id}")

    # 0.5) 工位解析：绑定集合 ∩ 流程工位集
    #   不同产品的工艺完全不同时，一台物理机台可为多个流程的不同工位提供服务；
    #   解析出唯一工位后回写 client.station_id（当前操作工位，运行态字段），
    #   后续心跳 / 断点匹配 / 释放沿用单工位逻辑，无需改动。多命中 = 配置歧义，
    #   400 要求修正机台绑定。
    candidates = sorted(set(_as_list(client.bound_stations)) & set(graph.stations))
    if not candidates:
        raise forbidden(
            "client_not_bound",
            f"client {client.client_id} 的绑定工位均不在流程 {graph.process_id} 内",
        )
    if len(candidates) > 1:
        raise bad_request(
            "station_ambiguous",
            f"station_ambiguous: {client.client_id} 在流程 {graph.process_id} 内绑定了多个工位"
            f" ({', '.join(candidates)})，请修正机台绑定",
        )
    station_id = candidates[0]
    client.station_id = station_id

    # 3) 取/建在制品（首工位动态建档）
    product = db.get(models.ProductStatus, sn)
    created = False
    if product is None:
        if station_id != graph.stations[0]:
            raise forbidden(
                "missing_prereq",
                f"missing_prereq: {sn} has no record yet, must start at {graph.stations[0]}",
                data={"missing": graph.stations[0]},
            )
        product = models.ProductStatus(
            sn=sn,
            product_model=product_model,
            current_fw_version=firmware,
            current_status=models.STATUS_IDLE,
            passed_stations=[],
            fail_count=0,
        )
        db.add(product)
        db.flush()
        created = True

    product = _locked_product(db, sn) or product

    # 4) 机型一致性（防止贴错机型 / 换线未清线）
    if product.product_model != product_model:
        raise forbidden(
            "model_mismatch",
            f"model_mismatch: {sn} is registered as {product.product_model}, got {product_model}",
        )

    # 5) 固件基线校验（口径由机型的 fw_match_rule 决定：完全一致 / 不低于基线）
    rule = model_row.fw_match_rule or models.FW_RULE_EXACT
    fw_match = fw_matches(firmware, model_row.target_fw_version, rule)
    if not fw_match and settings.ENFORCE_FW:
        raise forbidden(
            "firmware_mismatch",
            f"firmware_mismatch: {sn} expected {rule} {model_row.target_fw_version}, got {firmware}",
            data={
                "expected": model_row.target_fw_version,
                "actual": firmware,
                "rule": rule,
            },
        )
    product.current_fw_version = firmware

    # 6) 状态闸门
    if product.current_status == models.STATUS_SCRAPPED:
        raise forbidden("product_scrapped", f"product_scrapped: {sn} has been scrapped")
    if product.current_status == models.STATUS_LOCKED:
        raise forbidden(
            "product_locked",
            f"product_locked: {sn} exceeded fail limit ({product.fail_count}/{settings.FAIL_LIMIT})",
            exit_code=EXIT_PRODUCT_LOCKED,
            data={"fail_count": product.fail_count, "locked_reason": product.locked_reason},
        )

    passed = _passed(product)

    # 7) 复测拦截（已盖章工位严禁重测）
    if station_id in passed:
        raise conflict_error(
            "station_already_passed",
            f"station_already_passed: {sn} already passed {station_id}",
            data={"passed_stations": sorted(passed)},
        )

    # 8) 防跳站闸门（需求 2 核心）
    missing = missing_prereq(graph, station_id, passed)
    if missing:
        raise forbidden(
            "missing_prereq",
            f"missing_prereq: {sn} must finish {', '.join(missing)} before {station_id}",
            data={"missing": missing},
        )

    # 9) 用例ID 防漏测前置校验
    if case_ids is not None and settings.ENFORCE_CASE_IDS:
        covered = set(case_ids)
        absent = [c for c in mandatory_case_ids(graph, station_id) if c not in covered]
        if absent:
            raise bad_request(
                "case_id_mismatch",
                f"case_id_mismatch: {station_id} mandatory case IDs missing from collection: {', '.join(absent)}",
                exit_code=EXIT_CASE_ID_MISMATCH,
                data={"missing_case_ids": absent},
            )

    # 记下"判定时"观察到的锁状态，供最终加锁做 CAS 校验（见步骤 12）
    observed_status = product.current_status
    observed_client = product.current_client

    # 10) 租约加锁：失联 / 硬超时 → 允许接管，否则 409 锁冲突
    timeout_sec = _timeout_of(db, station_id)
    takeover = False
    takeover_from = None
    if product.current_status == models.STATUS_TESTING and product.current_client != client.client_id:
        reclaimable = is_lock_lost(product) or is_lock_expired(product, timeout_sec)
        if not reclaimable:
            raise conflict_error(
                "lock_conflict",
                f"lock_conflict: {sn} is under test by {product.current_client}",
                exit_code=EXIT_LOCK_CONFLICT,
                data={
                    "locked_client": product.current_client,
                    "lock_idle_sec": lock_idle_sec(product),
                    "grace_sec": settings.LOCK_HEARTBEAT_GRACE_SEC,
                },
            )
        # 原会话归档，供运维追溯"跑到哪崩的"
        previous = _running_session(db, sn, station_id)
        if previous is not None:
            _close_session(
                previous,
                models.SESSION_TAKEN_OVER,
                f"taken over by {client.client_id}",
                ended_by=client.client_id,
            )
        takeover = True
        takeover_from = product.current_client

    # 11) 会话解析：显式 resume_session_id > 同机台自动续 > 新建
    session = None
    resume = schemas.ResumeInfo()
    if resume_session_id:
        session = db.get(models.TestSession, resume_session_id)
        if session is None:
            raise not_found("session_not_found", f"session_not_found: {resume_session_id}")
        if session.sn != sn or session.station_id != station_id:
            raise bad_request(
                "session_mismatch",
                f"session_mismatch: {resume_session_id} does not belong to {sn}@{station_id}",
            )
        if session.status == models.SESSION_COMPLETED:
            raise conflict_error(
                "session_completed",
                f"session_completed: {resume_session_id} already checked out",
            )
    elif not takeover:
        # 同机台重进站（崩溃重启）自动续测，无需客户端显式携带
        session = _running_session(db, sn, station_id)
        if session is not None and session.client_id != client.client_id:
            session = None

    if session is None:
        session = models.TestSession(
            session_id=uuid.uuid4().hex,
            sn=sn,
            station_id=station_id,
            client_id=client.client_id,
            checkpoint={"items": [], "cursor": {}},
            attempt=1,
            status=models.SESSION_RUNNING,
            started_at=utcnow(),
            last_heartbeat_at=utcnow(),
        )
        db.add(session)
    else:
        session.attempt = (session.attempt or 1) + 1
        session.client_id = client.client_id
        session.status = models.SESSION_RUNNING
        session.ended_at = None
        session.end_reason = None
        session.ended_by = None
        session.last_heartbeat_at = utcnow()
        resume = schemas.ResumeInfo(
            resumed=True,
            attempt=session.attempt,
            completed_case_ids=_completed_case_ids(session),
            cursor=(session.checkpoint or {}).get("cursor") or {},
        )

    # 12) 换发 token 并加锁（同机台续测也换 token，保证 fencing 有效）
    token = new_lock_token()
    now = utcnow()

    # 原子抢占（CAS）：仅当锁状态仍是"判定时观察到的"才更新成功。
    # 行锁可能因方言/隔离级别失效（SQLite 无行锁），此 UPDATE 是并发安全的最后防线：
    # 并发的第二个事务会被第一个事务的写锁阻塞，待其提交后 WHERE 不再匹配 →
    # rowcount=0，返回 409，避免同一 SN 被两台机台同时持锁。
    claim_stmt = (
        update(models.ProductStatus)
        .where(
            models.ProductStatus.sn == sn,
            models.ProductStatus.current_status == observed_status,
            models.ProductStatus.current_client == observed_client,
        )
        .values(lock_token=token)
    )
    if _claimed_rowcount(db, claim_stmt) != 1:
        db.rollback()
        raise conflict_error(
            "lock_conflict",
            f"lock_conflict: {sn} was claimed by another station concurrently",
            exit_code=EXIT_LOCK_CONFLICT,
            data={"locked_client": observed_client, "sn": sn},
        )

    product.current_status = models.STATUS_TESTING
    product.current_client = client.client_id
    product.testing_started_at = now
    product.lock_acquired_at = now
    product.lock_last_seen_at = now
    product.lock_heartbeat_count = 0
    product.updated_at = now
    session.lock_token = token
    db.commit()

    return schemas.CheckInData(
        sn=product.sn,
        product_model=product.product_model,
        process_id=graph.process_id,
        station_id=station_id,
        station_name=graph.name_of.get(station_id, station_id),
        timeout_sec=timeout_sec,
        is_first_station=station_id == graph.stations[0],
        created_on_checkin=created,
        rules=station_rules(graph, station_id),
        passed_stations=sorted(passed),
        next_stations=next_stations(graph, passed),
        server_time=utcnow().isoformat(),
        session_id=session.session_id,
        lock_token=token,
        attempt=session.attempt or 1,
        heartbeat_interval_sec=settings.LOCK_HEARTBEAT_INTERVAL_SEC,
        heartbeat_grace_sec=settings.LOCK_HEARTBEAT_GRACE_SEC,
        resume=resume,
        takeover=takeover,
        takeover_from=takeover_from,
    )


# =====================================================================
# 保活（只刷新 last_seen，不续期硬超时）
# =====================================================================
def heartbeat(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    lock_token: Optional[str] = None,
) -> schemas.HeartbeatData:
    product = db.get(models.ProductStatus, sn)
    holding = (
        product is not None
        and product.current_status == models.STATUS_TESTING
        and product.current_client == client.client_id
    )
    session = None
    remaining = 0
    lease_remaining = 0
    heartbeat_count = 0
    timeout_sec = _timeout_of(db, client.station_id or "")

    if holding and product is not None:
        if _token_mismatch(product, lock_token):
            holding = False  # 锁已被接管，明确告知上位机停止测试
        else:
            now = utcnow()
            product.lock_last_seen_at = now
            product.lock_heartbeat_count = (product.lock_heartbeat_count or 0) + 1
            product.updated_at = now
            heartbeat_count = product.lock_heartbeat_count
            remaining = max(0, settings.LOCK_HEARTBEAT_GRACE_SEC - lock_idle_sec(product))
            lease_remaining = max(0, round(timeout_sec - lock_held_sec(product)))
            session = _running_session(db, sn, client.station_id or "")
            if session is not None:
                session.last_heartbeat_at = now
            client.last_seen_at = now
            db.commit()

    return schemas.HeartbeatData(
        sn=sn,
        holding_lock=holding,
        remaining_sec=remaining,
        server_time=utcnow().isoformat(),
        lease_remaining_sec=lease_remaining,
        heartbeat_count=heartbeat_count,
        session_id=session.session_id if session else None,
    )


# =====================================================================
# 断点上报（续测核心）
# =====================================================================
def save_checkpoint(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    session_id: str,
    items: List[dict],
    cursor: Optional[dict] = None,
    lock_token: Optional[str] = None,
) -> schemas.CheckpointData:
    session = db.get(models.TestSession, session_id)
    if session is None:
        raise not_found("session_not_found", f"session_not_found: {session_id}")
    if session.sn != sn:
        raise bad_request("session_mismatch", f"session_mismatch: {session_id} does not belong to {sn}")
    if session.station_id != (client.station_id or ""):
        raise bad_request(
            "station_mismatch",
            f"station_mismatch: {session_id} belongs to {session.station_id}",
        )
    if session.status == models.SESSION_COMPLETED:
        raise conflict_error("session_completed", f"session_completed: {session_id}")
    # 运维中止会话（换测试用例清单前的"先停再换"）：在断点上报这一步就明确告知，
    # 上位机不必跑完全部用例才发现出站被拒，避免白跑一轮。
    if session.status == models.SESSION_ABORTED:
        raise conflict_error(
            "session_aborted",
            f"session_aborted: {session_id} terminated by operator, stop testing now",
            exit_code=EXIT_LOCK_EXPIRED,
            data={"session_id": session_id},
        )

    product = db.get(models.ProductStatus, sn)
    if product is not None and product.lock_token and _token_mismatch(product, lock_token):
        raise forbidden(
            "lock_invalid",
            f"lock_invalid: {sn} lock has been taken over, checkpoint rejected",
            exit_code=EXIT_LOCK_EXPIRED,
            data={"session_id": session_id},
        )

    merged = _merge_into_checkpoint(session, items)
    if cursor:
        payload = session.checkpoint if isinstance(session.checkpoint, dict) else {}
        payload["cursor"] = cursor
        session.checkpoint = payload
    now = utcnow()
    session.last_heartbeat_at = now
    # 只有当前持锁方能给锁"续命"：否则同工位任意机台都能刷别人的心跳，
    # 使失联锁永远达不到接管/回收条件。与 heartbeat() 的 holding 判定保持一致。
    if (
        product is not None
        and product.current_status == models.STATUS_TESTING
        and product.current_client == client.client_id
    ):
        product.lock_last_seen_at = now
    db.commit()

    return schemas.CheckpointData(
        sn=sn,
        session_id=session_id,
        accepted_count=len(items),
        merged_count=merged,
        completed_case_ids=_completed_case_ids(session),
        server_time=now.isoformat(),
    )


# =====================================================================
# 释放锁：主动释放 / 强制解锁
# =====================================================================
def release_lock(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    lock_token: Optional[str] = None,
    reason: str = "",
) -> schemas.ReleaseData:
    """上位机主动放弃锁（优雅退出 / 放弃本次测试）。不计产品失败。"""
    product = db.get(models.ProductStatus, sn)
    session_id = None
    if product is None:
        raise not_found("product_not_found", f"product_not_found: {sn}")

    holding = (
        product.current_status == models.STATUS_TESTING
        and product.current_client == client.client_id
    )
    if holding:
        if _token_mismatch(product, lock_token):
            raise forbidden(
                "lock_invalid",
                f"lock_invalid: {sn} lock has been taken over",
                exit_code=EXIT_LOCK_EXPIRED,
            )
        session = _running_session(db, sn, client.station_id or "")
        if session is not None:
            session_id = session.session_id
            _close_session(
                session, models.SESSION_ABORTED, reason or "released by client", client.client_id
            )
        _release_lock(product)
        product.current_status = models.STATUS_IDLE
        product.updated_at = utcnow()
        db.commit()

    return schemas.ReleaseData(
        sn=sn, released=holding, session_id=session_id, server_time=utcnow().isoformat()
    )


def force_release_lock(
    db: Session, *, sn: str, reason: str, operator: str
) -> schemas.ForceReleaseOut:
    """运维强制解锁：仅释放锁与会话，不改变印章/失败计数。"""
    product = _locked_product(db, sn)
    if product is None:
        raise not_found("product_not_found", f"product_not_found: {sn}")

    previous_client = product.current_client
    session_id = None
    if product.current_status == models.STATUS_TESTING:
        session = (
            db.query(models.TestSession)
            .filter(
                models.TestSession.sn == sn,
                models.TestSession.status == models.SESSION_RUNNING,
            )
            .order_by(models.TestSession.started_at.desc())
            .first()
        )
        if session is not None:
            session_id = session.session_id
            _close_session(session, models.SESSION_ABORTED, f"force_release: {reason}", operator)
        _release_lock(product)
        product.current_status = models.STATUS_IDLE
        product.updated_at = utcnow()
        db.commit()

    return schemas.ForceReleaseOut(
        sn=sn,
        released=True,
        previous_client=previous_client,
        session_id=session_id,
        reason=reason,
    )


# =====================================================================
# 孤儿锁回收（后台任务）
# =====================================================================
def _running_session_of(db: Session, sn: str):
    """该 SN 当前处于 RUNNING 的最新会话（回收与接管都以此为准）。"""
    return (
        db.query(models.TestSession)
        .filter(
            models.TestSession.sn == sn,
            models.TestSession.status == models.SESSION_RUNNING,
        )
        .order_by(models.TestSession.started_at.desc())
        .first()
    )


def sweep_orphan_locks(db: Session) -> dict:
    """回收失联/超时锁，无需等待他人抢锁。

    失联（心跳断流 > grace）→ 解锁，会话 ABORTED，**不计失败**；
    连续失联达到 LOST_LOCK_FAIL_THRESHOLD 次才计一次失败并告警。
    硬超时（持锁 > timeout_sec）→ 解锁并计一次失败。
    """
    stats = {"lost": 0, "expired": 0, "failed": 0}
    rows = (
        db.query(models.ProductStatus)
        .filter(models.ProductStatus.current_status == models.STATUS_TESTING)
        .all()
    )
    for product in rows:
        # rows 是本轮开始时的一次性快照。判定用的是快照里的 lock_acquired_at，
        # 若"判定"与"回收"之间该 SN 被其他机台接管，锁已被换发——此时继续回收
        # 就会关掉别人刚建立的新会话并误计一次失败。故记录快照值用于下方 CAS 校验。
        observed_acquired = product.lock_acquired_at

        probe = _running_session_of(db, product.sn)
        station_id = (probe.station_id if probe else None) or _station_of_client(db, product)
        timeout_sec = _timeout_of(db, station_id)

        expired = is_lock_expired(product, timeout_sec)
        lost = (not expired) and is_lock_lost(product)
        if not (expired or lost):
            continue

        # 原子抢占回收权：仅当锁仍是"判定时的那把"（lock_acquired_at 未变）才生效。
        # 抢不到说明锁已被接管/重新进站，本轮直接跳过，交由下一轮或业务方处理。
        reclaim_stmt = (
            update(models.ProductStatus)
            .where(
                models.ProductStatus.sn == product.sn,
                models.ProductStatus.current_status == models.STATUS_TESTING,
                models.ProductStatus.lock_acquired_at == observed_acquired,
            )
            .values(lock_token=None)
        )
        if _claimed_rowcount(db, reclaim_stmt) != 1:
            continue

        # 抢占成功 → 锁未变更，重新读取会话，确保关的是"与判定一致"的那一个
        session = _running_session_of(db, product.sn)

        if expired:
            if session is not None:
                _close_session(
                    session, models.SESSION_EXPIRED, f"lock timeout ({timeout_sec}s)", "sweeper"
                )
            _release_lock(product)
            product.current_status = models.STATUS_IDLE
            _apply_fail(product, f"lock timeout ({timeout_sec}s)")
            product.updated_at = utcnow()
            stats["expired"] += 1
            continue

        if session is not None:
            _close_session(
                session,
                models.SESSION_ABORTED,
                f"{LOST_REASON_PREFIX}: no heartbeat for {lock_idle_sec(product)}s",
                "sweeper",
            )
        _release_lock(product)
        product.current_status = models.STATUS_IDLE
        product.updated_at = utcnow()
        db.flush()
        if station_id and _count_consecutive_lost(db, product.sn, station_id) >= (
            settings.LOST_LOCK_FAIL_THRESHOLD
        ):
            _apply_fail(product, f"repeated client_lost at {station_id}")
            stats["failed"] += 1
        stats["lost"] += 1

    db.commit()
    return stats


def _station_of_client(db: Session, product: models.ProductStatus) -> str:
    """current_client 绑定的工位；机台不存在或尚未绑定工位时返回空串。

    station_id 可为空（上位机首次 resolve 会自动注册为未绑定），必须收敛成空串：
    返回值随后会被当作主键传给 _timeout_of，None 会触发 SQLAlchemy 的全空主键告警。
    """
    if not product.current_client:
        return ""
    client = db.get(models.StationClient, product.current_client)
    if client is None or not client.station_id:
        return ""
    return client.station_id


# =====================================================================
# 出站：需求 1 落库闭环 ACK + 漏测拦截
# =====================================================================
def _find_by_checkout_id(db: Session, sn: str, station_id: str, checkout_id: str):
    """按 executed_items.checkout_id 回放既有记录（限近 200 条，重试场景足够）。"""
    recent = (
        db.query(models.TestRecord)
        .filter(models.TestRecord.sn == sn, models.TestRecord.station_id == station_id)
        .order_by(models.TestRecord.record_id.desc())
        .limit(200)
        .all()
    )
    for record in recent:
        payload = record.executed_items if isinstance(record.executed_items, dict) else {}
        if payload.get("checkout_id") == checkout_id:
            return record
    return None


def check_out(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    checkout_id: str,
    items: List[dict],
    duration_ms: int = 0,
    reason: str = "",
    lock_token: Optional[str] = None,
) -> schemas.AckData:
    station_id = client.station_id
    if not station_id:
        raise forbidden("client_not_bound", f"client {client.client_id} is not bound to any station")

    product = _locked_product(db, sn)
    if product is None:
        raise not_found("product_not_found", f"product_not_found: {sn}")

    # 1) 幂等回放：网络重试命中同一 checkout_id 时直接返回既有回执
    replay = _find_by_checkout_id(db, sn, station_id, checkout_id)
    if replay is not None:
        return _ack(db, product, replay, checkout_id, replay=True)

    # 2) 持锁校验
    if (
        product.current_status != models.STATUS_TESTING
        or product.current_client != client.client_id
    ):
        raise forbidden(
            "lock_invalid",
            f"lock_invalid: {sn} is not held by {client.client_id}",
            exit_code=EXIT_LOCK_EXPIRED,
        )

    # 2.1) fencing：锁已被接管 → 旧持锁方一律拒绝，避免脏写
    if _token_mismatch(product, lock_token):
        raise forbidden(
            "lock_invalid",
            f"lock_invalid: {sn} lock has been taken over, checkout rejected",
            exit_code=EXIT_LOCK_EXPIRED,
            data={"current_client": product.current_client},
        )

    # 3) 锁硬超时自愈（自持锁开始计时，心跳不续期）
    timeout_sec = _timeout_of(db, station_id)
    if is_lock_expired(product, timeout_sec):
        session = _running_session(db, sn, station_id)
        if session is not None:
            _close_session(
                session, models.SESSION_EXPIRED, f"lock timeout at {station_id} ({timeout_sec}s)"
            )
        _release_lock(product)
        product.current_status = models.STATUS_IDLE
        _apply_fail(product, f"lock timeout at {station_id} ({timeout_sec}s)")
        db.commit()
        raise forbidden(
            "lock_expired",
            f"lock_expired: {sn} lock at {station_id} exceeded {timeout_sec}s",
            exit_code=EXIT_LOCK_EXPIRED,
        )

    model_row = db.get(models.ProductModel, product.product_model)
    graph = load_process(db, model_row.process_id) if model_row else None
    if graph is None or station_id not in graph.step_of:
        raise bad_request(
            "station_not_in_process",
            f"station_not_in_process: {station_id} is not configured for {product.product_model}",
        )

    # 4) 续测合并：用 checkpoint 补齐本次未提交的用例（崩溃前已跑完的不算漏测）
    session = _running_session(db, sn, station_id)
    merged_items = list(items)
    checkpoint_merged_count = 0
    if settings.MERGE_CHECKPOINT_ON_CHECKOUT and session is not None:
        submitted_ids = {it.get("case_id") for it in merged_items}
        for snapshot in _checkpoint_items(session):
            case_id = snapshot.get("case_id")
            if not case_id or case_id in submitted_ids:
                continue
            merged_items.append(snapshot)
            submitted_ids.add(case_id)
            checkpoint_merged_count += 1

    # 5) 漏测拦截：必测用例ID必须被实际执行
    #    未提交 / 被 SKIP  → 漏测，400 拦截
    #    已执行但判定 FAIL → 真实失败；必测 FAIL 计入 overall_result（见步骤 6）
    submitted = {it.get("case_id"): it for it in merged_items}
    required = mandatory_case_ids(graph, station_id)
    absent = [c for c in required if c not in submitted or submitted[c].get("result") == RESULT_SKIP]
    if absent:
        if session is not None:
            _close_session(session, models.SESSION_ABORTED, f"missing mandatory case IDs at {station_id}")
        _release_lock(product)
        product.current_status = models.STATUS_IDLE
        _apply_fail(product, f"missing mandatory case IDs at {station_id}")
        db.commit()
        raise bad_request(
            "missing_mandatory",
            f"missing_mandatory: {len(absent)} mandatory case ID(s) not executed at {station_id}",
            exit_code=EXIT_MISSING_MANDATORY,
            data={"case_ids": absent},
        )

    # 6) overall 判定：仅必测用例的 FAIL 判 FAIL，非必测（选做）用例只记录不判停。
    #    选做用例常为探索/加测项，其失败不应阻塞流转、更不应触发连续失败锁定；
    #    结果仍完整保留在 executed_items 里（台账/TopFailed 可见），只是不影响放行。
    mandatory_set = set(required)
    overall = (
        RESULT_FAIL
        if any(
            it.get("result") == RESULT_FAIL and it.get("case_id") in mandatory_set
            for it in merged_items
        )
        else RESULT_PASS
    )

    # 6) 落库（executed_items 内携带 checkout_id，作为 ACK 凭据）
    record = models.TestRecord(
        sn=product.sn,
        station_id=station_id,
        client_id=client.client_id,
        overall_result=overall,
        duration_ms=int(duration_ms or 0),
        is_valid=True,
        executed_items={
            "checkout_id": checkout_id,
            "firmware": product.current_fw_version,
            "reason": reason or None,
            "session_id": session.session_id if session else None,
            "attempt": session.attempt if session else 1,
            "checkpoint_merged_count": checkpoint_merged_count,
            "items": merged_items,
        },
    )
    db.add(record)
    db.flush()

    # 7) 盖章与状态推进
    if session is not None:
        _close_session(session, models.SESSION_COMPLETED, reason or "checked out")
    passed = _passed(product)
    _release_lock(product)
    if overall == RESULT_PASS:
        passed.add(station_id)
        _write_passed(product, passed, graph)
        product.fail_count = 0
        product.current_status = models.STATUS_IDLE
    else:
        product.current_status = models.STATUS_IDLE
        _apply_fail(product, f"failed at {station_id}: {reason or 'test failed'}".strip())
    product.updated_at = utcnow()
    db.commit()

    return _ack(db, product, record, checkout_id, replay=False)


def _ack(
    db: Session,
    product: models.ProductStatus,
    record: models.TestRecord,
    checkout_id: str,
    *,
    replay: bool,
) -> schemas.AckData:
    graph = None
    model_row = db.get(models.ProductModel, product.product_model)
    if model_row is not None:
        graph = load_process(db, model_row.process_id)

    passed = _passed(product)
    payload = record.executed_items if isinstance(record.executed_items, dict) else {}
    return schemas.AckData(
        acknowledged=True,
        record_id=record.record_id,
        checkout_id=checkout_id,
        session_id=payload.get("session_id"),
        checkpoint_merged_count=int(payload.get("checkpoint_merged_count") or 0),
        sn=product.sn,
        station_id=record.station_id,
        overall_result=record.overall_result,
        idempotent_replay=replay,
        fail_count=product.fail_count or 0,
        fail_limit=settings.FAIL_LIMIT,
        product_locked=product.current_status == models.STATUS_LOCKED,
        is_completed=bool(graph and is_completed(graph, passed)),
        passed_stations=sorted(passed),
        next_stations=next_stations(graph, passed) if graph else [],
        server_time=utcnow().isoformat(),
    )


# =====================================================================
# 维修处置：RETEST / ROLLBACK / RESET / SCRAP
# =====================================================================
def _invalidate_records(db: Session, sn: str, station_ids: Set[str]) -> int:
    if not station_ids:
        return 0
    rows = (
        db.query(models.TestRecord)
        .filter(
            models.TestRecord.sn == sn,
            models.TestRecord.station_id.in_(station_ids),
            models.TestRecord.is_valid.is_(True),
        )
        .all()
    )
    for row in rows:
        row.is_valid = False
    return len(rows)


def apply_repair(
    db: Session,
    *,
    sn: str,
    repair_action: str,
    target_station: Optional[str],
    reason: str,
    technician_id: str,
) -> Tuple[models.ProductStatus, models.RepairRecord]:
    product = _locked_product(db, sn)
    if product is None:
        raise not_found("product_not_found", f"product_not_found: {sn}")
    if product.current_status == models.STATUS_TESTING:
        raise conflict_error(
            "product_holding_lock",
            f"product_holding_lock: {sn} is under test by {product.current_client}",
            exit_code=EXIT_LOCK_CONFLICT,
        )

    model_row = db.get(models.ProductModel, product.product_model)
    graph = load_process(db, model_row.process_id) if model_row else None

    passed = _passed(product)
    action = repair_action.upper()

    if action == models.REPAIR_SCRAP:
        product.current_status = models.STATUS_SCRAPPED
        product.locked_at = utcnow()
        product.locked_reason = reason or "scrapped by technician"
    elif action == models.REPAIR_RESET:
        _invalidate_records(db, sn, set(graph.stations) if graph else set(passed))
        _write_passed(product, set(), graph)
        product.fail_count = 0
        product.current_status = models.STATUS_IDLE
        product.locked_at = None
        product.locked_reason = None
    elif action in (models.REPAIR_RETEST, models.REPAIR_ROLLBACK):
        if not target_station:
            raise bad_request("target_station_required", "target_station_required for RETEST/ROLLBACK")
        if graph is not None and target_station not in graph.step_of:
            raise bad_request(
                "station_not_in_process",
                f"station_not_in_process: {target_station} is not in {graph.process_id}",
            )
        if action == models.REPAIR_RETEST:
            revoked = {target_station} & passed
            if not revoked:
                raise bad_request(
                    "station_not_passed",
                    f"station_not_passed: {sn} has not passed {target_station}",
                )
        else:  # ROLLBACK：回退目标工位及其之后的所有工步
            base_order = graph.step_of.get(target_station, 0) if graph else 0
            revoked = {
                s for s in passed
                if not graph or graph.step_of.get(s, 0) >= base_order
            }
            if not revoked:
                raise bad_request(
                    "station_not_passed",
                    f"station_not_passed: nothing to roll back from {target_station}",
                )
        _invalidate_records(db, sn, revoked)
        _write_passed(product, passed - revoked, graph)
        product.current_status = models.STATUS_IDLE
        product.locked_at = None
        product.locked_reason = None
        if product.fail_count:
            product.fail_count = 0
    else:  # pragma: no cover - schema 层已校验
        raise bad_request("invalid_repair_action", f"invalid_repair_action: {repair_action}")

    product.updated_at = utcnow()
    repair = models.RepairRecord(
        sn=sn,
        repair_action=action,
        target_station=target_station or None,
        reason=reason or None,
        technician_id=technician_id,
    )
    db.add(repair)
    db.commit()
    return product, repair
