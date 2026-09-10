"""Pydantic 输入输出模型：API 契约的唯一声明处。"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from typing_extensions import Annotated

from .models import FW_RULE_EXACT, FW_RULE_MIN

_FW_RULE_PATTERN = rf"^({FW_RULE_EXACT}|{FW_RULE_MIN})$"


def _strip(value):
    return value.strip() if isinstance(value, str) else value


def _require_non_empty(value):
    if not value:
        raise ValueError("field_required: value cannot be empty")
    return value


Trimmed = Annotated[Optional[str], BeforeValidator(_strip)]
TrimmedRequired = Annotated[str, BeforeValidator(_strip), AfterValidator(_require_non_empty)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageOut(BaseModel):
    code: str


# =====================================================================
# 认证
# =====================================================================
class LoginRequest(BaseModel):
    username: TrimmedRequired
    password: str


class UserOut(ORMModel):
    id: int
    username: str
    full_name: Optional[str] = None
    is_admin: bool = True
    created_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


# =====================================================================
# 通道一：上位机 pytest
# =====================================================================
class ClientResolveIn(BaseModel):
    """机台身份上报（首次调用自动注册）。"""

    client_id: TrimmedRequired = Field(max_length=64)
    ip_address: Trimmed = Field(default=None, max_length=45)
    app_version: Trimmed = Field(default=None, max_length=50)


class CheckInIn(BaseModel):
    """进站：*IDN? 直读结果 + 本机待执行的用例ID清单。"""

    client_id: TrimmedRequired = Field(max_length=64)
    sn: TrimmedRequired = Field(max_length=64, description="*IDN? 直读序列号")
    product_model: TrimmedRequired = Field(max_length=64, description="*IDN? 直读机型")
    firmware: TrimmedRequired = Field(max_length=32, description="*IDN? 直读固件版本")
    case_ids: List[str] = Field(
        default_factory=list,
        description="本次待测的用例ID清单，用于防漏测前置校验",
    )
    app_version: Trimmed = Field(default=None, max_length=50)
    resume_session_id: Trimmed = Field(
        default=None,
        max_length=36,
        description="续测会话ID：崩溃重启后携带，服务端返回已完成用例清单",
    )


class ItemResultIn(BaseModel):
    """单个用例的执行结果快照。"""

    case_id: TrimmedRequired = Field(max_length=256, description="用例ID（与静态规则一致）")
    result: str = Field(description="PASS / FAIL / SKIP")
    values: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = Field(default=0, ge=0)
    message: Trimmed = Field(default=None)

    @field_validator("result")
    @classmethod
    def _check_result(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in ("PASS", "FAIL", "SKIP"):
            raise ValueError("invalid_result: must be PASS / FAIL / SKIP")
        return v


class CheckOutIn(BaseModel):
    """出站：上传执行结果，必须拿到服务端落库回执（ACK）。"""

    client_id: TrimmedRequired = Field(max_length=64)
    sn: TrimmedRequired = Field(max_length=64)
    checkout_id: TrimmedRequired = Field(max_length=64, description="出站幂等键；网络重试时复用同一值")
    items: List[ItemResultIn] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)
    reason: Trimmed = Field(default=None)
    lock_token: Trimmed = Field(
        default=None,
        max_length=64,
        description="fencing 凭证；锁已被接管时旧 token 写入一律拒绝",
    )


class CheckpointIn(BaseModel):
    """续测断点：增量上报已完成用例，按 case_id 去重覆盖（幂等）。"""

    client_id: TrimmedRequired = Field(max_length=64)
    sn: TrimmedRequired = Field(max_length=64)
    session_id: TrimmedRequired = Field(max_length=36)
    lock_token: Trimmed = Field(default=None, max_length=64)
    items: List[ItemResultIn] = Field(default_factory=list)
    cursor: Dict[str, Any] = Field(
        default_factory=dict, description="客户端断点上下文（当前步骤/仪器状态，原样回传）"
    )


class CheckpointData(BaseModel):
    sn: str
    session_id: str
    accepted_count: int = 0
    merged_count: int = 0
    completed_case_ids: List[str] = []
    server_time: str


class ReleaseIn(BaseModel):
    """主动放弃锁：优雅退出 / 崩溃前调用，避免占锁等待。"""

    client_id: TrimmedRequired = Field(max_length=64)
    sn: TrimmedRequired = Field(max_length=64)
    lock_token: Trimmed = Field(default=None, max_length=64)
    reason: Trimmed = Field(default=None, max_length=255)


class ReleaseData(BaseModel):
    sn: str
    released: bool = True
    session_id: Optional[str] = None
    server_time: str


class HeartbeatIn(BaseModel):
    client_id: TrimmedRequired = Field(max_length=64)
    sn: TrimmedRequired = Field(max_length=64)
    lock_token: Trimmed = Field(default=None, max_length=64)


class EnvelopeOut(BaseModel):
    """统一响应包：HTTP 状态码为主，exit_code 为辅。"""

    ok: bool
    exit_code: int
    code: str
    message: str
    data: Optional[Any] = None


class StationRule(BaseModel):
    """下发给上位机的静态执行规则。"""

    case_id: str
    item_name: str
    is_mandatory: bool = True


class NextStation(BaseModel):
    station_id: str
    station_name: str
    step_order: int


class ResumeInfo(BaseModel):
    """续测上下文：崩溃重启后上位机据此跳过已完成用例。"""

    resumed: bool = False
    attempt: int = 1
    completed_case_ids: List[str] = []
    cursor: Dict[str, Any] = {}


class CheckInData(BaseModel):
    sn: str
    product_model: str
    process_id: str
    station_id: str
    station_name: str
    timeout_sec: int
    is_first_station: bool
    created_on_checkin: bool = False
    rules: List[StationRule] = []
    passed_stations: List[str] = []
    next_stations: List[NextStation] = []
    server_time: str
    # ---- 租约锁与续测（v1.0）----
    session_id: Optional[str] = None
    lock_token: Optional[str] = None
    attempt: int = 1
    heartbeat_interval_sec: int = 30
    heartbeat_grace_sec: int = 120
    resume: ResumeInfo = Field(default_factory=ResumeInfo)
    takeover: bool = Field(default=False, description="本次进站接管了失联机台持有的锁")
    takeover_from: Optional[str] = Field(default=None, description="被接管的原持锁机台")


class AckData(BaseModel):
    """落库回执（需求 1：上位机必须校验此 ACK 才允许拔线流转）。"""

    acknowledged: bool = True
    record_id: int
    checkout_id: str
    sn: str
    station_id: str
    overall_result: str
    idempotent_replay: bool = False
    fail_count: int = 0
    fail_limit: int = 3
    product_locked: bool = False
    is_completed: bool = False
    passed_stations: List[str] = []
    next_stations: List[NextStation] = []
    server_time: str
    session_id: Optional[str] = None
    checkpoint_merged_count: int = Field(
        default=0, description="由 checkpoint 补齐的用例数（续测断点回放）"
    )


class HeartbeatData(BaseModel):
    sn: str
    holding_lock: bool
    remaining_sec: int = 0
    server_time: str
    # 硬超时剩余（自持锁开始计时，心跳不续期）
    lease_remaining_sec: int = 0
    heartbeat_count: int = 0
    session_id: Optional[str] = None


# =====================================================================
# 主数据：工艺流程 / 机型 / 工位 / 机台
# =====================================================================
class ProcessOut(ORMModel):
    process_id: str
    process_name: str
    is_active: bool = True
    created_at: Optional[datetime] = None


class ProcessStatOut(BaseModel):
    """流程概览：下拉与统计使用。"""

    process_id: str
    process_name: str
    is_active: bool = True
    model_count: int = 0
    models: List[str] = []
    station_count: int = 0
    item_count: int = 0
    created_at: Optional[datetime] = None


class ProcessCreateIn(BaseModel):
    process_id: TrimmedRequired = Field(max_length=64)
    process_name: Trimmed = Field(default="", max_length=128)
    is_active: bool = True


class ProcessUpdateIn(BaseModel):
    process_name: Trimmed = Field(default=None, max_length=128)
    is_active: Optional[bool] = None


class ProductModelOut(ORMModel):
    product_model: str
    process_id: str
    target_fw_version: str
    fw_match_rule: str = FW_RULE_EXACT
    created_at: Optional[datetime] = None


class ProductModelCreateIn(BaseModel):
    product_model: TrimmedRequired = Field(max_length=64)
    process_id: TrimmedRequired = Field(max_length=64)
    target_fw_version: TrimmedRequired = Field(max_length=32)
    fw_match_rule: str = Field(default=FW_RULE_EXACT, pattern=_FW_RULE_PATTERN)


class ProductModelUpdateIn(BaseModel):
    process_id: Trimmed = Field(default=None, max_length=64)
    target_fw_version: Trimmed = Field(default=None, max_length=32)
    fw_match_rule: Optional[str] = Field(default=None, pattern=_FW_RULE_PATTERN)


class StationOut(ORMModel):
    station_id: str
    station_name: str
    timeout_sec: int = 1800
    created_at: Optional[datetime] = None


class StationCreateIn(BaseModel):
    station_id: TrimmedRequired = Field(max_length=64)
    station_name: Trimmed = Field(default="", max_length=128)
    timeout_sec: int = Field(default=1800, ge=30, le=86400)


class StationUpdateIn(BaseModel):
    station_name: Trimmed = Field(default=None, max_length=128)
    timeout_sec: Optional[int] = Field(default=None, ge=30, le=86400)


class ClientOut(ORMModel):
    client_id: str
    station_id: Optional[str] = None
    ip_address: Optional[str] = None
    app_version: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    online: bool = False
    holding_sn: Optional[str] = None


class ClientCreateIn(BaseModel):
    client_id: TrimmedRequired = Field(max_length=64)
    station_id: TrimmedRequired = Field(max_length=64)
    ip_address: Trimmed = Field(default=None, max_length=45)
    app_version: Trimmed = Field(default=None, max_length=50)


class ClientUpdateIn(BaseModel):
    station_id: Trimmed = Field(default=None, max_length=64)
    ip_address: Trimmed = Field(default=None, max_length=45)


# =====================================================================
# 工艺拓扑与测试项
# =====================================================================
class ProcessStationOut(ORMModel):
    process_id: str
    station_id: str
    station_name: Optional[str] = None
    step_order: int
    depends_on: List[str] = []
    item_count: int = 0
    timeout_sec: int = 1800


class ProcessStationIn(BaseModel):
    station_id: TrimmedRequired = Field(max_length=64)
    step_order: int = Field(ge=1)
    depends_on: List[str] = Field(default_factory=list)


class StationItemOut(ORMModel):
    item_id: int
    process_id: str
    station_id: str
    case_id: str
    item_name: str
    is_mandatory: bool = True
    is_active: bool = True


class StationItemCreateIn(BaseModel):
    process_id: TrimmedRequired = Field(max_length=64)
    station_id: TrimmedRequired = Field(max_length=64)
    case_id: TrimmedRequired = Field(max_length=256)
    item_name: Trimmed = Field(default="", max_length=128)
    is_mandatory: bool = True
    is_active: bool = True


class StationItemUpdateIn(BaseModel):
    item_name: Trimmed = Field(default=None, max_length=128)
    is_mandatory: Optional[bool] = None
    is_active: Optional[bool] = None


class StationItemImportRow(BaseModel):
    """批量导入的一行：case_id 即 pytest nodeid。"""

    case_id: TrimmedRequired = Field(max_length=256)
    item_name: Trimmed = Field(default="", max_length=128)
    is_mandatory: bool = True


class StationItemImportIn(BaseModel):
    """按工位整批同步用例ID清单（幂等 upsert）。

    mode=upsert   清单内的项新增/更新并重新启用，清单外的项原样不动
    mode=replace  在 upsert 之上，把清单外的**启用中**项置为停用（不物理删，可回滚）
    dry_run       只算差异不落库，用于脚本的 --dry-run 预览
    """

    process_id: TrimmedRequired = Field(max_length=64)
    station_id: TrimmedRequired = Field(max_length=64)
    items: List[StationItemImportRow] = []
    mode: str = "upsert"
    dry_run: bool = False


class StationItemImportOut(BaseModel):
    process_id: str
    station_id: str
    mode: str
    dry_run: bool = False
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    total_active: int = 0
    # 清单外仍启用的项：上位机改名/删用例后，旧 nodeid 会残留成"没人跑的必测项"，
    # 导致进站 case_id_mismatch 全线拦截。upsert 模式只报告不动，replace 模式将其停用。
    orphan_count: int = 0
    orphans: List[str] = []
    warnings: List[str] = []


class TopologyOut(BaseModel):
    process_id: str
    steps: List[ProcessStationOut] = []
    items: List[StationItemOut] = []


class ValidateIssue(BaseModel):
    level: str  # error / warning
    code: str
    detail: str


class ValidateOut(BaseModel):
    process_id: str
    ok: bool
    issues: List[ValidateIssue] = []


class CloneIn(BaseModel):
    from_process: TrimmedRequired = Field(max_length=64)
    to_process: TrimmedRequired = Field(max_length=64)


class CloneOut(BaseModel):
    process_id: str
    cloned_steps: int
    cloned_items: int


# =====================================================================
# 在制品 / 维修处置
# =====================================================================
class ProductOut(ORMModel):
    sn: str
    product_model: str
    process_id: Optional[str] = None
    target_fw_version: Optional[str] = None
    current_fw_version: str = ""
    current_status: str
    passed_stations: List[str] = []
    fail_count: int = 0
    current_client: Optional[str] = None
    testing_started_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    locked_reason: Optional[str] = None
    updated_at: Optional[datetime] = None
    # ---- 租约锁派生字段（v1.0）----
    lock_token: Optional[str] = None
    lock_acquired_at: Optional[datetime] = None
    lock_last_seen_at: Optional[datetime] = None
    lock_heartbeat_count: int = 0
    # 派生字段
    total_steps: int = 0
    passed_count: int = 0
    is_completed: bool = False
    fw_match: bool = True
    lock_held_sec: int = Field(default=0, description="锁已持有时长（秒）")
    lock_idle_sec: int = Field(default=-1, description="距上次心跳时长（秒）；-1 表示无锁")
    lock_lease_remaining_sec: int = Field(default=0, description="硬超时剩余（秒）")
    lock_zombie: bool = Field(default=False, description="失联僵尸锁：可被接管或强制解锁")


class ProductPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[ProductOut]


class RepairIn(BaseModel):
    sn: TrimmedRequired = Field(max_length=64)
    repair_action: str = Field(description="RETEST / ROLLBACK / RESET / SCRAP")
    target_station: Trimmed = Field(default=None, max_length=64, description="RETEST / ROLLBACK 必填")
    reason: Trimmed = Field(default="")

    @field_validator("repair_action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in ("RETEST", "ROLLBACK", "RESET", "SCRAP"):
            raise ValueError("invalid_repair_action: must be RETEST / ROLLBACK / RESET / SCRAP")
        return v


class RepairOut(ORMModel):
    repair_id: int
    sn: str
    repair_action: str
    target_station: Optional[str] = None
    reason: Optional[str] = None
    technician_id: str
    created_at: Optional[datetime] = None


class RepairPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[RepairOut]


# =====================================================================
# 测试会话（续测与锁接管）
# =====================================================================
class SessionItemOut(BaseModel):
    """checkpoint 中单个用例的执行快照。"""

    case_id: str = ""
    result: str = ""
    message: Optional[str] = None
    duration_ms: int = 0
    seq: int = 0


class SessionOut(ORMModel):
    session_id: str
    sn: str
    station_id: str
    client_id: str
    attempt: int = 1
    status: str = "RUNNING"
    started_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    end_reason: Optional[str] = None
    ended_by: Optional[str] = None
    created_at: Optional[datetime] = None
    # 派生字段
    item_count: int = 0
    cursor: Dict[str, Any] = {}
    items: List[SessionItemOut] = []
    lock_held_sec: int = 0
    lock_idle_sec: int = -1


class SessionPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[SessionOut]


class ForceReleaseIn(BaseModel):
    """强制解锁：运维人工介入，需填写原因。"""

    reason: TrimmedRequired = Field(max_length=255, description="解锁原因（留痕）")


class ForceReleaseOut(BaseModel):
    sn: str
    released: bool = True
    previous_client: Optional[str] = None
    session_id: Optional[str] = None
    reason: Optional[str] = None


class SessionAbortIn(BaseModel):
    reason: Trimmed = Field(default=None, max_length=255)


class AbortedSessionPoint(BaseModel):
    session_id: str
    sn: str
    station_id: str
    client_id: Optional[str] = None


class SessionAbortRunningIn(BaseModel):
    """批量中止运行中的会话（换测试用例清单前的"先停再换"）。

    必须带至少一个过滤条件，禁止无参全量——一次误调用停掉整条产线代价太大。
    dry_run 只返回将受影响的会话，不执行。
    """

    station_id: Trimmed = Field(default=None, max_length=64)
    process_id: Trimmed = Field(default=None, max_length=64)
    sn: Trimmed = Field(default=None, max_length=64)
    client_id: Trimmed = Field(default=None, max_length=64)
    reason: Trimmed = Field(default=None, max_length=255)
    dry_run: bool = False


class SessionAbortRunningOut(BaseModel):
    aborted: int = 0
    dry_run: bool = False
    items: List[AbortedSessionPoint] = []


# =====================================================================
# 事件账本与追溯
# =====================================================================
class RecordOut(ORMModel):
    record_id: int
    sn: str
    station_id: str
    client_id: str
    overall_result: str
    executed_items: Dict[str, Any] = {}
    duration_ms: int = 0
    is_valid: bool = True
    created_at: Optional[datetime] = None


class RecordPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[RecordOut]


class TraceStep(BaseModel):
    station_id: str
    station_name: str
    step_order: int
    depends_on: List[str] = []
    passed: bool
    last_result: Optional[str] = None
    last_record_id: Optional[int] = None
    last_time: Optional[datetime] = None


class TraceOut(BaseModel):
    product: ProductOut
    steps: List[TraceStep] = []
    records: List[RecordOut] = []
    repairs: List[RepairOut] = []


# =====================================================================
# 仪表盘统计
# =====================================================================
class WipStat(BaseModel):
    idle: int = 0
    testing: int = 0
    locked: int = 0
    scrapped: int = 0
    completed: int = 0
    in_process: int = 0
    total: int = 0


class TodayStat(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0


class ClientStat(BaseModel):
    total: int = 0
    online: int = 0


class LockStat(BaseModel):
    """租约锁概览：活跃 / 僵尸（失联可接管）/ 会话。"""

    active: int = 0
    zombie: int = 0
    sessions_running: int = 0
    sessions_abnormal: int = 0


class YieldRow(BaseModel):
    key: str
    total: int = 0
    passed: int = 0
    pass_rate: float = 0.0
    # 仅件级良率使用：一次通过（整件无任何 FAIL 记录）的件数与占比。
    # 良率看终检结果，直通率看返修成本，两者并列才能看出"良率漂亮但重测多"。
    first_pass: int = 0
    first_pass_rate: float = 0.0


class YieldPoint(BaseModel):
    date: str
    total: int = 0
    passed: int = 0
    pass_rate: float = 0.0


class FailedItemPoint(BaseModel):
    case_id: str
    item_name: str
    fail_count: int


class WindowStat(BaseModel):
    """窗口整体良率：**按量加权**，与 trend 同源。

    日良率的算术平均会把"零产出日"按 0% 计入，导致窗口均值远低于真实良率
    （例：只有今日有 78 条且全 PASS，算术均值 = 100/14 ≈ 7.1%，加权 = 100%）。
    故由后端统一按 sum(passed)/sum(total) 计算，看板图例与趋势图均值线共用，
    避免同一页面出现两个口径的"日均值"。
    """

    total: int = 0
    passed: int = 0
    pass_rate: float = 0.0


class MetricsOverview(BaseModel):
    window_days: int
    wip: WipStat
    today: TodayStat
    window: WindowStat = Field(default_factory=WindowStat)
    trend: List[YieldPoint] = []
    station_yield: List[YieldRow] = []
    # 记录级口径：窗口内 test_records 的一次通过率。在制品未跑的工位不产生记录，
    # 故未完工的件只会抬高该值，不能代表"整件良率"（保留给需要按测试次数下钻的场景）。
    process_yield: List[YieldRow] = []
    # 件级良率（终检口径）：分母 = 窗口内已完结（走完全流程或报废）的在制品，
    # 在制不计入；不良只看终态——报废算不良，中途 FAIL 但重测通过算合格。
    process_unit_yield: List[YieldRow] = []
    process_unit_yield_pending: int = 0
    top_failed_items: List[FailedItemPoint] = []
    clients: ClientStat
    product_total: int = 0
    locks: LockStat = Field(default_factory=LockStat)
