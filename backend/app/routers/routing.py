"""通道二：工艺拓扑编排与用例ID测试项（/api/admin/routing）。

    process_stations  工步拓扑（step_order + depends_on → 防跳站闸门）
    station_items     用例ID静态清单（防漏测依据）；同一工位可挂多个用例集合（set_id），
                      进站时上报序列与任意一套集合匹配即放行
"""

from typing import Dict, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import bad_request, conflict_error, get_or_404, not_found
from ..security import current_user, require_manager
from ..services.routing import (
    process_topology,
    station_item_summary,
    topology_impact,
    validate_process,
)
from ..services.timeutil import utcnow

router = APIRouter(prefix="/api/admin/routing", tags=["admin-工艺拓扑"])

def _assert_station_in_process(db: Session, process_id: str, station_id: str) -> None:
    """测试项必须挂在流程拓扑内的工位上。

    station_items 没有指向 process_stations 的外键，只能在应用层守住，
    否则会写入"不在拓扑里的必测项"——它不参与进站闸门，却会污染流程的测试项统计。
    """
    step = (
        db.query(models.ProcessStation)
        .filter(
            models.ProcessStation.process_id == process_id,
            models.ProcessStation.station_id == station_id,
        )
        .first()
    )
    if step is None:
        raise bad_request(
            "station_not_in_process",
            f"station_not_in_process: {station_id} is not a step of {process_id}",
        )

def _ensure_case_set(
    db: Session, process_id: str, station_id: str, set_id: str
) -> models.StationCaseSet:
    """用例集合主数据兜底：脚本/导入直接落用例行时按字段码补建（名称缺省用字段码）。"""
    row = (
        db.query(models.StationCaseSet)
        .filter(
            models.StationCaseSet.process_id == process_id,
            models.StationCaseSet.station_id == station_id,
            models.StationCaseSet.set_id == set_id,
        )
        .first()
    )
    if row is None:
        row = models.StationCaseSet(
            process_id=process_id, station_id=station_id, set_id=set_id, set_name=set_id
        )
        db.add(row)
    return row

# "没测试项不让存拓扑 → 没拓扑不让加测试项"的死锁。
_BLOCKING_SAVE_CODES = frozenset(
    {"config_cycle", "station_undefined", "mandatory_dep_on_optional", "reverse_step_dep"}
)

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

