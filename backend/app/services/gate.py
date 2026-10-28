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
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import IS_SQLITE, settings
from ..errors import (
    EXIT_CASE_ID_MISMATCH,
    EXIT_LOCK_CONFLICT,
    EXIT_LOCK_EXPIRED,
    EXIT_MISSING_MANDATORY,
    EXIT_PRODUCT_LOCKED,
    bad_request,
    conflict_error,
    forbidden,
    not_found,
)
from ..logging import get_logger
from . import artifacts
from .version import version_matches
from .routing import (
    _as_list,
    is_completed,
    load_process,
    mandatory_case_ids,
    missing_prereq,
    next_stations,
    station_rules,
)
from .timeutil import as_utc, elapsed_sec, utcnow

logger = get_logger("gate")

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

def _staged_files(session: Optional[models.TestSession]) -> Dict[str, List[dict]]:
    """会话断点里暂存的产物引用：{case_id: [{object_key, filename, size}, ...]}。"""
    if session is None:
        return {}
    payload = session.checkpoint if isinstance(session.checkpoint, dict) else {}
    staged = payload.get("staged")
    if not isinstance(staged, dict):
        return {}
    result: Dict[str, List[dict]] = {}
    for case_id, bucket in staged.items():
        if not isinstance(bucket, dict):
            continue
        refs = [r for r in bucket.values() if isinstance(r, dict) and r.get("object_key")]
        if refs:
            result[str(case_id)] = refs
    return result

def stage_artifact(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    session_id: str,
    case_id: str,
    filename: str,
    size: int,
    lock_token: Optional[str] = None,
    commit: bool = True,
) -> str:
    """测试产物登记：对象由 artifacts 服务落盘，此处把引用暂存进会话断点。

    通用机制：任何机台客户端均可按 case_id 上传产物（结果文件/日志等）。
    同 case_id + filename 重复上传即覆盖（与对象存储同键覆盖语义一致）；
    出站时服务端把暂存引用自动并入对应 case_id 的 item.files 落库。
    commit=False 时由调用方在文件落盘成功后统一提交，保证"登记↔落盘"原子。
    """
    session = db.get(models.TestSession, session_id)
    if session is None:
        raise not_found("session_not_found", f"会话 {session_id} 不存在")
    if session.sn != sn or session.client_id != client.client_id:
        raise bad_request(
            "session_mismatch",
            f"会话 {session_id} 不属于 {sn}@{client.client_id}",
        )
    if session.status == models.SESSION_COMPLETED:
        raise conflict_error("session_completed", f"会话 {session_id} 已完成出站，请勿重复上报")
    if session.status == models.SESSION_ABORTED:
        raise conflict_error(
            "session_aborted",
            f"会话 {session_id} 已被运维中止，请立即停止上传",
            exit_code=EXIT_LOCK_EXPIRED,
            data={"session_id": session_id},
        )
    product = db.get(models.ProductStatus, sn)
    if product is None:
        raise not_found("product_not_found", f"序列号 {sn} 不存在或尚未建档")
    if product.lock_token and _token_mismatch(product, lock_token):
        raise forbidden(
            "lock_invalid",
            f"锁已失效：{sn} 的工位锁已被接管，产物上传被拒绝",
            exit_code=EXIT_LOCK_EXPIRED,
            data={"session_id": session_id},
        )

    key = artifacts.object_key(product.product_model, sn, case_id, filename)
    payload = session.checkpoint if isinstance(session.checkpoint, dict) else {}
    staged = payload.get("staged")
    if not isinstance(staged, dict):
        staged = {}
    bucket = staged.get(case_id)
    if not isinstance(bucket, dict):
        bucket = {}
    safe_name = key.rsplit("/", 1)[-1]
    bucket[safe_name] = {"object_key": key, "filename": safe_name, "size": int(size or 0)}
    staged[case_id] = bucket
    payload["staged"] = staged
    session.checkpoint = payload
    # JSONB 原地修改需显式标记，否则 SQLAlchemy 不会 flush
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(session, "checkpoint")
    if commit:
        db.commit()
    return key

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
def _label(code: str, name: Optional[str]) -> str:
    """操作员可读标识：代码（名称）；无名称或与代码相同时退化为纯代码。"""
    return f"{code}（{name}）" if name and name != code else code


def _station_label(graph, station_id: str) -> str:
    return _label(station_id, graph.name_of.get(station_id) if graph else None)


