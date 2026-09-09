"""数据初始化：静态工艺规则 + 随机在制品测试数据。

    python -m app.seed            幂等：工艺规则已存在则跳过（--reset 强制重建）
    python -m app.seed --reset    清空业务表后重建（保留 users）
    python -m app.seed --reset --products 60   指定随机在制品数量

静态规则严格按方案预置：
    PROC_TEK_MSO  带 AWG 选件，6 站完整流程
    PROC_TEK_DPO  无 AWG 标准，4 站流程（物理剔除所有 AWG 工步）
"""

import argparse
import random
import sys
import uuid
from datetime import datetime, timedelta

from .config import settings
from .database import Base, SessionLocal, ensure_schema
from .logging import get_logger
from .models import (
    STATUS_SCRAPPED,
    Process,
    ProcessStation,
    ProductModel,
    ProductStatus,
    Station,
    StationClient,
    StationItem,
    TestRecord,
    TestSession,
    User,
)
from .security import hash_password
from .services import apply_repair
from .services.routing import _as_list, is_completed, load_process
from .services.timeutil import local_day_start, utcnow

logger = get_logger("seed")

TARGET_FW = "V3.20"

# ---------------------------------------------------------------------
# 静态工艺规则（方案给定）
# ---------------------------------------------------------------------
PROCESSES = [
    ("PROC_TEK_MSO", "TEK数字示波器-带AWG选件流程"),
    ("PROC_TEK_DPO", "TEK数字示波器-无AWG标准流程"),
]

STATIONS = [
    ("CAL_PARAM", "校准-指标测试站位", 1800),
    ("CAL_IFACE", "校准-接口测试站位", 600),
    ("CAL_AWG", "校准-AWG站位", 300),
    ("TST_PARAM", "测试-指标测试站位", 1800),
    ("TST_IFACE", "测试-接口测试站位", 900),
    ("TST_AWG", "测试-AWG站位", 600),
]

MODELS = [
    ("MSO4054B", "PROC_TEK_MSO"),
    ("DPO4054B", "PROC_TEK_DPO"),
]

# (process_id, station_id, step_order, depends_on)
TOPOLOGY = [
    ("PROC_TEK_MSO", "CAL_PARAM", 10, []),
    ("PROC_TEK_MSO", "CAL_IFACE", 20, ["CAL_PARAM"]),
    ("PROC_TEK_MSO", "CAL_AWG", 30, ["CAL_PARAM"]),
    ("PROC_TEK_MSO", "TST_PARAM", 40, ["CAL_PARAM", "CAL_IFACE", "CAL_AWG"]),
    ("PROC_TEK_MSO", "TST_IFACE", 50, ["TST_PARAM"]),
    ("PROC_TEK_MSO", "TST_AWG", 60, ["TST_PARAM"]),
    ("PROC_TEK_DPO", "CAL_PARAM", 10, []),
    ("PROC_TEK_DPO", "CAL_IFACE", 20, ["CAL_PARAM"]),
    ("PROC_TEK_DPO", "TST_PARAM", 30, ["CAL_PARAM", "CAL_IFACE"]),
    ("PROC_TEK_DPO", "TST_IFACE", 40, ["TST_PARAM"]),
]

# (station_id, nodeid, item_name)
# nodeid 为用例ID：上位机 pytest 实际 nodeid，与执行时上报的值严格一致。
# 完整 nodeid 形式：tests/test_<station>.py::Test<Class>::test_<method>
# 长度上限由 station_items.nodeid(String(256)) 保障。
ITEMS = [
    ("CAL_PARAM", "tests/test_cal_param.py::TestAmp::test_amp_cal", "CHn幅度校准"),
    ("CAL_PARAM", "tests/test_cal_param.py::TestPhase::test_phase_cal", "CHn_相位校准"),
    ("CAL_PARAM", "tests/test_cal_param.py::TestAmpDc::test_amp_dc_1m", "CHn_幅度DC_1MΩ测试"),
    ("CAL_PARAM", "tests/test_cal_param.py::TestBandwidth::test_bw_hi_z", "CHn_带宽测试_高阻测试_全通道(部分档位)"),
    ("CAL_PARAM", "tests/test_cal_param.py::TestFastEdge::test_fast_edge_1m", "CHn_快沿_1MΩ测试"),
    ("CAL_IFACE", "tests/test_cal_iface.py::TestNoise::test_noise", "基线噪声测试"),
    ("CAL_IFACE", "tests/test_cal_iface.py::TestTouch::test_touch", "触屏测试"),
    ("CAL_IFACE", "tests/test_cal_iface.py::TestAux::test_aux", "AUX"),
    ("CAL_AWG",   "tests/test_cal_awg.py::TestDac::test_1k_dc", "1k&DC"),
    ("TST_PARAM", "tests/test_tst_param.py::TestTimebase::test_timebase", "时基精度测试"),
    ("TST_PARAM", "tests/test_tst_param.py::TestAmp::test_amp_ac", "CHn_幅度AC测试"),
    ("TST_PARAM", "tests/test_tst_param.py::TestAmpDc::test_amp_dc_1m", "CHn_幅度DC_1MΩ测试"),
    ("TST_PARAM", "tests/test_tst_param.py::TestBandwidth::test_bw_full", "CHn_带宽测试_高阻测试_全通道"),
    ("TST_PARAM", "tests/test_tst_param.py::TestBandwidth::test_bw_single", "CHn_带宽测试_高阻测试_单通道"),
    ("TST_IFACE", "tests/test_tst_iface.py::TestNoise::test_noise", "基线噪声测试"),
    ("TST_IFACE", "tests/test_tst_iface.py::TestLineTrig::test_line_trig", "市电触发测试"),
    ("TST_IFACE", "tests/test_tst_iface.py::TestRuntTrig::test_runt_trig", "欠幅脉冲触发测试"),
    ("TST_AWG",   "tests/test_tst_awg.py::TestAuxChk::test_aux_chk", "AUX检查"),
    ("TST_AWG",   "tests/test_tst_awg.py::TestAfgDc::test_afg_dc_ut8806", "AFG DC校验功能1通道测试(UT8806)"),
    ("TST_AWG",   "tests/test_tst_awg.py::TestAfgSine::test_afg_sine_osc", "AFG 通道1正弦波测试(OSC)"),
    ("TST_AWG",   "tests/test_tst_awg.py::TestAfgSquare::test_afg_square_osc", "AFG 通道1方波测试(OSC)"),
]

