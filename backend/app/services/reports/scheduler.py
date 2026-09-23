"""后台调度线程：扫描盖章完成的产品自动入队 + 限流执行 + 过期清理。

完全复用 sweeper 的常驻线程模式（lifespan 启停），
不触碰出站事务 —— 报告生成与测试主流程彻底解耦。
"""

import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

from ... import models
from ...config import settings
from ...database import SessionLocal
from ...logging import get_logger
from ..timeutil import utcnow
from . import engine, registry, store

logger = get_logger("report.scheduler")

CLEANUP_INTERVAL_SEC = 3600  # 过期清理执行间隔
_stop = threading.Event()
_wake = threading.Event()  # 入队/重试时立即唤醒调度循环，不必等满扫描周期
_thread: threading.Thread = None
_active: set = set()
_active_lock = threading.Lock()
_last_cleanup = 0.0

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
            _maybe_cleanup()
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
        existing = {row[0] for row in db.query(models.ReportJob.sn).all()}
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

def _run_one(job_id: str) -> None:
    try:
        engine.run_job(job_id)
    finally:
        with _active_lock:
            _active.discard(job_id)

def _maybe_cleanup() -> None:
    """按 CLEANUP_INTERVAL_SEC 节流执行过期清理；失败不阻断调度循环。"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL_SEC:
        return
    _last_cleanup = now
    try:
        cleanup_expired()
    except Exception as exc:
        logger.exception("报告过期清理失败：%s", exc)

def cleanup_expired() -> None:
    """清理过期任务：先删文件（MinIO 对象 + 本地工作目录），全部删净后再清 DB 记录。

    顺序保证：文件先删、记录后删——任何文件删除失败的任务保留 DB 记录，下一轮
    清理周期重试，避免出现无索引指向的孤儿文件。MinIO 未启用时本地文件即归档，
    不动磁盘（仅清 DB 记录）。
    """
    if settings.REPORT_RETENTION_DAYS <= 0:
        return
    deadline = utcnow() - timedelta(days=settings.REPORT_RETENTION_DAYS)
    root = Path(settings.REPORT_WORK_DIR)
    use_store = store.enabled()
    db = SessionLocal()
    try:
        expired = (
            db.query(models.ReportJob)
            .filter(
                models.ReportJob.created_at < deadline,
                models.ReportJob.status.in_(("success", "failed", "partial")),
            )
            .all()
        )
        stale = []
        for job in expired:
            if use_store:
                ok = True
                for art in (job.artifacts or []):
                    for key in ("object_key", "pdf_object_key"):
                        obj = art.get(key)
                        if obj:
                            try:
                                store.remove(obj)
                            except Exception as exc:
                                logger.warning("清理 MinIO 对象失败（本轮保留该任务记录）：%s %s", obj, exc)
                                ok = False
                _rmtree_under(root, engine.work_dir(job.model, job.sn))
                if not ok:
                    continue
            stale.append(job)
        for job in stale:
            db.delete(job)
        db.commit()
    finally:
        db.close()
    if use_store:
        _remove_orphan_workdirs(root)

def _remove_orphan_workdirs(root: Path) -> None:
    """删除 REPORT_WORK_DIR 下没有被任何任务记录引用的 <型号>/<SN> 目录（历史遗留）。"""
    db = SessionLocal()
    try:
        referenced = set(db.query(models.ReportJob.model, models.ReportJob.sn).all())
    finally:
        db.close()
    if not root.exists():
        return
    for model_dir in root.iterdir():
        if not model_dir.is_dir():
            continue
        for sn_dir in model_dir.iterdir():
            if sn_dir.is_dir() and (model_dir.name, sn_dir.name) not in referenced:
                _rmtree_under(root, sn_dir)
        try:
            model_dir.rmdir()  # 型号目录清空后移除，非空则静默保留
        except OSError:
            pass

def _rmtree_under(root: Path, path: Path) -> None:
    """删除 root 之下的一层 <型号>/<SN> 目录；越界或不存在时静默跳过（防御 DB 脏数据）。"""
    try:
        resolved = Path(path).resolve()
        if resolved != Path(root).resolve() and str(resolved).startswith(str(Path(root).resolve())):
            shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        pass