def _dict_station_label(db, station_id: str) -> str:
    """工位字典直查（无流程图上下文时使用）：代码（名称），缺失退化为纯代码。"""
    st = db.get(models.Station, station_id)
    return _label(station_id, st.station_name if st else None)


def apply_manifest(ps_row, case_ids) -> bool:
    """把上报的用例序列写入工位基线草稿（内容变化才写）；返回是否变更。

    不做提交——进站路径由主事务统一提交，独立上报端点由调用方显式提交。
    """
    normalized = [c.strip() for c in (case_ids or []) if c and c.strip()]
    if ps_row.baseline_manifest == normalized:
        return False
    ps_row.baseline_manifest = normalized
    ps_row.baseline_captured_at = utcnow()
    return True


def report_manifest(db: Session, client_id: str, product_model: str, case_ids) -> dict:
    """录制态草稿上报（独立于进站，不受任何进站校验影响）。

    上位机在 check-in 之前上报本次会话的执行序列：即使进站被拒（必测缺失/
    顺序不符/固件不符…），草稿也已留下机台真实序列。锁定态不覆盖草稿，
    仅返回当前基线状态（供上位机感知已受控）。
    """
    client = get_client(db, client_id)
    if client is None:
        raise not_found(
            "client_not_registered",
            f"机台 {client_id} 未注册，请先在管理端登记机台",
        )
    model_row = db.get(models.ProductModel, product_model)
    if model_row is None:
        raise not_found(
            "model_not_registered",
            f"机型 {product_model} 未登记，请先在管理端注册机型",
        )
    graph = load_process(db, model_row.process_id)
    if graph is None:
        raise not_found(
            "process_not_found",
            f"机型 {product_model} 绑定的流程 {model_row.process_id} 不存在",
        )
    candidates = sorted(set(_as_list(client.bound_stations)) & set(graph.stations))
    if not candidates:
        raise forbidden(
            "client_not_bound",
            f"机台 {_label(client.client_id, client.client_name)} 的绑定工位均不在流程 "
            f"{graph.process_id} 内",
        )
    if len(candidates) > 1:
        raise bad_request(
            "station_ambiguous",
            f"机台 {_label(client.client_id, client.client_name)} 在流程内绑定了多个工位"
            f"（{'、'.join(_station_label(graph, c) for c in candidates)}），请修正机台绑定",
        )
    station_id = candidates[0]
    ps_row = db.get(models.ProcessStation, (model_row.process_id, station_id))
    if ps_row is None:
        raise not_found("step_not_found", f"{model_row.process_id}/{station_id}")
    if ps_row.baseline_locked:
        return {
            "recorded": False,
            "locked": True,
            "station_id": station_id,
            "baseline_count": len(ps_row.baseline_manifest or []),
        }
    changed = apply_manifest(ps_row, case_ids)
    if changed:
        db.commit()
    return {
        "recorded": changed,
        "locked": False,
        "station_id": station_id,
        "baseline_count": len(ps_row.baseline_manifest or []),
    }


