"""报告生成管线：一个 job = 一台 SN 的全部报告。

流程：加载 job → 匹配报告规则 → 执行上传式插件 generate()（只读取数、产出 Excel）
→ 产物上传 MinIO → PDF（libreoffice 立即转 / external 留待 Worker 认领）
→ MES（依赖的两份 PDF 就绪后上传）。生成失败不影响测试主流程。
"""

import uuid
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from ... import models
from ...config import settings
from ...database import SessionLocal
from ...logging import get_logger
from ..timeutil import utcnow
from . import executor, extractor, mes as mes_mod, pdf as pdf_mod, registry, store

logger = get_logger("report.engine")

def enqueue(db, sn: str, model: str, auto: bool = True, created_by: str = None) -> models.ReportJob:
    rule = registry.find_rule(model)
    job = models.ReportJob(
        job_id=str(uuid.uuid4()), sn=sn, model=model,
        rule=rule.rule if rule else None,
        auto=auto, created_by=created_by,
    )
    db.add(job)
    db.commit()
    from . import scheduler  # 局部导入避免循环依赖；入队后立即唤醒调度线程派发
    scheduler.kick()
    return job

def _normalize_pdf_page_setup(path: Path) -> None:
    """消除 Excel→PDF 分页与排版歧义（多节/单页精准分流策略）。

    设计理念：最小侵入原则——根据报告类型定向修正，避免一刀切全局改动引发连锁副作用。

    解决核心痛点：
    1. 多节报告（带手动分节符，如校准报告）：
       - 分节模式修正：仅关闭 fitToPage 回退纯 scale 模式，消除 WPS 与 MS Excel 对
         分节符解释不一致导致插入空白页或丢失尾节的问题；
       - 严禁修改纸张规格（不设 A4）：保持模板原有的纸张与边距设计不变。A4 比 Letter 窄
         近 6mm，若强行给长表格改 A4 会导致右侧列横向溢出，使原本纵向 4 页被横切翻倍成 8 页。

    2. 单页设计（无手动分节符，如校准证书）：
       - 纸张锁定 A4（高度补足）：后台无头环境易回退美标 Letter 纸（比 A4 矮 17.6mm），
         此处强制锁定 A4，彻底消除高度不足导致最底端签名与审核日期被腰斩截断的问题；
       - 健全 fitToPage 契约：显式配置 fitToWidth=1 且 fitToHeight=1，并强制清空残留的
         固定 scale 缩放比（zoom=None），确保整张单页证书 100% 自动收敛在 1 页纸内。

    仅处理将要转 PDF 的临时产物；规范化失败不阻断主转换管线。
    """
    import openpyxl
    from openpyxl.worksheet.properties import PageSetupProperties

    wb = openpyxl.load_workbook(path)
    changed = False

    for ws in wb.worksheets:
        has_breaks = bool(ws.row_breaks.brk) or bool(ws.col_breaks.brk)
        pr = ws.sheet_properties.pageSetUpPr

        if has_breaks:
            if pr is not None and pr.fitToPage:
                pr.fitToPage = False
                changed = True
        else:
            if str(ws.page_setup.paperSize) != str(ws.PAPERSIZE_A4):
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
                changed = True

            if pr is None:
                ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
                changed = True
            elif not pr.fitToPage:
                pr.fitToPage = True
                changed = True

            if ws.page_setup.fitToWidth != 1 or ws.page_setup.fitToHeight != 1:
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 1
                changed = True

            if ws.page_setup.scale is not None:
                ws.page_setup.scale = None
                changed = True

    if changed:
        wb.save(path)
        logger.info("已规范化打印设置（精准分流模式）：%s", path.name)

def _artifact_pdf_local(art: dict, out_dir: Path, model: str = None, sn: str = None) -> Path:
    """PDF 本地路径：libreoffice 模式下已在本地；external 模式从 MinIO 拉回。"""
    filename = Path(art["filename"]).with_suffix(".pdf").name
    local = out_dir / filename
    if not local.exists() and art.get("pdf_object_key") and store.enabled():
        store.download_to(art["pdf_object_key"], local)
    return local

def work_dir(model: str, sn: str) -> Path:
    return Path(settings.REPORT_WORK_DIR) / model / sn

def local_artifact_path(art: dict, use_pdf: bool, model: str, sn: str):
    """产物本地路径；external 模式的 PDF 在 MinIO 上，先拉回本地。"""
    if use_pdf:
        local = art.get("local_pdf_path")
        if local:
            return Path(local)
        if art.get("pdf_object_key") and store.enabled():
            filename = (art.get("filename") or "report.xlsx").rsplit(".", 1)[0] + ".pdf"
            path = work_dir(model, sn) / filename
            store.download_to(art["pdf_object_key"], path)
            return path
        return None
    local = art.get("local_path")
    return Path(local) if local else None

def finalize_job(db, job: models.ReportJob) -> None:
    """综合渲染/上传/PDF 状态评定终态，并尝试 MES 上传。"""
    arts = job.artifacts or []
    ok_render = [a for a in arts if not a.get("failed")]
    pdf_ready = all(
        a.get("pdf_status") in ("done", "none") for a in arts if a.get("pdf")
    )
    if not ok_render:
        job.status = "failed"
        job.error = "all reports failed"
    elif not pdf_ready:
        job.status = "partial"
    elif len(ok_render) < len(arts):
        job.status = "partial"
    else:
        job.status = "success"
    job.finished_at = utcnow()
    db.commit()
    upload_mes_if_ready(db, job)

