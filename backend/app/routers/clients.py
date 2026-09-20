"""通道二：物理机台档案与工位绑定（/api/admin/clients）。"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import bad_request, conflict_error, get_or_404
from ..security import current_user, require_operator
from ..services.timeutil import is_client_online

router = APIRouter(prefix="/api/admin/clients", tags=["admin-机台"])

def _assert_binding_unambiguous(db: Session, station_ids: List[str]) -> None:
    """同一流程内绑定的工位必须 ≤ 1 个。

    进站按"件所属流程 ∩ bound_stations"解析唯一工位：同流程命中两个会让该
    流程的所有件进站即 400 station_ambiguous。把校验前移到配置层，保存绑定
    时就拒绝；进站处的运行时校验保留，兜底"绑定后流程拓扑又改了"的漂移场景
    （保存时合法、之后同流程新增了已绑定工位）。
    """
    if len(set(station_ids)) < 2:
        return
    rows = (
        db.query(models.ProcessStation.process_id, models.ProcessStation.station_id)
        .filter(models.ProcessStation.station_id.in_(list(set(station_ids))))
        .all()
    )
    by_process: Dict[str, List[str]] = {}
    for process_id, sid in rows:
        by_process.setdefault(process_id, []).append(sid)
    conflicts = {pid: sns for pid, sns in by_process.items() if len(sns) > 1}
    if conflicts:
        detail = "; ".join(f"{pid}: {', '.join(sorted(sns))}" for pid, sns in sorted(conflicts.items()))
        raise bad_request(
            "station_ambiguous",
            f"station_ambiguous: one station per process allowed, conflicts - {detail}",
        )

def _holding_sn(db: Session, client_id: str) -> Optional[str]:
    """该机台当前持有的在制品 SN（TESTING 且持锁方是它自己）。

    正常约束下一台机台只持有一把工位锁；但存量/演示数据可能出现一台机台对应
    多个 TESTING 件（如 seed 场景里 C099001~C099003 同属 SZ-L1-CAL-01）。
    原先这里用 .scalar()，结果多于一行时会抛 MultipleResultsFound → 机台清单
    按该工位筛选直接 500；改为按 lock_acquired_at 取最近一把，确定性且不再抛错。
    """
    row = (
        db.query(models.ProductStatus.sn)
        .filter(
            models.ProductStatus.current_status == models.STATUS_TESTING,
            models.ProductStatus.current_client == client_id,
        )
        .order_by(models.ProductStatus.lock_acquired_at.desc())
        .first()
    )
    return row[0] if row else None

def _client_view(db: Session, client: models.StationClient) -> schemas.ClientOut:
    view = schemas.ClientOut.model_validate(client)
    view.online = is_client_online(client.last_seen_at)
    view.holding_sn = _holding_sn(db, client.client_id)
    return view

def _assert_not_holding(db: Session, client_id: str) -> None:
    holding = _holding_sn(db, client_id)
    if holding:
        raise conflict_error(
            "client_holding_lock", f"client_holding_lock: {client_id} still holds {holding}"
        )

@router.get("", response_model=List[schemas.ClientOut], summary="机台清单(在线状态 + 持锁 SN)")
def list_clients(
    station_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    clients = db.query(models.StationClient).order_by(models.StationClient.client_id).all()
    if station_id:
        clients = [c for c in clients if station_id in (c.bound_stations or [])]
    return [_client_view(db, c) for c in clients]

@router.post("", response_model=schemas.ClientOut, status_code=201, summary="注册机台")
def create_client(payload: schemas.ClientCreateIn, db: Session = Depends(get_db), user=Depends(require_operator)):
    if db.get(models.StationClient, payload.client_id):
        raise conflict_error("client_already_registered", f"client_already_registered: {payload.client_id}")
    for sid in payload.bound_stations:
        get_or_404(db, models.Station, sid, "station")
    _assert_binding_unambiguous(db, payload.bound_stations)
    data = payload.model_dump()
    # 未填名称时回退为 client_id，保证列表永远有可读内容（同 stations.station_name）
    data["client_name"] = payload.client_name or payload.client_id
    client = models.StationClient(**data)
    db.add(client)
    db.commit()
    return _client_view(db, client)

@router.put("/{client_id}", response_model=schemas.ClientOut, summary="更新机台(改绑工位/IP)")
def update_client(
    client_id: str, payload: schemas.ClientUpdateIn, db: Session = Depends(get_db), user=Depends(require_operator)
):
    client = get_or_404(db, models.StationClient, client_id, "client")
    if payload.bound_stations is not None:
        for sid in payload.bound_stations:
            get_or_404(db, models.Station, sid, "station")
        _assert_binding_unambiguous(db, payload.bound_stations)
        if set(payload.bound_stations) != set(client.bound_stations or []):
            _assert_not_holding(db, client_id)
        client.bound_stations = payload.bound_stations
        if client.station_id and client.station_id not in client.bound_stations:
            client.station_id = None
    if payload.client_name is not None:
        client.client_name = payload.client_name or None
    if payload.ip_address is not None:
        client.ip_address = payload.ip_address
    db.commit()
    return _client_view(db, client)

@router.delete("/{client_id}", status_code=204, summary="注销机台(持锁时拒绝)")
def delete_client(client_id: str, db: Session = Depends(get_db), user=Depends(require_operator)):
    client = get_or_404(db, models.StationClient, client_id, "client")
    _assert_not_holding(db, client_id)
    db.delete(client)
    db.commit()