def check_in(
    db: Session,
    *,
    client: models.StationClient,
    sn: str,
    product_model: str,
    firmware: str,
    case_ids: Optional[List[str]] = None,
    resume_session_id: Optional[str] = None,
    app_version: Optional[str] = None,
) -> schemas.CheckInData:
    model_row = db.get(models.ProductModel, product_model)
    if model_row is None:
        raise forbidden(
            "model_not_registered",
            f"机型 {product_model} 未登记，请先在管理端注册机型",
        )

    # SN 准入极性（SERVER_ROLE 切换，服务端硬卡控；建档之前拦截，生产台账零痕迹）。
    # ⚠ 身份准入先于流程逻辑：置于机型直通/流程处理之前，试验设备身份不受流程豁免。
    is_registered = db.get(models.SnRegistry, sn) is not None
    if settings.SERVER_ROLE == "development":
        # 测试服（白名单语义）：未登记的设备，服务端直接 403 拒收。
        # 即使研发跳过 conftest/SDK，底层进站依然被服务端卡死，生产件在测试服绝不可能被跑通。
        if not is_registered:
            raise forbidden(
                "sn_not_registered_test_device",
                f"序列号 {sn} 未在 SN 登记中登记（本服务器为研发白名单模式），禁止进站测试",
                data={"sn": sn},
            )
    else:
        # 生产服（黑名单语义，默认 Fail-Safe）：登记过的试验机，严禁进入生产。
        if is_registered:
            raise forbidden(
                "sn_is_test_device",
                f"序列号 {sn} 已登记为试验设备（生产准入黑名单），禁止进入生产产线",
                data={"sn": sn},
            )

    # 受控直通：存量机型灰度——进站直接 ACK，零写入（不建档/不领锁/不进台账）。
    # 注意在 load_process 之前返回：直通不依赖流程，机台绑定也不校验。
    # （能走到这里说明 SN 已通过身份准入，直通仅豁免流程逻辑，不豁免身份。）
    if model_row.passthrough:
        return schemas.CheckInData(
            sn=sn,
            product_model=product_model,
            process_id=model_row.process_id,
            station_id="",
            station_name="",
            overall_result="",
            timeout_sec=0,
            is_first_station=False,
            passthrough=True,
            server_time=utcnow().isoformat(),
        )

    graph = load_process(db, model_row.process_id)
    if graph is None:
        raise not_found("process_not_found", f"机型 {product_model} 绑定的流程 {model_row.process_id} 不存在")

    candidates = sorted(set(_as_list(client.bound_stations)) & set(graph.stations))
    if not candidates:
        proc = db.get(models.Process, graph.process_id)
        raise forbidden(
            "client_not_bound",
            f"机台 {_label(client.client_id, client.client_name)} 的绑定工位均不在流程 "
            f"{_label(graph.process_id, proc.process_name if proc else None)} 内",
        )
    if len(candidates) > 1:
        raise bad_request(
            "station_ambiguous",
            f"机台 {_label(client.client_id, client.client_name)} 在流程内绑定了多个工位"
            f"（{', '.join(_station_label(graph, c) for c in candidates)}），请修正机台绑定",
        )
    station_id = candidates[0]
    client.station_id = station_id

    product = db.get(models.ProductStatus, sn)
    created = False
    if product is None:
        if station_id != graph.stations[0]:
            raise forbidden(
                "missing_prereq",
                f"序列号 {sn} 尚无测试记录，必须从工位 {_station_label(graph, graph.stations[0])} 开始",
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
            f"机型不符：序列号 {sn} 建档机型为 {product.product_model}，进站报的是 {product_model}",
        )

    rule = model_row.fw_match_rule or models.FW_RULE_EXACT
    fw_match = version_matches(firmware, model_row.target_fw_version, rule)
    if not fw_match and settings.ENFORCE_FW:
        raise forbidden(
            "firmware_mismatch",
            f"固件版本不符：{sn} 期望（{rule}）{model_row.target_fw_version}，实际 {firmware}",
            data={
                "expected": model_row.target_fw_version,
                "actual": firmware,
                "rule": rule,
            },
        )
    product.current_fw_version = firmware

    # 4.5) 测试软件版本基线：低于/不符合机型基线拒绝；unknown/空版本视为不满足
    target_app = model_row.target_app_version
    if settings.ENFORCE_APP_SW and target_app:
        reported = (app_version or "").strip()
        app_rule = model_row.app_match_rule or models.FW_RULE_EXACT
        if not reported or reported.lower() == "unknown" or not version_matches(
            reported, target_app, app_rule
        ):
            raise forbidden(
                "app_version_mismatch",
                f"测试软件版本不符：{sn} 期望（{app_rule}）{target_app}，"
                f"实际 {reported or 'unknown'}",
                data={
                    "expected": target_app,
                    "actual": reported or None,
                    "rule": app_rule,
                },
            )

    if product.current_status == models.STATUS_SCRAPPED:
        raise forbidden("product_scrapped", f"序列号 {sn} 已报废，禁止进站测试")
    if product.current_status == models.STATUS_LOCKED:
        raise forbidden(
            "product_locked",
            f"序列号 {sn} 连续失败已达上限（{product.fail_count}/{settings.FAIL_LIMIT}），已被工程锁定",
            exit_code=EXIT_PRODUCT_LOCKED,
            data={"fail_count": product.fail_count, "locked_reason": product.locked_reason},
        )

    passed = _passed(product)

    if station_id in passed:
        raise conflict_error(
            "station_already_passed",
            f"复测拦截：{sn} 已通过工位 {_station_label(graph, station_id)}",
            data={"passed_stations": sorted(passed)},
        )

    # 8) 防跳站闸门（需求 2 核心）
    missing = missing_prereq(graph, station_id, passed)
    if missing:
        raise forbidden(
            "missing_prereq",
            f"防跳站拦截：{sn} 须先完成工位 {'.、'.join(_station_label(graph, m) for m in missing)}，才能进入 {_station_label(graph, station_id)}",
            data={"missing": missing},
        )

    # 9) 用例序列校验：锁定工位 = 黄金样本全列表严格比对（含顺序、含查多，
    #    替代必测覆盖与相对顺序两项检查——等式已蕴含覆盖性）；未锁定工位 =
    #    现有必测覆盖 + 相对顺序校验（存量行为零变化），并在守卫通过后捕获草稿
    ps_row = db.get(models.ProcessStation, (model_row.process_id, station_id))
    baseline_locked = bool(ps_row and ps_row.baseline_locked)

    if baseline_locked:
        """ 黄金样本：进站上报的有序用例全集必须与受控基线逐项一致。
        case_ids 未上报视为全部缺失（锁定工位强制客户端自证清单）；
        独立于 ENFORCE_CASE_IDS——锁定是工艺员的显式承诺，不随全局开关失效。 """
        incoming = [c.strip() for c in (case_ids or []) if c and c.strip()]
        manifest = list(ps_row.baseline_manifest or [])
        if incoming != manifest:
            missing = [c for c in manifest if c not in incoming]
            extra = [c for c in incoming if c not in manifest]
            first_diff = next(
                (i for i, (a, b) in enumerate(zip(incoming, manifest)) if a != b),
                min(len(incoming), len(manifest)),
            )
            detail = f"缺失 {len(missing)} 项、多余 {len(extra)} 项"
            if not missing and not extra:
                detail = f"自第 {first_diff + 1} 项起顺序不同"
            raise bad_request(
                "case_order_mismatch",
                f"用例序列与受控基线不一致：工位 {_station_label(graph, station_id)} "
                f"{detail}（基线共 {len(manifest)} 项），请先在管理端审阅并解锁重录",
                exit_code=EXIT_CASE_ID_MISMATCH,
                data={"missing": missing, "extra": extra, "first_diff_index": first_diff},
            )
    else:
        if case_ids is not None and settings.ENFORCE_CASE_IDS:
            covered = set(case_ids)
            absent = [c for c in mandatory_case_ids(graph, station_id) if c not in covered]
            if absent:
                raise bad_request(
                    "case_id_mismatch",
                    f"用例清单不符：工位 {_station_label(graph, station_id)} 的必测用例未全部收集：{'.、'.join(absent)}",
                    exit_code=EXIT_CASE_ID_MISMATCH,
                    data={"missing_case_ids": absent},
                )
            """ 执行顺序契约：rules 按 sort_order 下发，上位机须按此顺序执行。
            case_ids 通常是整个工程的收集顺序，先滤出本工位用例再比相对顺序 """
            configured = [i.case_id for i in graph.items_of.get(station_id, [])]
            submitted = [c for c in case_ids if c in configured]
            expected = [c for c in configured if c in covered]
            if submitted != expected:
                raise bad_request(
                    "case_order_mismatch",
                    f"用例执行顺序不符：工位 {_station_label(graph, station_id)} 期望 "
                    f"{' → '.join(expected)}，实际 {' → '.join(submitted)}",
                    exit_code=EXIT_CASE_ID_MISMATCH,
                    data={"expected_case_ids": expected, "actual_case_ids": submitted},
                )
        # 录制态捕获：全部守卫通过后覆盖基线草稿（内容变化才写库，避免每次进站徒增写放大）
        if ps_row is not None and case_ids is not None:
            apply_manifest(ps_row, case_ids)

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
                f"锁冲突：{sn} 正在由机台 {product.current_client} 测试中",
                exit_code=EXIT_LOCK_CONFLICT,
                data={
                    "locked_client": product.current_client,
                    "lock_idle_sec": lock_idle_sec(product),
                    "grace_sec": settings.LOCK_HEARTBEAT_GRACE_SEC,
                },
            )
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

    session = None
    resume = schemas.ResumeInfo()
    if resume_session_id:
        session = db.get(models.TestSession, resume_session_id)
        if session is None:
            raise not_found("session_not_found", f"会话 {resume_session_id} 不存在，无法续测")
        if session.sn != sn or session.station_id != station_id:
            raise bad_request(
                "session_mismatch",
                f"会话 {resume_session_id} 不属于 {sn}@{station_id}，无法续测",
            )
        if session.status == models.SESSION_COMPLETED:
            raise conflict_error(
                "session_completed",
                f"会话 {resume_session_id} 已完成出站，无法续测",
            )
    elif not takeover:
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
            f"锁冲突：{sn} 刚被其他机台并发抢占，请重试",
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
        raise not_found("session_not_found", f"会话 {session_id} 不存在")
    if session.sn != sn:
        raise bad_request("session_mismatch", f"会话 {session_id} 不属于 {sn}")
    if session.station_id != (client.station_id or ""):
        raise bad_request(
            "station_mismatch",
            f"会话 {session_id} 属于工位 {_dict_station_label(db, session.station_id)}，与当前机台不符",
        )
    if session.status == models.SESSION_COMPLETED:
        raise conflict_error("session_completed", f"会话 {session_id} 已完成出站，请勿重复上报")
    # 上位机不必跑完全部用例才发现出站被拒，避免白跑一轮。
    if session.status == models.SESSION_ABORTED:
        raise conflict_error(
            "session_aborted",
            f"会话 {session_id} 已被运维中止，请立即停止测试",
            exit_code=EXIT_LOCK_EXPIRED,
            data={"session_id": session_id},
        )

    product = db.get(models.ProductStatus, sn)
    if product is not None and product.lock_token and _token_mismatch(product, lock_token):
        raise forbidden(
            "lock_invalid",
            f"锁已失效：{sn} 的工位锁已被接管，断点上报被拒绝",
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
        raise not_found("product_not_found", f"序列号 {sn} 不存在或尚未建档")

    holding = (
        product.current_status == models.STATUS_TESTING
        and product.current_client == client.client_id
    )
    if holding:
        if _token_mismatch(product, lock_token):
            raise forbidden(
                "lock_invalid",
                f"锁已失效：{sn} 的工位锁已被接管",
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
        raise not_found("product_not_found", f"序列号 {sn} 不存在或尚未建档")

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
        # 若"判定"与"回收"之间该 SN 被其他机台接管，锁已被换发——此时继续回收
        observed_acquired = product.lock_acquired_at

        probe = _running_session_of(db, product.sn)
        station_id = (probe.station_id if probe else None) or _station_of_client(db, product)
        timeout_sec = _timeout_of(db, station_id)

        expired = is_lock_expired(product, timeout_sec)
        lost = (not expired) and is_lock_lost(product)
        if not (expired or lost):
            continue

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
        raise forbidden("client_not_bound", f"机台 {_label(client.client_id, client.client_name)} 未绑定任何工位")

    product = _locked_product(db, sn)
    if product is None:
        raise not_found("product_not_found", f"序列号 {sn} 不存在或尚未建档")

    # 1) 幂等回放：网络重试命中同一 checkout_id 时直接返回既有回执
    replay = _find_by_checkout_id(db, sn, station_id, checkout_id)
    if replay is not None:
        return _ack(db, product, replay, checkout_id, replay=True)

    if (
        product.current_status != models.STATUS_TESTING
        or product.current_client != client.client_id
    ):
        raise forbidden(
            "lock_invalid",
            f"锁已失效：{sn} 的工位锁当前不在机台 {client.client_id} 手中",
            exit_code=EXIT_LOCK_EXPIRED,
        )

    # 2.1) fencing：锁已被接管 → 旧持锁方一律拒绝，避免脏写
    if _token_mismatch(product, lock_token):
        raise forbidden(
            "lock_invalid",
            f"锁已失效：{sn} 的工位锁已被接管，出站被拒绝",
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
            f"锁已超时：{sn} 在工位 {_dict_station_label(db, station_id)} 的锁超过 {timeout_sec}s 未心跳，已被自动释放",
            exit_code=EXIT_LOCK_EXPIRED,
        )

    model_row = db.get(models.ProductModel, product.product_model)
    graph = load_process(db, model_row.process_id) if model_row else None
    if graph is None or station_id not in graph.step_of:
        raise bad_request(
            "station_not_in_process",
            f"工位 {_dict_station_label(db, station_id)} 未配置在机型 {product.product_model} 的流程中",
        )

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

    # 产物并入：/client/artifact 暂存在会话断点里的文件引用，
    # 自动挂到对应 case_id 的 item.files（暂存即权威来源，无客户端回传通道）
    staged = _staged_files(session)
    if staged:
        for item in merged_items:
            refs = staged.get(item.get("case_id"))
            if refs:
                item["files"] = refs

    # 5) 漏测拦截：必测用例ID必须被实际执行
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
            f"漏测拦截：工位 {_station_label(graph, station_id)} 有 {len(absent)} 条必测用例未执行",
            exit_code=EXIT_MISSING_MANDATORY,
            data={"case_ids": absent},
        )

    mandatory_set = set(required)
    overall = (
        RESULT_FAIL
        if any(
            it.get("result") == RESULT_FAIL and it.get("case_id") in mandatory_set
            for it in merged_items
        )
        else RESULT_PASS
    )

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
def _invalidate_records(db: Session, sn: str, station_ids: Set[str]) -> List[int]:
    """作废指定工位的有效台账记录（软删），返回被作废的 record_id 列表（撤销快照用）。"""
    if not station_ids:
        return []
    rows = (
        db.query(models.TestRecord)
        .filter(
            models.TestRecord.sn == sn,
            models.TestRecord.station_id.in_(station_ids),
            models.TestRecord.is_valid.is_(True),
        )
        .all()
    )
    ids = [row.record_id for row in rows]
    for row in rows:
        row.is_valid = False
    return ids

def _dependents_closure(graph, target_station: str) -> Set[str]:
    """依赖闭包：直接或间接 depends_on 包含 target 的全部工位（级联重测范围）。

    沿"谁依赖我"反向遍历依赖图；工步顺序在后但依赖链不经过 target 的工位不在内。
    """
    dependents: Dict[str, List[str]] = {}
    for s in graph.stations:
        for d in graph.deps_of.get(s, []):
            dependents.setdefault(d, []).append(s)
    seen: Set[str] = set()
    stack = [target_station]
    while stack:
        for nxt in dependents.get(stack.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _check_repair(
    db: Session, *, sn: str, repair_action: str, target_station: Optional[str],
    confirm_downstream: bool = False,
) -> Tuple[models.ProductStatus, str, object, Set[str], bool, Optional[Set[str]], str, str]:
    """维修处置守卫：预演（dry_run）与执行共用同一套校验，杜绝口径漂移。不落库。

    返回 (product, action, graph, passed, was_completed, revoked, product_model, process_id)；
    revoked 仅 RETEST/ROLLBACK 有值（预演可据此计算破坏面）。
    RETEST 命中 invalidates_downstream 工位且下游有已盖章工位时，需 confirm_downstream
    确认依赖重测（未确认 409 downstream_invalidation_required）——重测改变了被测件状态，
    依赖本工位数据的下游测量基于旧状态。
    机型/流程随件返回：同一工位在各流程的测试项与工步序不同，批量混合批次必须让
    操作员看到每件归属，避免"同工位名、不同工艺内容"的误解。
    """
    product = _locked_product(db, sn)
    if product is None:
        raise not_found("product_not_found", f"序列号 {sn} 不存在或尚未建档")
    if product.current_status == models.STATUS_TESTING:
        raise conflict_error(
            "product_holding_lock",
            f"处置拦截：{sn} 正在由机台 {product.current_client} 测试中，请先中止会话",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    action = (repair_action or "").upper()
    if action not in (models.REPAIR_RETEST, models.REPAIR_ROLLBACK, models.REPAIR_RESET, models.REPAIR_SCRAP):
        raise bad_request("invalid_repair_action", f"未知的处置动作：{repair_action}")
    # 报废件封死复活通道：RESET/RETEST/ROLLBACK 会把 SCRAPPED 置回 IDLE，
    # 击穿报废语义。仅允许再次确认报废（幂等补留痕）。
    if product.current_status == models.STATUS_SCRAPPED and action != models.REPAIR_SCRAP:
        raise conflict_error(
            "product_scrapped",
            f"处置拦截：{sn} 已报废，不能通过 {action} 重新流线",
        )

    model_row = db.get(models.ProductModel, product.product_model)
    graph = load_process(db, model_row.process_id) if model_row else None
    passed = _passed(product)
    was_completed = bool(product.is_completed)

    revoked: Optional[Set[str]] = None
    if action in (models.REPAIR_RETEST, models.REPAIR_ROLLBACK):
        if not target_station:
            raise bad_request("target_station_required", "RETEST/ROLLBACK 需要指定目标工位")
        if graph is not None and target_station not in graph.step_of:
            proc = db.get(models.Process, graph.process_id)
            raise bad_request(
                "station_not_in_process",
                f"目标工位 {target_station} 不在流程 "
                f"{_label(graph.process_id, proc.process_name if proc else None)} 内",
            )
        if action == models.REPAIR_RETEST:
            revoked = {target_station} & passed
            if not revoked:
                raise bad_request(
                    "station_not_passed",
                    f"重测拦截：{sn} 尚未通过工位 {_station_label(graph, target_station)}",
                )
            mutates = target_station in graph.invalidates if graph else False
            downstream: Set[str] = set()
            if graph and mutates:
                downstream = _dependents_closure(graph, target_station) & passed
            if mutates and downstream:
                if not confirm_downstream:
                    raise conflict_error(
                        "downstream_invalidation_required",
                        "依赖重测确认：重测将一并清除以下下游工位的印章："
                        + ", ".join(sorted(downstream)),
                        data={"stations": sorted(downstream)},
                    )
                revoked |= downstream
        else:
            base_order = graph.step_of.get(target_station, 0) if graph else 0
            revoked = {
                s for s in passed
                if not graph or graph.step_of.get(s, 0) >= base_order
            }
            if not revoked:
                raise bad_request(
                    "station_not_passed",
                    f"回退拦截：从工位 {_station_label(graph, target_station)} 起没有可回退的印章",
                )
    return product, action, graph, passed, was_completed, revoked, product.product_model, (
        model_row.process_id if model_row else ""
    )


def apply_repair(
    db: Session,
    *,
    sn: str,
    repair_action: str,
    target_station: Optional[str],
    reason: str,
    technician_id: str,
    confirm_downstream: bool = False,
    batch_id: Optional[str] = None,
) -> Tuple[models.ProductStatus, models.RepairRecord]:
    product, action, graph, passed, was_completed, revoked, _model, _process = _check_repair(
        db, sn=sn, repair_action=repair_action, target_station=target_station,
        confirm_downstream=confirm_downstream,
    )
    # 处置前状态快照：撤销处置（undo_repair）的精确还原依据
    if action == models.REPAIR_RESET:
        snapshot_revoked = sorted(set(graph.stations) if graph else set(passed))
    else:
        snapshot_revoked = sorted(revoked) if revoked else []
    snapshot = {
        "passed_stations": sorted(passed),
        "fail_count": product.fail_count or 0,
        "current_status": product.current_status,
        "is_completed": was_completed,
        "locked_reason": product.locked_reason,
        "revoked_stations": snapshot_revoked,
        "invalidated_record_ids": [],
    }

    if action == models.REPAIR_SCRAP:
        product.current_status = models.STATUS_SCRAPPED
        product.locked_at = utcnow()
        product.locked_reason = reason or "scrapped by technician"
    elif action == models.REPAIR_RESET:
        snapshot["invalidated_record_ids"] = _invalidate_records(
            db, sn, set(graph.stations) if graph else set(passed)
        )
        _write_passed(product, set(), graph)
        product.fail_count = 0
        product.current_status = models.STATUS_IDLE
        product.locked_at = None
        product.locked_reason = None
    else:  # RETEST / ROLLBACK：revoked 已由守卫算出且非空
        snapshot["invalidated_record_ids"] = _invalidate_records(db, sn, revoked)
        _write_passed(product, passed - revoked, graph)
        product.current_status = models.STATUS_IDLE
        product.locked_at = None
        product.locked_reason = None
        if product.fail_count:
            product.fail_count = 0

    # 维修处置破坏完工态时，联动作废该 SN 的报告任务（含存储清理）；
    # 产品重新完工后由完工扫描自动生成新报告（SCRAP 不作废，报告为报废前最后有效记录）
    invalidated_jobs = []
    if action in (models.REPAIR_RETEST, models.REPAIR_ROLLBACK, models.REPAIR_RESET) and was_completed:
        from .reports import engine as report_engine

        invalidated_jobs = report_engine.invalidate_jobs_for_sn(db, sn)

    product.updated_at = utcnow()
    repair = models.RepairRecord(
        sn=sn,
        repair_action=action,
        target_station=target_station or None,
        reason=reason or None,
        technician_id=technician_id,
        snapshot=snapshot,
        batch_id=batch_id,
    )
    db.add(repair)
    db.commit()
    # 提交成功后才动外部存储：避免处置回滚后产物已被删、MES 已被作废
    if invalidated_jobs:
        logger.info(
            "维修处置使完工态失效，已作废 %d 个报告任务：%s %s", len(invalidated_jobs), sn, action
        )
        report_engine.purge_invalidated_jobs(db, invalidated_jobs)
    return product, repair


def undo_repair(db: Session, *, repair_id: int, operator_id: str) -> models.RepairRecord:
    """撤销维修处置：窄窗口精确还原（按处置前快照），SCRAP 永不可撤销。

    边界（服务端强制，全部满足才可撤销）：
    ① 仅 RETEST/ROLLBACK/RESET；② 仅该 SN 最新一条未撤销处置；
    ③ 处置后零后续动作（无新会话、印章集合仍等于处置后状态）；④ 未被撤销过。
    撤销效果：印章/失败计数/状态按快照还原，被作废的台账记录按快照 ID 精确恢复
    （只碰本条处置作废的记录，不碰更早的历史）；完工态恢复后由完工扫描自动重出报告。
    """
    repair = db.get(models.RepairRecord, repair_id)
    if repair is None:
        raise not_found("repair_not_found", f"处置记录 {repair_id} 不存在")
    if repair.undone_at is not None:
        raise conflict_error(
            "already_undone",
            f"该处置已由 {repair.undone_by} 撤销过（记录 {repair_id}），不能重复撤销",
        )
    if repair.repair_action == models.REPAIR_SCRAP:
        raise conflict_error(
            "action_not_undoable",
            f"报废处置不可撤销（记录 {repair_id}）",
        )
    snap = repair.snapshot or {}
    if not snap:
        raise conflict_error(
            "snapshot_missing", f"处置记录 {repair_id} 缺少状态快照，无法撤销"
        )
    product = db.get(models.ProductStatus, repair.sn)
    if product is None:
        raise not_found("product_not_found", f"product_not_found: {repair.sn}")

    # ② 仅最新一条未撤销处置（链条中间撤销会造成状态分叉）
    latest = (
        db.query(models.RepairRecord)
        .filter(
            models.RepairRecord.sn == repair.sn,
            models.RepairRecord.undone_at.is_(None),
        )
        .order_by(models.RepairRecord.repair_id.desc())
        .first()
    )
    if latest is None or latest.repair_id != repair.repair_id:
        raise conflict_error(
            "not_latest_disposition",
            f"撤销拦截：记录 {repair_id} 不是 {repair.sn} 最新一条处置（仅最新一条可撤销）",
        )

    # ③ 处置后零后续动作：无新会话（时间统一按 UTC 解析；created_at 为秒级精度，
    # 与会话 started_at 的微秒精度比较时加 1s 容差——印章比对才是主校验）
    created_at = as_utc(repair.created_at)
    sessions = db.query(models.TestSession).filter(models.TestSession.sn == repair.sn).all()
    if any(
        as_utc(s.started_at) > created_at + timedelta(seconds=1)
        for s in sessions
        if s.started_at is not None
    ):
        raise conflict_error(
            "repair_state_changed",
            f"撤销拦截：{repair.sn} 在处置 {repair_id} 之后产生了新的测试会话，撤销窗口已关闭",
        )
    # 印章集合仍等于处置后状态（处置前 - 撤销集合）
    pre_passed = set(snap.get("passed_stations") or [])
    post_passed = pre_passed - set(snap.get("revoked_stations") or [])
    if set(_passed(product)) != post_passed:
        raise conflict_error(
            "repair_state_changed",
            f"撤销拦截：{repair.sn} 的当前印章与处置 {repair_id} 后的状态不一致，撤销窗口已关闭",
        )

    # 按快照精确还原
    model_row = db.get(models.ProductModel, product.product_model)
    graph = load_process(db, model_row.process_id) if model_row else None
    _write_passed(product, pre_passed, graph)
    product.fail_count = snap.get("fail_count") or 0
    product.current_status = snap.get("current_status") or models.STATUS_IDLE
    product.locked_reason = snap.get("locked_reason")
    product.locked_at = None
    record_ids = snap.get("invalidated_record_ids") or []
    if record_ids:
        db.query(models.TestRecord).filter(models.TestRecord.record_id.in_(record_ids)).update(
            {models.TestRecord.is_valid: True}, synchronize_session=False
        )
    product.updated_at = utcnow()
    repair.undone_by = operator_id
    repair.undone_at = utcnow()
    db.commit()
    return repair

