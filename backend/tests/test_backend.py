"""ATE Manager 后端回归测试（SQLite + FastAPI TestClient）。

运行：
    python tests/test_backend.py
    pytest tests/test_backend.py

覆盖方案的确定性场景：
    A. 静态规则：流程/机型/工位/机台 CRUD 与删除保护、拓扑校验
    B. 正常流转：DPO 4 站 / MSO 6 站全流程盖章
    C. 需求 2：跳站 403、复测 409、工艺不符 400、固件 403、用例ID 400
    D. 需求 1：落库 ACK、幂等重传、ack 校验端点
    E. 漏测拦截 400、连续失败锁定 403、锁超时接管
    F. 维修处置：RETEST / ROLLBACK / RESET / SCRAP
    G. 台账追溯与仪表盘统计
"""

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///./backend_test.db"
os.environ["V1_API_KEY"] = "test-api-key-12345"
os.environ["JWT_SECRET"] = "test-jwt-secret-for-ate-manager"
os.environ["APP_DEBUG"] = "true"

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
_DB_PATH = os.path.join(_BACKEND_DIR, "backend_test.db")
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password  # noqa: E402

Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    db.add(
        models.User(
            username="admin",
            password_hash=hash_password("admin123"),
            full_name="Test Admin",
        )
    )
    db.commit()

client = TestClient(app)

# ---------------- 夹具 ----------------
STAMP = int(time.time())
P_MSO = f"PROC_MSOX-{STAMP}"
P_DPO = f"PROC_DPOX-{STAMP}"
M_MSO = "MSO4054B"
M_DPO = "DPO4054B"
FW = "V3.20"

STATIONS = [
    ("CAL_PARAM", "校准-指标测试站位", 1800),
    ("CAL_IFACE", "校准-接口测试站位", 600),
    ("CAL_AWG", "校准-AWG站位", 300),
    ("TST_PARAM", "测试-指标测试站位", 1800),
    ("TST_IFACE", "测试-接口测试站位", 900),
    ("TST_AWG", "测试-AWG站位", 600),
]
CLIENTS = {
    "CAL_PARAM": f"CLI-{STAMP}-CALP",
    "CAL_IFACE": f"CLI-{STAMP}-CALI",
    "CAL_AWG": f"CLI-{STAMP}-CALA",
    "TST_PARAM": f"CLI-{STAMP}-TSTP",
    "TST_IFACE": f"CLI-{STAMP}-TSTI",
    "TST_AWG": f"CLI-{STAMP}-TSTA",
}
# MSO 6 站 / DPO 4 站（物理剔除 AWG）
MSO_TOPOLOGY = [
    ("CAL_PARAM", 10, []),
    ("CAL_IFACE", 20, ["CAL_PARAM"]),
    ("CAL_AWG", 30, ["CAL_PARAM"]),
    ("TST_PARAM", 40, ["CAL_PARAM", "CAL_IFACE", "CAL_AWG"]),
    ("TST_IFACE", 50, ["TST_PARAM"]),
    ("TST_AWG", 60, ["TST_PARAM"]),
]
DPO_TOPOLOGY = [
    ("CAL_PARAM", 10, []),
    ("CAL_IFACE", 20, ["CAL_PARAM"]),
    ("TST_PARAM", 30, ["CAL_PARAM", "CAL_IFACE"]),
    ("TST_IFACE", 40, ["TST_PARAM"]),
]
ITEMS = {
    "CAL_PARAM": ["test_amp_cal", "test_phase_cal"],
    "CAL_IFACE": ["test_noise", "test_touch"],
    "CAL_AWG": ["test_1k_dc"],
    "TST_PARAM": ["test_timebase", "test_amp_ac"],
    "TST_IFACE": ["test_line_trig", "test_runt_trig"],
    "TST_AWG": ["test_aux_chk", "test_afg_sine_osc"],
}

# MSO 流程生效的用例ID总数（CAL_PARAM 2 + CAL_IFACE 2 + CAL_AWG 1 + TST_PARAM 2 + TST_IFACE 2 + TST_AWG 2）
MSO_ITEM_COUNT = sum(len(v) for v in ITEMS.values())

API_KEY = "test-api-key-12345"
_TOKEN = None


# ---------------- 工具 ----------------
def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def api(method, path, body=None, token=None, api_key=None, headers=None, params=None):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if api_key:
        h["X-API-Key"] = api_key
    if headers:
        h.update(headers)
    return client.request(method, path, json=body, headers=h, params=params)


def v1(action, payload, method="POST"):
    return api(method, f"/api/v1/client/{action}", body=payload, api_key=API_KEY)


def token() -> str:
    global _TOKEN
    if _TOKEN is None:
        resp = api("POST", "/api/auth/login", body={"username": "admin", "password": "admin123"})
        check(resp.status_code == 200, f"login failed {resp.status_code} {resp.text}")
        _TOKEN = resp.json()["access_token"]
    return _TOKEN


def admin(method, path, body=None, params=None):
    return api(method, path, body=body, token=token(), params=params)


def new_sn(tag):
    return f"SN-{STAMP}-{tag}"


def checkin(station, sn, model=M_DPO, firmware=FW, case_ids=None):
    return v1(
        "check-in",
        {
            "client_id": CLIENTS[station],
            "sn": sn,
            "product_model": model,
            "firmware": firmware,
            **({"case_ids": case_ids} if case_ids is not None else {}),
        },
    )


def rules_of(resp) -> list:
    """进站下发的用例ID规则清单。"""
    return [r["case_id"] for r in resp.json()["data"]["rules"]]


def items_for(case_ids, fail=(), skip=()):
    return [
        {
            "case_id": c,
            "result": "FAIL" if c in fail else ("SKIP" if c in skip else "PASS"),
            "values": {"v": 1.0},
            "duration_ms": 1200,
        }
        for c in case_ids
    ]


def checkout(station, sn, case_ids, checkout_id=None, fail=(), skip=(), lock_token=None):
    return v1(
        "check-out",
        {
            "client_id": CLIENTS[station],
            "sn": sn,
            "checkout_id": checkout_id or uuid.uuid4().hex,
            "items": items_for(case_ids, fail=fail, skip=skip),
            "duration_ms": 60000,
            **({"lock_token": lock_token} if lock_token else {}),
        },
    )


def pass_station(station, sn, model=M_DPO):
    """完成一次合法进站 → 出站。"""
    resp = checkin(station, sn, model)
    check(resp.status_code == 200, f"check-in {station}: {resp.status_code} {resp.text}")
    case_ids = rules_of(resp)
    resp = checkout(station, sn, case_ids)
    check(resp.status_code == 201, f"check-out {station}: {resp.status_code} {resp.text}")
    return resp


