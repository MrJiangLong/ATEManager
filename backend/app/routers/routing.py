"""通道二：工艺拓扑编排与用例ID测试项（/api/admin/routing）。

    process_stations  工步拓扑（step_order + depends_on → 防跳站闸门）
    station_items     用例ID静态清单（防漏测依据）
"""

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import bad_request, conflict_error, get_or_404, not_found
from ..security import current_user
from ..services.routing import process_topology, station_item_summary, validate_process

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


# 保存前阻断的结构性问题：这类错误一旦落库，流程会直接不可用，且无法靠后续补配置挽回。
# 刻意不含 station_no_item —— 先编排拓扑、后补测试项是正常操作顺序，一并阻断会形成
# "没测试项不让存拓扑 → 没拓扑不让加测试项"的死锁。
_BLOCKING_SAVE_CODES = frozenset({"config_cycle", "station_undefined"})


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
    force: bool = Query(False, description="跳过结构性校验强行保存"),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """以「整体覆盖」方式保存拓扑，前端一屏编排后一次性提交。

    被移出拓扑的工位，其测试项会一并清理 —— 与 DELETE /stations/{station_id} 的行为
    保持一致。否则 station_items 会残留成"没人跑却仍计数的必测项"。

    保存后会做结构性校验（成环等），不通过则整体回滚；成功则流程 version +1。
    """
    process = get_or_404(db, models.Process, process_id, "process")
    for step in payload:
        get_or_404(db, models.Station, step.station_id, "station")

    keep = {step.station_id for step in payload}
    removed = {
        row[0]
        for row in db.query(models.ProcessStation.station_id)
        .filter(models.ProcessStation.process_id == process_id)
        .all()
    } - keep
    if removed:
        db.query(models.StationItem).filter(
            models.StationItem.process_id == process_id,
            models.StationItem.station_id.in_(removed),
        ).delete(synchronize_session=False)

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

    process.version = (process.version or 0) + 1
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
    _assert_station_in_process(db, payload.process_id, payload.station_id)
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


@router.post("/items/import", response_model=schemas.StationItemImportOut, summary="批量导入用例ID清单(幂等upsert)")
def import_items(
    payload: schemas.StationItemImportIn, db: Session = Depends(get_db), user=Depends(current_user)
):
    """整批同步某工位的用例ID清单，供上位机脚本自动对齐（pytest --collect-only 的输出）。

    匹配键 (process_id, station_id, case_id) 与唯一约束一致，故重复导入幂等：
    第二次执行全部 unchanged，不会产生重复行，也不会因已存在而报 409。
    """
    get_or_404(db, models.Process, payload.process_id, "process")
    _assert_station_in_process(db, payload.process_id, payload.station_id)
    if payload.mode not in ("upsert", "replace"):
        raise bad_request("bad_mode", "bad_mode: mode must be 'upsert' or 'replace'")

    existing = {
        row.case_id: row
        for row in db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == payload.process_id,
            models.StationItem.station_id == payload.station_id,
        )
        .all()
    }

    warnings: List[str] = []
    seen = set()
    created = updated = unchanged = 0
    for row_in in payload.items:
        case_id = row_in.case_id
        if not case_id:
            continue
        if case_id in seen:
            warnings.append(f"duplicate case_id in payload: {case_id}")
            continue
        seen.add(case_id)

        row = existing.get(case_id)
        # 未传 item_name 时：新增用 case_id 兜底，已存在的保留原名（不把中文名抹成 nodeid）
        name = row_in.item_name or (row.item_name if row is not None else case_id)
        if row is None:
            row = models.StationItem(
                process_id=payload.process_id,
                station_id=payload.station_id,
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
            row.is_active = True  # 重新出现在清单里 → 自动复活
            changed = True
        updated += 1 if changed else 0
        unchanged += 1 if not changed else 0

    # 清单外仍启用的项：upsert 只报告（保持必测），replace 才停用
    orphans = sorted(
        case_id for case_id, row in existing.items() if case_id not in seen and row.is_active
    )
    deactivated = 0
    if payload.mode == "replace":
        for case_id in orphans:
            existing[case_id].is_active = False
            deactivated += 1

    total_active = sum(1 for r in existing.values() if r.is_active)

    if payload.dry_run:
        db.rollback()
    else:
        db.commit()

    return schemas.StationItemImportOut(
        process_id=str(payload.process_id),
        station_id=str(payload.station_id),
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
