"""通道二：出厂报告生成（/api/admin/report-jobs）。

查询对登录用户开放；生成/重试/重传 MES 需 operator 及以上；
PDF Worker（外部 WPS 转换机）走 X-API-Key（api_caller）认领与回写。
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import bad_request, conflict_error, not_found
from ..logging import get_logger
from ..security import api_caller, current_user, require_admin, require_operator
from ..services import reports
from ..services.reports import engine as report_engine
from ..services.reports import pdf as pdf_mod, registry, store
from ..services.reports import scheduler as report_scheduler
from ..services.timeutil import local_day_start_of_date, sql_time, utcnow

logger = get_logger("api.reports")

router = APIRouter(prefix="/api/admin/report-jobs", tags=["admin-报告生成"])
rules_router = APIRouter(prefix="/api/admin/report-rules", tags=["admin-报告规则"])
standards_router = APIRouter(prefix="/api/admin/report-standards", tags=["admin-标准器台账"])

# PDF 认领租约：Worker 领取后超过此时长未回传结果即视为失联，认领作废重新分发
PDF_CLAIM_LEASE_SEC = 600

def _parse_date(value: str, field: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise bad_request(
            "invalid_date_format", f"invalid_date_format: {field} should be YYYY-MM-DD, got {value}"
        ) from None

def _can_download(user) -> bool:
    return True  # 登录即可下载；生成/重试另有 require_operator 收口

@router.get("", response_model=schemas.ReportJobPageOut, summary="报告任务清单(分页)")
def list_jobs(
    sn: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.ReportJob)
    if sn:
        query = query.filter(models.ReportJob.sn.ilike(f"%{sn}%"))
    if model:
        query = query.filter(models.ReportJob.model == model)
    if status:
        query = query.filter(models.ReportJob.status == status)
    total = query.count()
    rows = (
        query.order_by(models.ReportJob.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return schemas.ReportJobPageOut(
        total=total, page=page, page_size=page_size,
        items=[schemas.ReportJobOut.model_validate(r) for r in rows],
    )

@router.get("/models", response_model=list, summary="可选型号(已有盖章完成产品的型号)")
def list_models(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = (
        db.query(models.ProductStatus.product_model)
        .filter(models.ProductStatus.is_completed.is_(True))
        .distinct()
        .all()
    )
    return sorted(r[0] for r in rows)

@router.get("/candidates", response_model=schemas.ReportCandidatePageOut, summary="候选设备(盖章完成)")
def list_candidates(
    model: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.ProductStatus).filter(models.ProductStatus.is_completed.is_(True))
    if model:
        query = query.filter(models.ProductStatus.product_model == model)
    if date_from:
        query = query.filter(
            models.ProductStatus.updated_at
            >= sql_time(local_day_start_of_date(_parse_date(date_from, "date_from")))
        )
    if date_to:
        query = query.filter(
            models.ProductStatus.updated_at
            < sql_time(local_day_start_of_date(_parse_date(date_to, "date_to") + timedelta(days=1)))
        )
    total = query.count()
    rows = (
        query.order_by(models.ProductStatus.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    job_sns = {row[0] for row in db.query(models.ReportJob.sn).all()}
    items = [
        schemas.ReportCandidateOut(
            sn=p.sn,
            product_model=p.product_model,
            current_fw_version=p.current_fw_version,
            completed_at=p.updated_at,
            has_job=p.sn in job_sns,
        )
        for p in rows
    ]
    return schemas.ReportCandidatePageOut(total=total, page=page, page_size=page_size, items=items)

@router.post("/batch", response_model=schemas.ReportBatchCreatedOut, summary="批量生成报告(仅 operator+)")
def batch_create(
    body: schemas.ReportBatchIn,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    created, skipped = 0, []
    existing = {
        row[0]
        for row in db.query(models.ReportJob.sn)
        .filter(models.ReportJob.status != models.REPORT_JOB_INVALID)
        .all()
    }
    for sn in body.sns:
        product = db.get(models.ProductStatus, sn)
        if product is None:
            skipped.append({"sn": sn, "reason": "product_not_found"})
            continue
        if sn in existing:
            skipped.append({"sn": sn, "reason": "job_exists"})
            continue
        if reports.registry.find_rule(product.product_model) is None:
            skipped.append({"sn": sn, "reason": "no_plugin"})
            continue
        report_engine.enqueue(db, sn, product.product_model, auto=False, created_by=user.username)
        existing.add(sn)
        created += 1
    return schemas.ReportBatchCreatedOut(created=created, skipped=skipped)

@router.post("/{job_id}/retry", response_model=schemas.ReportJobOut, summary="重试任务(仅 operator+)")
def retry_job(job_id: str, db: Session = Depends(get_db), user=Depends(require_operator)):
    job = db.get(models.ReportJob, job_id)
    if job is None:
        raise not_found("job_not_found", f"job_not_found: {job_id}")
    job.status = "pending"
    job.artifacts = []
    job.failed_items = []
    job.error = None
    job.mes_status = "none"
    job.mes_message = None
    job.started_at = None
    job.finished_at = None
    db.commit()
    report_scheduler.kick()  # 重试立即派发，不等扫描周期
    return job

@router.post("/{job_id}/resend-mes", response_model=schemas.ReportJobOut, summary="重传 MES(仅 operator+)")
def resend_mes(job_id: str, db: Session = Depends(get_db), user=Depends(require_operator)):
    job = db.get(models.ReportJob, job_id)
    if job is None:
        raise not_found("job_not_found", f"job_not_found: {job_id}")
    if job.status == models.REPORT_JOB_INVALID:
        raise conflict_error(
            "job_invalid",
            f"job_invalid: {job_id} was invalidated by repair disposition; "
            "reports regenerate automatically after re-completion",
        )
    report_engine.upload_mes_if_ready(db, job)
    return job

@router.get("/{job_id}/download/{index}", response_model=schemas.ReportDownloadOut, summary="产物下载(预签名 URL)")
def download(
    job_id: str,
    index: int,
    variant: str = "xlsx",
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    job = db.get(models.ReportJob, job_id)
    if job is None or index < 0 or index >= len(job.artifacts or []):
        raise not_found("job_not_found", f"job_not_found or artifact index invalid: {job_id}#{index}")
    art = job.artifacts[index]
    use_pdf = variant == "pdf"
    key = art.get("pdf_object_key") if use_pdf else art.get("object_key")
    filename = art.get("filename", "report.xlsx")
    if use_pdf:
        filename = filename.rsplit(".", 1)[0] + ".pdf" if "." in filename else filename
    if key and store.enabled():
        return schemas.ReportDownloadOut(filename=filename, url=store.presigned_url(key))
    # MinIO 未配置：直接流式返回本地归档文件
    path = report_engine.local_artifact_path(art, use_pdf, job.model, job.sn)
    if path is None or not path.exists():
        raise not_found("artifact_missing", f"artifact missing: {filename}")
    return FileResponse(path, filename=filename)

@router.get("/pdf-claim", response_model=schemas.ReportPdfClaimOut, summary="外部 PDF Worker 认领一个待转换产物")
def pdf_claim(db: Session = Depends(get_db), caller: str = Depends(api_caller)):
    if pdf_mod.mode() != "external":
        raise bad_request("pdf_mode_off", "REPORT_PDF_MODE is not 'external'")
    jobs = (
        db.query(models.ReportJob)
        .filter(models.ReportJob.status.in_(("running", "success", "partial")))
        .order_by(models.ReportJob.created_at.asc())
        .all()
    )
    now = datetime.utcnow().timestamp()
    for job in jobs:
        for i, art in enumerate(job.artifacts or []):
            status = art.get("pdf_status")
            if status == "claimed":
                # 租约回收：Worker 领取后失联（崩溃/被杀/下载失败/服务端重启打断），
                # 超过租约期仍未回传结果的认领作废，回 pending 重新分发；
                # 无时间戳的是历史遗留（修复前认领的），直接回收
                claimed_at = art.get("claimed_at")
                if claimed_at is None or now - claimed_at > PDF_CLAIM_LEASE_SEC:
                    art["pdf_status"] = "pending"
                    art.pop("claimed_at", None)
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(job, "artifacts")
                    db.commit()
            if art.get("pdf_status") == "pending":
                art["pdf_status"] = "claimed"
                art["claimed_at"] = now
                # 记下本次转换对应的数据源指纹：回传时比对，Excel 被重新生成覆盖则结果作废
                art["claimed_etag"] = store.stat_etag(art["object_key"]) if store.enabled() else None
                from sqlalchemy.orm.attributes import flag_modified

                flag_modified(job, "artifacts")
                db.commit()
                return schemas.ReportPdfClaimOut(
                    job_id=job.job_id,
                    artifact_index=i,
                    download_url=store.presigned_url(art["object_key"]),
                )
    raise not_found("no_pending_pdf", "no pending pdf artifact")

@router.post("/pdf-result", response_model=schemas.ReportJobOut, summary="外部 PDF Worker 回写转换结果")
async def pdf_result(
    job_id: str = Form(...),
    artifact_index: int = Form(...),
    success: bool = Form(...),
    error: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    caller: str = Depends(api_caller),
):
    job = db.get(models.ReportJob, job_id)
    if job is None:
        raise not_found("job_not_found", f"job_not_found: {job_id}")
    artifacts = job.artifacts or []
    if artifact_index < 0 or artifact_index >= len(artifacts):
        raise bad_request("artifact_index_invalid", f"artifact_index invalid: {artifact_index}")
    art = artifacts[artifact_index]
    art.pop("claimed_at", None)  # 租约已结束（成功或失败），清掉时间戳
    # 新鲜度校验：转换期间源 Excel 被重新生成覆盖（ETag 变化）→ 本次结果基于旧数据，
    # 丢弃并立即回 pending 重新转换，避免旧 PDF 盖到新 Excel 上
    claimed_etag = art.pop("claimed_etag", None)
    if success and claimed_etag and store.enabled():
        current_etag = store.stat_etag(art.get("object_key") or "")
        if current_etag and current_etag != claimed_etag:
            art["pdf_status"] = "pending"
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(job, "artifacts")
            db.commit()
            logger.warning(
                "丢弃过期 PDF 转换结果（源 Excel 已更新）：%s#%s", job_id, artifact_index
            )
            return job
    if success and file is not None:
        suffix = ".pdf"
        filename = (art.get("filename") or "report.xlsx").rsplit(".", 1)[0] + suffix
        local = report_engine.work_dir(job.model, job.sn) / filename
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(await file.read())
        art["pdf_status"] = "done"
        if store.enabled():
            key = store.object_key(job.model, job.sn, filename)
            store.upload(local, key)
            art["pdf_object_key"] = key
        art["local_pdf_path"] = str(local)
    else:
        art["pdf_status"] = "failed"
        art["pdf_error"] = error or "worker failed"
    # JSONB 原地修改必须显式标记，否则 commit 不会持久化（同 gate._merge_into_checkpoint）
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(job, "artifacts")
    job.status = "running" if job.status in ("success", "partial") else job.status
    db.commit()
    report_engine.finalize_job(db, job)
    return job


# =====================================================================
# 报告产品族（上传式插件：脚本 = 受信任代码，仅 admin）
# =====================================================================
def _validate_script(source: str, rule: str) -> None:
    """上传校验：UTF-8 解码、语法编译、generate(api) 入口存在。"""
    try:
        code = compile(source, f"{rule}.py", "exec")
    except SyntaxError as exc:
        raise bad_request("script_syntax_error", f"script syntax error: {exc}") from None
    namespace: dict = {}
    exec(code, namespace)  # 编译期检查入口；模块级代码会在注册表加载时再执行
    if not callable(namespace.get("generate")):
        raise bad_request("script_entry_missing", "script must define generate(api)")

def _rule_out(db: Session, row: models.ReportRule) -> schemas.ReportRuleOut:
    templates = [
        t.filename
        for t in db.query(models.ReportTemplate)
        .filter(models.ReportTemplate.rule == row.rule)
        .order_by(models.ReportTemplate.filename)
        .all()
    ]
    return schemas.ReportRuleOut(
        rule=row.rule,
        models=row.models or [],
        mes_url=row.mes_url,
        auto_trigger=row.auto_trigger,
        enabled=row.enabled,
        has_script=bool(row.script),
        script_name=row.script_name,
        templates=templates,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )

@rules_router.get("", response_model=List[schemas.ReportRuleOut], summary="报告规则清单(仅 admin)")
def list_rules(db: Session = Depends(get_db), user: models.User = Depends(require_admin)):
    rows = db.query(models.ReportRule).order_by(models.ReportRule.rule).all()
    return [_rule_out(db, r) for r in rows]

@rules_router.post("", response_model=schemas.ReportRuleOut, status_code=201, summary="新建报告规则(仅 admin)")
def create_rule(
    payload: schemas.ReportRuleIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    if db.get(models.ReportRule, payload.rule) is not None:
        raise bad_request("rule_exists", f"rule_exists: {payload.rule}")
    row = reports.registry.register_rule(
        db,
        rule=payload.rule,
        models_list=payload.models,
        script="",
        mes_url=payload.mes_url,
        auto_trigger=payload.auto_trigger,
        enabled=payload.enabled,
        updated_by=user.username,
    )
    return _rule_out(db, row)

@rules_router.put("/{rule}", response_model=schemas.ReportRuleOut, summary="更新报告规则(仅 admin)")
def update_rule(
    rule: str,
    payload: schemas.ReportRuleIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    row = db.get(models.ReportRule, rule)
    if row is None:
        raise not_found("rule_not_found", f"rule_not_found: {rule}")
    reports.registry.register_rule(
        db,
        rule=rule,
        models_list=payload.models,
        script=row.script or "",
        script_name=row.script_name,
        mes_url=payload.mes_url,
        auto_trigger=payload.auto_trigger,
        enabled=payload.enabled,
        updated_by=user.username,
    )
    return _rule_out(db, db.get(models.ReportRule, rule))

@rules_router.delete("/{rule}", status_code=204, summary="删除报告规则(仅 admin)")
def delete_rule(rule: str, db: Session = Depends(get_db), user: models.User = Depends(require_admin)):
    row = db.get(models.ReportRule, rule)
    if row is None:
        raise not_found("rule_not_found", f"rule_not_found: {rule}")
    # 先收集 MinIO 对象键，删 DB 后再清对象存储（脚本归档 + 模板）
    template_keys = [
        t.object_key for t in db.query(models.ReportTemplate)
        .filter(models.ReportTemplate.rule == rule).all()
        if t.object_key
    ]
    script_name = row.script_name
    db.query(models.ReportTemplate).filter(models.ReportTemplate.rule == rule).delete()
    db.delete(row)
    db.commit()
    if store.enabled():
        if script_name:
            store.remove(f"{registry.SCRIPT_MINIO_PREFIX}/{rule}/{script_name}")
        for key in template_keys:
            store.remove(key)
    reports.registry.reset_cache()

@rules_router.post("/{rule}/script", response_model=schemas.ReportRuleOut, summary="上传插件脚本 .py(仅 admin)")
async def upload_script(
    rule: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    row = db.get(models.ReportRule, rule)
    if row is None:
        raise not_found("rule_not_found", f"rule_not_found: {rule}")
    if not (file.filename or "").endswith(".py"):
        raise bad_request("script_not_python", "only .py files are accepted")
    try:
        source = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        raise bad_request("script_not_utf8", "script must be UTF-8 encoded") from None
    _validate_script(source, rule)

    filename = Path(file.filename or "").name
    old_name = row.script_name
    # 归档镜像到 MinIO（执行源仍以 DB 为准）；换名上传时删除旧归档对象
    if store.enabled():
        if old_name and old_name != filename:
            store.remove(f"{registry.SCRIPT_MINIO_PREFIX}/{rule}/{old_name}")
        store.upload_bytes(
            source.encode("utf-8"), f"{registry.SCRIPT_MINIO_PREFIX}/{rule}/{filename}"
        )
    row.script = source
    row.script_name = filename
    row.updated_by = user.username
    row.updated_at = utcnow()
    db.commit()
    reports.registry.reset_cache()
    return _rule_out(db, row)

@rules_router.post("/{rule}/templates", response_model=schemas.ReportRuleOut, summary="上传 Excel 模板(可多选，仅 admin)")
async def upload_templates(
    rule: str,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    row = db.get(models.ReportRule, rule)
    if row is None:
        raise not_found("rule_not_found", f"rule_not_found: {rule}")
    for file in files:
        if not (file.filename or "").lower().endswith(".xlsx"):
            raise bad_request("template_not_xlsx", f"only .xlsx accepted, got {file.filename}")
        content = await file.read()
        filename = Path(file.filename or "").name  # 防路径穿越
        object_key = None
        if store.enabled():
            object_key = f"{registry.TEMPLATE_MINIO_PREFIX}/{rule}/{filename}"
            store.upload_bytes(content, object_key)
        else:
            local = registry.TEMPLATE_LOCAL_DIR / rule / filename
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(content)
        existing = db.get(models.ReportTemplate, {"rule": rule, "filename": filename})
        if existing is None:
            db.add(models.ReportTemplate(rule=rule, filename=filename, object_key=object_key))
        else:
            existing.object_key = object_key
    row.updated_by = user.username
    row.updated_at = utcnow()
    db.commit()
    reports.registry.reset_cache()
    return _rule_out(db, row)

@rules_router.delete("/{rule}/templates/{filename}", status_code=204, summary="删除模板(仅 admin)")
def delete_template(
    rule: str,
    filename: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    row = db.get(models.ReportTemplate, {"rule": rule, "filename": filename})
    if row is None:
        raise not_found("template_not_found", f"template_not_found: {rule}/{filename}")
    db.delete(row)
    db.commit()
    reports.registry.reset_cache()


# =====================================================================
# 标准器台账（证书回填与校准日期校验的数据源，仅 admin）
# =====================================================================
@standards_router.get("", response_model=List[schemas.ReportStandardOut], summary="标准器台账(按报告规则，仅 admin)")
def list_standards(
    rule: str = Query(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    return (
        db.query(models.ReportStandard)
        .filter(models.ReportStandard.rule == rule)
        .order_by(models.ReportStandard.id)
        .all()
    )

@standards_router.post("", response_model=schemas.ReportStandardOut, status_code=201, summary="新增标准器(仅 admin)")
def create_standard(
    payload: schemas.ReportStandardIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    if db.get(models.ReportRule, payload.rule) is None:
        raise not_found("rule_not_found", f"rule_not_found: {payload.rule}")
    row = models.ReportStandard(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

@standards_router.put("/{std_id}", response_model=schemas.ReportStandardOut, summary="更新标准器(仅 admin)")
def update_standard(
    std_id: int,
    payload: schemas.ReportStandardIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    row = db.get(models.ReportStandard, std_id)
    if row is None:
        raise not_found("standard_not_found", f"standard_not_found: {std_id}")
    if db.get(models.ReportRule, payload.rule) is None:
        raise not_found("rule_not_found", f"rule_not_found: {payload.rule}")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return row

@standards_router.delete("/{std_id}", status_code=204, summary="删除标准器(仅 admin)")
def delete_standard(std_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_admin)):
    row = db.get(models.ReportStandard, std_id)
    if row is None:
        raise not_found("standard_not_found", f"standard_not_found: {std_id}")
    db.delete(row)
    db.commit()
