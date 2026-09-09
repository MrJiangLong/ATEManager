"""读模型装配：在制品 `ProductOut` 视图。

在制品列表/详情（products）与 SN 追溯（records）返回的是同一个 `ProductOut`，
装配逻辑集中在本模块，避免两处各写一遍导致 `is_completed` / `fw_match`
等派生字段漂移。
"""

from typing import Optional

from sqlalchemy.orm import Session

from .. import models, schemas
from .gate import (
    _station_of_client,
    _timeout_of,
    is_lock_lost,
    lock_held_sec,
    lock_idle_sec,
)
from .routing import ProcessGraph, _as_list, is_completed, load_process


def build_product_out(
    db: Session,
    row: models.ProductStatus,
    *,
    graph: Optional[ProcessGraph] = None,
    model_row: Optional[models.ProductModel] = None,
    include_lock: bool = False,
) -> schemas.ProductOut:
    """装配 `ProductOut`。

    `graph` / `model_row` 允许调用方传入已装载的拓扑与机型，避免同一请求内重复装载。
    `include_lock=True` 时额外填充租约锁派生字段（前端据此渲染"失联可接管"与强制解锁入口）。
    """
    view = schemas.ProductOut.model_validate(row)
    passed = set(_as_list(row.passed_stations))

    if model_row is None:
        model_row = db.get(models.ProductModel, row.product_model)
    if graph is None and model_row is not None:
        graph = load_process(db, model_row.process_id)

    if graph:
        view.process_id = graph.process_id
        view.total_steps = len(graph.stations)
        # 报废品即使印章齐全也不视为完工
        view.is_completed = (
            row.current_status != models.STATUS_SCRAPPED and is_completed(graph, passed)
        )
    if model_row:
        view.target_fw_version = model_row.target_fw_version
        view.fw_match = (row.current_fw_version or "") == (model_row.target_fw_version or "")

    view.passed_count = len(passed)

    if include_lock:
        if row.current_status == models.STATUS_TESTING:
            view.lock_held_sec = lock_held_sec(row)
            view.lock_idle_sec = lock_idle_sec(row)
            view.lock_lease_remaining_sec = max(
                0, (_timeout_of(db, _station_of_client(db, row)) - view.lock_held_sec)
            )
            view.lock_zombie = is_lock_lost(row)
        else:
            view.lock_idle_sec = -1
    return view
