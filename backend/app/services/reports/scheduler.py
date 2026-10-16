"""后台调度线程：扫描盖章完成的产品自动入队 + 限流执行。

完全复用 sweeper 的常驻线程模式（lifespan 启停），
不触碰出站事务 —— 报告生成与测试主流程彻底解耦。
"""

import threading
from pathlib import Path

from ... import models
from ...config import settings
from ...database import SessionLocal
from ...logging import get_logger
from ..timeutil import utcnow
from . import engine, registry

logger = get_logger("report.scheduler")

_stop = threading.Event()
_wake = threading.Event()  # 入队/重试时立即唤醒调度循环，不必等满扫描周期
_thread: threading.Thread = None
_active: set = set()
_active_lock = threading.Lock()

def kick() -> None:
    """唤醒调度循环（立即执行一轮入队扫描+派发）。engine.enqueue / retry 调用。"""
    _wake.set()

def enabled() -> bool:
    """仅当工厂测试库已配置且存在产品族时启动（缺任一条件则报告功能整体关闭）。

    查询异常（如启动时序问题）按「不可用」处理，绝不阻塞应用启动。"""
    from . import extractor

    if not extractor.configured():
        return False
    try:
        return bool(registry.load_rules())
    except Exception as exc:
        logger.warning("报告规则检查失败，调度器保持关闭：%s", exc)
        return False

def start() -> None:
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="report-scheduler", daemon=True)
    _thread.start()
    logger.info("报告调度器已启动（scan=%ss, concurrency=%s, pdf=%s）",
                settings.REPORT_SCAN_INTERVAL_SEC, settings.REPORT_MAX_CONCURRENCY, settings.REPORT_PDF_MODE)

def stop() -> None:
    _stop.set()

def _loop() -> None:
    while not _stop.is_set():
        # 被 kick() 唤醒（手动入队/重试）或到扫描周期，二者取先
        _wake.wait(timeout=settings.REPORT_SCAN_INTERVAL_SEC)
        _wake.clear()
        if _stop.is_set():
            break
        try:
            _enqueue_completed()
            _drain_queue()
            _reclaim_stale_running()
        except Exception as exc:
            logger.exception("调度循环异常：%s", exc)

def _enqueue_completed() -> None:
    """把「全部盖章完成且尚无报告任务」的产品自动入队（无产品族跳过）。"""
    db = SessionLocal()
    try:
        products = (
            db.query(models.ProductStatus)
            .filter(models.ProductStatus.is_completed.is_(True))
            .order_by(models.ProductStatus.updated_at.desc())
            .limit(settings.REPORT_SCAN_BATCH * 4)
            .all()
        )
        existing = {
            row[0]
            for row in db.query(models.ReportJob.sn)
            .filter(models.ReportJob.status != models.REPORT_JOB_INVALID)
            .all()
        }
        created = 0
        for product in products:
            if created >= settings.REPORT_SCAN_BATCH:
                break
            if product.sn in existing:
                continue
            rule = registry.find_rule(product.product_model)
            if rule is None or not rule.auto_trigger:
                continue
            engine.enqueue(db, product.sn, product.product_model, auto=True)
            existing.add(product.sn)
            created += 1
        if created:
            logger.info("自动入队 %s 个报告任务", created)
    finally:
        db.close()

def _drain_queue() -> None:
    """按并发上限取 pending 任务派发到工作线程。"""
    with _active_lock:
        capacity = settings.REPORT_MAX_CONCURRENCY - len(_active)
    if capacity <= 0:
        return
    db = SessionLocal()
    try:
        jobs = (
            db.query(models.ReportJob)
            .filter(models.ReportJob.status == "pending")
            .order_by(models.ReportJob.created_at.asc())
            .limit(capacity)
            .all()
        )
        for job in jobs:
            job.status = "running"
            job.started_at = utcnow()
            db.commit()
            with _active_lock:
                _active.add(job.job_id)
            threading.Thread(target=_run_one, args=(job.job_id,), daemon=True).start()
    finally:
        db.close()

def _reclaim_stale_running() -> None:
    """看门狗：运行超时的任务判失败。

    running 态本应被插件超时（REPORT_SCRIPT_TIMEOUT_SEC）与本地执行路径约束在
    几分钟内；超过 REPORT_JOB_TIMEOUT_SEC 仍是 running 的一定是后端进程中途
    崩溃/重启遗留的孤儿（重启后没有任何机制会再次派发它们）。判失败并清理
    活跃集合，可从页面手动重试。<=0 关闭看门狗。
    """
    timeout = settings.REPORT_JOB_TIMEOUT_SEC
    if timeout <= 0:
        return
    deadline = utcnow() - timedelta(seconds=timeout)
    db = SessionLocal()
    try:
        stale = (
            db.query(models.ReportJob)
            .filter(
                models.ReportJob.status == "running",
                models.ReportJob.started_at < deadline,
            )
            .all()
        )
        if not stale:
            return
        stale_ids = {job.job_id for job in stale}
        for job in stale:
            job.status = "failed"
            job.error = f"运行超时（>{timeout}s，通常因服务重启中断）；可手动重试"
            logger.warning("报告任务运行超时判失败：%s %s %s", job.job_id, job.model, job.sn)
        db.commit()
        with _active_lock:
            _active.difference_update(stale_ids)
    finally:
        db.close()

def _run_one(job_id: str) -> None:
    try:
        engine.run_job(job_id)
    finally:
        with _active_lock:
            _active.discard(job_id)
