"""通道二：维修处置履历（/api/admin/repairs）—— 只读 + 授权处置。"""

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import current_user
from ..services import apply_repair
from ..services.timeutil import utcnow

router = APIRouter(prefix="/api/admin/repairs", tags=["admin-维修处置"])


@router.get("", response_model=schemas.RepairPageOut, summary="维修处置履历")
def list_repairs(
    sn: Optional[str] = None,
    repair_action: Optional[str] = None,
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
def create_repair(payload: schemas.RepairIn, db: Session = Depends(get_db), user=Depends(current_user)):
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
    )
    return record


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
