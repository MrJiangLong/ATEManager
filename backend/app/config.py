"""集中式配置：环境变量 → Settings 单例。

加载顺序（先加载者生效）：仓库根 .env → backend/.env → 默认值。
"""

import os
from datetime import timedelta, timezone, tzinfo
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ROOT_DIR = _BACKEND_DIR.parent

for _env_path in (_ROOT_DIR / ".env", _BACKEND_DIR / ".env"):
    if _env_path.exists():
        load_dotenv(_env_path, override=False)

def _as_bool(value, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")

def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

def _tz_info() -> tzinfo:
    """统计日界所用时区：优先 IANA 名称，缺 tzdata（Windows）时回退固定偏移。

    看板的"今日良率""日趋势"按自然日切分，必须落在用户所在时区，
    否则 UTC+8 的机器会把本地 08:00 之前的数据算到"昨天"。
    """
    name = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=_as_int(os.getenv("APP_TZ_OFFSET_HOURS"), 8)))

class Settings:
    # ---- 应用 ----
    APP_NAME: str = "ATE Manager API"
    APP_VERSION: str = "2.0.0"
    APP_DEBUG: bool = _as_bool(os.getenv("APP_DEBUG"), True)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG" if APP_DEBUG else "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "logs/app.log")

    # ---- 数据库（PostgreSQL 生产 / SQLite 单测与本地开发）----
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+pg8000://postgres:postgres@localhost:5432/ate_manager",
    )

    # ---- Web 管理端鉴权 ----
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-me-in-production")
    JWT_EXPIRE_HOURS: int = _as_int(os.getenv("JWT_EXPIRE_HOURS"), 168)
    DEFAULT_ADMIN_USERNAME: str = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    DEFAULT_ADMIN_PASSWORD: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
    DEFAULT_ADMIN_NAME: str = os.getenv("DEFAULT_ADMIN_NAME", "系统管理员")

    # ---- 通道一：上位机 X-API-Key ----
    V1_API_KEY: str = os.getenv("V1_API_KEY", "")

    # ---- CORS ----
    CORS_ORIGINS: list = [
        o.strip()
        for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
        if o.strip()
    ]

    # ---- 防呆规则 ----
    FAIL_LIMIT: int = _as_int(os.getenv("FAIL_LIMIT"), 3)
    ENFORCE_FW: bool = _as_bool(os.getenv("ENFORCE_FW"), True)
    # 进站时是否强制比对用例ID清单（防漏测前置校验）
    ENFORCE_CASE_IDS: bool = _as_bool(os.getenv("ENFORCE_CASE_IDS"), True)
    CLIENT_ONLINE_WINDOW_SECONDS: int = _as_int(os.getenv("CLIENT_ONLINE_WINDOW_SECONDS"), 90)

    # ---- 租约锁：崩溃续测与快速接管 ----
    # 失联宽限：超过该时长未心跳即判定机台失联，锁可被任意同工位机台接管
    LOCK_HEARTBEAT_GRACE_SEC: int = _as_int(os.getenv("LOCK_HEARTBEAT_GRACE_SEC"), 120)
    LOCK_HEARTBEAT_INTERVAL_SEC: int = _as_int(os.getenv("LOCK_HEARTBEAT_INTERVAL_SEC"), 30)
    # 严格 fencing：请求未携带 lock_token 时是否拒绝（True = 老上位机必须升级）。
    # 无论该开关如何，**token 不匹配一律拒绝**，保证锁被接管后旧持锁方无法脏写。
    STRICT_LOCK_TOKEN: bool = _as_bool(os.getenv("STRICT_LOCK_TOKEN"), False)
    SWEEPER_INTERVAL_SEC: int = _as_int(os.getenv("SWEEPER_INTERVAL_SEC"), 30)
    # 否则种子里的失联锁会在 30s 内被自动回收，页面上看不到"失联·可接管"状态
    SWEEPER_ENABLED: bool = _as_bool(os.getenv("SWEEPER_ENABLED"), True)
    LOST_LOCK_FAIL_THRESHOLD: int = _as_int(os.getenv("LOST_LOCK_FAIL_THRESHOLD"), 3)
    MERGE_CHECKPOINT_ON_CHECKOUT: bool = _as_bool(os.getenv("MERGE_CHECKPOINT_ON_CHECKOUT"), True)
    METRICS_WINDOW_DAYS: int = _as_int(os.getenv("METRICS_WINDOW_DAYS"), 14)
    APP_TIMEZONE: str = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
    APP_TZ_OFFSET_HOURS: int = _as_int(os.getenv("APP_TZ_OFFSET_HOURS"), 8)
    TZ_INFO: tzinfo = _tz_info()

    # ---- 出厂报告引擎 ----
    # 生成过程工作目录（xlsx 先落盘再传 MinIO；MinIO 未配置时也作为归档目录）
    REPORT_WORK_DIR: str = os.getenv("REPORT_WORK_DIR", "reports")
    REPORT_MAX_CONCURRENCY: int = _as_int(os.getenv("REPORT_MAX_CONCURRENCY"), 2)
    # 盖章完成产品扫描入队 + 调度间隔
    REPORT_SCAN_INTERVAL_SEC: int = _as_int(os.getenv("REPORT_SCAN_INTERVAL_SEC"), 60)
    REPORT_SCAN_BATCH: int = _as_int(os.getenv("REPORT_SCAN_BATCH"), 20)
    REPORT_RETENTION_DAYS: int = _as_int(os.getenv("REPORT_RETENTION_DAYS"), 90)
    # PDF 转换：off = 仅 Excel；libreoffice = 服务端 headless；external = 外部 Worker（WPS COM）经 API 领取
    REPORT_PDF_MODE: str = os.getenv("REPORT_PDF_MODE", "off")
    SOFFICE_CMD: str = os.getenv("SOFFICE_CMD", "soffice")
    # MES（各产品族统一接口：multipart sn + calibrationReport + calibrationCertificate）
    REPORT_MES_URL: str = os.getenv(
        "REPORT_MES_URL", "http://mes.uni-t.com.cn:7775/MES/api/upload/tek/calibration/data"
    )
    # MinIO 对象存储（报告成品归档与下载；模板登记同样复用此桶）
    MINIO_ENDPOINT: str = os.getenv("MINIO_ENDPOINT", "")
    MINIO_ACCESS_KEY: str = os.getenv("MINIO_ACCESS_KEY", "")
    MINIO_SECRET_KEY: str = os.getenv("MINIO_SECRET_KEY", "")
    MINIO_BUCKET: str = os.getenv("MINIO_BUCKET", "share")
    MINIO_SECURE: bool = _as_bool(os.getenv("MINIO_SECURE"), False)
    MINIO_REPORT_PREFIX: str = os.getenv("MINIO_REPORT_PREFIX", "reports")
    MINIO_TEMPLATE_PREFIX: str = os.getenv("MINIO_TEMPLATE_PREFIX", "report-templates")
    MINIO_SCRIPT_PREFIX: str = os.getenv("MINIO_SCRIPT_PREFIX", "report-scripts")
    # 上传式插件：模板物化缓存目录 + 脚本执行超时
    REPORT_TEMPLATE_DIR: str = os.getenv("REPORT_TEMPLATE_DIR", "report_templates")
    REPORT_SCRIPT_TIMEOUT_SEC: int = _as_int(os.getenv("REPORT_SCRIPT_TIMEOUT_SEC"), 300)
    # 工厂测试库（各型号分库同一台 PG，库名 = 型号；报告引擎只读）
    FACTORY_DB_HOST: str = os.getenv("FACTORY_DB_HOST", "")
    FACTORY_DB_PORT: int = _as_int(os.getenv("FACTORY_DB_PORT"), 5432)
    FACTORY_DB_USER: str = os.getenv("FACTORY_DB_USER", "")
    FACTORY_DB_PASSWORD: str = os.getenv("FACTORY_DB_PASSWORD", "")

settings = Settings()

# 方言判定：数组列类型与行锁语法依赖此标志
IS_SQLITE: bool = "sqlite" in settings.DATABASE_URL.lower()