# ================= A. 静态规则 =================
def test_health():
    resp = api("GET", "/api/health")
    check(resp.status_code == 200 and resp.json()["status"] == "ok", f"health: {resp.text}")


def test_masters_seed():
    for pid, name in ((P_MSO, "MSO 带AWG流程"), (P_DPO, "DPO 无AWG流程")):
        resp = admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": name})
        check(resp.status_code == 201, f"create process {pid}: {resp.text}")

    for sid, name, timeout in STATIONS:
        resp = admin(
            "POST", "/api/admin/stations", {"station_id": sid, "station_name": name, "timeout_sec": timeout}
        )
        check(resp.status_code == 201, f"create station {sid}: {resp.text}")

    for model, pid in ((M_MSO, P_MSO), (M_DPO, P_DPO)):
        resp = admin(
            "POST",
            "/api/admin/product-models",
            {"product_model": model, "process_id": pid, "target_fw_version": FW},
        )
        check(resp.status_code == 201, f"create model {model}: {resp.text}")

    for station, cid in CLIENTS.items():
        resp = admin(
            "POST", "/api/admin/clients", {"client_id": cid, "station_id": station, "ip_address": "10.1.60.1"}
        )
        check(resp.status_code == 201, f"create client {cid}: {resp.text}")


def test_topology_and_items():
    for pid, topology in ((P_MSO, MSO_TOPOLOGY), (P_DPO, DPO_TOPOLOGY)):
        payload = [
            {"station_id": sid, "step_order": order, "depends_on": deps}
            for sid, order, deps in topology
        ]
        resp = admin("PUT", "/api/admin/routing/stations", payload, params={"process_id": pid})
        check(resp.status_code == 200, f"save topology {pid}: {resp.text}")
        check(len(resp.json()) == len(topology), f"topology size {pid}: {resp.text}")

        for sid, case_ids in ITEMS.items():
            if pid == P_DPO and sid in ("CAL_AWG", "TST_AWG"):
                continue
            for case_id in case_ids:
                resp = admin(
                    "POST",
                    "/api/admin/routing/items",
                    {
                        "process_id": pid,
                        "station_id": sid,
                        "case_id": case_id,
                        "item_name": case_id,
                    },
                )
                check(resp.status_code == 201, f"create item {pid}/{sid}/{case_id}: {resp.text}")


def test_validate_ok_and_cycle():
    for pid in (P_MSO, P_DPO):
        resp = admin("GET", "/api/admin/routing/validate", params={"process_id": pid})
        check(resp.status_code == 200 and resp.json()["ok"] is True, f"validate {pid}: {resp.text}")

    # 人为制造成环
    bad = f"PROC_BAD-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": bad, "process_name": "cycle demo"})
    admin("POST", "/api/admin/stations", {"station_id": "S1", "station_name": "S1"})
    admin("POST", "/api/admin/stations", {"station_id": "S2", "station_name": "S2"})
    cycle = [
        {"station_id": "S1", "step_order": 10, "depends_on": ["S2"]},
        {"station_id": "S2", "step_order": 20, "depends_on": ["S1"]},
    ]
    # 保存前校验会拦下成环拓扑
    resp = admin("PUT", "/api/admin/routing/stations", cycle, params={"process_id": bad})
    check(
        resp.status_code == 409 and resp.json()["code"] == "topology_invalid",
        f"cycle must be rejected on save: {resp.text}",
    )

    # force 才允许落库（供排查/回滚等特殊场景）
    resp = admin(
        "PUT", "/api/admin/routing/stations", cycle, params={"process_id": bad, "force": True}
    )
    check(resp.status_code == 200, f"force save cycle: {resp.text}")

    resp = admin("GET", "/api/admin/routing/validate", params={"process_id": bad})
    check(resp.json()["ok"] is False, f"cycle should fail: {resp.text}")
    codes = [i["code"] for i in resp.json()["issues"]]
    check("config_cycle" in codes and "station_no_item" in codes, f"issue codes: {codes}")


def test_delete_guards():
    resp = admin("DELETE", f"/api/admin/processes/{P_MSO}")
    check(resp.status_code == 409 and "process_has_models" in resp.json()["code"], f"process guard: {resp.text}")
    resp = admin("DELETE", f"/api/admin/stations/CAL_PARAM")
    check(resp.status_code == 409 and "station_in_topology" in resp.json()["code"], f"station guard: {resp.text}")

    # 机型无在制品时可删；删后重建，避免影响后续用例
    resp = admin("DELETE", f"/api/admin/product-models/{M_MSO}")
    check(resp.status_code == 204, f"model delete: {resp.status_code} {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {"product_model": M_MSO, "process_id": P_MSO, "target_fw_version": FW},
    )
    check(resp.status_code == 201, f"model recreate: {resp.text}")

    # 存在在制品时拒绝删除
    sn = new_sn("GUARD")
    pass_station("CAL_PARAM", sn)
    resp = admin("DELETE", f"/api/admin/product-models/{M_DPO}")
    check(resp.status_code == 409 and resp.json()["code"] == "model_in_use", f"model guard: {resp.text}")