@router.post(
    "/topology/impact",
    response_model=schemas.TopologyImpactOut,
    summary="拓扑保存前影响面预演（只读、非阻断：草案不落库）",
)
def topology_impact_endpoint(
    payload: List[schemas.ProcessStationIn],
    process_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """用请求体里的**草案拓扑**计算影响面：受影响在制品（缺前置将被拦）、
    被移除工位上的失效用例数、绑定失效的机台。仅提示，不阻断保存。"""
    get_or_404(db, models.Process, process_id, "process")
    return topology_impact(db, process_id, payload)


@router.put("/stations", response_model=schemas.TopologySaveOut, summary="整体保存工步拓扑")
def save_steps(
    process_id: str,
    payload: List[schemas.ProcessStationIn],
    force: bool = Query(False, description="跳过结构性校验强行保存"),
    db: Session = Depends(get_db),
    user=Depends(require_manager),
):
    """以「整体覆盖」方式保存拓扑，前端一屏编排后一次性提交。

    被移出拓扑的工位，其测试项会一并清理 —— 与 DELETE /stations/{station_id} 的行为
    保持一致。否则 station_items 会残留成"没人跑却仍计数的必测项"。

    保存后会做结构性校验（成环等），不通过则整体回滚（可用 force=true 跳过）。
    """
    get_or_404(db, models.Process, process_id, "process")
    for step in payload:
        get_or_404(db, models.Station, step.station_id, "station")

    keep = {step.station_id for step in payload}
    prev_steps = {
        row.station_id: row
        for row in db.query(models.ProcessStation)
        .filter(models.ProcessStation.process_id == process_id)
        .all()
    }
    removed = set(prev_steps) - keep
    if removed:
        db.query(models.StationItem).filter(
            models.StationItem.process_id == process_id,
            models.StationItem.station_id.in_(removed),
        ).delete(synchronize_session=False)
        db.query(models.StationCaseSet).filter(
            models.StationCaseSet.process_id == process_id,
            models.StationCaseSet.station_id.in_(removed),
        ).delete(synchronize_session=False)

    db.query(models.ProcessStation).filter(models.ProcessStation.process_id == process_id).delete()
    for step in payload:
        prev = prev_steps.get(step.station_id)
        db.add(
            models.ProcessStation(
                process_id=process_id,
                station_id=step.station_id,
                step_order=step.step_order,
                depends_on=list(step.depends_on or []),
                is_optional=step.is_optional,
                invalidates_downstream=step.invalidates_downstream,
                # 拓扑编辑不静默清空基线：锁定状态与草稿/基线清单按工位原样保留
                baseline_locked=prev.baseline_locked if prev else False,
                baseline_manifest=prev.baseline_manifest if prev else None,
                baseline_captured_at=prev.baseline_captured_at if prev else None,
            )
        )
    # 先落库再校验，以便直接复用 validate_process 的完整规则；不通过则整体回滚
    db.flush()

    if not force:
        blocking = [
            issue
            for issue in validate_process(db, process_id).issues
            if issue.level == "error" and issue.code in _BLOCKING_SAVE_CODES
        ]
        if blocking:
            db.rollback()
            raise conflict_error(
                "topology_invalid",
                "topology_invalid: " + "; ".join(f"{i.code}: {i.detail}" for i in blocking),
                data={"issues": [i.model_dump() for i in blocking]},
            )

    db.commit()

    # 移除工位时，在制品上残留的该工位印章属历史事实——列出清单供工艺员决定
    # 是否对在制品做维修处置（RESET/RETEST），系统不做静默清除
    removed_wip_map: Dict[str, List[str]] = {}
    if removed:
        models_in = [
            m.product_model
            for m in db.query(models.ProductModel)
            .filter(models.ProductModel.process_id == process_id)
            .all()
        ]
        for p in db.query(models.ProductStatus).filter(
            models.ProductStatus.product_model.in_(models_in or [""])
        ).all():
            stamped = [s for s in (p.passed_stations or []) if s in removed]
            for sid in stamped:
                removed_wip_map.setdefault(sid, []).append(p.sn)
    removed_wip = [
        schemas.WipResidue(station_id=sid, wip_sns=sns)
        for sid, sns in sorted(removed_wip_map.items())
    ]

    return schemas.TopologySaveOut(
        steps=process_topology(db, process_id).steps,
        removed_wip=removed_wip,
    )

@router.delete("/stations/{station_id}", status_code=204, summary="移除工步(连带清理其测试项)")
def delete_step(
    process_id: str, station_id: str, db: Session = Depends(get_db), user=Depends(require_manager)
):
    row = db.get(models.ProcessStation, (process_id, station_id))
    if row is None:
        raise not_found("step_not_found", f"step_not_found: {process_id}/{station_id}")
    db.delete(row)
    db.query(models.StationItem).filter(
        models.StationItem.process_id == process_id,
        models.StationItem.station_id == station_id,
    ).delete(synchronize_session=False)
    db.query(models.StationCaseSet).filter(
        models.StationCaseSet.process_id == process_id,
        models.StationCaseSet.station_id == station_id,
    ).delete(synchronize_session=False)
    db.commit()

def _running_sessions_on(db: Session, process_id: str, station_id: str) -> int:
    """该流程 + 工位上正在跑的会话数。

    test_sessions 没有 process_id 字段，只能按 SN 反查（与 abort-running 口径一致）：
    会话 SN → 在制品机型 → 机型所属流程。同名工位在别的流程上跑不拦，避免误伤。
    """
    models_in = [
        m.product_model
        for m in db.query(models.ProductModel)
        .filter(models.ProductModel.process_id == process_id)
        .all()
    ]
    sns = [
        p.sn
        for p in db.query(models.ProductStatus)
        .filter(models.ProductStatus.product_model.in_(models_in or [""]))
        .all()
    ]
    return (
        db.query(models.TestSession)
        .filter(
            models.TestSession.station_id == station_id,
            models.TestSession.sn.in_(sns or [""]),
            models.TestSession.status == models.SESSION_RUNNING,
        )
        .count()
    )


def _assert_baseline_unlocked(db: Session, process_id: str, station_id: str) -> None:
    """锁定工位的用例清单由基线固化，禁止手工增删改（先解锁再编辑）。"""
    ps = db.get(models.ProcessStation, (process_id, station_id))
    if ps is not None and ps.baseline_locked:
        raise conflict_error(
            "baseline_locked",
            f"baseline_locked: 工位 {station_id} 的用例清单已由基线固化，请先在拓扑页解锁",
        )

# ==================== 用例ID测试项 ====================
@router.get("/items", response_model=List[schemas.StationItemOut], summary="测试项清单")
def list_items(
    process_id: str = Query(..., min_length=1),
    station_id: str = Query(None),
    set_id: str = Query(None, description="只看某个用例集合"),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.StationItem).filter(models.StationItem.process_id == process_id)
    if station_id:
        query = query.filter(models.StationItem.station_id == station_id)
    if set_id:
        query = query.filter(models.StationItem.set_id == set_id)
    return query.order_by(
        models.StationItem.station_id,
        models.StationItem.set_id,
        models.StationItem.sort_order.is_(None),
        models.StationItem.sort_order,
        models.StationItem.case_id,
    ).all()

@router.post("/items", response_model=schemas.StationItemOut, status_code=201, summary="新增用例ID测试项")
def create_item(payload: schemas.StationItemCreateIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    """新增测试项；未指定 sort_order 时按步长 10 追加到**该集合**末尾（预留中间插入空间）。"""
    get_or_404(db, models.Process, payload.process_id, "process")
    _assert_station_in_process(db, payload.process_id, payload.station_id)
    _assert_baseline_unlocked(db, payload.process_id, payload.station_id)
    exists = (
        db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == payload.process_id,
            models.StationItem.station_id == payload.station_id,
            models.StationItem.set_id == payload.set_id,
            models.StationItem.case_id == payload.case_id,
        )
        .first()
    )
    if exists:
        raise conflict_error(
            "item_already_exists", f"item_already_exists: {payload.set_id}/{payload.case_id}"
        )
    next_order = payload.sort_order
    if next_order is None:
        max_order = (
            db.query(func.max(models.StationItem.sort_order))
            .filter(
                models.StationItem.process_id == payload.process_id,
                models.StationItem.station_id == payload.station_id,
                models.StationItem.set_id == payload.set_id,
            )
            .scalar()
        )
        next_order = (max_order or 0) + 10
    _ensure_case_set(db, payload.process_id, payload.station_id, payload.set_id)
    row = models.StationItem(
        **{
            **payload.model_dump(exclude={"sort_order"}),
            "item_name": payload.item_name or payload.case_id,
            "sort_order": next_order,
        }
    )
    db.add(row)
    db.commit()
    return row

@router.put("/items/{item_id}", response_model=schemas.StationItemOut, summary="更新测试项")
def update_item(
    item_id: int, payload: schemas.StationItemUpdateIn, db: Session = Depends(get_db), user=Depends(require_manager)
):
    """更新测试项；sort_order 显式传 null = 重置为未编号（排在已编号项之后按 case_id 队尾）。"""
    row = get_or_404(db, models.StationItem, item_id, "item")
    _assert_baseline_unlocked(db, row.process_id, row.station_id)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key == "sort_order":
            continue
        if value is not None:
            setattr(row, key, value)
    if "sort_order" in data:
        row.sort_order = data["sort_order"]
    db.commit()
    return row

@router.delete("/items/{item_id}", status_code=204, summary="删除测试项")
def delete_item(item_id: int, db: Session = Depends(get_db), user=Depends(require_manager)):
    row = get_or_404(db, models.StationItem, item_id, "item")
    _assert_baseline_unlocked(db, row.process_id, row.station_id)
    db.delete(row)
    db.commit()

@router.post("/items/import", response_model=schemas.StationItemImportOut, summary="批量导入用例ID清单(幂等upsert)")
def import_items(
    payload: schemas.StationItemImportIn,
    force: bool = Query(False, description="忽略 RUNNING 会话闸门强行同步"),
    db: Session = Depends(get_db),
    user=Depends(require_manager),
):
    """整批同步某工位**某一套用例集合**的用例ID清单，供上位机脚本自动对齐
    （pytest --collect-only 的输出）。

    匹配键 (process_id, station_id, set_id, case_id) 与唯一约束一致，故重复导入幂等：
    第二次执行全部 unchanged，不会产生重复行，也不会因已存在而报 409。
    同工位的其他集合完全不受影响（replace 只停用**本集合内**清单外的项）。

    闸门：本次导入会"新增必测项"（新用例 / 停用复活 / is_mandatory 翻 true）且该
    工位有 RUNNING 会话时默认 409 拒绝 —— 运行中会话的断点里没有新必测项，出站会被
    missing_mandatory 误计一次失败（FAIL_LIMIT 次 → LOCKED）。先停会话再同步，或
    force=true 强行覆盖。只收缩清单（停用/降级/改名）不拦，随时可做。
    执行顺序按导入载荷顺序整体编号（导入顺序即执行顺序）；replace 模式停用的清单外
    项序号清空。
    """
    get_or_404(db, models.Process, payload.process_id, "process")
    _assert_station_in_process(db, payload.process_id, payload.station_id)
    _assert_baseline_unlocked(db, payload.process_id, payload.station_id)
    if payload.mode not in ("upsert", "replace"):
        raise bad_request("bad_mode", "bad_mode: mode must be 'upsert' or 'replace'")
    # 脚本/上位机直接按字段码同步时，集合主数据自动补建（名称缺省用字段码）
    _ensure_case_set(db, payload.process_id, payload.station_id, payload.set_id)

    existing = {
        row.case_id: row
        for row in db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == payload.process_id,
            models.StationItem.station_id == payload.station_id,
            models.StationItem.set_id == payload.set_id,
        )
        .all()
    }
    prev_state = {cid: (bool(r.is_active), bool(r.is_mandatory)) for cid, r in existing.items()}

    warnings: List[str] = []
    seen = set()
    payload_order: List[str] = []
    mand: Dict[str, bool] = {}
    created = updated = unchanged = 0
    for row_in in payload.items:
        case_id = row_in.case_id
        if not case_id:
            continue
        if case_id in seen:
            warnings.append(f"duplicate case_id in payload: {case_id}")
            continue
        seen.add(case_id)
        payload_order.append(case_id)
        mand[case_id] = bool(row_in.is_mandatory)

        row = existing.get(case_id)
        # 未传 item_name 时：新增用 case_id 兜底，已存在的保留原名（不把中文名抹成 nodeid）
        name = row_in.item_name or (row.item_name if row is not None else case_id)
        if row is None:
            row = models.StationItem(
                process_id=payload.process_id,
                station_id=payload.station_id,
                set_id=payload.set_id,
                case_id=case_id,
                item_name=name,
                is_mandatory=row_in.is_mandatory,
                is_active=True,
            )
            db.add(row)
            existing[case_id] = row
            created += 1
            continue

        changed = False
        if row.item_name != name:
            row.item_name = name
            changed = True
        if bool(row.is_mandatory) != bool(row_in.is_mandatory):
            row.is_mandatory = row_in.is_mandatory
            changed = True
        if not row.is_active:
            row.is_active = True
            changed = True
        updated += 1 if changed else 0
        unchanged += 1 if not changed else 0

    newly_mandatory = sorted(
        cid for cid, m in mand.items() if m and (cid not in prev_state or not all(prev_state[cid]))
    )
    if newly_mandatory and not force:
        running = _running_sessions_on(db, payload.process_id, payload.station_id)
        if running:
            if payload.dry_run:
                warnings.append(
                    f"would_block: {running} RUNNING session(s) on {payload.station_id} "
                    f"lack newly mandatory case(s); abort them first or pass force=true"
                )
            else:
                raise conflict_error(
                    "running_session_block",
                    f"running_session_block: {running} RUNNING session(s) on "
                    f"{payload.process_id}/{payload.station_id} would fail checkout "
                    f"missing_mandatory for: {', '.join(newly_mandatory)}",
                    data={"running_sessions": running, "newly_mandatory": newly_mandatory},
                )

    orphans = sorted(
        case_id for case_id, row in existing.items() if case_id not in seen and row.is_active
    )
    deactivated = 0
    if payload.mode == "replace":
        for case_id in orphans:
            existing[case_id].is_active = False
            existing[case_id].sort_order = None
            deactivated += 1

    for idx, case_id in enumerate(payload_order, 1):
        row = existing.get(case_id)
        if row is not None and row.sort_order != idx * 10:
            row.sort_order = idx * 10

    total_active = sum(1 for r in existing.values() if r.is_active)

    if payload.dry_run:
        db.rollback()
    else:
        db.commit()

    return schemas.StationItemImportOut(
        process_id=str(payload.process_id),
        station_id=str(payload.station_id),
        set_id=str(payload.set_id),
        mode=payload.mode,
        dry_run=payload.dry_run,
        created=created,
        updated=updated,
        unchanged=unchanged,
        deactivated=deactivated,
        total_active=total_active,
        orphan_count=len(orphans),
        orphans=orphans[:20],
        warnings=warnings,
    )

