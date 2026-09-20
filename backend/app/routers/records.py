"""通道二：事件台账与 SN 全生命周期追溯（/api/admin/records）。"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import bad_request, get_or_404
from ..security import current_user
from ..services.routing import _as_list, load_process
from ..services.timeutil import local_day_start_of_date, sql_time
from ..services.views import build_product_out

router = APIRouter(prefix="/api/admin/records", tags=["admin-台账追溯"])

def _parse_date(value: str, field: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise bad_request(
            "invalid_date_format", f"invalid_date_format: {field} should be YYYY-MM-DD, got {value}"
        ) from None

@router.get("", response_model=schemas.RecordPageOut, summary="测试记录清单(多条件分页)")
def list_records(
    sn: Optional[str] = None,
    product_model: Optional[str] = None,
    station_id: Optional[str] = None,
    client_id: Optional[str] = None,
    overall_result: Optional[str] = None,
    is_valid: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    query = db.query(models.TestRecord)
    if sn:
        query = query.filter(models.TestRecord.sn.ilike(f"%{sn}%"))
    if station_id:
        query = query.filter(models.TestRecord.station_id == station_id)
    if client_id:
        query = query.filter(models.TestRecord.client_id == client_id)
    if overall_result:
        query = query.filter(models.TestRecord.overall_result == overall_result.upper())
    if is_valid is not None:
        query = query.filter(models.TestRecord.is_valid.is_(is_valid))
    if product_model:
        sns = [
            p.sn
            for p in db.query(models.ProductStatus)
            .filter(models.ProductStatus.product_model == product_model)
            .all()
        ]
        query = query.filter(models.TestRecord.sn.in_(sns or [""]))
    if date_from:
        query = query.filter(
            models.TestRecord.created_at
            >= sql_time(local_day_start_of_date(_parse_date(date_from, "date_from")))
        )
    if date_to:
        query = query.filter(
            models.TestRecord.created_at
            < sql_time(local_day_start_of_date(_parse_date(date_to, "date_to") + timedelta(days=1)))
        )

    total = query.count()
    rows = (
        query.order_by(models.TestRecord.record_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [schemas.RecordOut.model_validate(r) for r in rows]
    return schemas.RecordPageOut(total=total, page=page, page_size=page_size, items=items)

@router.get("/{record_id}", response_model=schemas.RecordOut, summary="记录详情(含用例ID执行快照)")
def get_record(record_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return get_or_404(db, models.TestRecord, record_id, "record")

@router.get("/trace/{sn}", response_model=schemas.TraceOut, summary="SN 全生命周期追溯")
def trace_sn(sn: str, db: Session = Depends(get_db), user=Depends(current_user)):
    product = get_or_404(db, models.ProductStatus, sn, "product")

    records = (
        db.query(models.TestRecord)
        .filter(models.TestRecord.sn == sn)
        .order_by(models.TestRecord.record_id.asc())
        .all()
    )
    repairs = (
        db.query(models.RepairRecord)
        .filter(models.RepairRecord.sn == sn)
        .order_by(models.RepairRecord.repair_id.asc())
        .all()
    )

    last_by_station = {}
    for rec in records:
        last_by_station[rec.station_id] = rec

    steps: list = []
    model_row = db.get(models.ProductModel, product.product_model)
    graph = load_process(db, model_row.process_id) if model_row else None
    passed = set(_as_list(product.passed_stations))
    if graph:
        for station_id in graph.stations:
            rec = last_by_station.get(station_id)
            steps.append(
                schemas.TraceStep(
                    station_id=station_id,
                    station_name=graph.name_of.get(station_id, station_id),
                    step_order=graph.step_of.get(station_id, 0),
                    depends_on=graph.deps_of.get(station_id, []),
                    passed=station_id in passed,
                    last_result=rec.overall_result if rec else None,
                    last_record_id=rec.record_id if rec else None,
                    last_time=rec.created_at if rec else None,
                )
            )

    view = build_product_out(db, product, graph=graph, model_row=model_row)

    return schemas.TraceOut(
        product=view,
        steps=steps,
        records=[schemas.RecordOut.model_validate(r) for r in records],
        repairs=[schemas.RepairOut.model_validate(r) for r in repairs],
    )