def test_station_item_guards():
    """station_items 无外键级联，只能靠应用层守住：

    - 测试项不得挂到拓扑外的工位（否则是"没人跑却计入统计"的孤儿必测项）
    - 工步被移出拓扑时，其测试项须连带清理
    - 工位字典被测试项引用时拒绝删除；删除流程则连带清理
    """
    pid = f"PROC_GUARD-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "item guard"})
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [
            {"station_id": "CAL_PARAM", "step_order": 10, "depends_on": []},
            {"station_id": "CAL_IFACE", "step_order": 20, "depends_on": ["CAL_PARAM"]},
        ],
        params={"process_id": pid},
    )

    def create_item(station_id, case_id):
        return admin(
            "POST",
            "/api/admin/routing/items",
            {"process_id": pid, "station_id": station_id, "case_id": case_id, "item_name": case_id},
        )

    resp = create_item("CAL_PARAM", "tests/guard_a.py::test_one")
    check(resp.status_code == 201, f"item in topology must be 201: {resp.text}")

    resp = create_item("TST_PARAM", "tests/guard_b.py::test_two")
    check(
        resp.status_code == 400 and resp.json()["code"] == "station_not_in_process",
        f"item outside topology must be 400: {resp.text}",
    )

    create_item("CAL_IFACE", "tests/guard_c.py::test_three")
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL_PARAM", "step_order": 10, "depends_on": []}],
        params={"process_id": pid},
    )
    resp = admin("GET", "/api/admin/routing/items", params={"process_id": pid})
    stations = {i["station_id"] for i in resp.json()}
    check("CAL_IFACE" not in stations, f"removed step items must be purged: {stations}")
    check("CAL_PARAM" in stations, f"kept step items must survive: {stations}")

    # 工位是跨流程共享字典，被测试项引用时拒绝删除
    orphan_station = f"S_GUARD-{STAMP}"
    admin("POST", "/api/admin/stations", {"station_id": orphan_station, "station_name": "orphan"})
    with SessionLocal() as db:
        db.add(
            models.StationItem(
                process_id=pid, station_id=orphan_station, case_id="legacy::one", item_name="legacy"
            )
        )
        db.commit()
    resp = admin("DELETE", f"/api/admin/stations/{orphan_station}")
    check(
        resp.status_code == 409 and resp.json()["code"] == "station_has_items",
        f"station referenced by items must be 409: {resp.text}",
    )

    # 清空拓扑 → 原工步的测试项连带清理（从未进过拓扑的孤儿项由删除流程兜底）
    admin("PUT", "/api/admin/routing/stations", [], params={"process_id": pid})
    resp = admin("GET", "/api/admin/routing/items", params={"process_id": pid})
    stations = {i["station_id"] for i in resp.json()}
    check("CAL_PARAM" not in stations, f"items must be purged with topology: {stations}")

    # 存量孤儿项不得让流程永远删不掉
    with SessionLocal() as db:
        db.add(
            models.StationItem(
                process_id=pid, station_id=orphan_station, case_id="legacy::two", item_name="legacy2"
            )
        )
        db.commit()
    resp = admin("DELETE", f"/api/admin/processes/{pid}")
    check(resp.status_code == 204, f"delete process with orphan items: {resp.text}")
    with SessionLocal() as db:
        left = db.query(models.StationItem).filter(models.StationItem.process_id == pid).count()
    check(left == 0, f"process items must be purged, left={left}")

    resp = admin("DELETE", f"/api/admin/stations/{orphan_station}")
    check(resp.status_code == 204, f"station deletable after purge: {resp.text}")


def test_client_unbound_state():
    """上位机自动注册的机台处于未绑定态：可保持解绑、可改 IP，进站时 403。"""
    cid = f"CLI-{STAMP}-UNBOUND"
    resp = v1("resolve", {"client_id": cid, "ip_address": "10.1.60.99"})
    check(resp.status_code == 200, f"auto register: {resp.text}")
    check(resp.json()["data"]["bound"] is False, f"auto registered must be unbound: {resp.text}")

    # 未绑定机台也要能改 IP：不传 station_id 即保持解绑
    resp = admin("PUT", f"/api/admin/clients/{cid}", {"ip_address": "10.1.60.100"})
    check(resp.status_code == 200, f"update ip: {resp.text}")
    check(resp.json()["ip_address"] == "10.1.60.100", f"ip updated: {resp.text}")
    check(not resp.json()["station_id"], f"must stay unbound: {resp.text}")

    resp = admin("PUT", f"/api/admin/clients/{cid}", {"station_id": "CAL_PARAM"})
    check(resp.status_code == 200 and resp.json()["station_id"] == "CAL_PARAM", f"bind: {resp.text}")
    resp = admin("PUT", f"/api/admin/clients/{cid}", {"station_id": ""})
    check(resp.status_code == 200 and not resp.json()["station_id"], f"unbind: {resp.text}")

    resp = v1(
        "check-in",
        {"client_id": cid, "sn": new_sn("UNBOUND"), "product_model": M_DPO, "firmware": FW},
    )
    check(
        resp.status_code == 403 and resp.json()["code"] == "client_not_bound",
        f"unbound check-in must be 403: {resp.text}",
    )
    admin("DELETE", f"/api/admin/clients/{cid}")


def test_clone_process():
    target = f"PROC_CLONE-{STAMP}"
    resp = admin("POST", "/api/admin/routing/clone", {"from_process": P_MSO, "to_process": target})
    check(resp.status_code == 200, f"clone: {resp.text}")
    data = resp.json()
    check(data["cloned_steps"] == 6, f"clone steps: {data}")
    check(data["cloned_items"] == MSO_ITEM_COUNT, f"clone items: {data}")
    admin("DELETE", f"/api/admin/processes/{target}")


# ================= B. 正常流转 =================
def test_dpo_full_flow():
    """DPO 无 AWG：4 站流程，AWG 站位从未出现。"""
    sn = new_sn("DPO-OK")
    for station in ("CAL_PARAM", "CAL_IFACE", "TST_PARAM", "TST_IFACE"):
        resp = checkin(station, sn)
        check(resp.status_code == 200, f"DPO check-in {station}: {resp.status_code} {resp.text}")
        case_ids = rules_of(resp)
        resp = checkout(station, sn, case_ids)
        check(resp.status_code == 201, f"DPO check-out {station}: {resp.status_code} {resp.text}")
        check(resp.json()["data"]["overall_result"] == "PASS", f"result {station}: {resp.text}")

    # 首工位动态建档
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.status_code == 200, f"product created: {resp.text}")
    check(resp.json()["process_id"] == P_DPO, "process binding")
    check(resp.json()["current_fw_version"] == FW, "firmware recorded")
    check(resp.json()["total_steps"] == 4, f"DPO should have 4 steps: {resp.json()}")
    check(resp.json()["is_completed"] is True, f"should be completed: {resp.json()}")
    check(
        sorted(resp.json()["passed_stations"]) == ["CAL_IFACE", "CAL_PARAM", "TST_IFACE", "TST_PARAM"],
        f"stamps: {resp.json()}",
    )


def test_mso_full_flow():
    """MSO 带 AWG：6 站完整流程，且校准三站未齐前不得进入测试阶段。"""
    sn = new_sn("MSO-OK")
    for station in ("CAL_PARAM", "CAL_IFACE", "CAL_AWG"):
        resp = checkin(station, sn, model=M_MSO)
        check(resp.status_code == 200, f"MSO check-in {station}: {resp.status_code} {resp.text}")
        resp = checkout(station, sn, rules_of(resp))
        check(resp.status_code == 201, f"MSO check-out {station}: {resp.status_code} {resp.text}")

    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["total_steps"] == 6, "MSO should have 6 steps")

    # 补齐剩余三站
    for station in ("TST_PARAM", "TST_IFACE", "TST_AWG"):
        resp = checkin(station, sn, model=M_MSO)
        check(resp.status_code == 200, f"MSO check-in {station}: {resp.status_code} {resp.text}")
        resp = checkout(station, sn, rules_of(resp))
        check(resp.status_code == 201, f"MSO check-out {station}: {resp.status_code} {resp.text}")

    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["is_completed"] is True, f"MSO completed: {resp.text}")


