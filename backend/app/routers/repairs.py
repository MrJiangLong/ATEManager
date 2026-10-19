"""通道二：维修处置履历（/api/admin/repairs）—— 只读 + 授权处置（单条 + 批量）。"""

import uuid
from datetime import timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..errors import AppError, bad_request, not_found
from ..security import current_user, require_operator
from ..services import apply_repair, undo_repair
from ..services.gate import _check_repair
from ..services.timeutil import date_range_bounds, utcnow

router = APIRouter(prefix="/api/admin/repairs", tags=["admin-维修处置"])

# 批量处置单次上限：够一轮返修批次用，又足以限制误操作的破坏面
REPAIR_BATCH_LIMIT = 50


def _local_day_cutoff(days: int):
    """本地时区"近 N 天"的日界 cutoff（含今天）：1 = 今天零点起，3 = 前天零点起。"""
    local_now = utcnow().astimezone(settings.TZ_INFO)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    return local_start.astimezone(timezone.utc)


@router.get("", response_model=schemas.RepairPageOut, summary="维修处置履历")
def list_repairs(
    sn: Optional[str] = None,
    repair_action: Optional[str] = None,
    technician: Optional[str] = None,
    undone: Optional[bool] = Query(None, description="撤销状态筛选：true=已撤销 / false=有效（缺省=全部）"),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.RepairRecord)
    if sn:
        query = query.filter(models.RepairRecord.sn.ilike(f"%{sn}%"))
    if repair_action:
        query = query.filter(models.RepairRecord.repair_action == repair_action.upper())
    if technician:
        query = query.filter(models.RepairRecord.technician_id.ilike(f"%{technician}%"))
    if undone is True:
        query = query.filter(models.RepairRecord.undone_at.isnot(None))
    elif undone is False:
        query = query.filter(models.RepairRecord.undone_at.is_(None))
    start, end = date_range_bounds(date_from, date_to)
    if start:
        query = query.filter(models.RepairRecord.created_at >= start)
    if end:
        query = query.filter(models.RepairRecord.created_at < end)
    total = query.count()
    rows = (
        query.order_by(models.RepairRecord.repair_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [schemas.RepairOut.model_validate(r) for r in rows]
    return schemas.RepairPageOut(total=total, page=page, page_size=page_size, items=items)


@router.post("", response_model=schemas.RepairOut, status_code=201, summary="登记维修处置")
def create_repair(payload: schemas.RepairIn, db: Session = Depends(get_db), user=Depends(require_operator)):
    """维修处置语义
        RETEST   清除指定工位印章，允许重测
        ROLLBACK 回退到指定工位（清除该工位及其后续所有印章）
        RESET    清空全部印章与失败计数，重新投产
        SCRAP    报废，永久拦截
    """
    _product, record = apply_repair(
        db,
        sn=payload.sn,
        repair_action=payload.repair_action,
        target_station=payload.target_station or None,
        reason=payload.reason or "",
        technician_id=user.username,
        confirm_downstream=payload.confirm_downstream,
    )
    return record


@router.post(
    "/{repair_id}/undo",
    response_model=schemas.RepairOut,
    summary="撤销维修处置（窄窗口：最新一条 + 处置后零后续动作；SCRAP 不可撤）",
)
def undo_repair_api(repair_id: int, db: Session = Depends(get_db), user=Depends(require_operator)):
    """按处置前快照精确还原：印章/失败计数/状态恢复，被作废的台账记录按快照 ID 精确恢复；
    完工件恢复完工态后由完工扫描自动重出报告。四条边界（SCRAP/最新/零后续/未撤销）
    由服务端强制，任一不满足即 409。"""
    return undo_repair(db, repair_id=repair_id, operator_id=user.username)


@router.post(
    "/batch",
    response_model=schemas.RepairBatchOut,
    summary="批量维修处置（dry_run 预演；RESET/SCRAP 禁批量，须单条 + 二次确认）",
)
def batch_repairs(payload: schemas.RepairBatchIn, db: Session = Depends(get_db), user=Depends(require_operator)):
    """同一动作/目标逐条套用一批 SN，逐条返回成功/失败与破坏面。

    - dry_run=true 只跑守卫不落库，回显每件当前状态、将被撤销的印章、将作废的台账记录数
    - 高破坏动作 RESET/SCRAP 不允许批量（400 action_not_batchable），须单条 + 强确认
    - 逐条独立生效：失败项不影响已成功项，全部留痕 repair_records
    """
    action = payload.repair_action
    if action in (models.REPAIR_RESET, models.REPAIR_SCRAP):
        raise bad_request("action_not_batchable", f"action_not_batchable: {action} is single-SN only")
    batch_id = uuid.uuid4().hex if not payload.dry_run else None
    if not payload.items:
        raise bad_request("items_required", "items_required: at least one sn")
    if len(payload.items) > REPAIR_BATCH_LIMIT:
        raise bad_request("batch_too_large", f"batch_too_large: max {REPAIR_BATCH_LIMIT} items per batch")

    items: List[schemas.RepairBatchItemOut] = []
    seen: set = set()
    for raw in payload.items:
        sn = (raw or "").strip()
        if not sn:
            items.append(schemas.RepairBatchItemOut(sn="", ok=False, code="field_required", message="sn required"))
            continue
        if sn in seen:
            items.append(
                schemas.RepairBatchItemOut(sn=sn, ok=False, code="duplicate_sn", message=f"duplicate sn: {sn}")
            )
            continue
        seen.add(sn)
        try:
            product, _action, _graph, passed, _completed, revoked, product_model, process_id = _check_repair(
                db, sn=sn, repair_action=action, target_station=payload.target_station or None,
                confirm_downstream=payload.confirm_downstream,
            )
        except AppError as exc:
            items.append(schemas.RepairBatchItemOut(sn=sn, ok=False, code=exc.code, message=exc.message))
            continue
        invalidating = 0
        if revoked:
            invalidating = (
                db.query(models.TestRecord)
                .filter(
                    models.TestRecord.sn == sn,
                    models.TestRecord.station_id.in_(revoked),
                    models.TestRecord.is_valid.is_(True),
                )
                .count()
            )
        items.append(
            schemas.RepairBatchItemOut(
                sn=sn,
                ok=True,
                product_model=product_model,
                process_id=process_id,
                current_status=product.current_status,
                passed_stations=sorted(passed),
                revoked_stations=sorted(revoked) if revoked else [],
                invalidating_records=invalidating,
            )
        )
        if not payload.dry_run:
            apply_repair(
                db,
                sn=sn,
                repair_action=action,
                target_station=payload.target_station or None,
                reason=payload.reason or "",
                technician_id=user.username,
                confirm_downstream=payload.confirm_downstream,
                batch_id=batch_id,
            )

    return schemas.RepairBatchOut(
        dry_run=payload.dry_run,
        total=len(items),
        ok_count=sum(1 for i in items if i.ok),
        fail_count=sum(1 for i in items if not i.ok),
        items=items,
        batch_id=batch_id,
    )


@router.post(
    "/batch-undo",
    response_model=schemas.RepairBatchUndoOut,
    summary="批量撤销：按 batch_id 撤销整批处置（逐条独立生效，走单条撤销的全部守卫）",
)
def batch_undo(payload: schemas.BatchUndoIn, db: Session = Depends(get_db), user=Depends(require_operator)):
    records = (
        db.query(models.RepairRecord)
        .filter(models.RepairRecord.batch_id == payload.batch_id)
        .order_by(models.RepairRecord.repair_id)
        .all()
    )
    if not records:
        raise not_found("batch_not_found", f"batch_not_found: {payload.batch_id}")
    items = []
    for record in records:
        try:
            undo_repair(db, repair_id=record.repair_id, operator_id=user.username)
            items.append(schemas.RepairBatchItemOut(sn=record.sn, ok=True))
        except AppError as exc:
            items.append(
                schemas.RepairBatchItemOut(sn=record.sn, ok=False, code=exc.code, message=exc.message)
            )
    return schemas.RepairBatchUndoOut(
        batch_id=payload.batch_id,
        total=len(items),
        ok_count=sum(1 for i in items if i.ok),
        fail_count=sum(1 for i in items if not i.ok),
        items=items,
    )


@router.get("/stats", response_model=schemas.RepairStatsOut, summary="处置动作分布统计")
def repair_stats(
    days: int = Query(0, ge=0, le=3650, description="统计最近 N 天；0 = 全部"),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """按处置动作聚合条数，供饼图/环图使用。"""
    query = db.query(
        models.RepairRecord.repair_action, func.count(models.RepairRecord.repair_id)
    )
    if days > 0:
        query = query.filter(models.RepairRecord.created_at >= utcnow() - timedelta(days=days))
    rows = query.group_by(models.RepairRecord.repair_action).all()
    items = [schemas.RepairActionStat(action=action.upper(), count=count) for action, count in rows]
    return schemas.RepairStatsOut(total=sum(i.count for i in items), items=items)
