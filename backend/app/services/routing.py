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
        .order_by(models.StationItem.station_id, models.StationItem.case_id)
        .all()
    )
    items_of: Dict[str, List[models.StationItem]] = {}
    for item in items:
        items_of.setdefault(item.station_id, []).append(item)

    return ProcessGraph(
        process_id=process_id,
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
    )

def missing_prereq(graph: ProcessGraph, station_id: str, passed: Set[str]) -> List[str]:
    """未满足的前置工步（防跳站闸门）。"""
    return [d for d in graph.deps_of.get(station_id, []) if d not in passed]

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
    """该工位必测的用例ID清单（is_active AND is_mandatory）。"""
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
    return bool(graph.stations) and set(graph.stations).issubset(passed)

# ---------------------------------------------------------------------
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

    # 依赖顺序倒置（前置工步的 step_order 大于自身）
    order_of = {s.station_id: s.step_order for s in steps}
    for s in sorted(steps, key=lambda x: (x.step_order, x.station_id)):
        for dep in _as_list(s.depends_on):
            if dep in order_of and order_of[dep] > s.step_order:
                issues.append(
                    schemas.ValidateIssue(
                        level="warning",
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
    for item in items:
        if not item.is_active:
            continue
        active_of[item.station_id] = active_of.get(item.station_id, 0) + 1
        if item.is_mandatory:
            mandatory_of[item.station_id] = mandatory_of.get(item.station_id, 0) + 1

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
# ---------------------------------------------------------------------
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
        .order_by(models.StationItem.station_id, models.StationItem.case_id)
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
                item_count=item_count.get(s.station_id, 0),
                timeout_sec=timeouts.get(s.station_id, 1800),
            )
            for s in steps
        ],
        items=[schemas.StationItemOut.model_validate(i) for i in items],
    )