def test_mso_gate_requires_all_cal():
    """MSO：校准三站未齐 → TST_PARAM 拦截（方案中的闸门工位）。"""
    sn = new_sn("MSO-GATE")
    for station in ("CAL_PARAM", "CAL_IFACE"):
        resp = checkin(station, sn, model=M_MSO)
        check(resp.status_code == 200, f"check-in {station}: {resp.text}")
        checkout(station, sn, rules_of(resp))

    resp = checkin("TST_PARAM", sn, model=M_MSO)
    check(resp.status_code == 403, f"gate must be 403: {resp.status_code} {resp.text}")
    check(resp.json()["data"]["missing"] == ["CAL_AWG"], f"missing: {resp.json()}")


# ================= C. 需求 2：卡控 =================
def test_jump_station_blocked_403():
    """跳过 CAL_IFACE 直奔 TST_PARAM → 403。"""
    sn = new_sn("JUMP")
    pass_station("CAL_PARAM", sn)

    resp = checkin("TST_PARAM", sn)
    check(resp.status_code == 403, f"jump must be 403, got {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["code"] == "missing_prereq", f"code: {body}")
    check(body["exit_code"] == 10, f"exit_code: {body}")
    check("CAL_IFACE" in body["data"]["missing"], f"missing: {body}")


def test_retest_blocked_409():
    """已盖章工位严禁复测 → 409。"""
    sn = new_sn("RETEST")
    pass_station("CAL_PARAM", sn)

    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 409, f"retest must be 409, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "station_already_passed", f"code: {resp.json()}")


def test_wrong_process_blocked_400():
    """DPO 机型进入 AWG 工位（不在其流程中）→ 400。"""
    sn = new_sn("WRONGP")
    resp = checkin("TST_AWG", sn, model=M_DPO)
    check(resp.status_code == 400, f"wrong process must be 400, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "station_not_in_process", f"code: {resp.json()}")


def test_firmware_mismatch_403():
    sn = new_sn("FW")
    resp = checkin("CAL_PARAM", sn, firmware="V3.10")
    check(resp.status_code == 403, f"fw mismatch must be 403, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "firmware_mismatch", f"code: {resp.json()}")


def test_case_id_mismatch_400():
    """上报的用例ID未覆盖必测清单 → 400。"""
    sn = new_sn("CASEID")
    resp = checkin("CAL_PARAM", sn, case_ids=[ITEMS["CAL_PARAM"][0]])
    check(resp.status_code == 400, f"case_id mismatch must be 400, got {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["code"] == "case_id_mismatch", f"code: {body}")
    check(body["exit_code"] == 15, f"exit_code: {body}")
    check(len(body["data"]["missing_case_ids"]) > 0, f"missing: {body}")

    # 完整清单放行
    resp = checkin("CAL_PARAM", sn, case_ids=list(ITEMS["CAL_PARAM"]))
    check(resp.status_code == 200, f"full case IDs should pass: {resp.text}")


def test_first_station_required():
    """未建档的机器不得从中间工位开局。"""
    sn = new_sn("MIDDLE")
    resp = checkin("TST_IFACE", sn)
    check(resp.status_code == 403, f"must start at first station: {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "missing_prereq", f"code: {resp.json()}")


def test_model_mismatch():
    sn = new_sn("MODELM")
    pass_station("CAL_PARAM", sn, model=M_DPO)
    resp = checkin("CAL_IFACE", sn, model=M_MSO)
    check(resp.status_code == 403 and resp.json()["code"] == "model_mismatch", f"{resp.status_code} {resp.text}")


# ================= D. 需求 1：落库 ACK =================
def test_ack_and_idempotent_replay():
    sn = new_sn("ACK")
    pass_station("CAL_PARAM", sn)
    resp = checkin("CAL_IFACE", sn)
    case_ids = rules_of(resp)
    cid = uuid.uuid4().hex

    first = checkout("CAL_IFACE", sn, case_ids, checkout_id=cid)
    check(first.status_code == 201, f"checkout: {first.status_code} {first.text}")
    body = first.json()
    check(body["data"]["acknowledged"] is True, f"ack flag: {body}")
    check(body["data"]["idempotent_replay"] is False, f"first should not be replay: {body}")
    record_id = body["data"]["record_id"]

    # 网络重传：同一 checkout_id
    second = checkout("CAL_IFACE", sn, case_ids, checkout_id=cid)
    check(second.status_code == 201, f"replay: {second.status_code} {second.text}")
    check(second.json()["data"]["idempotent_replay"] is True, f"replay flag: {second.json()}")
    check(second.json()["data"]["record_id"] == record_id, "record_id must be identical")

    # 独立 ack 校验端点
    resp = v1("ack", None, method="GET")
    check(resp.status_code == 422, "sn/checkout_id required")
    resp = api(
        "GET", "/api/v1/client/ack", api_key=API_KEY, params={"sn": sn, "checkout_id": cid}
    )
    check(resp.status_code == 200, f"ack endpoint: {resp.status_code} {resp.text}")
    check(resp.json()["data"]["record_id"] == record_id, "ack record mismatch")

    resp = api(
        "GET", "/api/v1/client/ack", api_key=API_KEY, params={"sn": sn, "checkout_id": "not-exist"}
    )
    check(resp.status_code == 404 and resp.json()["code"] == "ack_not_found", f"ack 404: {resp.text}")

    # 账本只应有一条
    resp = admin("GET", "/api/admin/records", params={"sn": sn})
    check(resp.json()["total"] == 2, f"records should be 2: {resp.json()['total']}")


# ================= E. 漏测与锁定 =================
def test_missing_mandatory_400():
    sn = new_sn("MISSING")
    resp = checkin("CAL_PARAM", sn)
    case_ids = rules_of(resp)
    session_id = resp.json()["data"]["session_id"]
    # 漏掉一个必测项
    resp = checkout("CAL_PARAM", sn, case_ids[:-1])
    check(resp.status_code == 400, f"missing mandatory must be 400, got {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["code"] == "missing_mandatory", f"code: {body}")
    check(body["exit_code"] == 13, f"exit_code: {body}")

    # 漏测拦截必须同时关闭会话并释放锁，否则会残留 RUNNING 会话
    with SessionLocal() as db:
        session = db.get(models.TestSession, session_id)
        check(session.status == models.SESSION_ABORTED, f"session must be aborted, got {session.status}")
        product = db.get(models.ProductStatus, sn)
        check(product.current_status == models.STATUS_IDLE, f"lock must be released: {product.current_status}")
        check(product.lock_token is None, "lock_token must be cleared")

    # SKIP 视为漏测
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"re-enter after fail: {resp.text}")
    resp = checkout("CAL_PARAM", sn, case_ids, skip=(case_ids[-1],))
    check(resp.status_code == 400 and resp.json()["code"] == "missing_mandatory", f"skip: {resp.text}")

    # 已执行但判定 FAIL → 真实失败，201 + exit_code 1，不拦截
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"re-enter: {resp.text}")
    resp = checkout("CAL_PARAM", sn, case_ids, fail=(case_ids[0],))
    check(resp.status_code == 201, f"fail should be recorded: {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["exit_code"] == 1 and body["data"]["overall_result"] == "FAIL", f"exit_code: {body}")


def test_fail_streak_lock_403():
    sn = new_sn("LOCK")
    for i in range(3):
        resp = checkin("CAL_PARAM", sn)
        check(resp.status_code == 200, f"check-in round {i}: {resp.text}")
        case_ids = rules_of(resp)
        resp = checkout("CAL_PARAM", sn, case_ids, fail=(case_ids[0],))
        check(resp.status_code == 201 and resp.json()["data"]["overall_result"] == "FAIL", f"round {i}: {resp.text}")
        locked = resp.json()["data"]["product_locked"]
        check(locked is (i == 2), f"round {i} lock flag: {resp.json()['data']}")

    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 403, f"locked must be 403, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "product_locked", f"code: {resp.json()}")
    check(resp.json()["exit_code"] == 16, f"exit_code: {resp.json()}")

    # RETEST 处置后解锁
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "工程师确认后重投"})
    check(resp.status_code == 201, f"reset: {resp.text}")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"unlocked: {resp.text}")


def test_lock_conflict_409_and_timeout_takeover():
    sn = new_sn("CONFLICT")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")

    # 另一台同工位机台抢占 → 409
    other = f"CLI-{STAMP}-OTHER"
    admin("POST", "/api/admin/clients", {"client_id": other, "station_id": "CAL_PARAM"})
    resp = v1(
        "check-in",
        {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW},
    )
    check(resp.status_code == 409, f"conflict must be 409, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "lock_conflict", f"code: {resp.json()}")

    # 心跳续期
    resp = v1("heartbeat", {"client_id": CLIENTS["CAL_PARAM"], "sn": sn})
    check(resp.status_code == 200 and resp.json()["data"]["holding_lock"] is True, f"heartbeat: {resp.text}")
    check(resp.json()["data"]["remaining_sec"] > 0, "remaining should be positive")

    # 人为把持锁开始时间推到硬超时之前 → 允许接管（心跳不续期硬超时）
    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        product.testing_started_at = datetime.now(timezone.utc) - timedelta(seconds=1900)
        product.lock_acquired_at = datetime.now(timezone.utc) - timedelta(seconds=1900)
        db.commit()

    resp = v1(
        "check-in",
        {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW},
    )
    check(resp.status_code == 200, f"takeover after timeout: {resp.status_code} {resp.text}")

    # 原持锁方超时后出站 → 403 lock_expired
    resp = checkout("CAL_PARAM", sn, rules_of(resp))
    check(resp.status_code == 403, f"stale lock must be 403, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "lock_invalid", f"code: {resp.json()}")


def test_scrap_blocked():
    sn = new_sn("SCRAP")
    pass_station("CAL_PARAM", sn)
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "SCRAP", "reason": "主板损伤"})
    check(resp.status_code == 201, f"scrap: {resp.text}")
    resp = checkin("CAL_IFACE", sn)
    check(resp.status_code == 403 and resp.json()["code"] == "product_scrapped", f"{resp.status_code} {resp.text}")


# ================= F. 维修处置 =================
def test_repair_actions():
    sn = new_sn("REPAIR")
    for station in ("CAL_PARAM", "CAL_IFACE", "TST_PARAM"):
        pass_station(station, sn)

    # RETEST：收回 TST_PARAM 印章，作废旧记录
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "TST_PARAM", "reason": "抽检漂移"},
    )
    check(resp.status_code == 201, f"retest: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check("TST_PARAM" not in resp.json()["passed_stations"], f"stamp removed: {resp.json()}")
    resp = admin("GET", "/api/admin/records", params={"sn": sn, "station_id": "TST_PARAM", "is_valid": False})
    check(resp.json()["total"] == 1, f"invalidated record: {resp.json()['total']}")

    # 重测通过
    pass_station("TST_PARAM", sn)

    # ROLLBACK：回退到 CAL_IFACE（清除其及之后所有印章）
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "ROLLBACK", "target_station": "CAL_IFACE", "reason": "批量异常"},
    )
    check(resp.status_code == 201, f"rollback: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(sorted(resp.json()["passed_stations"]) == ["CAL_PARAM"], f"rollback result: {resp.json()}")

    # SCRAP
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "SCRAP", "reason": "报废"})
    check(resp.status_code == 201, f"scrap: {resp.text}")
    # SCRAP 后仍可 RESET 复活
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "复判合格"})
    check(resp.status_code == 201, f"reset: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["current_status"] == "IDLE" and resp.json()["passed_stations"] == [], f"reset: {resp.json()}")

    resp = admin("GET", "/api/admin/repairs", params={"sn": sn})
    check(resp.json()["total"] == 4, f"repair history: {resp.json()['total']}")


def test_repair_guards():
    sn = new_sn("RGUARD")
    pass_station("CAL_PARAM", sn)
    # 目标工位未通过
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "TST_PARAM", "reason": "x"},
    )
    check(resp.status_code == 400 and resp.json()["code"] == "station_not_passed", f"{resp.text}")
    # 持锁中禁止处置
    checkin("CAL_IFACE", sn)
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "x"})
    check(resp.status_code == 409 and resp.json()["code"] == "product_holding_lock", f"{resp.text}")