# DPO 流程剔除 AWG 站位
DPO_EXCLUDED = {"CAL_AWG", "TST_AWG"}

CLIENTS = [
    ("CAL-DESK-01", "CAL_PARAM", "10.1.60.11"),
    ("CAL-DESK-02", "CAL_IFACE", "10.1.60.12"),
    ("CAL-DESK-03", "CAL_AWG", "10.1.60.13"),
    # 备用机台：与主机台同工位，用于承载演示场景数据
    # 一台机台同时只应持有一把工位锁，故每个持锁的场景件各占一台
    ("CAL-DESK-07", "CAL_PARAM", "10.1.60.17"),
    ("CAL-DESK-08", "CAL_PARAM", "10.1.60.18"),
    ("TST-DESK-01", "TST_PARAM", "10.1.61.11"),
    ("TST-DESK-02", "TST_IFACE", "10.1.61.12"),
    ("TST-DESK-03", "TST_AWG", "10.1.61.13"),
    # 备用机台：与主机台同工位，用于演示"崩溃后被接管"
    ("CAL-DESK-09", "CAL_PARAM", "10.1.60.19"),
    ("TST-DESK-09", "TST_PARAM", "10.1.61.19"),
]

STATION_CLIENT = {c[1]: c[0] for c in CLIENTS}


# ---------------------------------------------------------------------
# 随机测试数据生成
# ---------------------------------------------------------------------
def _values_for(case_id: str, rng: random.Random, failed: bool) -> dict:
    """按用例类型生成贴近真实的测量值快照。

    case_id 现采用 pytest 完整 nodeid（tests/test_xxx.py::TestCls::test_method），
    通过 `test_xxx` 用例方法名识别测试类型，与上位机 pytest 端命名保持一致。
    """
    # 提取方法名：`tests/test_cal_param.py::TestAmp::test_amp_cal` -> `test_amp_cal`
    method = case_id.rsplit("::", 1)[-1].lower()

    if "amp" in method:
        v = 3.3 + rng.uniform(0.09, 0.16) if failed else 3.3 + rng.uniform(-0.02, 0.02)
        return {"voltage_v": round(v, 4), "limit_v": 3.3}
    if "timebase" in method or "phase" in method:
        return {"ppm": round(rng.uniform(-2.5, 2.5) if not failed else rng.uniform(6, 12), 3)}
    if method.startswith("test_bw"):
        return {"bandwidth_mhz": round(500 + rng.uniform(-8, 8) if not failed else 460, 1)}
    if "noise" in method:
        return {"noise_mvrms": round(rng.uniform(0.4, 1.2) if not failed else rng.uniform(2.5, 4.0), 3)}
    if "fast_edge" in method:
        return {"rise_ps": round(rng.uniform(600, 700), 1)}
    if "touch" in method:
        return {"touch_points": 5 if not failed else 3}
    if "aux" in method:
        return {"aux_ok": 0 if failed else 1}
    if "afg" in method:
        return {
            "amplitude_vpp": round(1.0 + rng.uniform(-0.02, 0.02) if not failed else rng.uniform(0.85, 0.93), 4),
            "freq_hz": 1000,
        }
    if "trig" in method:
        return {"triggered": 0 if failed else 1, "level_v": round(rng.uniform(0.9, 1.1), 3)}
    return {"value": round(rng.uniform(0, 1), 4)}


