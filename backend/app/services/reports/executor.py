"""插件脚本执行器：注入只读 api，在受控线程中运行 generate() 并校验产物。

脚本 = 受信任代码（manager/admin 上传）；取数走注入 api：
api.query 是「分区取最新 PASS」的便利封装，api.sql 支持任意参数化只读查询，
api.tables/describe 供插件自助探查 schema；连接强制只读且带超时/行数上限。
脚本只负责产出 Excel，MinIO / PDF / MES 管线留在引擎。
"""

import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from ...config import settings
from ...logging import get_logger
from . import extractor

logger = get_logger("report.executor")

class ScriptApi:
    """注入给插件 generate() 的服务端接口面（契约见 插件开发规则.md）。"""

    def __init__(self, rule, model: str, sn: str, work_dir: Path):
        self._rule = rule
        self.model = model
        self.sn = sn
        self.work_dir = work_dir
        self.standards = []
        self.logs: list = []
        self._query_ms = 0.0  # 工厂库取数累计耗时（性能观测）
        self._query_count = 0

    def log(self, msg) -> None:
        self.logs.append(str(msg))
        logger.info("插件[%s %s]: %s", self._rule.rule, self.sn, msg)

    def query(self, table: str, partition: list, select: list, filters: dict) -> list:
        """工厂库取数：ROW_NUMBER 分区取 date 最新；PASS 条件由 filters 声明（非内置）；
        该连接强制只读，缺表返回 []。filters 仅支持等值。"""
        t0 = time.perf_counter()
        rows = extractor.fetch_newest_pass(self.model, table, self.sn, select, partition, filters)
        self._query_ms += (time.perf_counter() - t0) * 1000
        self._query_count += 1
        return rows

    def latest(self, table: str, column: str) -> str:
        t0 = time.perf_counter()
        val = extractor.fetch_latest(self.model, table, self.sn, column)
        self._query_ms += (time.perf_counter() - t0) * 1000
        self._query_count += 1
        return val

    def template(self, name: str) -> Path:
        return self._rule.template_path(name)

    def sql(self, query: str, params: Optional[dict] = None) -> list:
        """任意只读查询：参数化 SQL，固定查当前型号分库（库名 = api.model）。

        资源上限由 REPORT_SQL_TIMEOUT_MS / REPORT_SQL_MAX_ROWS 约束；
        写操作由连接级只读事务在 PG 层拒绝。
        """
        t0 = time.perf_counter()
        rows = extractor.fetch_sql(
            self.model, query, params,
            timeout_ms=settings.REPORT_SQL_TIMEOUT_MS,
            max_rows=settings.REPORT_SQL_MAX_ROWS,
        )
        self._account(t0, "sql", query, len(rows))
        return rows

    def tables(self) -> list:
        """当前型号库 public schema 下的表/视图清单（自助探查 schema）。"""
        t0 = time.perf_counter()
        rows = extractor.list_tables(self.model, timeout_ms=settings.REPORT_SQL_TIMEOUT_MS)
        self._account(t0, "tables", "information_schema.tables", len(rows))
        return rows

    def describe(self, table: str) -> list:
        """表/视图的列清单：column_name / data_type / is_nullable / ordinal_position。"""
        t0 = time.perf_counter()
        rows = extractor.describe_table(
            self.model, table, timeout_ms=settings.REPORT_SQL_TIMEOUT_MS
        )
        self._account(t0, "describe", f"describe {table}", len(rows))
        return rows

    def _account(self, t0: float, kind: str, statement: str, row_count: int) -> None:
        """统一观测：SQL 摘要只写服务端日志（api.log 不落库，这里也不落 report_jobs）。"""
        ms = (time.perf_counter() - t0) * 1000
        self._query_ms += ms
        self._query_count += 1
        summary = " ".join(str(statement or "").split())[:200]
        logger.info(
            "插件SQL[%s %s] %s: rows=%d %.1fms %s",
            getattr(self._rule, "rule", "?"), self.sn, kind, row_count, ms, summary,
        )

def _validate_artifacts(raw, work_dir: Path) -> list:
    """校验插件返回的产物清单，规范化为引擎 artifact dict。"""
    if not isinstance(raw, list):
        raise ValueError(f"generate() must return a list, got {type(raw).__name__}")
    artifacts = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"artifacts[{i}] is not a dict")
        path = Path(item.get("path") or (work_dir / item.get("filename", "")))
        if not str(path).startswith(str(work_dir)) or not path.exists():
            raise ValueError(f"artifacts[{i}].path missing or outside work_dir: {path}")
        artifacts.append({
            "type": str(item.get("type") or f"report_{i}"),
            "filename": path.name,
            "local_path": str(path),
            "pdf": bool(item.get("pdf")),
            "mes": bool(item.get("mes")),
            "mes_field": item.get("mes_field"),
            "pdf_status": "none" if not item.get("pdf") else "pending",
            "pdf_object_key": None,
            "failed": bool(item.get("failed_items")),
            "failed_items": list(item.get("failed_items") or []),
        })
    return artifacts

def run_generate(rule, model: str, sn: str, work_dir: Path) -> tuple:
    """执行插件 generate()，返回 (artifacts, failed_items, logs)。

    在隔离线程中带超时运行：插件崩溃/超时只判本任务失败，不影响服务。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    api = ScriptApi(rule, model, sn, work_dir)
    # 版本来源由脚本自定（api.latest），服务器不注入任何机型相关值
    api.standards = rule.load_standards()

    result: dict = {}

    def target():
        try:
            result["raw"] = rule.module.generate(api)
        except Exception:
            result["error"] = traceback.format_exc()

    thread = threading.Thread(target=target, name=f"report-plugin-{sn}", daemon=True)
    thread.start()
    thread.join(settings.REPORT_SCRIPT_TIMEOUT_SEC)
    if thread.is_alive():
        raise TimeoutError(f"plugin generate() timeout after {settings.REPORT_SCRIPT_TIMEOUT_SEC}s")
    if "error" in result:
        raise RuntimeError(f"plugin raised:\n{result['error']}")

    artifacts = _validate_artifacts(result.get("raw"), work_dir)
    logger.info(
        "生成耗时[%s %s]: 工厂库取数 %d 次 / %.1fs",
        rule.rule, sn, api._query_count, api._query_ms / 1000,
    )
    failed_items = []
    for art in artifacts:
        failed_items.extend(f"{art['type']}: {m}" for m in art.pop("failed_items", []))
    return artifacts, failed_items, api.logs