# ================= G. 台账与统计 =================
def test_trace():
    sn = new_sn("TRACE")
    for station in ("CAL_PARAM", "CAL_IFACE"):
        pass_station(station, sn)

    resp = admin("GET", f"/api/admin/records/trace/{sn}")
    check(resp.status_code == 200, f"trace: {resp.text}")
    body = resp.json()
    check(len(body["steps"]) == 4, f"DPO trace steps: {len(body['steps'])}")
    check(body["product"]["process_id"] == P_DPO, "trace process")
    passed_steps = [s for s in body["steps"] if s["passed"]]
    check(len(passed_steps) == 2, f"passed steps: {passed_steps}")
    check(len(body["records"]) == 2, f"records: {len(body['records'])}")
    # 工步按 step_order 排序
    orders = [s["step_order"] for s in body["steps"]]
    check(orders == sorted(orders), f"step order: {orders}")


def test_metrics():
    resp = admin("GET", "/api/admin/metrics/overview", params={"days": 14})
    check(resp.status_code == 200, f"overview: {resp.text}")
    body = resp.json()
    check(body["wip"]["total"] > 0, f"wip: {body['wip']}")
    check(len(body["trend"]) == 14, f"trend: {len(body['trend'])}")
    check(any(r["key"] == "CAL_PARAM" for r in body["station_yield"]), f"station yield: {body['station_yield']}")
    check(any(r["key"] == P_DPO for r in body["process_yield"]), f"process yield: {body['process_yield']}")
    check(body["clients"]["total"] >= 6, f"clients: {body['clients']}")

    resp = admin("GET", "/api/admin/metrics/overview", params={"days": 7, "process_id": P_MSO})
    check(resp.status_code == 200 and len(resp.json()["trend"]) == 7, f"filtered: {resp.text}")


