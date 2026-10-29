"""静态工艺拓扑服务：流程装载、依赖闸门、用例ID清单、DAG 校验。

硬件构型差异由独立的 `process_id` 物理隔离 —— 带 AWG 的 MSO 流程与不带 AWG 的
DPO 流程各自维护完整的工步拓扑与用例ID清单，运行期不存在任何条件分支。
"""

from typing import Dict, List, NamedTuple, Optional, Set

from sqlalchemy.orm import Session

from .. import models, schemas
from ..errors import not_found

def _as_list(value) -> List[str]:
    """数组列归一化（SQLite 存 JSON，PG 存 TEXT[]）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return list(value)

class ProcessGraph(NamedTuple):
    """一次流程装载的内存视图：进站/出站全程复用，避免重复查库。"""

    process_id: str
    stations: List[str]
    step_of: Dict[str, int]
    deps_of: Dict[str, List[str]]
    timeout_of: Dict[str, int]
    name_of: Dict[str, str]
    items_of: Dict[str, List[models.StationItem]]
    sequential: bool = True  # 严格顺序：前序必跑工位未盖章即拦截（并行流程关闭）
    optional: Set[str] = frozenset()  # 选检工位（QA 抽查），不参与完工判定
    invalidates: Set[str] = frozenset()  # 依赖重测触发的工位（invalidates_downstream 配置）

def load_process(db: Session, process_id: str) -> Optional[ProcessGraph]:
    """装载流程拓扑。流程不存在或无任何工步时返回 None。"""
    process = db.get(models.Process, process_id)
    if process is None:
        return None

    steps = (
        db.query(models.ProcessStation)
        .filter(models.ProcessStation.process_id == process_id)
        .order_by(models.ProcessStation.step_order, models.ProcessStation.station_id)
        .all()
    )
    if not steps:
        return None

    station_ids = {s.station_id for s in steps}

    station_map = {
        s.station_id: s
        for s in db.query(models.Station).filter(models.Station.station_id.in_(station_ids)).all()
    }
    items = (
        db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == process_id,
            models.StationItem.is_active.is_(True),
        )
        .order_by(
            models.StationItem.station_id,
            models.StationItem.sort_order.is_(None),
            models.StationItem.sort_order,
            models.StationItem.case_id,
        )
        .all()
    )
    items_of: Dict[str, List[models.StationItem]] = {}
    for item in items:
        items_of.setdefault(item.station_id, []).append(item)

    return ProcessGraph(
        process_id=process_id,
        sequential=bool(process.is_sequential),
        stations=[s.station_id for s in steps],
        step_of={s.station_id: s.step_order for s in steps},
        deps_of={
            s.station_id: [d for d in _as_list(s.depends_on) if d in station_ids] for s in steps
        },
        timeout_of={
            s.station_id: (station_map[s.station_id].timeout_sec if s.station_id in station_map else 1800)
            for s in steps
        },
        name_of={
            s.station_id: (station_map[s.station_id].station_name if s.station_id in station_map else s.station_id)
            for s in steps
        },
        items_of=items_of,
        optional={s.station_id for s in steps if s.is_optional},
        invalidates={s.station_id for s in steps if s.invalidates_downstream},
    )

def missing_prereq(graph: ProcessGraph, station_id: str, passed: Set[str]) -> List[str]:
    """未满足的前置工步（防跳站闸门）。

    声明的 depends_on 始终生效；流程开启严格顺序（is_sequential，默认开）时，
    还要求所有 step_order 更小的**必跑**工位已盖章（选检工位不构成前置）。
    返回按工步次序排列，便于提示文案阅读。
    """
    deps = set(graph.deps_of.get(station_id, []))
    if graph.sequential:
        order = graph.step_of.get(station_id, 0)
        for sid in graph.stations:
            if sid == station_id or sid in graph.optional:
                continue
            if graph.step_of.get(sid, 0) < order:
                deps.add(sid)
    lacking = [d for d in deps if d not in passed]
    return sorted(lacking, key=lambda s: graph.step_of.get(s, 0))

def next_stations(graph: ProcessGraph, passed: Set[str]) -> List[schemas.NextStation]:
    """前置已满足且尚未盖章的可进站工位。"""
    result = []
    for station_id in graph.stations:
        if station_id in passed:
            continue
        if missing_prereq(graph, station_id, passed):
            continue
        result.append(
            schemas.NextStation(
                station_id=station_id,
                station_name=graph.name_of.get(station_id, station_id),
                step_order=graph.step_of.get(station_id, 0),
            )
        )
    return result

def mandatory_case_ids(graph: ProcessGraph, station_id: str) -> List[str]:
    """该工位必测的用例ID清单（is_active AND is_mandatory），按配置的执行顺序返回。"""
    return [i.case_id for i in graph.items_of.get(station_id, []) if i.is_mandatory]

def station_rules(graph: ProcessGraph, station_id: str) -> List[schemas.StationRule]:
    return [
        schemas.StationRule(
            case_id=i.case_id,
            item_name=i.item_name,
            is_mandatory=bool(i.is_mandatory),
        )
        for i in graph.items_of.get(station_id, [])
    ]

def is_completed(graph: ProcessGraph, passed: Set[str]) -> bool:
    """完工 = 全部必跑工位盖章；选检工位（QA 抽查）缺席不卡完工。"""
    required = set(graph.stations) - graph.optional
    return bool(required) and required.issubset(passed)

# ---------------------------------------------------------------------
def _find_cycle(deps_of: Dict[str, List[str]]) -> Optional[List[str]]:
    """DFS 三色标记，返回首个环路径（闭合）或 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {s: WHITE for s in deps_of}
    path: List[str] = []

    def dfs(node: str) -> Optional[List[str]]:
        color[node] = GRAY
        path.append(node)
        for dep in deps_of.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                return path[path.index(dep):] + [dep]
            if color[dep] == WHITE:
                found = dfs(dep)
                if found:
                    return found
        color[node] = BLACK
        path.pop()
        return None

    for station in sorted(deps_of):
        if color[station] == WHITE:
            cycle = dfs(station)
            if cycle:
                return cycle
    return None

