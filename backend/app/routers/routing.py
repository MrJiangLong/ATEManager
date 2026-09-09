"""通道二：工艺拓扑编排与用例ID测试项（/api/admin/routing）。

    process_stations  工步拓扑（step_order + depends_on → 防跳站闸门）
    station_items     用例ID静态清单（防漏测依据）
"""

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import conflict_error, get_or_404, not_found
from ..security import current_user
from ..services.routing import process_topology, station_item_summary, validate_process

router = APIRouter(prefix="/api/admin/routing", tags=["admin-工艺拓扑"])


# ==================== 工步拓扑 ====================
@router.get("/topology", response_model=schemas.TopologyOut, summary="流程拓扑视图(工步 + 测试项)")
def topology(
    process_id: str = Query(..., min_length=1), db: Session = Depends(get_db), user=Depends(current_user)
):
    return process_topology(db, process_id)


@router.get("/stations", response_model=List[schemas.ProcessStationOut], summary="工步清单")
def list_steps(
    process_id: str = Query(..., min_length=1), db: Session = Depends(get_db), user=Depends(current_user)
):
    get_or_404(db, models.Process, process_id, "process")
    return process_topology(db, process_id).steps


@router.put("/stations", response_model=List[schemas.ProcessStationOut], summary="整体保存工步拓扑")
def save_steps(
    process_id: str,
    payload: List[schemas.ProcessStationIn],
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """以「整体覆盖」方式保存拓扑，前端一屏编排后一次性提交。"""
    get_or_404(db, models.Process, process_id, "process")
    for step in payload:
        get_or_404(db, models.Station, step.station_id, "station")

    db.query(models.ProcessStation).filter(models.ProcessStation.process_id == process_id).delete()
    for step in payload:
        db.add(
            models.ProcessStation(
                process_id=process_id,
                station_id=step.station_id,
                step_order=step.step_order,
                depends_on=list(step.depends_on or []),
            )
        )
    db.commit()
    return process_topology(db, process_id).steps


@router.delete("/stations/{station_id}", status_code=204, summary="移除工步(连带清理其测试项)")
def delete_step(
    process_id: str, station_id: str, db: Session = Depends(get_db), user=Depends(current_user)
):
    row = db.get(models.ProcessStation, (process_id, station_id))
    if row is None:
        raise not_found("step_not_found", f"step_not_found: {process_id}/{station_id}")
    db.delete(row)
    db.query(models.StationItem).filter(
        models.StationItem.process_id == process_id,
        models.StationItem.station_id == station_id,
    ).delete(synchronize_session=False)
    db.commit()


# ==================== 用例ID测试项 ====================
@router.get("/items", response_model=List[schemas.StationItemOut], summary="测试项清单")
def list_items(
    process_id: str = Query(..., min_length=1),
    station_id: str = Query(None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.StationItem).filter(models.StationItem.process_id == process_id)
    if station_id:
        query = query.filter(models.StationItem.station_id == station_id)
    return query.order_by(models.StationItem.station_id, models.StationItem.case_id).all()


@router.post("/items", response_model=schemas.StationItemOut, status_code=201, summary="新增用例ID测试项")
def create_item(payload: schemas.StationItemCreateIn, db: Session = Depends(get_db), user=Depends(current_user)):
    get_or_404(db, models.Process, payload.process_id, "process")
    exists = (
        db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == payload.process_id,
            models.StationItem.station_id == payload.station_id,
            models.StationItem.case_id == payload.case_id,
        )
        .first()
    )
    if exists:
        raise conflict_error("item_already_exists", f"item_already_exists: {payload.case_id}")
    row = models.StationItem(
        **{**payload.model_dump(), "item_name": payload.item_name or payload.case_id}
    )
    db.add(row)
    db.commit()
    return row


@router.put("/items/{item_id}", response_model=schemas.StationItemOut, summary="更新测试项")
def update_item(
    item_id: int, payload: schemas.StationItemUpdateIn, db: Session = Depends(get_db), user=Depends(current_user)
):
    row = get_or_404(db, models.StationItem, item_id, "item")
    for key, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(row, key, value)
    db.commit()
    return row


@router.delete("/items/{item_id}", status_code=204, summary="删除测试项")
def delete_item(item_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    row = get_or_404(db, models.StationItem, item_id, "item")
    db.delete(row)
    db.commit()


# ==================== 校验与克隆 ====================
@router.get("/validate", response_model=schemas.ValidateOut, summary="校验流程拓扑(成环/悬空依赖/无测试项)")
def validate(
    process_id: str = Query(..., min_length=1), db: Session = Depends(get_db), user=Depends(current_user)
):
    return validate_process(db, process_id)


@router.post("/clone", response_model=schemas.CloneOut, summary="克隆流程(派生机型时复用拓扑与测试项)")
def clone_process(payload: schemas.CloneIn, db: Session = Depends(get_db), user=Depends(current_user)):
    steps = (
        db.query(models.ProcessStation)
        .filter(models.ProcessStation.process_id == payload.from_process)
        .all()
    )
    items = (
        db.query(models.StationItem)
        .filter(models.StationItem.process_id == payload.from_process)
        .all()
    )
    if not steps and not items:
        raise not_found("source_not_found", f"source_not_found: {payload.from_process}")

    if db.get(models.Process, payload.to_process) is None:
        source = db.get(models.Process, payload.from_process)
        db.add(
            models.Process(
                process_id=payload.to_process,
                process_name=f"{source.process_name} (副本)" if source else payload.to_process,
            )
        )
        db.flush()

    for step in steps:
        db.merge(
            models.ProcessStation(
                process_id=payload.to_process,
                station_id=step.station_id,
                step_order=step.step_order,
                depends_on=list(step.depends_on or []),
            )
        )
    for item in items:
        db.merge(
            models.StationItem(
                process_id=payload.to_process,
                station_id=item.station_id,
                case_id=item.case_id,
                item_name=item.item_name,
                is_mandatory=item.is_mandatory,
                is_active=item.is_active,
            )
        )
    db.commit()
    return schemas.CloneOut(
        process_id=payload.to_process, cloned_steps=len(steps), cloned_items=len(items)
    )


@router.get("/item-summary", summary="各工位生效测试项数")
def item_summary(
    process_id: str = Query(..., min_length=1), db: Session = Depends(get_db), user=Depends(current_user)
):
    return station_item_summary(db, process_id)