# ==================== 用例集合（一个工位多套） ====================
@router.get("/case-sets", response_model=List[schemas.CaseSetOut], summary="工位用例集合清单")
def list_case_sets(
    process_id: str = Query(..., min_length=1),
    station_id: str = Query(None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.StationCaseSet).filter(models.StationCaseSet.process_id == process_id)
    if station_id:
        query = query.filter(models.StationCaseSet.station_id == station_id)
    rows = query.order_by(models.StationCaseSet.station_id, models.StationCaseSet.set_id).all()
    counts = {
        (sid, set_id): n
        for sid, set_id, n in db.query(
            models.StationItem.station_id,
            models.StationItem.set_id,
            func.count(models.StationItem.item_id),
        )
        .filter(
            models.StationItem.process_id == process_id,
            models.StationItem.is_active.is_(True),
        )
        .group_by(models.StationItem.station_id, models.StationItem.set_id)
        .all()
    }
    return [
        schemas.CaseSetOut(
            process_id=row.process_id,
            station_id=row.station_id,
            set_id=row.set_id,
            set_name=row.set_name or row.set_id,
            item_count=counts.get((row.station_id, row.set_id), 0),
        )
        for row in rows
    ]

@router.post("/case-sets", response_model=schemas.CaseSetOut, status_code=201, summary="新建工位用例集合")
def create_case_set(
    payload: schemas.CaseSetCreateIn,
    db: Session = Depends(get_db),
    user=Depends(require_manager),
):
    """新建一套用例集合（字段码 + 名称）；用例在「测试项」里挂到该字段码下。"""
    get_or_404(db, models.Process, payload.process_id, "process")
    _assert_station_in_process(db, payload.process_id, payload.station_id)
    _assert_baseline_unlocked(db, payload.process_id, payload.station_id)
    exists = (
        db.query(models.StationCaseSet)
        .filter(
            models.StationCaseSet.process_id == payload.process_id,
            models.StationCaseSet.station_id == payload.station_id,
            models.StationCaseSet.set_id == payload.set_id,
        )
        .first()
    )
    if exists:
        raise conflict_error(
            "case_set_already_exists",
            f"case_set_already_exists: {payload.station_id}/{payload.set_id}",
        )
    row = models.StationCaseSet(
        process_id=payload.process_id,
        station_id=payload.station_id,
        set_id=payload.set_id,
        set_name=payload.set_name or payload.set_id,
    )
    db.add(row)
    db.commit()
    return schemas.CaseSetOut(
        process_id=row.process_id,
        station_id=row.station_id,
        set_id=row.set_id,
        set_name=row.set_name,
        item_count=0,
    )

@router.delete("/case-sets", status_code=204, summary="删除工位用例集合(连带其用例清单)")
def delete_case_set(
    process_id: str = Query(..., min_length=1),
    station_id: str = Query(..., min_length=1),
    set_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    user=Depends(require_manager),
):
    """删除整套集合：该集合下的用例行一并清理（其他集合不受影响）。

    闸门：锁定态 409 baseline_locked（清单由基线固化）；该工位有 RUNNING 会话
    409 running_session_block（会话断点属于被删集合，出站会被漏测误杀）。
    """
    row = (
        db.query(models.StationCaseSet)
        .filter(
            models.StationCaseSet.process_id == process_id,
            models.StationCaseSet.station_id == station_id,
            models.StationCaseSet.set_id == set_id,
        )
        .first()
    )
    if row is None:
        raise not_found("case_set_not_found", f"case_set_not_found: {station_id}/{set_id}")
    _assert_baseline_unlocked(db, process_id, station_id)
    running = _running_sessions_on(db, process_id, station_id)
    if running:
        raise conflict_error(
            "running_session_block",
            f"running_session_block: {running} RUNNING session(s) on "
            f"{process_id}/{station_id}，删除集合前请先中止会话",
            data={"running_sessions": running},
        )
    db.query(models.StationItem).filter(
        models.StationItem.process_id == process_id,
        models.StationItem.station_id == station_id,
        models.StationItem.set_id == set_id,
    ).delete(synchronize_session=False)
    db.delete(row)
    db.commit()

# ==================== 工位基线（黄金样本快照锁） ====================
def _station_case_sets(db: Session, process_id: str, station_id: str) -> Dict[str, List[str]]:
    """该工位启用中的用例集合：{set_id: [用例ID（按执行序号）]}。"""
    rows = (
        db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == process_id,
            models.StationItem.station_id == station_id,
            models.StationItem.is_active.is_(True),
        )
        .order_by(
            models.StationItem.set_id,
            models.StationItem.sort_order.is_(None),
            models.StationItem.sort_order,
            models.StationItem.case_id,
        )
        .all()
    )
    sets: Dict[str, List[str]] = {}
    for row in rows:
        sets.setdefault(row.set_id, []).append(row.case_id)
    return sets