def validate_process(db: Session, process_id: str) -> schemas.ValidateOut:
    """校验流程拓扑：悬空依赖、成环、工步无测试项、step_order 重复、不可达工位。"""
    process = db.get(models.Process, process_id)
    if process is None:
        raise not_found("process_not_found", f"process_not_found: {process_id}")

    steps = (
        db.query(models.ProcessStation)
        .filter(models.ProcessStation.process_id == process_id)
        .order_by(models.ProcessStation.step_order)
        .all()
    )
    issues: List[schemas.ValidateIssue] = []

    if not steps:
        issues.append(
            schemas.ValidateIssue(level="error", code="process_empty", detail="流程未编排任何工步")
        )
        return schemas.ValidateOut(process_id=process_id, ok=False, issues=issues)

    defined = {s.station_id for s in steps}
    step_values: Dict[int, List[str]] = {}
    for s in steps:
        step_values.setdefault(s.step_order, []).append(s.station_id)

    for order, stations in sorted(step_values.items()):
        if len(stations) > 1:
            issues.append(
                schemas.ValidateIssue(
                    level="warning",
                    code="step_order_duplicate",
                    detail=f"step_order={order}: {', '.join(sorted(stations))}",
                )
            )

    # 悬空依赖
    for s in sorted(steps, key=lambda x: (x.step_order, x.station_id)):
        dangling = [d for d in _as_list(s.depends_on) if d not in defined]
        if dangling:
            issues.append(
                schemas.ValidateIssue(
                    level="warning",
                    code="config_dep_missing",
                    detail=f"{s.station_id} -> {', '.join(dangling)}",
                )
            )

    # 依赖顺序倒置（前置工步的 step_order 大于自身）：error——回退深度按 step_order
    # 计算、进站卡控按 deps，两者矛盾会导致回退深度与卡控边界不一致，保存即拦截
    order_of = {s.station_id: s.step_order for s in steps}
    for s in sorted(steps, key=lambda x: (x.step_order, x.station_id)):
        for dep in _as_list(s.depends_on):
            if dep in order_of and order_of[dep] > s.step_order:
                issues.append(
                    schemas.ValidateIssue(
                        level="error",
                        code="reverse_step_dep",
                        detail=f"{s.station_id}({s.step_order}) -> {dep}({order_of[dep]})",
                    )
                )

    deps_of = {s.station_id: [d for d in _as_list(s.depends_on) if d in defined] for s in steps}
    cycle = _find_cycle(deps_of)
    if cycle:
        issues.append(
            schemas.ValidateIssue(level="error", code="config_cycle", detail=" -> ".join(cycle))
        )

    # 选检工位不得被必跑工位依赖：一旦被依赖，它就变相成了必跑，抽查语义失效
    optional = {s.station_id for s in steps if s.is_optional}
    for s in sorted(steps, key=lambda x: (x.step_order, x.station_id)):
        if s.station_id in optional:
            continue
        dep_on_optional = [d for d in deps_of.get(s.station_id, []) if d in optional]
        if dep_on_optional:
            issues.append(
                schemas.ValidateIssue(
                    level="error",
                    code="mandatory_dep_on_optional",
                    detail=f"{s.station_id} -> {', '.join(sorted(dep_on_optional))}",
                )
            )

    known = {
        st.station_id
        for st in db.query(models.Station).filter(models.Station.station_id.in_(defined)).all()
    }
    for station_id in sorted(defined - known):
        issues.append(
            schemas.ValidateIssue(level="error", code="station_undefined", detail=station_id)
        )

    items = (
        db.query(models.StationItem)
        .filter(models.StationItem.process_id == process_id)
        .all()
    )
    active_of: Dict[str, int] = {}
    mandatory_of: Dict[str, int] = {}
    order_seen: Dict[Tuple[str, int], List[str]] = {}
    for item in items:
        if not item.is_active:
            continue
        active_of[item.station_id] = active_of.get(item.station_id, 0) + 1
        if item.is_mandatory:
            mandatory_of[item.station_id] = mandatory_of.get(item.station_id, 0) + 1
        if item.sort_order is not None:
            order_seen.setdefault((item.station_id, item.sort_order), []).append(item.case_id)
    for (station_id, order), cases in sorted(order_seen.items()):
        if len(cases) > 1:
            issues.append(
                schemas.ValidateIssue(
                    level="warning",
                    code="item_order_duplicate",
                    detail=f"{station_id} sort_order={order}: {', '.join(sorted(cases))}",
                )
            )

    for station_id in sorted(defined):
        if active_of.get(station_id, 0) == 0:
            issues.append(
                schemas.ValidateIssue(level="error", code="station_no_item", detail=station_id)
            )
        elif mandatory_of.get(station_id, 0) == 0:
            issues.append(
                schemas.ValidateIssue(level="warning", code="station_no_mandatory", detail=station_id)
            )

    ok = not any(i.level == "error" for i in issues)
    return schemas.ValidateOut(process_id=process_id, ok=ok, issues=issues)

