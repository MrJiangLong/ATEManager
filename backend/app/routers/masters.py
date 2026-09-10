"""通道二：静态主数据 —— 工艺流程 / 机型 / 工位字典 / 机台档案。

删除操作均做引用检查，避免留下悬空配置。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import conflict_error, get_or_404
from ..security import current_user
from ..services.routing import process_overview

router = APIRouter()


def _get_active_process(db: Session, process_id: str) -> models.Process:
    """机型只能绑到启用中的流程。

    已停用的流程对存量机型继续有效（在制品照常流转），只是不再接受新绑定。
    """
    row = get_or_404(db, models.Process, process_id, "process")
    if not row.is_active:
        raise conflict_error("process_inactive", f"process_inactive: {process_id} is archived")
    return row


# ==================== 工艺流程 ====================
process_router = APIRouter(prefix="/api/admin/processes", tags=["admin-工艺流程"])


@process_router.get("", response_model=List[schemas.ProcessStatOut], summary="流程清单(含机型数/工位数/测试项数)")
def list_processes(db: Session = Depends(get_db), user=Depends(current_user)):
    return process_overview(db)


@process_router.post("", response_model=schemas.ProcessOut, status_code=201, summary="新建流程")
def create_process(payload: schemas.ProcessCreateIn, db: Session = Depends(get_db), user=Depends(current_user)):
    if db.get(models.Process, payload.process_id):
        raise conflict_error("process_already_exists", f"process_already_exists: {payload.process_id}")
    row = models.Process(
        process_id=payload.process_id,
        process_name=payload.process_name or payload.process_id,
        is_active=payload.is_active,
    )
    db.add(row)
    db.commit()
    return row


@process_router.put("/{process_id}", response_model=schemas.ProcessOut, summary="更新流程")
def update_process(
    process_id: str, payload: schemas.ProcessUpdateIn, db: Session = Depends(get_db), user=Depends(current_user)
):
    row = get_or_404(db, models.Process, process_id, "process")
    if payload.process_name is not None:
        row.process_name = payload.process_name
    if payload.is_active is not None:
        row.is_active = payload.is_active
    db.commit()
    return row


@process_router.delete("/{process_id}", status_code=204, summary="删除流程(存在机型或拓扑时拒绝)")
def delete_process(process_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    get_or_404(db, models.Process, process_id, "process")
    if db.query(models.ProductModel).filter(models.ProductModel.process_id == process_id).count():
        raise conflict_error("process_has_models", "process_has_models: reassign its models first")
    if db.query(models.ProcessStation).filter(models.ProcessStation.process_id == process_id).count():
        raise conflict_error("process_has_steps", "process_has_steps: remove its topology first")
    # station_items 无外键级联：流程都删了，其测试项必然无意义，连带清理而非拒绝，
    # 否则存量孤儿项会让流程永远删不掉。
    db.query(models.StationItem).filter(models.StationItem.process_id == process_id).delete(
        synchronize_session=False
    )
    db.delete(db.get(models.Process, process_id))
    db.commit()


# ==================== 机型 ====================
model_router = APIRouter(prefix="/api/admin/product-models", tags=["admin-机型"])


@model_router.get("", response_model=List[schemas.ProductModelOut], summary="机型清单")
def list_models(
    process_id: Optional[str] = None, db: Session = Depends(get_db), user=Depends(current_user)
):
    query = db.query(models.ProductModel)
    if process_id:
        query = query.filter(models.ProductModel.process_id == process_id)
    return query.order_by(models.ProductModel.product_model).all()


@model_router.post("", response_model=schemas.ProductModelOut, status_code=201, summary="新建机型")
def create_model(payload: schemas.ProductModelCreateIn, db: Session = Depends(get_db), user=Depends(current_user)):
    if db.get(models.ProductModel, payload.product_model):
        raise conflict_error("model_already_exists", f"model_already_exists: {payload.product_model}")
    _get_active_process(db, payload.process_id)
    row = models.ProductModel(**payload.model_dump())
    db.add(row)
    db.commit()
    return row


@model_router.put("/{product_model}", response_model=schemas.ProductModelOut, summary="更新机型")
def update_model(
    product_model: str,
    payload: schemas.ProductModelUpdateIn,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    row = get_or_404(db, models.ProductModel, product_model, "model")
    if payload.process_id:
        _get_active_process(db, payload.process_id)
    for key, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(row, key, value)
    db.commit()
    return row


@model_router.delete("/{product_model}", status_code=204, summary="删除机型(存在在制品或台账时拒绝)")
def delete_model(product_model: str, db: Session = Depends(get_db), user=Depends(current_user)):
    get_or_404(db, models.ProductModel, product_model, "model")
    if db.query(models.ProductStatus).filter(models.ProductStatus.product_model == product_model).count():
        raise conflict_error("model_in_use", "model_in_use: WIP products reference this model")
    db.delete(db.get(models.ProductModel, product_model))
    db.commit()


# ==================== 工位字典 ====================
station_router = APIRouter(prefix="/api/admin/stations", tags=["admin-工位"])


@station_router.get("", response_model=List[schemas.StationOut], summary="工位清单")
def list_stations(db: Session = Depends(get_db), user=Depends(current_user)):
    return db.query(models.Station).order_by(models.Station.station_id).all()


@station_router.post("", response_model=schemas.StationOut, status_code=201, summary="新建工位")
def create_station(payload: schemas.StationCreateIn, db: Session = Depends(get_db), user=Depends(current_user)):
    if db.get(models.Station, payload.station_id):
        raise conflict_error("station_already_exists", f"station_already_exists: {payload.station_id}")
    row = models.Station(
        station_id=payload.station_id,
        station_name=payload.station_name or payload.station_id,
        timeout_sec=payload.timeout_sec,
    )
    db.add(row)
    db.commit()
    return row


@station_router.put("/{station_id}", response_model=schemas.StationOut, summary="更新工位")
def update_station(
    station_id: str, payload: schemas.StationUpdateIn, db: Session = Depends(get_db), user=Depends(current_user)
):
    row = get_or_404(db, models.Station, station_id, "station")
    for key, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(row, key, value)
    db.commit()
    return row


@station_router.delete("/{station_id}", status_code=204, summary="删除工位(存在拓扑或机台绑定时拒绝)")
def delete_station(station_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    get_or_404(db, models.Station, station_id, "station")
    if db.query(models.ProcessStation).filter(models.ProcessStation.station_id == station_id).count():
        raise conflict_error("station_in_topology", "station_in_topology: remove it from processes first")
    if db.query(models.StationClient).filter(models.StationClient.station_id == station_id).count():
        raise conflict_error("station_has_clients", "station_has_clients: unbind its clients first")
    # 工位是跨流程共享字典，删一个会波及所有引用它的流程，故只拒绝不连带清理；
    # 与 station_in_topology 一样，要求先从各流程移除。
    if db.query(models.StationItem).filter(models.StationItem.station_id == station_id).count():
        raise conflict_error("station_has_items", "station_has_items: remove its test items first")
    db.delete(db.get(models.Station, station_id))
    db.commit()


router.include_router(process_router)
router.include_router(model_router)
router.include_router(station_router)
