"""通道二：事件台账与 SN 全生命周期追溯（/api/admin/records）。"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..errors import AppError, get_or_404, not_found
from ..security import current_user
from ..services import artifacts as artifact_store
from ..services.routing import _as_list, load_process
from ..services.timeutil import date_range_bounds
from ..services.views import build_product_out

router = APIRouter(prefix="/api/admin/records", tags=["admin-台账追溯"])

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
    start, end = date_range_bounds(date_from, date_to)
    if start:
        query = query.filter(models.TestRecord.created_at >= start)
    if end:
        query = query.filter(models.TestRecord.created_at < end)

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

def _record_files(record: models.TestRecord) -> List[dict]:
    """拉平 executed_items.items[].files 为有序清单（下载索引用）。"""
    payload = record.executed_items if isinstance(record.executed_items, dict) else {}
    files: List[dict] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        for ref in item.get("files") or []:
            if isinstance(ref, dict) and ref.get("object_key"):
                files.append(ref)
    return files

@router.get(
    "/{record_id}/files/{index}",
    response_model=schemas.ReportDownloadOut,
    summary="测试产物下载(结果文件/日志等；返回 MinIO 预签名 URL)",
)
def download_artifact(
    record_id: int,
    index: int,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    record = get_or_404(db, models.TestRecord, record_id, "record")
    files = _record_files(record)
    if index < 0 or index >= len(files):
        raise not_found(
            "artifact_not_found", f"artifact_not_found: record {record_id} has no file #{index}"
        )
    ref = files[index]
    key = ref["object_key"]
    filename = ref.get("filename") or key.rsplit("/", 1)[-1]
    if not artifact_store.enabled():
        raise AppError(
            503,
            "artifact_storage_unavailable",
            "artifact_storage_unavailable: MinIO is not configured on the server",
        )
    return schemas.ReportDownloadOut(filename=filename, url=artifact_store.presigned_url(key))

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
        if not rec.is_valid:
            continue
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
                    is_optional=station_id in graph.optional,
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