def _make_items(db, process_id: str, station_id: str, rng: random.Random, failed: bool) -> list:
    rows = (
        db.query(StationItem)
        .filter(
            StationItem.process_id == process_id,
            StationItem.station_id == station_id,
            StationItem.is_active.is_(True),
        )
        .all()
    )
    items = []
    for row in rows:
        # 失败时仅让某个必测项判定 FAIL，其余照常 PASS
        item_failed = failed and row.is_mandatory
        items.append(
            {
                "case_id": row.case_id,
                "item_name": row.item_name,
                "result": "FAIL" if item_failed else "PASS",
                "values": _values_for(row.case_id, rng, item_failed),
                "duration_ms": rng.randint(800, 45000),
                "message": "测量值超差，请原地复测" if item_failed else None,
            }
        )
        if item_failed:
            failed = False  # 每次出站只让一项失败，贴近真实
    return items


def _write_record(db, *, sn, station_id, items, overall, when, firmware, duration_ms) -> TestRecord:
    record = TestRecord(
        sn=sn,
        station_id=station_id,
        client_id=STATION_CLIENT.get(station_id, "UNKNOWN"),
        overall_result=overall,
        duration_ms=duration_ms,
        is_valid=True,
        executed_items={
            "checkout_id": uuid.uuid4().hex,
            "firmware": firmware,
            "reason": None,
            "items": items,
        },
        created_at=when,
    )
    db.add(record)
    db.flush()
    return record


def _plan_start(now, rng, *, span_minutes: int) -> datetime:
    """把整条流程平移到统计窗口内的某个本地工作日，且不落到未来。

    旧实现以 `now - randint(0,13)天` 起步、再按分钟累加，整批记录会堆在
    "当前时刻"附近（同一天、同一秒），日趋势只剩一天、窗口均值失去意义。
    """
    # 最晚起点：整条流程跑完后仍在过去
    latest = now - timedelta(minutes=5) - timedelta(minutes=max(span_minutes, 0))
    day_start = local_day_start(now) - timedelta(days=rng.randint(0, 13))
    # 工作时段 08:00–20:00 内随机起步，贴近真实产线节拍
    cursor = day_start + timedelta(minutes=rng.randint(8 * 60, 19 * 60 + 59))
    return cursor if cursor < latest else latest


def _simulate_product(db, rng, *, sn, product_model, firmware, now) -> None:
    """按流程拓扑推进一台在制品，产生合法的事件账本。"""
    from .models import ProductModel as PM

    row = db.get(PM, product_model)
    graph = load_process(db, row.process_id)
    if graph is None:
        return

    passed: set = set()
    fail_count = 0
    status = "IDLE"
    locked_reason = None
    # 先用"相对分钟"排出整条时间轴，最后整体平移到窗口内的某个本地工作日
    cursor = 0
    plan = []  # [(offset_minutes, station_id, overall)]

    # 25% 的在制品停在半途，让看板呈现真实在制分布
    stop_at = len(graph.stations)
    if rng.random() < 0.25 and len(graph.stations) > 1:
        stop_at = rng.randint(1, len(graph.stations) - 1)

    for index, station_id in enumerate(graph.stations):
        if index >= stop_at:
            break

        roll = rng.random()
        if roll < 0.05:  # 连续失败达上限 → 工程锁定
            for _ in range(settings.FAIL_LIMIT):
                cursor += rng.randint(20, 90)
                plan.append((cursor, station_id, "FAIL"))
            fail_count = settings.FAIL_LIMIT
            status = "LOCKED"
            locked_reason = f"连续 {settings.FAIL_LIMIT} 次在 {station_id} 判定 FAIL"
            break
        if roll < 0.18:  # 一次失败后重测通过
            cursor += rng.randint(20, 90)
            plan.append((cursor, station_id, "FAIL"))
            cursor += rng.randint(20, 90)

        cursor += rng.randint(15, 120)
        plan.append((cursor, station_id, "PASS"))
        passed.add(station_id)
        fail_count = 0

    # 整体平移到窗口内某个本地工作日：既铺满 14 天，又不写出未来时间戳
    base = _plan_start(now, rng, span_minutes=cursor)
    for offset, station_id, overall in plan:
        _write_record(
            db,
            sn=sn,
            station_id=station_id,
            items=_make_items(db, graph.process_id, station_id, rng, overall == "FAIL"),
            overall=overall,
            when=base + timedelta(minutes=offset),
            firmware=firmware,
            duration_ms=rng.randint(60000, 400000 if overall == "FAIL" else 900000),
        )

    db.add(
        ProductStatus(
            sn=sn,
            product_model=product_model,
            current_fw_version=firmware,
            current_status=status,
            passed_stations=sorted(passed),
            fail_count=fail_count,
            current_client=STATION_CLIENT.get(graph.stations[0]) if passed else None,
            locked_at=now if status == "LOCKED" else None,
            locked_reason=locked_reason,
            updated_at=base + timedelta(minutes=cursor),
        )
    )
    db.flush()