def test_no_bilingual():
    """后端只输出英文，语种由前端 i18n 决定。"""
    resp = api(
        "POST",
        "/api/v1/client/resolve",
        body={"client_id": CLIENTS["CAL_PARAM"]},
        api_key=API_KEY,
        headers={"Accept-Language": "zh-CN"},
    )
    zh = resp.json()["message"]
    resp = api(
        "POST",
        "/api/v1/client/resolve",
        body={"client_id": CLIENTS["CAL_PARAM"]},
        api_key=API_KEY,
        headers={"Accept-Language": "en-US"},
    )
    check(resp.json()["message"] == zh, "message must be language-independent")


def test_auth_guard():
    resp = api("GET", "/api/admin/clients", token="invalid")
    check(resp.status_code == 401 and resp.json()["code"] == "authentication_required", f"{resp.text}")
    resp = api("POST", "/api/v1/client/heartbeat", body={}, api_key="wrong-key")
    check(resp.status_code == 401 and resp.json()["code"] == "invalid_credentials", f"{resp.text}")


# ================= H. 租约锁：崩溃续测与快速接管 =================
def _backdate(sn, **deltas):
    """把锁时间戳往前推，模拟失联 / 长跑。"""
    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        for field, seconds in deltas.items():
            setattr(product, field, datetime.now(timezone.utc) - timedelta(seconds=seconds))
        db.commit()


def test_status_filter_separates_completed_from_idle():
    """已完工是派生状态：筛"待测试"不得混入，筛"已完工"应能单独查到。"""
    done_sn = new_sn("WIPDONE")
    for station in ("CAL_PARAM", "CAL_IFACE", "TST_PARAM", "TST_IFACE"):
        pass_station(station, done_sn)

    resp = admin("GET", "/api/admin/products", params={"current_status": "COMPLETED", "sn": done_sn})
    check(resp.status_code == 200, f"completed filter: {resp.text}")
    check(resp.json()["total"] == 1, f"已完工应能筛出: {resp.json()['total']}")

    resp = admin("GET", "/api/admin/products", params={"current_status": "IDLE", "sn": done_sn})
    check(resp.status_code == 200, f"idle filter: {resp.text}")
    check(resp.json()["total"] == 0, f"已完工不得出现在待测试: {resp.json()['total']}")

    wip_sn = new_sn("WIPIDLE")
    pass_station("CAL_PARAM", wip_sn)
    resp = admin("GET", "/api/admin/products", params={"current_status": "IDLE", "sn": wip_sn})
    check(resp.json()["total"] == 1, f"未完工在制品应在待测试: {resp.json()['total']}")


def test_lock_lost_takeover():
    """机台失联（心跳断流 > grace）→ 新机台立即接管，旧持锁方写入被拒。"""
    sn = new_sn("LOST")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    stale_token = resp.json()["data"]["lock_token"]
    check(bool(stale_token), "lock_token must be issued")

    other = f"CLI-{STAMP}-LOST2"
    admin("POST", "/api/admin/clients", {"client_id": other, "station_id": "CAL_PARAM"})

    # 未失联 → 仍然 409
    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 409, f"still locked: {resp.status_code} {resp.text}")

    # 心跳断流 200s（> grace 120s），但持锁总时长远未到硬超时 → 可接管
    _backdate(sn, lock_last_seen_at=200)
    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 200, f"takeover: {resp.status_code} {resp.text}")
    body = resp.json()["data"]
    check(body["takeover"] is True, f"takeover flag: {body}")
    check(body["takeover_from"] == CLIENTS["CAL_PARAM"], f"takeover_from: {body}")
    check(body["lock_token"] != stale_token, "新持锁方必须换发 token")

    # 旧持锁方持旧 token 出站 → 403，绝不污染数据
    resp = v1(
        "check-out",
        {
            "client_id": CLIENTS["CAL_PARAM"],
            "sn": sn,
            "checkout_id": uuid.uuid4().hex,
            "items": items_for(["test_amp_cal", "test_phase_cal"]),
            "lock_token": stale_token,
        },
    )
    check(resp.status_code == 403, f"zombie checkout: {resp.status_code} {resp.text}")


def test_heartbeat_does_not_extend_hard_timeout():
    """心跳只续失联窗口，不续硬超时。"""
    sn = new_sn("LEASE")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    first_lease = resp.json()["data"]["timeout_sec"]

    resp = v1("heartbeat", {"client_id": CLIENTS["CAL_PARAM"], "sn": sn})
    body = resp.json()["data"]
    check(body["lease_remaining_sec"] <= first_lease, "心跳不得延长硬超时")
    check(body["heartbeat_count"] == 1, f"heartbeat_count: {body}")

    # 心跳新鲜（未失联）但持锁已超硬超时 → 出站判 lock_expired
    _backdate(sn, lock_acquired_at=1900, testing_started_at=1900)
    resp = checkout("CAL_PARAM", sn, ["test_amp_cal", "test_phase_cal"])
    check(resp.status_code == 403 and resp.json()["code"] == "lock_expired", f"{resp.status_code} {resp.text}")


