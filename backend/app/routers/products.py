"""通道二：在制品（/api/admin/products）—— 清单、详情、维修处置。"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import get_or_404
from ..security import current_user, require_operator
from ..services import force_release_lock
from ..services.routing import load_process
from ..services.views import build_product_out
from .sessions import _session_view

router = APIRouter(prefix="/api/admin/products", tags=["admin-在制品"])

# TESTING 行数 ≤ 在线机台数，故该上限仅是兜底，实际不会触达。
_DERIVED_SCAN_LIMIT = 5000

def _prefetch_models_and_graphs(db: Session):
    """预载全部机型与流程拓扑：派生字段依赖机型与拓扑，逐行装载会产生 N+1 查询。"""
    model_rows = db.query(models.ProductModel).all()
    models_by_name = {m.product_model: m for m in model_rows}
    graphs = {}
    for mr in model_rows:
        if mr.process_id not in graphs:
            graphs[mr.process_id] = load_process(db, mr.process_id)
    return models_by_name, graphs

def _build_view(db: Session, row, models_by_name, graphs):
    """用预载的机型/拓扑装配 ProductOut（不触发逐行查询）。"""
    mr = models_by_name.get(row.product_model)
    graph = graphs.get(mr.process_id) if mr is not None else None
    return build_product_out(db, row, graph=graph, model_row=mr, include_lock=True)

@router.get("", response_model=schemas.ProductPageOut, summary="在制品清单(多条件分页)")
def list_products(
    sn: Optional[str] = None,
    product_model: Optional[str] = None,
    current_status: Optional[str] = None,
    process_id: Optional[str] = None,
    zombie_only: bool = Query(False, description="仅看失联僵尸锁（心跳断流，可被接管）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.ProductStatus)
    if sn:
        query = query.filter(models.ProductStatus.sn.ilike(f"%{sn}%"))
    if product_model:
        query = query.filter(models.ProductStatus.product_model == product_model)
    status_filter = (current_status or "").upper()
    if current_status and status_filter not in ("IDLE", "COMPLETED"):
        query = query.filter(models.ProductStatus.current_status == status_filter)
    if process_id:
        models_in = [
            m.product_model
            for m in db.query(models.ProductModel)
            .filter(models.ProductModel.process_id == process_id)
            .all()
        ]
        query = query.filter(models.ProductStatus.product_model.in_(models_in or [""]))

    # 预载机型与拓扑：下面各分支的派生字段都依赖它
    models_by_name, graphs = _prefetch_models_and_graphs(db)

    # 故"待测试 / 已完工"必须按 is_completed 冗余列区分，否则两种状态混在一起。
    if status_filter in ("IDLE", "COMPLETED"):
        query = query.filter(
            models.ProductStatus.current_status == models.STATUS_IDLE,
            models.ProductStatus.is_completed == (status_filter == "COMPLETED"),
        )
        total = query.count()
        rows = (
            query.order_by(models.ProductStatus.updated_at.desc().nullslast())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return schemas.ProductPageOut(
            total=total,
            page=page,
            page_size=page_size,
            items=[_build_view(db, r, models_by_name, graphs) for r in rows],
        )

    # 没有可落库的字段（随时间自行变化，不能像 is_completed 那样冗余），
    if zombie_only:
        candidates = (
            query.filter(models.ProductStatus.current_status == models.STATUS_TESTING)
            .order_by(models.ProductStatus.updated_at.desc().nullslast())
            .limit(_DERIVED_SCAN_LIMIT)
            .all()
        )
        views = [
            v for v in (_build_view(db, r, models_by_name, graphs) for r in candidates) if v.lock_zombie
        ]
        start = (page - 1) * page_size
        return schemas.ProductPageOut(
            total=len(views),
            page=page,
            page_size=page_size,
            items=views[start : start + page_size],
        )

    total = query.count()
    rows = (
        query.order_by(models.ProductStatus.updated_at.desc().nullslast())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return schemas.ProductPageOut(
        total=total,
        page=page,
        page_size=page_size,
        items=[_build_view(db, r, models_by_name, graphs) for r in rows],
    )

@router.get("/{sn}", response_model=schemas.ProductOut, summary="在制品详情")
def get_product(sn: str, db: Session = Depends(get_db), user=Depends(current_user)):
    row = get_or_404(db, models.ProductStatus, sn, "product")
    return build_product_out(db, row, include_lock=True)

@router.post(
    "/{sn}/force-release",
    response_model=schemas.ForceReleaseOut,
    summary="强制解锁(机台失联/卡死时人工介入)",
)
def force_release(
    sn: str, payload: schemas.ForceReleaseIn, db: Session = Depends(get_db), user=Depends(require_operator)
):
    """仅释放工位锁与会话，不动印章 / 失败计数；随后可继续维修处置。"""
    return force_release_lock(db, sn=sn, reason=payload.reason, operator=user.username)

@router.get("/{sn}/sessions", response_model=List[schemas.SessionOut], summary="该 SN 的测试会话时间线")
def product_sessions(sn: str, db: Session = Depends(get_db), user=Depends(current_user)):
    """追溯"试了几次、每次跑到哪崩的"。"""
    rows = (
        db.query(models.TestSession)
        .filter(models.TestSession.sn == sn)
        .order_by(models.TestSession.started_at.desc())
        .limit(50)
        .all()
    )
    return [_session_view(r) for r in rows]

