"""通道二：测试会话（/api/admin/sessions）—— 续测断点与锁接管的可视化。"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..errors import bad_request, get_or_404
from ..security import current_user
from ..services import force_release_lock
from ..services.gate import _close_session
from ..services.timeutil import as_utc, elapsed_int

router = APIRouter(prefix="/api/admin/sessions", tags=["admin-测试会话"])


def _session_view(row: models.TestSession) -> schemas.SessionOut:
    view = schemas.SessionOut.model_validate(row)
    payload = row.checkpoint if isinstance(row.checkpoint, dict) else {}
    items = [it for it in (payload.get("items") or []) if isinstance(it, dict)]
    view.item_count = len(items)
    view.cursor = payload.get("cursor") or {}
    view.items = [
        schemas.SessionItemOut(
            case_id=it.get("case_id") or "",
            result=it.get("result") or "",
            message=it.get("message"),
            duration_ms=int(it.get("duration_ms") or 0),
            seq=int(it.get("seq") or 0),
        )
        for it in items
    ]
    if row.status == models.SESSION_RUNNING:
        view.lock_held_sec = elapsed_int(row.started_at)
        view.lock_idle_sec = elapsed_int(row.last_heartbeat_at or row.started_at)
    else:
        started = as_utc(row.started_at)
        ended = as_utc(row.ended_at)
        view.lock_held_sec = (
            round((ended - started).total_seconds()) if started and ended else 0
        )
        view.lock_idle_sec = -1
    return view


@router.get("", response_model=schemas.SessionPageOut, summary="测试会话清单(含续测断点)")
def list_sessions(
    sn: Optional[str] = None,
    station_id: Optional[str] = None,
    client_id: Optional[str] = None,
    status: Optional[str] = None,
    abnormal_only: bool = Query(False, description="仅看异常终止（失联/超时/被接管）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.TestSession)
    if sn:
        query = query.filter(models.TestSession.sn.ilike(f"%{sn}%"))
    if station_id:
        query = query.filter(models.TestSession.station_id == station_id)
    if client_id:
        query = query.filter(models.TestSession.client_id == client_id)
    if abnormal_only:
        query = query.filter(models.TestSession.status.in_(models.ABNORMAL_SESSION_STATUSES))
    elif status:
        query = query.filter(models.TestSession.status == status.upper())

    total = query.count()
    rows = (
        query.order_by(models.TestSession.started_at.desc().nullslast())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return schemas.SessionPageOut(
        total=total, page=page, page_size=page_size, items=[_session_view(r) for r in rows]
    )


@router.get("/zombie-locks", response_model=schemas.SessionPageOut, summary="失联僵尸锁清单")
def zombie_locks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """运行中但心跳已断流超过宽限窗口的会话：可被接管或强制解锁。"""
    cutoff = settings.LOCK_HEARTBEAT_GRACE_SEC
    rows = (
        db.query(models.TestSession)
        .filter(models.TestSession.status == models.SESSION_RUNNING)
        .order_by(models.TestSession.started_at.desc())
        .all()
    )
    zombies = [r for r in rows if (elapsed_int(r.last_heartbeat_at or r.started_at) > cutoff)]
    start = (page - 1) * page_size
    return schemas.SessionPageOut(
        total=len(zombies),
        page=page,
        page_size=page_size,
        items=[_session_view(r) for r in zombies[start : start + page_size]],
    )


@router.get("/{session_id}", response_model=schemas.SessionOut, summary="会话详情(含断点明细)")
def get_session(session_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    row = get_or_404(db, models.TestSession, session_id, "session")
    return _session_view(row)


@router.post("/{session_id}/abort", response_model=schemas.SessionOut, summary="强制终止会话并解锁")
def abort_session(
    session_id: str,
    payload: Optional[schemas.SessionAbortIn] = None,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """终止运行中的会话：关闭会话 + 释放其持有的工位锁（不改变印章/失败计数）。"""
    row = get_or_404(db, models.TestSession, session_id, "session")
    if row.status == models.SESSION_COMPLETED:
        raise bad_request("session_completed", f"session_completed: {session_id}")

    reason = (payload.reason if payload else None) or "aborted by operator"
    product = db.get(models.ProductStatus, row.sn)
    if (
        product is not None
        and product.current_status == models.STATUS_TESTING
        and product.current_client == row.client_id
    ):
        force_release_lock(db, sn=row.sn, reason=reason, operator=user.username)
    else:
        _close_session(row, models.SESSION_ABORTED, reason, user.username)
        db.commit()

    db.refresh(row)
    return _session_view(row)


@router.post("/abort-running", response_model=schemas.SessionAbortRunningOut, summary="批量中止运行中的会话")
def abort_running(
    payload: schemas.SessionAbortRunningIn,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """换测试用例清单前"先停再换"：批量关闭 RUNNING 会话并释放工位锁。

    与单点 abort 一样走 force_release_lock —— 只关会话 + 放锁，不改印章、
    不计失败（这点与 missing_mandatory 完全不同：后者会计一次失败）。
    被中止的件回到 IDLE 且未盖章，需重新进站跑一遍。
    """
    filters = [payload.station_id, payload.process_id, payload.sn, payload.client_id]
    if not any(filters):
        raise bad_request(
            "scope_required",
            "scope_required: give at least one of station_id / process_id / sn / client_id "
            "(aborting everything by accident is too costly)",
        )

    query = db.query(models.TestSession).filter(models.TestSession.status == models.SESSION_RUNNING)
    if payload.station_id:
        query = query.filter(models.TestSession.station_id == payload.station_id)
    if payload.sn:
        query = query.filter(models.TestSession.sn == payload.sn)
    if payload.client_id:
        query = query.filter(models.TestSession.client_id == payload.client_id)
    if payload.process_id:
        models_in = [
            m.product_model
            for m in db.query(models.ProductModel)
            .filter(models.ProductModel.process_id == payload.process_id)
            .all()
        ]
        sns = [
            p.sn
            for p in db.query(models.ProductStatus)
            .filter(models.ProductStatus.product_model.in_(models_in or [""]))
            .all()
        ]
        query = query.filter(models.TestSession.sn.in_(sns or [""]))

    rows = query.all()
    points = [
        schemas.AbortedSessionPoint(
            session_id=r.session_id, sn=r.sn, station_id=r.station_id, client_id=r.client_id
        )
        for r in rows
    ]
    if payload.dry_run:
        return schemas.SessionAbortRunningOut(
            aborted=len(points), dry_run=True, items=points
        )

    reason = payload.reason or "aborted before case-list sync"
    for row in rows:
        product = db.get(models.ProductStatus, row.sn)
        if (
            product is not None
            and product.current_status == models.STATUS_TESTING
            and product.current_client == row.client_id
        ):
            force_release_lock(db, sn=row.sn, reason=reason, operator=user.username)
        else:
            _close_session(row, models.SESSION_ABORTED, reason, user.username)
            db.commit()

    return schemas.SessionAbortRunningOut(aborted=len(points), items=points)