# ---------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------
def _ensure_admin(db) -> None:
    if db.query(User).count() == 0:
        db.add(
            User(
                username=settings.DEFAULT_ADMIN_USERNAME,
                password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
                full_name=settings.DEFAULT_ADMIN_NAME,
            )
        )
        db.commit()
        print(f"[seed] 默认管理员 {settings.DEFAULT_ADMIN_USERNAME} / {settings.DEFAULT_ADMIN_PASSWORD}")


def _reset_all_tables(db) -> None:
    """清空全部业务表与工艺配置表（users 保留），随后由 _seed_static_rules 重建。

    注意：这会覆盖现场手工维护的工艺配置。若只想清业务数据、保留工艺配置，
    请使用 --clear-business。
    """
    for table in (
        "repair_records",
        "test_sessions",
        "test_records",
        "product_status",
        "station_clients",
        "station_items",
        "process_stations",
        "product_models",
        "stations",
        "processes",
    ):
        db.execute(Base.metadata.tables[table].delete())
    db.commit()
    print("[seed] --reset: 已清空业务表与工艺配置（users 保留，随后重建工艺配置）")


def _clear_business_only(db) -> None:
    """只清空运行时业务数据，保留工艺配置与 users。

    用于"保留现场工艺配置、重新跑一轮测试"：清空在制品、测试记录、会话、
    维修履历与机台注册，工艺配置（流程/工位/机型/工步/用例清单）原样保留。
    """
    for table in (
        "repair_records",
        "test_sessions",
        "test_records",
        "product_status",
        "station_clients",
    ):
        db.execute(Base.metadata.tables[table].delete())
    db.commit()
    print("[seed] --clear-business: 已清空业务数据（工艺配置与 users 保留）")


def _backfill_is_completed(db) -> None:
    """回填 is_completed 冗余列：新增该列后对存量数据执行一次。

    之后的写入由 gate._write_passed() 自动维护，无需再回填。
    派生规则与 views.build_product_out 保持一致（报废品不视为完工）。
    """
    models_by_name = {m.product_model: m for m in db.query(ProductModel).all()}
    graphs = {}
    for mr in models_by_name.values():
        if mr.process_id not in graphs:
            graphs[mr.process_id] = load_process(db, mr.process_id)

    rows = db.query(ProductStatus).all()
    updated = 0
    for p in rows:
        mr = models_by_name.get(p.product_model)
        graph = graphs.get(mr.process_id) if mr is not None else None
        val = bool(
            p.current_status != STATUS_SCRAPPED
            and graph is not None
            and is_completed(graph, set(_as_list(p.passed_stations)))
        )
        if p.is_completed != val:
            p.is_completed = val
            updated += 1
    db.commit()
    print(f"[seed] 回填 is_completed：更新 {updated} / {len(rows)} 行")


def _seed_static_rules(db) -> None:
    for process_id, name in PROCESSES:
        db.merge(Process(process_id=process_id, process_name=name))
    for station_id, name, timeout in STATIONS:
        db.merge(Station(station_id=station_id, station_name=name, timeout_sec=timeout))
    for model, process_id in MODELS:
        db.merge(
            ProductModel(
                product_model=model, process_id=process_id, target_fw_version=TARGET_FW
            )
        )
    for process_id, station_id, order, deps in TOPOLOGY:
        db.merge(
            ProcessStation(
                process_id=process_id,
                station_id=station_id,
                step_order=order,
                depends_on=list(deps),
            )
        )
    for process_id in (p[0] for p in PROCESSES):
        for station_id, case_id, item_name in ITEMS:
            if process_id == "PROC_TEK_DPO" and station_id in DPO_EXCLUDED:
                continue
            db.merge(
                StationItem(
                    process_id=process_id,
                    station_id=station_id,
                    case_id=case_id,
                    item_name=item_name,
                    is_mandatory=True,
                    is_active=True,
                )
            )
    for client_id, station_id, ip in CLIENTS:
        db.merge(
            StationClient(client_id=client_id, station_id=station_id, ip_address=ip)
        )
    db.commit()


def _seed_random_products(db, count: int, rng: random.Random) -> None:
    now = utcnow()
    for i in range(count):
        product_model = "MSO4054B" if rng.random() < 0.55 else "DPO4054B"
        sn = f"C0{20000 + i}"
        # 8% 的在制品固件非基线，用于演示固件拦截场景
        firmware = TARGET_FW if rng.random() < 0.92 else "V3.10"
        _simulate_product(db, rng, sn=sn, product_model=product_model, firmware=firmware, now=now)
    db.commit()
    print(f"[seed] 随机在制品 {count} 台")


