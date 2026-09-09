"""通道二：仪表盘统计（/api/admin/metrics）。"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import schemas
from ..config import settings
from ..database import get_db
from ..security import current_user
from ..services import build_overview

router = APIRouter(prefix="/api/admin/metrics", tags=["admin-统计"])


@router.get("/overview", response_model=schemas.MetricsOverview, summary="运行概况")
def overview(
    days: int = Query(settings.METRICS_WINDOW_DAYS, ge=1, le=90, description="统计窗口天数"),
    process_id: Optional[str] = Query(None, description="按工艺流程过滤；为空表示全部"),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    return build_overview(db, days=days, process_id=process_id)
