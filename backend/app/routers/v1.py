"""通道一：上位机 pytest 自动化 API（/api/v1/*，X-API-Key 鉴权）。

调用顺序
    1. resolve     机台身份上报 + 工位反查（首次自动注册）
    2. check-in    进站：`*IDN?` 直读结果建档 + 跳站/复测/固件/用例ID 卡控
                   返回 session_id / lock_token；失联或硬超时的锁可被接管（takeover=true）
    3. heartbeat   保活：只刷新 lock_last_seen_at，硬超时不续期
    4. checkpoint  续测断点：增量上报已完成用例（按 case_id 幂等覆盖）
    5. check-out   出站：checkpoint 补齐 + 漏测拦截 + 落库并返回 ACK（需求 1）
    6. ack         上传闭环校验：网络抖动后确认服务端已落库
    7. release     主动放弃锁（优雅退出 / 放弃本次测试）

崩溃续测
    上位机重启后 check-in 携带 resume_session_id（同机台亦可自动续），
    服务端 attempt+1 并换发新 lock_token，回传 resume.completed_case_ids，
    上位机跳过已完成用例；出站时服务端用 checkpoint 补齐，避免漏测误判。

卡控语义（HTTP 状态码即契约）
    200 放行 / 201 落库成功
    400 工位不在该流程、用例ID清单不匹配、漏测拦截
    403 跳站拦截、固件不符、已锁定、已报废、锁失效（含被接管的旧 token 写入）
    409 复测拦截、锁冲突
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import EXIT_FAIL, EXIT_OK, not_found
from ..security import api_caller
from ..services import (
    check_in,
    check_out,
    get_client,
    heartbeat,
    release_lock,
    save_checkpoint,
    touch_client,
)
from ..services.timeutil import as_utc, utcnow

router = APIRouter(prefix="/api/v1", tags=["v1-上位机"])

MESSAGES = {
    "ok.checkin": "Check-in accepted",
    "ok.checkout": "Check-out acknowledged",
    "ok.heartbeat": "Heartbeat accepted",
    "ok.checkpoint": "Checkpoint accepted",
    "ok.release": "Lock released",
    "ok.resolve": "Client resolved",
}


def _ok(code: str, data, exit_code: int = EXIT_OK) -> schemas.EnvelopeOut:
    return schemas.EnvelopeOut(
        ok=exit_code == EXIT_OK,
        exit_code=exit_code,
        code=code,
        message=MESSAGES.get(code, code),
        data=data,
    )


@router.post("/client/resolve", response_model=schemas.EnvelopeOut, summary="机台身份上报与工位反查")
def resolve(
    payload: schemas.ClientResolveIn,
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    client = get_client(
        db, payload.client_id, ip=payload.ip_address, app_version=payload.app_version, create=True
    )
    if client is None:
        raise not_found("client_not_registered", f"client_not_registered: {payload.client_id}")

    touch_client(db, payload.client_id)

    return _ok(
        "ok.resolve",
        {
            "client_id": client.client_id,
            "bound_stations": list(client.bound_stations or []),
            "station_id": client.station_id or None,
            "ip_address": client.ip_address,
            "bound": bool(client.bound_stations),
            "last_seen_at": as_utc(client.last_seen_at).isoformat() if client.last_seen_at else None,
            "server_time": utcnow().isoformat(),
        },
    )


@router.post("/client/check-in", response_model=schemas.EnvelopeOut, summary="进站：防跳站/防复测/固件/用例ID 卡控")
def check_in_endpoint(
    payload: schemas.CheckInIn,
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    client = get_client(db, payload.client_id, app_version=payload.app_version)
    if client is None:
        raise not_found("client_not_registered", f"client_not_registered: {payload.client_id}")

    data = check_in(
        db,
        client=client,
        sn=payload.sn,
        product_model=payload.product_model,
        firmware=payload.firmware,
        case_ids=payload.case_ids or None,
        resume_session_id=payload.resume_session_id or None,
    )
    return _ok("ok.checkin", data.model_dump())


@router.post("/client/heartbeat", response_model=schemas.EnvelopeOut, summary="心跳保活：续期工位锁")
def heartbeat_endpoint(
    payload: schemas.HeartbeatIn,
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    client = get_client(db, payload.client_id)
    if client is None:
        raise not_found("client_not_registered", f"client_not_registered: {payload.client_id}")
    data = heartbeat(db, client=client, sn=payload.sn, lock_token=payload.lock_token or None)
    return _ok("ok.heartbeat", data.model_dump())


@router.post("/client/checkpoint", response_model=schemas.EnvelopeOut, summary="续测断点：增量上报已完成用例")
def checkpoint_endpoint(
    payload: schemas.CheckpointIn,
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    """崩溃续测核心：按 case_id 去重覆盖，重复上报幂等。

    上报失败不应阻断测试——上位机本地落盘，恢复后批量补传即可。
    """
    client = get_client(db, payload.client_id)
    if client is None:
        raise not_found("client_not_registered", f"client_not_registered: {payload.client_id}")

    data = save_checkpoint(
        db,
        client=client,
        sn=payload.sn,
        session_id=payload.session_id,
        items=[item.model_dump(exclude_none=True) for item in payload.items],
        cursor=payload.cursor or None,
        lock_token=payload.lock_token or None,
    )
    return _ok("ok.checkpoint", data.model_dump())


@router.post("/client/release", response_model=schemas.EnvelopeOut, summary="主动放弃工位锁")
def release_endpoint(
    payload: schemas.ReleaseIn,
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    client = get_client(db, payload.client_id)
    if client is None:
        raise not_found("client_not_registered", f"client_not_registered: {payload.client_id}")
    data = release_lock(
        db,
        client=client,
        sn=payload.sn,
        lock_token=payload.lock_token or None,
        reason=payload.reason or "",
    )
    return _ok("ok.release", data.model_dump())


@router.post(
    "/client/check-out",
    response_model=schemas.EnvelopeOut,
    status_code=201,
    summary="出站：漏测拦截 + 落库 ACK（需求 1）",
)
def check_out_endpoint(
    payload: schemas.CheckOutIn,
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    client = get_client(db, payload.client_id)
    if client is None:
        raise not_found("client_not_registered", f"client_not_registered: {payload.client_id}")

    data = check_out(
        db,
        client=client,
        sn=payload.sn,
        checkout_id=payload.checkout_id,
        items=[item.model_dump(exclude_none=True) for item in payload.items],
        duration_ms=payload.duration_ms,
        reason=payload.reason or "",
        lock_token=payload.lock_token or None,
    )
    exit_code = EXIT_OK if data.overall_result == "PASS" else EXIT_FAIL
    return _ok("ok.checkout", data.model_dump(), exit_code)


@router.get("/client/ack", response_model=schemas.EnvelopeOut, summary="上传闭环校验：确认服务端已落库")
def ack(
    sn: str = Query(..., min_length=1),
    checkout_id: str = Query(..., min_length=1),
    _: str = Depends(api_caller),
    db: Session = Depends(get_db),
):
    """上位机在超时/断网重传后调用：命中即证明数据已落库，可放行流转。"""
    rows = (
        db.query(models.TestRecord)
        .filter(models.TestRecord.sn == sn)
        .order_by(models.TestRecord.record_id.desc())
        .limit(200)
        .all()
    )
    for record in rows:
        payload = record.executed_items if isinstance(record.executed_items, dict) else {}
        if payload.get("checkout_id") == checkout_id:
            return _ok(
                "ok.checkout",
                {
                    "acknowledged": True,
                    "record_id": record.record_id,
                    "sn": record.sn,
                    "station_id": record.station_id,
                    "overall_result": record.overall_result,
                    "created_at": as_utc(record.created_at).isoformat() if record.created_at else None,
                },
            )
    raise not_found("ack_not_found", f"ack_not_found: no record for checkout_id {checkout_id}")