def _seed_repairs(db, rng: random.Random) -> None:
    """在已生成的在制品上登记若干维修处置，展示履历与回滚效果。"""
    now = utcnow()
    candidates = (
        db.query(ProductStatus)
        .filter(ProductStatus.current_status == "IDLE")
        .all()
    )
    with_stamps = [p for p in candidates if _as_list(p.passed_stations)]
    rng.shuffle(with_stamps)

    applied = 0
    for product in with_stamps[:2]:  # RETEST：收回最后一个印章允许重测
        try:
            apply_repair(
                db,
                sn=product.sn,
                repair_action="RETEST",
                target_station=sorted(_as_list(product.passed_stations))[-1],
                reason="品质抽检发现指标漂移，安排重测",
                technician_id="admin",
            )
            applied += 1
        except Exception as exc:  # 状态不满足则跳过
            logger.debug("skip RETEST %s: %s", product.sn, exc)

    for product in with_stamps[2:4]:  # ROLLBACK：回退到第二工步
        stamps = sorted(_as_list(product.passed_stations))
        if len(stamps) < 2:
            continue
        try:
            apply_repair(
                db,
                sn=product.sn,
                repair_action="ROLLBACK",
                target_station=stamps[1],
                reason="接口工位批量异常，回退重跑",
                technician_id="admin",
            )
            applied += 1
        except Exception as exc:
            logger.debug("skip ROLLBACK %s: %s", product.sn, exc)

    scrap = next((p for p in candidates if p.sn not in {x.sn for x in with_stamps[:4]}), None)
    if scrap:
        try:
            apply_repair(
                db,
                sn=scrap.sn,
                repair_action="SCRAP",
                target_station=None,
                reason="主板严重损伤，判定报废",
                technician_id="admin",
            )
            applied += 1
        except Exception as exc:
            logger.debug("skip SCRAP %s: %s", scrap.sn, exc)

    db.commit()
    print(f"[seed] 维修处置履历 {applied} 条")


# ---------------------------------------------------------------------
# 租约锁 / 测试会话场景数据（崩溃续测、失联接管、硬超时、强制解锁）
# ---------------------------------------------------------------------
# 场景 SN 统一使用 C0990xx 前缀，与随机在制品 C02xxxx 区分，便于在页面上检索。
SCENARIO_SNS = {
    "healthy": "C099001",  # 正常持锁：心跳新鲜
    "lost": "C099002",  # 僵尸锁：心跳断流，可被接管 / 强制解锁
    "resuming": "C099003",  # 崩溃过一次，当前正在续测（attempt=2）
    "expired": "C099004",  # 硬超时：持锁超 30min 但心跳仍在
    "resumed": "C099005",  # 历史：崩溃后续测成功出库
    "takeover": "C099006",  # 历史：原机台崩溃，被备用机台接管
    "lost_repeat": "C099007",  # 连续失联达阈值 → 已计一次失败
    "timeout": "C099008",  # 历史：硬超时终止（计失败）
}


def _mk_session(
    db,
    *,
    sn: str,
    station_id: str,
    client_id: str,
    attempt: int,
    status: str,
    started_at,
    last_heartbeat_at=None,
    items=None,
    end_reason=None,
    ended_by=None,
    ended_at=None,
) -> TestSession:
    row = TestSession(
        session_id=uuid.uuid4().hex,
        sn=sn,
        station_id=station_id,
        client_id=client_id,
        lock_token=uuid.uuid4().hex if status == "RUNNING" else None,
        attempt=attempt,
        status=status,
        checkpoint={"items": items or [], "cursor": {"step": len(items or [])}},
        started_at=started_at,
        last_heartbeat_at=last_heartbeat_at or started_at,
        ended_at=ended_at,
        end_reason=end_reason,
        ended_by=ended_by,
        created_at=started_at,
    )
    db.add(row)
    db.flush()
    return row


def _mk_product(db, *, sn, product_model, status, passed, fail_count=0, locked_reason=None):
    row = db.get(ProductStatus, sn)
    if row is None:
        row = ProductStatus(
            sn=sn,
            product_model=product_model,
            current_fw_version=TARGET_FW,
            current_status=status,
            passed_stations=sorted(passed),
            fail_count=fail_count,
        )
        db.add(row)
        db.flush()
    else:
        row.current_status = status
        row.passed_stations = sorted(passed)
        row.fail_count = fail_count
    row.locked_reason = locked_reason
    return row


def _partial_items(db, process_id, station_id, rng, count):
    """取该工位前 count 个用例作为"已执行"的断点。"""
    return _make_items(db, process_id, station_id, rng, False)[:count]


def _ensure_scenario_clients(db) -> None:
    """确保演示场景依赖的机台档案存在（幂等 merge）。

    单独执行 --scenarios（只重建场景、不动静态规则）时，若场景引用了新机台，
    会出现"会话里的机台在机台档案中查不到"。既有的库升级时尤其需要这一步。
    """
    for client_id, station_id, ip in (
        ("CAL-DESK-01", "CAL_PARAM", "10.1.60.11"),
        ("CAL-DESK-07", "CAL_PARAM", "10.1.60.17"),
        ("CAL-DESK-08", "CAL_PARAM", "10.1.60.18"),
        ("CAL-DESK-09", "CAL_PARAM", "10.1.60.19"),
        ("CAL-DESK-02", "CAL_IFACE", "10.1.60.12"),
        ("TST-DESK-01", "TST_PARAM", "10.1.61.11"),
    ):
        db.merge(StationClient(client_id=client_id, station_id=station_id, ip_address=ip))
    db.commit()