def _overwrite_set_with_draft(
    db: Session,
    process_id: str,
    station_id: str,
    set_id: str,
    case_ids: List[str],
    names: Dict[str, str],
) -> int:
    """把录制草稿**物理覆盖**到指定集合：草稿外的行删除，草稿内的行按序重建。

    锁定 = 发布承诺，因此这里不做可回滚的软停用：「锁定后这套集合的清单就是草稿」。
    返回被删除的用例条数。
    """
    _ensure_case_set(db, process_id, station_id, set_id)
    existing = {
        row.case_id: row
        for row in db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == process_id,
            models.StationItem.station_id == station_id,
            models.StationItem.set_id == set_id,
        )
        .all()
    }
    keep = set(case_ids)
    stale_ids = [row.item_id for cid, row in existing.items() if cid not in keep]
    if stale_ids:
        db.query(models.StationItem).filter(
            models.StationItem.item_id.in_(stale_ids)
        ).delete(synchronize_session=False)
    for idx, case_id in enumerate(case_ids, 1):
        row = existing.get(case_id)
        name = (names.get(case_id) or "").strip()
        if row is None:
            db.add(
                models.StationItem(
                    process_id=process_id,
                    station_id=station_id,
                    set_id=set_id,
                    case_id=case_id,
                    item_name=name or case_id,
                    is_mandatory=True,
                    is_active=True,
                    sort_order=idx * 10,
                )
            )
            continue
        row.item_name = name or row.item_name or case_id
        row.is_mandatory = True
        row.is_active = True
        row.sort_order = idx * 10
    return len(stale_ids)