# ---------------------------------------------------------------------
def topology_impact(
    db: Session, process_id: str, draft_steps: List[schemas.ProcessStationIn]
) -> schemas.TopologyImpactOut:
    """原地改拓扑的影响面预演（只读，非阻断）：草案保留在内存，不落库。

    判定口径：以草案拓扑构造内存流程图，逐件在制品计算
    - removed_stamps：印章所在工位被草案移除（历史保留，不参与新判定）
    - missing_prereq/blocked：草案下"下一条必跑工位"缺前置（进站会被防跳站拦）
    """
    process = db.get(models.Process, process_id)
    if process is None:
        raise not_found("process_not_found", f"流程 {process_id} 不存在")

    saved = (
        db.query(models.ProcessStation)
        .filter(models.ProcessStation.process_id == process_id)
        .all()
    )
    old_ids = {row.station_id for row in saved}
    ordered = sorted(draft_steps, key=lambda x: (x.step_order, x.station_id))
    draft_ids = {x.station_id for x in ordered}

    names = {
        row.station_id: row.station_name
        for row in db.query(models.Station).filter(models.Station.station_id.in_(draft_ids or {""})).all()
    }
    graph = ProcessGraph(
        process_id=process_id,
        stations=[x.station_id for x in ordered],
        step_of={x.station_id: x.step_order for x in ordered},
        deps_of={x.station_id: list(x.depends_on or []) for x in ordered},
        timeout_of={},
        name_of=names,
        items_of={},
        sequential=bool(process.is_sequential),
        optional=frozenset(x.station_id for x in ordered if x.is_optional),
    )

    removed_items = 0
    removed_ids = old_ids - draft_ids
    if removed_ids:
        removed_items = (
            db.query(models.StationItem)
            .filter(
                models.StationItem.process_id == process_id,
                models.StationItem.station_id.in_(removed_ids),
                models.StationItem.is_active.is_(True),
            )
            .count()
        )

    model_ids = [
        row.product_model
        for row in db.query(models.ProductModel)
        .filter(models.ProductModel.process_id == process_id)
        .all()
    ]
    wip_rows = (
        db.query(models.ProductStatus)
        .filter(
            models.ProductStatus.product_model.in_(model_ids or [""]),
            models.ProductStatus.is_completed.is_(False),
            models.ProductStatus.current_status != models.STATUS_SCRAPPED,
        )
        .order_by(models.ProductStatus.sn)
        .all()
    )

    required = [s for s in graph.stations if s not in graph.optional]
    wip: List[schemas.TopologyImpactWip] = []
    affected = blocked_total = 0
    for product in wip_rows:
        passed = set(_as_list(product.passed_stations))
        removed_stamps = sorted(s for s in old_ids if s not in draft_ids and s in passed)
        unpassed = [s for s in required if s not in passed]
        missing = missing_prereq(graph, unpassed[0], passed) if unpassed else []
        blocked = bool(missing)
        if blocked or removed_stamps:
            affected += 1
        if blocked:
            blocked_total += 1
        wip.append(
            schemas.TopologyImpactWip(
                sn=product.sn,
                product_model=product.product_model,
                current_status=product.current_status,
                passed_stations=sorted(passed),
                removed_stamps=removed_stamps,
                missing_prereq=missing,
                blocked=blocked,
            )
        )

    machines_lost = []
    for client in db.query(models.StationClient).all():
        bound = _as_list(client.bound_stations)
        if bound and (set(bound) & old_ids) and not (set(bound) & draft_ids):
            machines_lost.append(
                schemas.SwitchImpactMachine(
                    client_id=client.client_id,
                    client_name=client.client_name,
                    bound_stations=bound,
                )
            )

    return schemas.TopologyImpactOut(
        process_id=process_id,
        wip_count=len(wip_rows),
        affected_count=affected,
        blocked_count=blocked_total,
        removed_items=removed_items,
        machines_lost=machines_lost,
        wip=wip[:50],
    )


