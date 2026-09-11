"""通道二：物理机台档案与工位绑定（/api/admin/clients）。"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import conflict_error, get_or_404
from ..security import current_user
from ..services.timeutil import is_client_online

router = APIRouter(prefix="/api/admin/clients", tags=["admin-机台"])


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
    query = db.query(models.StationClient)
    if station_id:
        query = query.filter(models.StationClient.station_id == station_id)
    return [_client_view(db, c) for c in query.order_by(models.StationClient.client_id).all()]


@router.post("", response_model=schemas.ClientOut, status_code=201, summary="注册机台")
def create_client(payload: schemas.ClientCreateIn, db: Session = Depends(get_db), user=Depends(current_user)):
    if db.get(models.StationClient, payload.client_id):
        raise conflict_error("client_already_registered", f"client_already_registered: {payload.client_id}")
    get_or_404(db, models.Station, payload.station_id, "station")
    data = payload.model_dump()
    # 未填名称时回退为 client_id，保证列表永远有可读内容（同 stations.station_name）
    data["client_name"] = payload.client_name or payload.client_id
    client = models.StationClient(**data)
    db.add(client)
    db.commit()
    return _client_view(db, client)


@router.put("/{client_id}", response_model=schemas.ClientOut, summary="更新机台(改绑工位/IP)")
def update_client(
    client_id: str, payload: schemas.ClientUpdateIn, db: Session = Depends(get_db), user=Depends(current_user)
):
    client = get_or_404(db, models.StationClient, client_id, "client")
    if payload.station_id is not None:
        if payload.station_id:
            get_or_404(db, models.Station, payload.station_id, "station")
            if client.station_id != payload.station_id:
                _assert_not_holding(db, client_id)
            client.station_id = payload.station_id
        else:
            # 显式传空 = 解除工位绑定。自动注册的机台本就处于未绑定态，
            # 若不允许解绑，这类机台连改 IP 都提交不了。
            if client.station_id:
                _assert_not_holding(db, client_id)
            client.station_id = None
    if payload.client_name is not None:
        client.client_name = payload.client_name or None
    if payload.ip_address is not None:
        client.ip_address = payload.ip_address
    db.commit()
    return _client_view(db, client)


@router.delete("/{client_id}", status_code=204, summary="注销机台(持锁时拒绝)")
def delete_client(client_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    client = get_or_404(db, models.StationClient, client_id, "client")
    _assert_not_holding(db, client_id)
    db.delete(client)
    db.commit()