def _seed_lock_scenarios(db, rng: random.Random) -> int:
    """构造覆盖各类租约锁/会话状态的场景数据（幂等：已存在则整组重建）。"""
    _ensure_scenario_clients(db)
    now = utcnow()
    existing = db.query(ProductStatus).filter(ProductStatus.sn.in_(SCENARIO_SNS.values())).count()
    if existing:
        # 场景数据依赖相对时间（心跳多久之前），重复 seed 会失真，故先清理重建
        db.query(TestSession).filter(TestSession.sn.in_(SCENARIO_SNS.values())).delete(
            synchronize_session=False
        )
        db.query(TestRecord).filter(TestRecord.sn.in_(SCENARIO_SNS.values())).delete(
            synchronize_session=False
        )
        db.query(ProductStatus).filter(ProductStatus.sn.in_(SCENARIO_SNS.values())).delete(
            synchronize_session=False
        )
        db.commit()

    dpo = db.get(ProductModel, "DPO4054B")
    process_id = dpo.process_id if dpo else "PROC_TEK_DPO"

    # --- 1) 正常持锁：心跳 20s 前，已上报 3/5 断点 ---
    p = _mk_product(
        db, sn=SCENARIO_SNS["healthy"], product_model="DPO4054B",
        status="TESTING", passed=[],
    )
    items = _partial_items(db, process_id, "CAL_PARAM", rng, 3)
    session = _mk_session(
        db, sn=p.sn, station_id="CAL_PARAM", client_id="CAL-DESK-01", attempt=1,
        status="RUNNING", started_at=now - timedelta(seconds=120),
        last_heartbeat_at=now - timedelta(seconds=20), items=items,
    )
    p.current_client = "CAL-DESK-01"
    p.lock_token = session.lock_token
    p.lock_acquired_at = now - timedelta(seconds=120)
    p.lock_last_seen_at = now - timedelta(seconds=20)
    p.lock_heartbeat_count = 5
    p.testing_started_at = p.lock_acquired_at
    p.updated_at = p.lock_last_seen_at

    # --- 2) 僵尸锁：心跳 300s 前（> grace 120s），可被接管 / 强制解锁 ---
    p = _mk_product(
        db, sn=SCENARIO_SNS["lost"], product_model="DPO4054B",
        status="TESTING", passed=[],
    )
    items = _partial_items(db, process_id, "CAL_PARAM", rng, 2)
    session = _mk_session(
        db, sn=p.sn, station_id="CAL_PARAM", client_id="CAL-DESK-08", attempt=1,
        status="RUNNING", started_at=now - timedelta(seconds=600),
        last_heartbeat_at=now - timedelta(seconds=300), items=items,
    )
    p.current_client = "CAL-DESK-08"
    p.lock_token = session.lock_token
    p.lock_acquired_at = now - timedelta(seconds=600)
    p.lock_last_seen_at = now - timedelta(seconds=300)
    p.lock_heartbeat_count = 10
    p.testing_started_at = p.lock_acquired_at
    p.updated_at = p.lock_last_seen_at

    # --- 3) 崩溃一次后正在续测：attempt=2，心跳新鲜 ---
    p = _mk_product(
        db, sn=SCENARIO_SNS["resuming"], product_model="DPO4054B",
        status="TESTING", passed=[],
    )
    items = _partial_items(db, process_id, "CAL_PARAM", rng, 4)
    _mk_session(  # 第一次尝试：崩溃失联
        db, sn=p.sn, station_id="CAL_PARAM", client_id="CAL-DESK-07", attempt=1,
        status="ABORTED", started_at=now - timedelta(seconds=900),
        last_heartbeat_at=now - timedelta(seconds=780),
        items=_partial_items(db, process_id, "CAL_PARAM", rng, 1),
        end_reason="client_lost: no heartbeat for 180s", ended_by="sweeper",
        ended_at=now - timedelta(seconds=600),
    )
    session = _mk_session(  # 第二次尝试：续测中
        db, sn=p.sn, station_id="CAL_PARAM", client_id="CAL-DESK-07", attempt=2,
        status="RUNNING", started_at=now - timedelta(seconds=300),
        last_heartbeat_at=now - timedelta(seconds=30), items=items,
    )
    p.current_client = "CAL-DESK-07"
    p.lock_token = session.lock_token
    p.lock_acquired_at = now - timedelta(seconds=300)
    p.lock_last_seen_at = now - timedelta(seconds=30)
    p.lock_heartbeat_count = 9
    p.testing_started_at = p.lock_acquired_at
    p.updated_at = p.lock_last_seen_at

    # --- 4) 硬超时：持锁 35min、心跳仍在（长测试跑满工位 timeout_sec）---
    p = _mk_product(
        db, sn=SCENARIO_SNS["expired"], product_model="DPO4054B",
        status="TESTING", passed=["CAL_PARAM", "CAL_IFACE"],
    )
    items = _partial_items(db, process_id, "TST_PARAM", rng, 5)
    session = _mk_session(
        db, sn=p.sn, station_id="TST_PARAM", client_id="TST-DESK-01", attempt=1,
        status="RUNNING", started_at=now - timedelta(seconds=2100),
        last_heartbeat_at=now - timedelta(seconds=25), items=items,
    )
    p.current_client = "TST-DESK-01"
    p.lock_token = session.lock_token
    p.lock_acquired_at = now - timedelta(seconds=2100)
    p.lock_last_seen_at = now - timedelta(seconds=25)
    p.lock_heartbeat_count = 70
    p.testing_started_at = p.lock_acquired_at
    p.updated_at = p.lock_last_seen_at

    # --- 5) 历史：崩溃后续测成功出库（attempt1 失联 + attempt2 完成）---
    p = _mk_product(
        db, sn=SCENARIO_SNS["resumed"], product_model="DPO4054B",
        status="IDLE", passed=["CAL_PARAM", "CAL_IFACE"],
    )
    _mk_session(
        db, sn=p.sn, station_id="CAL_IFACE", client_id="CAL-DESK-02", attempt=1,
        status="ABORTED", started_at=now - timedelta(hours=3),
        last_heartbeat_at=now - timedelta(hours=3) + timedelta(seconds=90),
        items=_partial_items(db, process_id, "CAL_IFACE", rng, 1),
        end_reason="client_lost: no heartbeat for 150s", ended_by="sweeper",
        ended_at=now - timedelta(hours=3) + timedelta(seconds=240),
    )
    resumed_session = _mk_session(
        db, sn=p.sn, station_id="CAL_IFACE", client_id="CAL-DESK-02", attempt=2,
        status="COMPLETED", started_at=now - timedelta(hours=2),
        last_heartbeat_at=now - timedelta(hours=2) + timedelta(seconds=180),
        items=_partial_items(db, process_id, "CAL_IFACE", rng, 1),
        end_reason="checked out", ended_at=now - timedelta(hours=2) + timedelta(seconds=200),
    )
    # 台账：本次出站由 checkpoint 补回了 2 个崩溃前已跑完的用例
    all_items = _make_items(db, process_id, "CAL_IFACE", rng, False)
    record = _write_record(
        db, sn=p.sn, station_id="CAL_IFACE",
        items=all_items, overall="PASS",
        when=now - timedelta(hours=2) + timedelta(seconds=200),
        firmware=TARGET_FW, duration_ms=182000,
    )
    # 整体重新赋值才会让 SQLAlchemy 标记 JSON 字段为脏
    record.executed_items = {
        **record.executed_items,
        "session_id": resumed_session.session_id,
        "attempt": 2,
        "checkpoint_merged_count": 2,
    }
    db.flush()

    # --- 6) 历史：原机台崩溃，被备用机台接管（TAKEN_OVER + RUNNING）---
    p = _mk_product(
        db, sn=SCENARIO_SNS["takeover"], product_model="DPO4054B",
        status="TESTING", passed=[],
    )
    _mk_session(
        db, sn=p.sn, station_id="CAL_PARAM", client_id="CAL-DESK-01", attempt=1,
        status="TAKEN_OVER", started_at=now - timedelta(seconds=800),
        last_heartbeat_at=now - timedelta(seconds=700),
        items=_partial_items(db, process_id, "CAL_PARAM", rng, 2),
        end_reason="taken over by CAL-DESK-09", ended_by="CAL-DESK-09",
        ended_at=now - timedelta(seconds=180),
    )
    session = _mk_session(
        db, sn=p.sn, station_id="CAL_PARAM", client_id="CAL-DESK-09", attempt=1,
        status="RUNNING", started_at=now - timedelta(seconds=180),
        last_heartbeat_at=now - timedelta(seconds=15),
        items=_partial_items(db, process_id, "CAL_PARAM", rng, 1),
    )
    p.current_client = "CAL-DESK-09"
    p.lock_token = session.lock_token
    p.lock_acquired_at = now - timedelta(seconds=180)
    p.lock_last_seen_at = now - timedelta(seconds=15)
    p.lock_heartbeat_count = 6
    p.testing_started_at = p.lock_acquired_at
    p.updated_at = p.lock_last_seen_at

    # --- 7) 连续失联达阈值 → 已计一次失败 ---
    p = _mk_product(
        db, sn=SCENARIO_SNS["lost_repeat"], product_model="DPO4054B",
        status="IDLE", passed=["CAL_PARAM"], fail_count=1,
        locked_reason=None,
    )
    for i in range(settings.LOST_LOCK_FAIL_THRESHOLD):
        _mk_session(
            db, sn=p.sn, station_id="CAL_IFACE", client_id="CAL-DESK-02", attempt=1,
            status="ABORTED",
            started_at=now - timedelta(hours=6 - i * 2),
            last_heartbeat_at=now - timedelta(hours=6 - i * 2) + timedelta(seconds=60),
            items=_partial_items(db, process_id, "CAL_IFACE", rng, 1),
            end_reason="client_lost: no heartbeat for 200s", ended_by="sweeper",
            ended_at=now - timedelta(hours=6 - i * 2) + timedelta(seconds=260),
        )

    # --- 8) 历史：硬超时终止（计失败）---
    p = _mk_product(
        db, sn=SCENARIO_SNS["timeout"], product_model="DPO4054B",
        status="IDLE", passed=["CAL_PARAM", "CAL_IFACE"], fail_count=1,
    )
    expired_at = now - timedelta(hours=5)
    _mk_session(
        db, sn=p.sn, station_id="TST_PARAM", client_id="TST-DESK-01", attempt=1,
        status="EXPIRED", started_at=expired_at - timedelta(seconds=1900),
        last_heartbeat_at=expired_at,
        items=_partial_items(db, process_id, "TST_PARAM", rng, 3),
        end_reason="lock timeout (1800s)", ended_by="sweeper", ended_at=expired_at,
    )
    _write_record(
        db, sn=p.sn, station_id="TST_PARAM",
        items=_make_items(db, process_id, "TST_PARAM", rng, True),
        overall="FAIL", when=expired_at, firmware=TARGET_FW, duration_ms=1900000,
    )

    db.commit()
    print(f"[seed] 租约锁/会话场景 {len(SCENARIO_SNS)} 组（SN 前缀 C0990xx）")
    if settings.SWEEPER_ENABLED:
        print(
            "[seed] 提示：失联锁（C099002）与硬超时锁（C099004）会被后台回收任务在 30s 内自动释放；\n"
            "       若要让这两组场景数据常驻可见，请以 SWEEPER_ENABLED=false 启动后端。"
        )
    return len(SCENARIO_SNS)