def model_switch_impact(db: Session, product_model: str, target_process_id: str) -> schemas.ModelSwitchImpactOut:
    """机型改绑流程的影响面预演（只读，R2 交集结转口径）。

    - 印章继承：目标流程仍存在的同 ID 工位印章保留（carried）
    - 历史保留：旧流程有、目标流程无的工位印章仅作历史，不参与新流程判定（dropped）
    - 补测义务：目标流程的必跑工位中尚未盖章的（to_test，按工步序）
    - 机台风险：绑定工位与目标流程零交集的机台，切换后将 403 client_not_bound
    """
    model_row = db.get(models.ProductModel, product_model)
    if model_row is None:
        raise not_found("model_not_registered", f"机型 {product_model} 未登记，请先在管理端注册机型")
    target = load_process(db, target_process_id)
    if target is None:
        raise not_found("process_not_found", f"流程 {target_process_id} 不存在或尚未配置工步")
    source = load_process(db, model_row.process_id)
    old_stations = set(source.stations) if source else set()
    target_set = set(target.stations)
    target_required = [s for s in target.stations if s not in target.optional]

    wip_rows = (
        db.query(models.ProductStatus)
        .filter(
            models.ProductStatus.product_model == product_model,
            models.ProductStatus.is_completed.is_(False),
            models.ProductStatus.current_status != models.STATUS_SCRAPPED,
        )
        .order_by(models.ProductStatus.sn)
        .all()
    )
    wip: List[schemas.SwitchImpactWip] = []
    for product in wip_rows:
        passed = set(_as_list(product.passed_stations))
        wip.append(
            schemas.SwitchImpactWip(
                sn=product.sn,
                current_status=product.current_status,
                passed_stations=sorted(passed),
                carried_stations=[s for s in target.stations if s in passed],
                dropped_stations=sorted(s for s in old_stations if s not in target_set and s in passed),
                to_test_stations=[s for s in target_required if s not in passed],
                completed=bool(product.is_completed),
            )
        )

    machines_lost: List[schemas.SwitchImpactMachine] = []
    for client in db.query(models.StationClient).all():
        bound = _as_list(client.bound_stations)
        # 仅统计“当前服务于本流程”的机台：与旧流程有交集、且与目标流程零交集 → 切换后失效
        if bound and (set(bound) & old_stations) and not (set(bound) & target_set):
            machines_lost.append(
                schemas.SwitchImpactMachine(
                    client_id=client.client_id,
                    client_name=client.client_name,
                    bound_stations=bound,
                )
            )

    locked_stations = [
        row.station_id
        for row in db.query(models.ProcessStation)
        .filter(
            models.ProcessStation.process_id == target_process_id,
            models.ProcessStation.baseline_locked.is_(True),
        )
        .all()
    ]
    src_proc = db.get(models.Process, model_row.process_id)
    tgt_proc = db.get(models.Process, target_process_id)
    return schemas.ModelSwitchImpactOut(
        product_model=product_model,
        from_process=model_row.process_id,
        from_process_name=src_proc.process_name if src_proc else None,
        to_process=target_process_id,
        to_process_name=tgt_proc.process_name if tgt_proc else None,
        wip_count=len(wip_rows),
        wip=wip[:50],
        machines_lost=machines_lost,
        added_stations=[s for s in target.stations if s not in old_stations],
        removed_stations=sorted(old_stations - target_set),
        locked_stations=sorted(locked_stations),
    )


