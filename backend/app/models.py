"""ORM 数据模型：10 张生产级表 + 1 张鉴权表（users）。

    ┌─ 静态工艺与主数据（Web 低频维护）──────────────────────────┐
    │  processes ──┬── product_models (机型 → 专属流程 + 固件基线)  │
    │              └── process_stations (工步拓扑 DAG + step_order) │
    │  stations ────── station_items (用例ID静态清单)               │
    └────────────────────────────────────────────────────────────┘
    ┌─ 运行时数据（上位机高频读写）─────────────────────────────┐
    │  station_clients  物理机台档案（绑定逻辑工位）              │
    │  product_status   在制品状态机（首站 *IDN? 动态建档）       │
    │  test_sessions    测试会话（崩溃续测与锁接管的载体）        │
    │  test_records     事件账本（executed_items JSONB 快照）     │
    │  repair_records   维修处置与回滚履历                        │
    └────────────────────────────────────────────────────────────┘

硬件构型差异（如带/不带 AWG）由 **独立的 process_id** 物理隔离，
运行期不存在任何 IF/ELSE 条件分支。
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import IS_SQLITE
from .database import Base

if IS_SQLITE:
    ArrayText = JSON
    JsonbType = JSON
else:
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB

    ArrayText = ARRAY(Text)
    JsonbType = JSONB

# 状态机取值（与 DDL CHECK 约束一致）
STATUS_IDLE = "IDLE"
STATUS_TESTING = "TESTING"
STATUS_LOCKED = "LOCKED"
STATUS_SCRAPPED = "SCRAPPED"

FW_RULE_EXACT = "exact"  # 必须与基线完全一致
FW_RULE_MIN = "min"  # 不低于基线即可（版本按数字段比较，避免 V3.9 > V3.10 的字典序坑）

REPAIR_RETEST = "RETEST"
REPAIR_ROLLBACK = "ROLLBACK"
REPAIR_RESET = "RESET"
REPAIR_SCRAP = "SCRAP"

SESSION_RUNNING = "RUNNING"
SESSION_COMPLETED = "COMPLETED"
SESSION_ABORTED = "ABORTED"
SESSION_EXPIRED = "EXPIRED"  # 超过工位硬超时（计一次失败）
SESSION_TAKEN_OVER = "TAKEN_OVER"  # 被其他机台接管

# 异常终止：非正常出库结束的会话。列表筛选与概览统计共用，避免两处口径漂移
ABNORMAL_SESSION_STATUSES = (SESSION_ABORTED, SESSION_EXPIRED, SESSION_TAKEN_OVER)

def _pk_column():
    """自增主键：PG 用 BIGINT IDENTITY，SQLite 用 INTEGER AUTOINCREMENT。"""
    if IS_SQLITE:
        return mapped_column(Integer, primary_key=True, autoincrement=True)
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)

def _array_column():
    """TEXT[]：PG 原生数组（可 GIN 索引），SQLite 退化为 JSON。"""
    if IS_SQLITE:
        return mapped_column(JSON, default=list, server_default=text("'[]'"))
    return mapped_column(ArrayText, default=list, server_default=text("'{}'"))

def _json_column():
    """JSONB：PG 原生，SQLite 退化为 JSON。"""
    if IS_SQLITE:
        return mapped_column(JSON, default=dict, server_default=text("'{}'"))
    return mapped_column(JsonbType, default=dict, server_default=text("'{}'"))

def _gin_index(name: str, column):
    """PG 专用 GIN 索引；SQLite 返回 None（由 _table_args 过滤）。"""
    if IS_SQLITE:
        return None
    return Index(name, column, postgresql_using="gin")

def _table_args(*args):
    return tuple(a for a in args if a is not None)

# =====================================================================
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin", server_default=text("'admin'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

# =====================================================================
class Process(Base):
    """工艺流程主表：一个硬件构型一条独立流程（带 AWG / 不带 AWG 彻底解耦）。"""

    __tablename__ = "processes"

    process_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    process_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 否则一次停用就会打断正在这条流程上跑的产线。
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    models = relationship("ProductModel", back_populates="process", cascade="all, delete-orphan")
    steps = relationship("ProcessStation", back_populates="process", cascade="all, delete-orphan")

class ProductModel(Base):
    """机型主数据：绑定专属流程 + 强制固件基线。"""

    __tablename__ = "product_models"
    __table_args__ = (Index("idx_pm_process", "process_id"),)

    product_model: Mapped[str] = mapped_column(String(64), primary_key=True)
    process_id: Mapped[str] = mapped_column(
        ForeignKey("processes.process_id", onupdate="CASCADE"), nullable=False, index=True
    )
    target_fw_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # 基线匹配口径：exact 要求完全一致；min 只要求不低于基线（兼容小版本升级）
    fw_match_rule: Mapped[str] = mapped_column(
        String(16), default=FW_RULE_EXACT, server_default=FW_RULE_EXACT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    process = relationship("Process", back_populates="models")

class Station(Base):
    """逻辑工位字典（对应产线物理站位）。"""

    __tablename__ = "stations"

    station_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    station_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=1800, server_default=text("1800"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    clients = relationship("StationClient", back_populates="station")

class ProcessStation(Base):
    """流程工步拓扑：step_order 决定顺序，depends_on 决定防跳站闸门。"""

    __tablename__ = "process_stations"

    process_id: Mapped[str] = mapped_column(
        ForeignKey("processes.process_id", ondelete="CASCADE"), primary_key=True
    )
    station_id: Mapped[str] = mapped_column(
        ForeignKey("stations.station_id"), primary_key=True, index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    depends_on: Mapped[List[str]] = _array_column()

    process = relationship("Process", back_populates="steps")
    station = relationship("Station")

class StationItem(Base):
    """工位测试项：用例ID静态清单（防漏测依据）。

    ORM 属性名为 `case_id`；物理列名沿用 DDL 中的 `nodeid`，故不改动表结构。
    """

    __tablename__ = "station_items"
    __table_args__ = (
        UniqueConstraint("process_id", "station_id", "nodeid", name="uq_station_items"),
        Index("idx_station_items_check", "process_id", "station_id", "is_active"),
    )

    item_id: Mapped[int] = _pk_column()
    process_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    station_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column("nodeid", String(256), nullable=False)
    item_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))

# =====================================================================
class StationClient(Base):
    """物理测试机台档案：一台工控机绑定一个逻辑工位。"""

    __tablename__ = "station_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[Optional[str]] = mapped_column(String(128))
    bound_stations: Mapped[list] = _array_column()
    # 当前操作工位（运行态）：进站解析成功后回写，供心跳超时查询 /
    station_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("stations.station_id"), nullable=True, index=True
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    # 上位机程序版本：现场"同机型结果不可比"的常见根因是版本漂移，留档便于排查
    app_version: Mapped[Optional[str]] = mapped_column(String(50))
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    station = relationship("Station", back_populates="clients")

# =====================================================================
class ProductStatus(Base):
    """在制品状态机：sn 主键，首工位 *IDN? 直读后动态建档。

    并发锁见下方「租约锁」字段组：持锁 = current_status == TESTING 且 lock_token 有效。
    失联与硬超时的判定口径见 gate.is_lock_lost() 与 gate.sweep_orphan_locks()。
    """

    __tablename__ = "product_status"
    __table_args__ = _table_args(
        Index("idx_ps_status", "current_status"),
        Index("idx_ps_model", "product_model"),
        _gin_index("idx_ps_passed", "passed_stations"),
        CheckConstraint(
            "current_status IN ('IDLE','TESTING','LOCKED','SCRAPPED')",
            name="chk_ps_status",
        ),
    )

    sn: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_model: Mapped[str] = mapped_column(
        ForeignKey("product_models.product_model"), nullable=False, index=True
    )
    current_fw_version: Mapped[str] = mapped_column(String(32), nullable=False)
    current_status: Mapped[str] = mapped_column(
        String(16), default=STATUS_IDLE, server_default=STATUS_IDLE
    )
    passed_stations: Mapped[List[str]] = _array_column()
    is_completed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    current_client: Mapped[Optional[str]] = mapped_column(String(64))
    testing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # ---- 租约锁（v1.0：崩溃续测与快速接管）----
    # lock_token       fencing 凭证：每次进站/接管生成新值，旧持锁方写入被拒
    # lock_acquired_at 本次持锁开始时间（心跳不篡改）→ 硬超时判定
    # lock_last_seen_at 最后心跳时间 → 失联判定（默认 120s 即可接管）
    lock_token: Mapped[Optional[str]] = mapped_column(String(64))
    lock_acquired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lock_last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lock_heartbeat_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    locked_reason: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

class TestRecord(Base):
    """测试记录底账：一次出站一条，作为上传闭环的落库凭据（ACK 来源）。"""

    __tablename__ = "test_records"
    __table_args__ = (
        Index("idx_records_sn_station", "sn", "station_id", "created_at"),
        Index("idx_records_created", "created_at"),
        CheckConstraint("overall_result IN ('PASS','FAIL')", name="chk_tr_result"),
    )

    record_id: Mapped[int] = _pk_column()
    sn: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    station_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    overall_result: Mapped[str] = mapped_column(String(16), nullable=False)
    executed_items: Mapped[dict] = mapped_column(JsonbType, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_valid: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class RepairRecord(Base):
    """维修处置与回滚履历：RETEST / ROLLBACK / RESET / SCRAP。"""

    __tablename__ = "repair_records"
    __table_args__ = (
        Index("idx_repair_sn", "sn"),
        Index("idx_repair_created", "created_at"),
        CheckConstraint(
            "repair_action IN ('RETEST','ROLLBACK','RESET','SCRAP')",
            name="chk_repair_action",
        ),
    )

    repair_id: Mapped[int] = _pk_column()
    sn: Mapped[str] = mapped_column(
        ForeignKey("product_status.sn"), nullable=False, index=True
    )
    repair_action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_station: Mapped[Optional[str]] = mapped_column(String(64))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    technician_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class TestSession(Base):
    """测试会话：一次「进站 → 出站」的尝试，承载续测断点。

    崩溃续测的核心载体：
        - 上位机每完成若干用例即增量上报 checkpoint（按 case_id 去重覆盖，幂等）
        - 崩溃重启后进站携带 resume_session_id，attempt 累加并换发新 lock_token
        - 出站时服务端用 checkpoint 补齐本次未提交的用例，避免漏测误判
    """

    __tablename__ = "test_sessions"
    __table_args__ = (
        Index("idx_session_sn", "sn"),
        Index("idx_session_status", "status"),
        Index("idx_session_client", "client_id"),
        Index("idx_session_started", "started_at"),
        CheckConstraint(
            "status IN ('RUNNING','COMPLETED','ABORTED','EXPIRED','TAKEN_OVER')",
            name="chk_session_status",
        ),
    )

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sn: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    station_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lock_token: Mapped[Optional[str]] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(16), default=SESSION_RUNNING, server_default=SESSION_RUNNING)
    checkpoint: Mapped[dict] = _json_column()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[Optional[str]] = mapped_column(Text)
    ended_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

# =====================================================================
REPORT_JOB_INVALID = "invalid"  # 维修处置破坏完工态后，原报告任务作废（存储已清，待重新完工再生成）
class ReportJob(Base):
    """出厂报告任务：一台盖章完成的 SN 一次全量生成（数据报告/校准报告/证书）。

    产物清单落 artifacts JSONB（每元素：type/filename/object_key/mes/pdf/
    pdf_status/pdf_object_key），使「生成」与「转 PDF」两阶段可独立重试，
    并支持外部 Worker（WPS COM）经 API 认领转换。生成失败不影响测试主流程。
    """

    __tablename__ = "report_jobs"
    __table_args__ = (
        Index("idx_rj_sn", "sn"),
        Index("idx_rj_status", "status"),
        Index("idx_rj_created", "created_at"),
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sn: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    rule: Mapped[Optional[str]] = mapped_column(String(64))  # 报告规则（rule 名）
    auto: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    artifacts: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    failed_items: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    mes_status: Mapped[str] = mapped_column(
        String(16), default="none", server_default=text("'none'")
    )
    mes_message: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ReportRule(Base):
    """出厂报告规则：绑定确定型号清单 + 上传式插件（受信任代码，仅 admin 维护）。

    一条规则 = 一个脚本（generate(api)）+ 多个模板，服务确定型号清单内的全部型号；
    型号派生（通道/带宽/AFG 等）全部在脚本内完成，服务器不解析型号字符串。
    版本来源由脚本自定（api.latest），上传同名覆盖、保存即生效。
    """

    __tablename__ = "report_rules"

    rule: Mapped[str] = mapped_column(String(64), primary_key=True)
    models: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    mes_url: Mapped[Optional[str]] = mapped_column(String(256))
    auto_trigger: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    script: Mapped[Optional[str]] = mapped_column(Text)
    script_name: Mapped[Optional[str]] = mapped_column(String(256))  # 上传时的原始文件名（MinIO 归档同名）
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReportTemplate(Base):
    """报告规则 Excel 模板登记：文件本体存 MinIO（未配置时存本地模板目录）。"""

    __tablename__ = "report_templates"

    rule: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(256), primary_key=True)
    object_key: Mapped[Optional[str]] = mapped_column(String(512))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportStandard(Base):
    """标准器台账（按报告规则隔离）：证书回填与「标准器校准日期不得早于被测件」校验的数据源。"""

    __tablename__ = "report_standards"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    rule: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    manufacturer: Mapped[str] = mapped_column(String(64), nullable=False)
    pc_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    sn: Mapped[str] = mapped_column(String(128), nullable=False)
    cal_date: Mapped[Optional[datetime]] = mapped_column(Date)