@router.post("/baseline/lock", response_model=schemas.BaselineLockOut, summary="锁定工位基线（冻结全部用例集合并开启严格比对）")
def lock_baseline(payload: schemas.BaselineLockIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    """黄金样本快照锁：冻结该工位的**全部用例集合**，进站上报的用例序列必须与
    任意一套集合逐项一致（全列表含顺序，任何增删/改序进站即 400 case_order_mismatch）。

    来源（`source`）：
    - `items`（默认）：以现有启用中的各套集合为基线——规格书/Excel 导入后无需再让机台
      跑一遍（顺序即各集合当前 `sort_order`；停用项不进基线），不物化、不改清单
    - `draft`：把机台录制草稿**物理覆盖**到 `set_id` 指定的集合（该集合内草稿外的用例
      删除、草稿内的用例全部必测、步长 10 编号），其他集合原样保留——设备实证过的真实序列

    闸门：来源为空 400 baseline_empty；draft 未指定集合 400 set_required；该工位有
    运行中会话 409 running_session_block（新基线必测集合与运行中断点不同，出站会被
    漏测误杀）。解锁走 /baseline/unlock。
    """
    get_or_404(db, models.Process, payload.process_id, "process")
    _assert_station_in_process(db, payload.process_id, payload.station_id)
    ps = get_or_404(db, models.ProcessStation, (payload.process_id, payload.station_id), "process_station")
    if ps.baseline_locked:
        raise conflict_error("baseline_already_locked", f"baseline_already_locked: {payload.station_id}")

    source = (payload.source or "items").strip().lower()
    if source not in ("draft", "items"):
        raise bad_request("bad_source", "bad_source: source must be 'draft' or 'items'")

    set_id = (payload.set_id or "").strip()
    manifest: List[str] = []
    if source == "items":
        if not _station_case_sets(db, payload.process_id, payload.station_id):
            raise bad_request(
                "baseline_empty",
                f"baseline_empty: 工位 {payload.station_id} 没有启用中的用例集合"
                f"（source=items 需先导入清单）",
            )
    else:
        if not set_id:
            raise bad_request(
                "set_required",
                f"set_required: 草稿锁定须指定物化到的用例集合（工位 {payload.station_id}）",
            )
        manifest = [c for c in (ps.baseline_manifest or []) if c]
        if not manifest:
            raise bad_request(
                "baseline_empty",
                f"baseline_empty: 工位 {payload.station_id} 尚未捕获到用例序列草稿（无进站记录）",
            )
    running = _running_sessions_on(db, payload.process_id, payload.station_id)
    if running:
        raise conflict_error(
            "running_session_block",
            f"running_session_block: {running} RUNNING session(s) on "
            f"{payload.process_id}/{payload.station_id}，锁定基线前请先中止会话",
            data={"running_sessions": running},
        )

    removed = 0
    if source == "draft":
        # 物化 = 该集合内**物理覆盖**为草稿（锁定即发布承诺，草稿外的用例直接删除）
        removed = _overwrite_set_with_draft(
            db, payload.process_id, payload.station_id, set_id, manifest, payload.names or {}
        )
    ps.baseline_locked = True
    db.commit()

    sets = _station_case_sets(db, payload.process_id, payload.station_id)
    warnings = ["新增必测不追溯已盖章在制品：如需按新基线补测，请对在制品做维修处置"]
    if source == "draft":
        warnings.append(f"草稿已物化为集合 {set_id}：进站须与该集合序列完全一致")
        if removed:
            warnings.append(f"已删除该集合内草稿外的 {removed} 条用例（锁定即覆盖，不可恢复）")
    else:
        warnings.append("本次基线来源为导入清单（未经机台录制实证）：建议上线前用锁定基线跑一遍确认")
    return schemas.BaselineLockOut(
        locked=True,
        items_count=sum(len(ids) for ids in sets.values()),
        source=source,
        set_count=len(sets),
        warnings=warnings,
    )


@router.post("/baseline/unlock", response_model=schemas.BaselineLockOut, summary="解锁工位基线（回到录制态）")
def unlock_baseline(payload: schemas.BaselineLockIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    """解锁后回到录制态：下次进站将以上位机当前执行序列覆盖基线草稿，
    用例集合恢复可手工编辑。用于"改代码 → 重录 → 再锁定"的发版循环。"""
    get_or_404(db, models.Process, payload.process_id, "process")
    _assert_station_in_process(db, payload.process_id, payload.station_id)
    ps = get_or_404(db, models.ProcessStation, (payload.process_id, payload.station_id), "process_station")
    if not ps.baseline_locked:
        raise conflict_error("baseline_not_locked", f"baseline_not_locked: {payload.station_id}")
    ps.baseline_locked = False
    db.commit()
    sets = _station_case_sets(db, payload.process_id, payload.station_id)
    return schemas.BaselineLockOut(
        locked=False,
        items_count=sum(len(ids) for ids in sets.values()),
        set_count=len(sets),
        warnings=["已回到录制态：下次进站将以上位机当前执行序列覆盖基线草稿"],
    )

# ==================== 校验与克隆 ====================
@router.get("/validate", response_model=schemas.ValidateOut, summary="校验流程拓扑(成环/悬空依赖/无测试项)")
def validate(
    process_id: str = Query(..., min_length=1), db: Session = Depends(get_db), user=Depends(current_user)
):
    return validate_process(db, process_id)

@router.post("/clone", response_model=schemas.CloneOut, summary="克隆流程(派生机型时复用拓扑与测试项)")
def clone_process(payload: schemas.CloneIn, db: Session = Depends(get_db), user=Depends(require_manager)):
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

    if db.get(models.Process, payload.to_process) is not None:
        raise conflict_error(
            "process_already_exists",
            f"process_already_exists: {payload.to_process}（目标流程已存在，请换编号）",
        )
    source = db.get(models.Process, payload.from_process)
    db.add(
        models.Process(
            process_id=payload.to_process,
            process_name=f"{source.process_name} (副本)" if source else payload.to_process,
            # 语义随源流程复制：并行分支流程的克隆不应被默认开启严格顺序
            is_active=source.is_active if source else True,
            is_sequential=source.is_sequential if source else True,
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
                is_optional=step.is_optional,
                invalidates_downstream=step.invalidates_downstream,
            )
        )
    for item in items:
        db.merge(
            models.StationItem(
                process_id=payload.to_process,
                station_id=item.station_id,
                set_id=item.set_id,
                case_id=item.case_id,
                item_name=item.item_name,
                is_mandatory=item.is_mandatory,
                is_active=item.is_active,
                sort_order=item.sort_order,
            )
        )
    # 用例集合（字段码 + 名称）随流程复制
    for cs in (
        db.query(models.StationCaseSet)
        .filter(models.StationCaseSet.process_id == payload.from_process)
        .all()
    ):
        db.add(
            models.StationCaseSet(
                process_id=payload.to_process,
                station_id=cs.station_id,
                set_id=cs.set_id,
                set_name=cs.set_name,
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

# ==================== 流程导入 / 导出 ====================
def _build_process_export(db: Session, process: models.Process) -> schemas.ProcessExportOut:
    """组装单个流程的完整定义（流程 + 引用工位 + 机型 + 工步 + 用例）。"""
    topo = process_topology(db, process.process_id)
    stations = (
        db.query(models.Station)
        .filter(models.Station.station_id.in_([s.station_id for s in topo.steps] or [""]))
        .all()
    )
    models_rows = (
        db.query(models.ProductModel)
        .filter(models.ProductModel.process_id == process.process_id)
        .all()
    )
    return schemas.ProcessExportOut(
        exported_at=utcnow(),
        process=schemas.ExportProcess(
            process_id=process.process_id,
            process_name=process.process_name or "",
            is_active=process.is_active,
            is_sequential=process.is_sequential,
        ),
        stations=[
            schemas.ExportStation(
                station_id=s.station_id,
                station_name=s.station_name or "",
                timeout_sec=s.timeout_sec,
            )
            for s in stations
        ],
        models=[
            schemas.ExportModel(
                product_model=m.product_model,
                target_fw_version=m.target_fw_version,
                fw_match_rule=m.fw_match_rule,
                target_app_version=m.target_app_version,
                app_match_rule=m.app_match_rule,
                passthrough=bool(m.passthrough),
            )
            for m in models_rows
        ],
        case_sets=[
            schemas.ExportCaseSet(
                station_id=row.station_id,
                set_id=row.set_id,
                set_name=row.set_name or "",
            )
            for row in db.query(models.StationCaseSet)
            .filter(models.StationCaseSet.process_id == process.process_id)
            .order_by(models.StationCaseSet.station_id, models.StationCaseSet.set_id)
            .all()
        ],
        steps=[
            schemas.ExportStep(
                station_id=s.station_id,
                step_order=s.step_order,
                depends_on=list(s.depends_on or []),
                is_optional=bool(s.is_optional),
                invalidates_downstream=bool(s.invalidates_downstream),
            )
            for s in topo.steps
        ],
        items=[
            schemas.ExportItem(
                station_id=i.station_id,
                set_id=i.set_id,
                case_id=i.case_id,
                item_name=i.item_name or "",
                is_mandatory=i.is_mandatory,
                is_active=bool(i.is_active),
                sort_order=i.sort_order,
            )
            for i in topo.items
        ],
    )

@router.get(
    "/processes/{process_id}/export",
    response_model=schemas.ProcessExportOut,
    summary="导出流程完整定义（流程+引用工位+机型+工步+用例，JSON）",
)
def export_process(
    process_id: str, db: Session = Depends(get_db), user=Depends(current_user)
):
    process = get_or_404(db, models.Process, process_id, "process")
    return _build_process_export(db, process)

@router.get(
    "/processes/export-all",
    response_model=schemas.ProcessExportAllOut,
    summary="导出全部流程定义（每项可单独导入）",
)
def export_all_processes(db: Session = Depends(get_db), user=Depends(current_user)):
    processes = db.query(models.Process).order_by(models.Process.process_id).all()
    return schemas.ProcessExportAllOut(
        exported_at=utcnow(),
        processes=[_build_process_export(db, p) for p in processes],
    )

@router.post(
    "/processes/import",
    response_model=schemas.ProcessImportResult,
    status_code=201,
    summary="导入流程完整定义（JSON）",
)
def import_process(payload: schemas.ProcessImportIn, db: Session = Depends(get_db), user=Depends(require_manager)):
    """事务式导入：流程不存在（409 可改编号重试）；缺失工位自动补建（已有配置
    不覆盖——工位是跨流程共享字典）；机型已存在时跳过（绑定关系不迁移）；
    用例按导出的 set_id 归入各自的用例集合，执行顺序保留文件携带的 sort_order，
    未携带时按同集合内的导入顺序以步长 10 自动编号；全部写入后复用拓扑校验，
    error 级问题整体回滚。"""
    proc_in = payload.process
    if db.get(models.Process, proc_in.process_id):
        raise conflict_error(
            "process_already_exists",
            f"process_already_exists: {proc_in.process_id}（可修改编号后作为副本导入）",
        )
    if not payload.steps:
        raise bad_request("process_empty", "导入文件未包含任何工步")

    step_ids = {s.station_id for s in payload.steps}
    bad_deps = sorted({d for s in payload.steps for d in s.depends_on if d not in step_ids})
    if bad_deps:
        raise bad_request(
            "topology_invalid",
            f"depends_on 引用了流程外的工位: {', '.join(bad_deps)}",
        )

    db.add(
        models.Process(
            process_id=proc_in.process_id,
            process_name=proc_in.process_name or proc_in.process_id,
            is_active=proc_in.is_active,
            is_sequential=proc_in.is_sequential,
        )
    )

    stations_created = 0
    station_ids = {s.station_id for s in payload.stations} | step_ids
    for sid in sorted(station_ids):
        if db.get(models.Station, sid) is not None:
            continue
        src = next((s for s in payload.stations if s.station_id == sid), None)
        db.add(
            models.Station(
                station_id=sid,
                station_name=(src.station_name if src else "") or sid,
                timeout_sec=src.timeout_sec if src else 1800,
            )
        )
        stations_created += 1

    # 3) 机型：不存在才注册；已存在（绑定其他流程）→ 跳过，绑定关系不迁移
    models_created = models_skipped = 0
    for m in payload.models:
        if db.get(models.ProductModel, m.product_model):
            models_skipped += 1
            continue
        db.add(
            models.ProductModel(
                product_model=m.product_model,
                process_id=proc_in.process_id,
                target_fw_version=m.target_fw_version,
                fw_match_rule=m.fw_match_rule,
                target_app_version=m.target_app_version,
                app_match_rule=m.app_match_rule,
                passthrough=bool(m.passthrough),
            )
        )
        models_created += 1

    for s in payload.steps:
        db.add(
            models.ProcessStation(
                process_id=proc_in.process_id,
                station_id=s.station_id,
                step_order=s.step_order,
                depends_on=list(s.depends_on or []),
                is_optional=bool(s.is_optional),
                invalidates_downstream=s.invalidates_downstream,
            )
        )
    # 4) 用例集合主数据：以导出文件里的名称为准；文件没带而用例里出现的按字段码兜底
    set_names = {
        (cs.station_id, cs.set_id): cs.set_name for cs in payload.case_sets
    }
    sets_written = set()
    for it in payload.items:
        key = (it.station_id, it.set_id)
        if key in sets_written:
            continue
        sets_written.add(key)
        db.add(
            models.StationCaseSet(
                process_id=proc_in.process_id,
                station_id=it.station_id,
                set_id=it.set_id,
                set_name=set_names.get(key) or it.set_id,
            )
        )
    for key, name in set_names.items():
        if key in sets_written:
            continue
        db.add(
            models.StationCaseSet(
                process_id=proc_in.process_id,
                station_id=key[0],
                set_id=key[1],
                set_name=name or key[1],
            )
        )

    items_created = 0
    # 执行序号按 (工位, 集合) 独立续排：多套集合并存时互不干扰
    set_seq: Dict[tuple, int] = {}
    for it in payload.items:
        key = (it.station_id, it.set_id)
        if it.sort_order is not None:
            seq = it.sort_order
        else:
            seq = set_seq.get(key, 0) + 10
        set_seq[key] = max(set_seq.get(key, 0), seq)
        db.add(
            models.StationItem(
                process_id=proc_in.process_id,
                station_id=it.station_id,
                set_id=it.set_id,
                case_id=it.case_id,
                item_name=it.item_name or it.case_id,
                is_mandatory=it.is_mandatory,
                is_active=bool(it.is_active),
                sort_order=seq,
            )
        )
        items_created += 1

    # 6) 复用拓扑校验兜底：先 flush 让新行在本事务内可见，error 级问题整体回滚
    db.flush()
    check = validate_process(db, proc_in.process_id)
    if not check.ok:
        db.rollback()
        raise bad_request(
            "topology_invalid",
            f"导入的拓扑未通过校验: {[(i.level, i.detail) for i in check.issues]}",
        )

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise bad_request("import_failed", "导入写入失败：数据与现有配置冲突（检查用例ID是否重复）")

    return schemas.ProcessImportResult(
        process_id=proc_in.process_id,
        stations_created=stations_created,
        models_created=models_created,
        models_skipped=models_skipped,
        steps_created=len(payload.steps),
        items_created=items_created,
    )