def process_overview(db: Session) -> List[schemas.ProcessStatOut]:
    """流程清单 + 机型数 / 工位数 / 测试项数。"""
    result = []
    for process in db.query(models.Process).order_by(models.Process.process_id).all():
        steps = (
            db.query(models.ProcessStation)
            .filter(models.ProcessStation.process_id == process.process_id)
            .all()
        )
        models_under = (
            db.query(models.ProductModel)
            .filter(models.ProductModel.process_id == process.process_id)
            .order_by(models.ProductModel.product_model)
            .all()
        )
        item_count = (
            db.query(models.StationItem)
            .filter(models.StationItem.process_id == process.process_id)
            .count()
        )
        result.append(
            schemas.ProcessStatOut(
                process_id=process.process_id,
                process_name=process.process_name,
                is_active=bool(process.is_active),
                is_sequential=bool(process.is_sequential),
                model_count=len(models_under),
                models=[m.product_model for m in models_under],
                station_count=len(steps),
                item_count=item_count,
                created_at=process.created_at,
            )
        )
    return result

def station_item_summary(db: Session, process_id: str) -> Dict[str, int]:
    """{station_id: 生效测试项数}。"""
    rows = (
        db.query(models.StationItem)
        .filter(
            models.StationItem.process_id == process_id,
            models.StationItem.is_active.is_(True),
        )
        .all()
    )
    summary: Dict[str, int] = {}
    for row in rows:
        summary[row.station_id] = summary.get(row.station_id, 0) + 1
    return summary

def process_topology(db: Session, process_id: str) -> schemas.TopologyOut:
    """拓扑视图：工步 + 测试项，供前端一屏展示。"""
    if db.get(models.Process, process_id) is None:
        raise not_found("process_not_found", f"process_not_found: {process_id}")

    steps = (
        db.query(models.ProcessStation)
        .filter(models.ProcessStation.process_id == process_id)
        .order_by(models.ProcessStation.step_order, models.ProcessStation.station_id)
        .all()
    )
    items = (
        db.query(models.StationItem)
        .filter(models.StationItem.process_id == process_id)
        .order_by(
            models.StationItem.station_id,
            models.StationItem.sort_order.is_(None),
            models.StationItem.sort_order,
            models.StationItem.case_id,
        )
        .all()
    )
    step_stations = (
        db.query(models.Station)
        .filter(models.Station.station_id.in_([s.station_id for s in steps]))
        .all()
    )
    station_names = {s.station_id: s.station_name for s in step_stations}
    timeouts = {s.station_id: s.timeout_sec for s in step_stations}
    item_count = station_item_summary(db, process_id)

    return schemas.TopologyOut(
        process_id=process_id,
        steps=[
            schemas.ProcessStationOut(
                process_id=process_id,
                station_id=s.station_id,
                station_name=station_names.get(s.station_id),
                step_order=s.step_order,
                depends_on=_as_list(s.depends_on),
                is_optional=bool(s.is_optional),
                invalidates_downstream=bool(s.invalidates_downstream),
                item_count=item_count.get(s.station_id, 0),
                timeout_sec=timeouts.get(s.station_id, 1800),
                baseline_locked=bool(s.baseline_locked),
                baseline_manifest=_as_list(s.baseline_manifest),
                baseline_captured_at=s.baseline_captured_at,
            )
            for s in steps
        ],
        items=[schemas.StationItemOut.model_validate(i) for i in items],
    )