def seed(
    reset: bool = False,
    products: int = 60,
    with_repairs: bool = True,
    scenarios_only: bool = False,
    clear_business: bool = False,
    backfill_completed: bool = False,
) -> None:
    ensure_schema()
    db = SessionLocal()
    try:
        _ensure_admin(db)
        if clear_business:
            _clear_business_only(db)
            return
        if backfill_completed:
            _backfill_is_completed(db)
            return
        if reset:
            _reset_all_tables(db)
        elif db.query(ProcessStation).count():
            if scenarios_only:
                # 只重建场景数据：依赖相对时间，重复 seed 才会刷新"多久之前心跳"
                _seed_lock_scenarios(db, random.Random())
                return
            print("[seed] 工艺规则已存在，跳过静态数据（如需重建请执行 python -m app.seed --reset）")
            return

        _seed_static_rules(db)
        rng = random.Random()
        _seed_random_products(db, products, rng)
        if with_repairs:
            _seed_repairs(db, rng)
        _seed_lock_scenarios(db, rng)
        # is_completed 由 gate._write_passed() 维护，seed 是直接构造行、不走该路径，
        # 故末尾统一回填：否则列表页"已完工"筛选（SQL 层按该列过滤）一条都筛不出，
        # 但列表标签（运行时派生）却显示"已完工"，两处口径打架。
        _backfill_is_completed(db)

        models_count = db.query(ProductModel).count()
        items_count = db.query(StationItem).count()
        print("[seed] 初始化完成")
        print(f"       流程 {len(PROCESSES)} / 工位 {len(STATIONS)} / 机型 {models_count} / 机台 {len(CLIENTS)}")
        print(f"       工步 {len(TOPOLOGY)} / 用例ID测试项 {items_count}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ATE Manager 数据初始化")
    parser.add_argument("--reset", action="store_true", help="清空业务表与工艺配置后重建")
    parser.add_argument(
        "--clear-business",
        action="store_true",
        help="只清空业务数据（在制品/记录/会话/维修/机台），保留工艺配置与 users",
    )
    parser.add_argument(
        "--backfill-completed",
        action="store_true",
        help="回填 product_status.is_completed 冗余列（新增列后对存量数据执行一次）",
    )
    parser.add_argument("--products", type=int, default=60, help="随机在制品数量")
    parser.add_argument("--no-repairs", action="store_true", help="不生成维修处置履历")
    parser.add_argument(
        "--scenarios",
        action="store_true",
        help="仅重建租约锁/会话场景数据（C0990xx，不改动其余数据）",
    )
    args = parser.parse_args()
    seed(
        reset=args.reset,
        products=args.products,
        with_repairs=not args.no_repairs,
        scenarios_only=args.scenarios,
        clear_business=args.clear_business,
        backfill_completed=args.backfill_completed,
    )


if __name__ == "__main__":
    sys.exit(main())
