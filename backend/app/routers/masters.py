"""通道二：静态主数据 —— 工艺流程 / 机型 / 工位字典 / 机台档案。

删除操作均做引用检查，避免留下悬空配置。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import bad_request, conflict_error, get_or_404
from ..security import current_user, require_manager
from ..services.routing import load_process, model_switch_impact, process_overview
from ..services.timeutil import utcnow

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
def create_process(payload: schemas.ProcessCreateIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    if db.get(models.Process, payload.process_id):
        raise conflict_error("process_already_exists", f"process_already_exists: {payload.process_id}")
    row = models.Process(
        process_id=payload.process_id,
        process_name=payload.process_name or payload.process_id,
        is_active=payload.is_active,
        is_sequential=payload.is_sequential,
    )
    db.add(row)
    db.commit()
    return row

@process_router.put("/{process_id}", response_model=schemas.ProcessOut, summary="更新流程")
def update_process(
    process_id: str, payload: schemas.ProcessUpdateIn, db: Session = Depends(get_db), user=Depends(require_manager)
):
    row = get_or_404(db, models.Process, process_id, "process")
    if payload.process_name is not None:
        row.process_name = payload.process_name
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.is_sequential is not None:
        row.is_sequential = payload.is_sequential
    db.commit()
    return row

@process_router.delete("/{process_id}", status_code=204, summary="删除流程(存在机型或拓扑时拒绝)")
def delete_process(process_id: str, db: Session = Depends(get_db), user=Depends(require_manager)):
    get_or_404(db, models.Process, process_id, "process")
    if db.query(models.ProductModel).filter(models.ProductModel.process_id == process_id).count():
        raise conflict_error(
            "process_has_models",
            f"删除拦截：流程 {process_id} 仍有机型绑定，不能删除。"
            f"日常调整请直接原地编辑该流程的拓扑与用例；确需换流程时走「克隆 → 影响面预演 → "
            f"机型改绑（在制品交集结转）→ 旧流程归档（停用）」路径",
        )
    if db.query(models.ProcessStation).filter(models.ProcessStation.process_id == process_id).count():
        raise conflict_error(
            "process_has_steps",
            f"删除拦截：流程 {process_id} 仍有工步拓扑，不能删除。如需废弃请归档（停用），保留历史可追溯",
        )
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
def create_model(payload: schemas.ProductModelCreateIn, db: Session = Depends(get_db), user=Depends(require_manager)):
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
    user=Depends(require_manager),
):
    row = get_or_404(db, models.ProductModel, product_model, "model")
    if payload.process_id:
        _get_active_process(db, payload.process_id)
        # 工艺切换（改绑流程）：存在在制品时必须显式确认（R2 交集结转——
        # 印章按新流程判定：交集工位继承、被删工位仅作历史、新增必跑需补测）
        if payload.process_id != row.process_id:
            # 测试中的件：改绑后出站会按新流程校验工位 → station_not_in_process，
            # 必须先完成或中止，因此不做“确认放行”
            testing = (
                db.query(models.ProductStatus)
                .filter(
                    models.ProductStatus.product_model == row.product_model,
                    models.ProductStatus.current_status == models.STATUS_TESTING,
                )
                .count()
            )
            if testing:
                raise conflict_error(
                    "process_switch_blocked_by_running",
                    f"改绑拦截：机型 {row.product_model} 有 {testing} 个序列号正在测试中，"
                    f"改绑后出站会因工位不在新流程被拒。请先完成或中止这些会话再切换",
                    data={"testing": testing},
                )
            wip = (
                db.query(models.ProductStatus)
                .filter(
                    models.ProductStatus.product_model == row.product_model,
                    models.ProductStatus.is_completed.is_(False),
                    models.ProductStatus.current_status != models.STATUS_SCRAPPED,
                )
                .count()
            )
            if wip and not payload.confirm_switch:
                src = db.get(models.Process, row.process_id)
                tgt = db.get(models.Process, payload.process_id)
                src_label = (
                    f"{row.process_id}（{src.process_name}）" if src and src.process_name else row.process_id
                )
                tgt_label = (
                    f"{payload.process_id}（{tgt.process_name}）"
                    if tgt and tgt.process_name
                    else payload.process_id
                )
                raise conflict_error(
                    "process_switch_wip_confirm_required",
                    f"工艺切换确认：机型 {row.product_model} 当前有 {wip} 件在制品，"
                    f"从流程 {src_label} 切换到 {tgt_label} 后将按新流程判定"
                    f"（交集工位印章继承、新增必跑工位需补测），确认切换？",
                    data={"wip": wip, "from_process": row.process_id, "to_process": payload.process_id},
                )
    # 模式切换校验（直通 ⇄ 严格卡控）
    new_mode = payload.passthrough
    if new_mode is not None and new_mode != row.passthrough:
        if not new_mode:
            # 关直通 = 进入严格卡控：拓扑必须齐备，避免切完进站 404 process_not_found
            if load_process(db, row.process_id) is None:
                raise conflict_error(
                    "model_topology_missing", f"model_topology_missing: {row.process_id} has no steps"
                )
            # 产线清线（Pipeline Drain）：直通件零痕迹，服务端无在制台账可查，
            # 清线只能由工艺员线下确认——服务端强制要求显式确认位，防止直连 API 绕过前端弹窗。
            if not payload.confirm_drain:
                raise conflict_error(
                    "drain_confirm_required",
                    f"drain_confirm_required: {row.product_model} needs pipeline drain "
                    "confirmation before switching to strict control",
                )
        else:
            # 开直通 = 退出卡控：不允许在制件与旁路件混合态
            wip = (
                db.query(models.ProductStatus)
                .filter(
                    models.ProductStatus.product_model == row.product_model,
                    models.ProductStatus.is_completed.is_(False),
                    models.ProductStatus.current_status != models.STATUS_SCRAPPED,
                )
                .count()
            )
            if wip:
                raise conflict_error("model_has_wip", f"model_has_wip: {wip} units in flight")
    for key, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        if key in ("confirm_drain", "confirm_switch"):  # 确认位仅消费不落库
            continue
        setattr(row, key, value)
    db.commit()
    return row

@model_router.get(
    "/{product_model}/switch-impact",
    response_model=schemas.ModelSwitchImpactOut,
    summary="改绑流程影响面预演（在制品继承/补测、失效机台、拓扑差异）",
)
def model_switch_impact_endpoint(
    product_model: str,
    target_process_id: str,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """只读预演：改绑到目标流程后，在制品逐件的印章继承/补测判定与机台绑定风险。"""
    return model_switch_impact(db, product_model, target_process_id)


@model_router.delete("/{product_model}", status_code=204, summary="删除机型(存在在制品或台账时拒绝)")
def delete_model(product_model: str, db: Session = Depends(get_db), user=Depends(require_manager)):
    get_or_404(db, models.ProductModel, product_model, "model")
    if db.query(models.ProductStatus).filter(models.ProductStatus.product_model == product_model).count():
        raise conflict_error(
            "model_in_use",
            f"删除拦截：机型 {product_model} 已有在制品/台账记录，不能删除（历史需可追溯）。"
            f"如需退出使用，请改绑到归档流程或停止下发",
        )
    db.delete(db.get(models.ProductModel, product_model))
    db.commit()

# ==================== 工位字典 ====================
station_router = APIRouter(prefix="/api/admin/stations", tags=["admin-工位"])

@station_router.get("", response_model=List[schemas.StationOut], summary="工位清单")
def list_stations(db: Session = Depends(get_db), user=Depends(current_user)):
    return db.query(models.Station).order_by(models.Station.station_id).all()

@station_router.post("", response_model=schemas.StationOut, status_code=201, summary="新建工位")
def create_station(payload: schemas.StationCreateIn, db: Session = Depends(get_db), user=Depends(require_manager)):
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
    station_id: str, payload: schemas.StationUpdateIn, db: Session = Depends(get_db), user=Depends(require_manager)
):
    row = get_or_404(db, models.Station, station_id, "station")
    for key, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(row, key, value)
    db.commit()
    return row

@station_router.delete("/{station_id}", status_code=204, summary="删除工位(存在拓扑或机台绑定时拒绝)")
def delete_station(station_id: str, db: Session = Depends(get_db), user=Depends(require_manager)):
    get_or_404(db, models.Station, station_id, "station")
    if db.query(models.ProcessStation).filter(models.ProcessStation.station_id == station_id).count():
        raise conflict_error("station_in_topology", "station_in_topology: remove it from processes first")
    for c in db.query(models.StationClient).all():
        if station_id in (c.bound_stations or []):
            raise conflict_error("station_has_clients", "station_has_clients: unbind its clients first")
    # 运行态"当前操作工位"指针直接置空（历史事实以台账为准），FK 才不会阻塞删除
    db.query(models.StationClient).filter(models.StationClient.station_id == station_id).update(
        {models.StationClient.station_id: None}, synchronize_session=False
    )
    if db.query(models.StationItem).filter(models.StationItem.station_id == station_id).count():
        raise conflict_error("station_has_items", "station_has_items: remove its test items first")
    db.delete(db.get(models.Station, station_id))
    db.commit()

# ==================== 试验设备登记 ====================
sn_registry_router = APIRouter(prefix="/api/admin/sn-registry", tags=["admin-试验设备"])

@sn_registry_router.get("", response_model=List[schemas.SnRegistryOut], summary="试验设备登记清单")
def list_sn_registry(db: Session = Depends(get_db), user=Depends(current_user)):
    return db.query(models.SnRegistry).order_by(models.SnRegistry.created_at.desc()).all()

@sn_registry_router.post("", response_model=schemas.SnRegistryOut, status_code=201, summary="登记试验设备")
def create_sn_registry(payload: schemas.SnRegistryIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    if db.get(models.SnRegistry, payload.sn):
        raise conflict_error("sn_already_registered", f"sn_already_registered: {payload.sn}")
    row = models.SnRegistry(sn=payload.sn, note=payload.note or None, registered_by=user.username)
    db.add(row)
    db.commit()
    return row

@sn_registry_router.delete("/{sn}", status_code=204, summary="注销 SN 登记(仅删除登记，不触碰生产/测试记录)")
def delete_sn_registry(sn: str, db: Session = Depends(get_db), user=Depends(require_manager)):
    # 注销只做一件事：恢复进站资格。不自动重置——"是否曾运行研发测试代码"
    # 只有实验室知道，误判清零会摧毁真实产线历史；重流线由工艺员显式走
    # 维修处置 RESET（清印章/作废报告/履历留痕），被动过的设备回产线会被
    # 固件基线 403 与用例判定自动拦下，不存在静默出货路径。
    db.delete(get_or_404(db, models.SnRegistry, sn, "sn_registry"))
    db.commit()


@sn_registry_router.get("/export", response_model=schemas.SnRegistryExportOut, summary="导出 SN 登记清单")
def export_sn_registry(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.query(models.SnRegistry).order_by(models.SnRegistry.created_at.desc()).all()
    return schemas.SnRegistryExportOut(
        exported_at=utcnow(),
        items=[schemas.SnRegistryOut.model_validate(r) for r in rows],
    )


@sn_registry_router.post("/import", response_model=schemas.SnRegistryImportOut, summary="导入 SN 登记清单（已存在的 SN 跳过）")
def import_sn_registry(payload: schemas.SnRegistryImportIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    """事务式导入：登记人统一记为当前操作者（操作留痕），与清单内文件值无关；
    已存在的 SN 逐条跳过（幂等，重复导入安全），空清单 400 items_required。"""
    if not payload.items:
        raise bad_request("items_required", "items_required: import list is empty")
    created = skipped = 0
    for item in payload.items:
        if db.get(models.SnRegistry, item.sn):
            skipped += 1
            continue
        db.add(models.SnRegistry(sn=item.sn, note=item.note or None, registered_by=user.username))
        created += 1
    db.commit()
    return schemas.SnRegistryImportOut(total=len(payload.items), created=created, skipped=skipped)

router.include_router(process_router)
router.include_router(model_router)
router.include_router(station_router)
router.include_router(sn_registry_router)