def upload_mes_if_ready(db, job: models.ReportJob) -> None:
    """全部声明 MES 的产物 PDF 就绪时一次性上传（multipart 字段名由产物 mes_field 声明）；
    否则标记 skipped。引擎不解释产物类型，只做传输。"""
    arts = job.artifacts or []
    mes_arts = [a for a in arts if a.get("mes")]
    if not mes_arts:
        return
    pending = [a for a in mes_arts if a.get("pdf") and a.get("pdf_status") != "done"]
    if pending:
        job.mes_status = "skipped"
        job.mes_message = "PDF not ready (mode=off or conversion pending/failed)"
        db.commit()
        return
    out_dir = work_dir(job.model, job.sn)
    rule = registry.get_rule(job.rule)
    files = {}
    for art in mes_arts:
        pdf_path = _artifact_pdf_local(art, out_dir, job.model, job.sn)
        if pdf_path is None or not pdf_path.exists():
            job.mes_status = "failed"
            job.mes_message = f"missing pdf file: {art.get('filename')}"
            db.commit()
            return
        files[art.get("mes_field") or art.get("type")] = pdf_path
    ok, msg = mes_mod.upload_mes(files, job.sn, url=rule.mes_url if rule else None)
    job.mes_status = "success" if ok else "failed"
    job.mes_message = msg
    if ok:
        # 通用清理规则（与产品族无关）：凡声明 mes=true 的产物，上传成功即视为
        # MES 是其唯一归档，立即清除 MinIO 对象与本地副本；未声明 MES 的产物
        # （如数据报告）保留在存储里作为 ATEManager 侧归档。失败任务不清理，保重试。
        for art in mes_arts:
            if store.enabled():
                if art.get("object_key"):
                    store.remove(art["object_key"])
                    art["object_key"] = None
                if art.get("pdf_object_key"):
                    store.remove(art["pdf_object_key"])
                    art["pdf_object_key"] = None
            for key in ("local_path", "local_pdf_path"):
                p = art.get(key)
                if p and Path(p).exists():
                    try:
                        Path(p).unlink()
                    except OSError:
                        pass
                art[key] = None
            art["purged"] = True
        flag_modified(job, "artifacts")
    db.commit()

def run_job(job_id: str) -> None:
    """执行一个报告任务（在调度线程池中运行，可阻塞）。"""
    db = SessionLocal()
    try:
        job = db.get(models.ReportJob, job_id)
        if job is None:
            return
        job.status = "running"
        job.started_at = utcnow()
        job.artifacts = []
        job.failed_items = []
        job.error = None
        db.commit()

        rule = registry.get_rule(job.rule) or registry.find_rule(job.model)
        if rule is None:
            job.status = "failed"
            job.error = f"no report rule for model {job.model}"
            db.commit()
            return
        if not extractor.configured():
            job.status = "failed"
            job.error = "FACTORY_DB_* settings not configured"
            db.commit()
            return

        out_dir = Path(settings.REPORT_WORK_DIR) / job.model / job.sn
        out_dir.mkdir(parents=True, exist_ok=True)

        artifacts, failed_items, _logs = executor.run_generate(rule, job.model, job.sn, out_dir)

        # 产物归档到 MinIO（未配置 MinIO 时保留本地文件作为归档）；
        # 转 PDF 的产物先统一打印设置，保证 Worker 端（WPS 或 MS Excel）分页一致
        for art in artifacts:
            if art.get("pdf"):
                try:
                    _normalize_pdf_page_setup(Path(art["local_path"]))
                except Exception as exc:
                    logger.warning("打印设置规范化失败，按原文件继续转换：%s %s", art.get("filename"), exc)
            if store.enabled():
                key = store.object_key(job.model, job.sn, art["filename"])
                store.upload(Path(art["local_path"]), key)
                art["object_key"] = key
            else:
                art["object_key"] = None

        job.artifacts = artifacts
        job.failed_items = failed_items

        # ---- PDF 阶段 ----
        mode = pdf_mod.mode()
        for art in artifacts:
            if not art.get("pdf") or art.get("failed"):
                continue
            if mode == "libreoffice":
                try:
                    pdf_path = pdf_mod.convert_libreoffice(Path(art["local_path"]), out_dir)
                    if store.enabled():
                        key = store.object_key(job.model, job.sn, pdf_path.name)
                        store.upload(pdf_path, key)
                        art["pdf_object_key"] = key
                    art["pdf_status"] = "done"
                except Exception as exc:
                    logger.warning("PDF 转换失败：%s %s %s", job.sn, art["type"], exc)
                    art["pdf_status"] = "failed"
                    art["pdf_error"] = str(exc)
            elif mode == "external":
                art["pdf_status"] = "pending"
            # off：保持 pdf_status=none，仅出 Excel

        # ---- 终态：按渲染/上传/PDF 结果综合评定，并尝试 MES ----
        finalize_job(db, job)
        logger.info("报告任务完成：%s %s status=%s failed_items=%s", job.sn, job.model, job.status, job.failed_items)
    except Exception as exc:
        logger.exception("报告任务异常：%s", job_id)
        try:
            job = db.get(models.ReportJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = utcnow()
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