def test_checkpoint_resume_and_merge():
    """崩溃续测：断点上报 → 重进站拿到已完成清单 → 出站补齐未提交用例。"""
    sn = new_sn("RESUME")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    data = resp.json()["data"]
    session_id = data["session_id"]
    token = data["lock_token"]

    # 崩溃前只上报了 1 个用例
    resp = v1(
        "checkpoint",
        {
            "client_id": CLIENTS["CAL_PARAM"],
            "sn": sn,
            "session_id": session_id,
            "lock_token": token,
            "items": items_for(["test_amp_cal"]),
            "cursor": {"step": 1},
        },
    )
    check(resp.status_code == 200, f"checkpoint: {resp.text}")
    check(resp.json()["data"]["completed_case_ids"] == ["test_amp_cal"], f"checkpoint: {resp.text}")

    # 重复上报幂等
    resp = v1(
        "checkpoint",
        {
            "client_id": CLIENTS["CAL_PARAM"],
            "sn": sn,
            "session_id": session_id,
            "lock_token": token,
            "items": items_for(["test_amp_cal"]),
        },
    )
    check(resp.json()["data"]["merged_count"] == 1, f"idempotent: {resp.text}")

    # 崩溃重启：同机台重进站自动续测
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"re-checkin: {resp.text}")
    data = resp.json()["data"]
    check(data["session_id"] == session_id, "必须复用同一会话")
    check(data["attempt"] == 2, f"attempt: {data}")
    check(data["resume"]["resumed"] is True, f"resume: {data}")
    check(data["resume"]["completed_case_ids"] == ["test_amp_cal"], f"resume: {data}")
    check(data["resume"]["cursor"] == {"step": 1}, f"cursor: {data}")

    # 只跑剩余用例即可出站，未提交的由 checkpoint 补齐
    resp = checkout("CAL_PARAM", sn, ["test_phase_cal"], lock_token=data["lock_token"])
    check(resp.status_code == 201, f"checkout: {resp.status_code} {resp.text}")
    ack = resp.json()["data"]
    check(ack["checkpoint_merged_count"] == 1, f"merged: {ack}")
    resp = admin("GET", f"/api/admin/records/trace/{sn}")
    record = [r for r in resp.json()["records"] if r["station_id"] == "CAL_PARAM"][0]
    case_ids = sorted(i["case_id"] for i in record["executed_items"]["items"])
    check(case_ids == ["test_amp_cal", "test_phase_cal"], f"merged items: {case_ids}")


def test_sweeper_releases_orphan_lock():
    """回收任务：失联锁自动释放，且不计产品失败。"""
    from app.services.sweeper import run_once

    sn = new_sn("SWEEP")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    session_id = resp.json()["data"]["session_id"]

    _backdate(sn, lock_last_seen_at=200)
    stats = run_once()
    check(stats.get("lost", 0) >= 1, f"sweeper stats: {stats}")

    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        check(product.current_status == models.STATUS_IDLE, f"status: {product.current_status}")
        check(product.lock_token is None, "锁必须释放")
        check(product.fail_count == 0, "失联不得计入产品失败")
        session = db.get(models.TestSession, session_id)
        check(session.status == models.SESSION_ABORTED, f"session: {session.status}")

    # 解锁后新机台可直接进站
    other = f"CLI-{STAMP}-SWEEP2"
    admin("POST", "/api/admin/clients", {"client_id": other, "station_id": "CAL_PARAM"})
    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 200, f"re-checkin after sweep: {resp.text}")


def test_client_app_version_tracked():
    """机台上报的程序版本要落库：现场"同机型结果不可比"的常见根因是版本漂移。"""
    cid = f"CLI-{STAMP}-VER"
    resp = v1("resolve", {"client_id": cid, "ip_address": "10.1.60.77", "app_version": "2.4.1"})
    check(resp.status_code == 200, f"resolve: {resp.text}")

    def fetch():
        return next(
            (c for c in admin("GET", "/api/admin/clients").json() if c["client_id"] == cid), None
        )

    row = fetch()
    check(row is not None, f"auto registered client must exist: {cid}")
    check(row["app_version"] == "2.4.1", f"app_version must persist: {row}")
    check(bool(row["created_at"]), f"created_at must be set: {row}")
    check(row["station_id"] is None, f"auto registered must be unbound: {row}")

    # 进站同样刷新版本（此前 CheckInIn 收了 app_version 却没落库）
    admin("PUT", f"/api/admin/clients/{cid}", {"station_id": "CAL_PARAM"})
    resp = v1(
        "check-in",
        {
            "client_id": cid,
            "sn": new_sn("VER"),
            "product_model": M_DPO,
            "firmware": FW,
            "app_version": "2.5.0",
        },
    )
    check(resp.status_code == 200, f"check-in: {resp.text}")
    check(fetch()["app_version"] == "2.5.0", f"app_version must refresh on check-in: {fetch()}")

    # 手工注册也允许登记版本
    manual = f"CLI-{STAMP}-VERM"
    resp = admin(
        "POST",
        "/api/admin/clients",
        {"client_id": manual, "station_id": "CAL_PARAM", "app_version": "2.6.0"},
    )
    check(resp.status_code == 201 and resp.json()["app_version"] == "2.6.0", f"manual register: {resp.text}")

    admin("DELETE", f"/api/admin/clients/{cid}")
    admin("DELETE", f"/api/admin/clients/{manual}")


def test_firmware_match_rule():
    """fw_match_rule：exact 要求完全一致；min 只要求不低于基线。

    min 规则下必须按数字段比较，否则 "V3.9" > "V3.20" 的字典序会放行旧固件。
    """
    pid = f"PROC_FWRULE-{STAMP}"
    model = f"MODEL_FWRULE-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "fw rule"})
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL_PARAM", "step_order": 10, "depends_on": []}],
        params={"process_id": pid},
    )
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {
            "product_model": model,
            "process_id": pid,
            "target_fw_version": "V3.20",
            "fw_match_rule": "min",
        },
    )
    check(resp.status_code == 201, f"create model: {resp.text}")
    check(resp.json()["fw_match_rule"] == "min", f"rule must persist: {resp.text}")

    def try_firmware(firmware, tag):
        # 每次换机台 + 换 SN，避免工位锁互相干扰
        cid = f"CLI-{STAMP}-FW-{tag}"
        admin("POST", "/api/admin/clients", {"client_id": cid, "station_id": "CAL_PARAM"})
        return v1(
            "check-in",
            {"client_id": cid, "sn": new_sn(f"FW-{tag}"), "product_model": model, "firmware": firmware},
        )

    resp = try_firmware("V3.20", "EQ")
    check(resp.status_code == 200, f"equal must pass under min: {resp.text}")
    resp = try_firmware("V3.21", "HI")
    check(resp.status_code == 200, f"higher must pass under min: {resp.text}")
    resp = try_firmware("V3.9", "LOW")
    check(
        resp.status_code == 403 and resp.json()["code"] == "firmware_mismatch",
        f"V3.9 is below V3.20 and must be blocked: {resp.text}",
    )

    # 切回 exact：高于基线也不再放行
    resp = admin("PUT", f"/api/admin/product-models/{model}", {"fw_match_rule": "exact"})
    check(resp.status_code == 200 and resp.json()["fw_match_rule"] == "exact", f"switch rule: {resp.text}")
    resp = try_firmware("V3.21", "EXACT")
    check(
        resp.status_code == 403 and resp.json()["code"] == "firmware_mismatch",
        f"exact must reject higher version: {resp.text}",
    )

    resp = admin("PUT", f"/api/admin/product-models/{model}", {"fw_match_rule": "bogus"})
    check(resp.status_code == 422, f"invalid rule must be 422: {resp.text}")


def test_process_status():
    """流程停用后不再接受新机型绑定，重新启用后恢复。"""
    pid = f"PROC_VER-{STAMP}"
    resp = admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "status demo"})
    check(resp.status_code == 201, f"create process: {resp.text}")
    check(resp.json()["is_active"] is True, f"default must be active: {resp.text}")

    resp = admin("PUT", f"/api/admin/processes/{pid}", {"is_active": False})
    check(resp.status_code == 200 and resp.json()["is_active"] is False, f"deactivate: {resp.text}")

    model = f"MODEL_VER-{STAMP}"
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {"product_model": model, "process_id": pid, "target_fw_version": "V1.0"},
    )
    check(
        resp.status_code == 409 and resp.json()["code"] == "process_inactive",
        f"inactive process must reject new model: {resp.text}",
    )

    resp = admin("PUT", f"/api/admin/processes/{pid}", {"is_active": True})
    check(resp.status_code == 200 and resp.json()["is_active"] is True, f"reactivate: {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {"product_model": model, "process_id": pid, "target_fw_version": "V1.0"},
    )
    check(resp.status_code == 201, f"reactivated process accepts model: {resp.text}")


def test_force_release_and_sessions_api():
    """运维强制解锁 + 会话管理接口。"""
    sn = new_sn("FORCE")
    resp = checkin("CAL_PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    session_id = resp.json()["data"]["session_id"]

    resp = admin("POST", f"/api/admin/products/{sn}/force-release", {"reason": "机台蓝屏"})
    check(resp.status_code == 200, f"force-release: {resp.text}")
    check(resp.json()["previous_client"] == CLIENTS["CAL_PARAM"], f"previous: {resp.text}")

    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        check(product.current_status == models.STATUS_IDLE, f"status after force: {product.current_status}")

    resp = admin("GET", "/api/admin/sessions", params={"sn": sn})
    check(resp.status_code == 200, f"sessions: {resp.text}")
    check(resp.json()["total"] >= 1, f"session total: {resp.text}")

    resp = admin("GET", f"/api/admin/sessions/{session_id}")
    check(resp.status_code == 200 and resp.json()["session_id"] == session_id, f"detail: {resp.text}")
    check(resp.json()["end_reason"] == "force_release: 机台蓝屏", f"reason: {resp.text}")

    resp = admin("GET", "/api/admin/sessions", params={"abnormal_only": True})
    check(resp.status_code == 200 and resp.json()["total"] >= 1, f"abnormal: {resp.text}")

    resp = admin("GET", f"/api/admin/products/{sn}/sessions")
    check(resp.status_code == 200 and len(resp.json()) >= 1, f"timeline: {resp.text}")

    resp = admin("GET", "/api/admin/metrics/overview", params={"days": 14})
    check("locks" in resp.json(), f"metrics locks: {resp.text}")


TESTS = [
    test_health,
    test_masters_seed,
    test_topology_and_items,
    test_validate_ok_and_cycle,
    test_delete_guards,
    test_station_item_guards,
    test_client_unbound_state,
    test_clone_process,
    test_dpo_full_flow,
    test_mso_full_flow,
    test_mso_gate_requires_all_cal,
    test_jump_station_blocked_403,
    test_retest_blocked_409,
    test_wrong_process_blocked_400,
    test_firmware_mismatch_403,
    test_case_id_mismatch_400,
    test_first_station_required,
    test_model_mismatch,
    test_ack_and_idempotent_replay,
    test_missing_mandatory_400,
    test_fail_streak_lock_403,
    test_lock_conflict_409_and_timeout_takeover,
    test_scrap_blocked,
    test_repair_actions,
    test_repair_guards,
    test_trace,
    test_metrics,
    test_no_bilingual,
    test_auth_guard,
    test_status_filter_separates_completed_from_idle,
    test_lock_lost_takeover,
    test_heartbeat_does_not_extend_hard_timeout,
    test_checkpoint_resume_and_merge,
    test_sweeper_releases_orphan_lock,
    test_client_app_version_tracked,
    test_firmware_match_rule,
    test_process_status,
    test_force_release_and_sessions_api,
]

if __name__ == "__main__":
    print("=" * 70)
    print("ATE Manager 后端回归测试（用例ID驱动防漏测 / 防跳工位）")
    print("=" * 70)
    results = []
    for fn in TESTS:
        start = time.perf_counter()
        try:
            fn()
            elapsed = (time.perf_counter() - start) * 1000
            results.append((fn.__name__, "PASS", "", elapsed))
            print(f"[PASS]  {fn.__name__} ({elapsed:.0f}ms)")
        except AssertionError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            results.append((fn.__name__, "FAIL", str(exc), elapsed))
            print(f"[FAIL]  {fn.__name__} ({elapsed:.0f}ms) {exc}")
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            results.append((fn.__name__, "ERROR", repr(exc), elapsed))
            print(f"[ERROR] {fn.__name__} ({elapsed:.0f}ms) {exc!r}")

    failed = [r for r in results if r[1] != "PASS"]
    print("-" * 70)
    print(f"汇总：{len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for name, status, detail, _ in failed:
        print(f"  {status} {name}: {detail}")

    client.close()
    engine.dispose()
    try:
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)
    except OSError:
        pass
    sys.exit(1 if failed else 0)
