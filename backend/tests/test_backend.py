"""ATE Manager 后端回归测试（SQLite + FastAPI TestClient）。

运行：
    python tests/test_backend.py
    pytest tests/test_backend.py

覆盖方案的确定性场景：
    A. 静态规则：流程/机型/工位/机台 CRUD 与删除保护、拓扑校验
    B. 正常流转：DPO 4 站 / MSO 6 站全流程盖章
    C. 需求 2：跳站 403、复测 409、工艺不符 400、固件 403、用例ID 400
    D. 需求 1：落库 ACK、幂等重传、ack 校验端点
    E. 漏测拦截 400、连续失败锁定 403、锁超时接管、主动释放锁
    F. 维修处置：RETEST / ROLLBACK / RESET / SCRAP
    G. 台账追溯与仪表盘统计
"""

import itertools
import json
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
from app.config import settings  # noqa: E402
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
            role="admin",
        )
    )
    db.commit()

client = TestClient(app)

# ---------------- 夹具 ----------------
STAMP = int(time.time())
P_MSO = f"PROC-TEST-MSO-{STAMP}"
P_DPO = f"PROC-TEST-DPO-{STAMP}"
M_MSO = "MSO4054B"
M_DPO = "DPO4054B"
FW = "V3.20"

STATIONS = [
    ("CAL-PARAM", "校准-指标测试站位", 1800),
    ("CAL-IFACE", "校准-接口测试站位", 600),
    ("CAL-AWG", "校准-AWG站位", 300),
    ("TST-PARAM", "测试-指标测试站位", 1800),
    ("TST-IFACE", "测试-接口测试站位", 900),
    ("TST-AWG", "测试-AWG站位", 600),
]
CLIENTS = {
    "CAL-PARAM": "SZ-L1-CAL-01",
    "CAL-IFACE": "SZ-L1-CAL-02",
    "CAL-AWG": "SZ-L1-CAL-03",
    "TST-PARAM": "SZ-L1-TST-01",
    "TST-IFACE": "SZ-L1-TST-02",
    "TST-AWG": "SZ-L1-TST-03",
}
MSO_TOPOLOGY = [
    ("CAL-PARAM", 10, []),
    ("CAL-IFACE", 20, ["CAL-PARAM"]),
    ("CAL-AWG", 30, ["CAL-PARAM"]),
    ("TST-PARAM", 40, ["CAL-PARAM", "CAL-IFACE", "CAL-AWG"]),
    ("TST-IFACE", 50, ["TST-PARAM"]),
    ("TST-AWG", 60, ["TST-PARAM"]),
]
DPO_TOPOLOGY = [
    ("CAL-PARAM", 10, []),
    ("CAL-IFACE", 20, ["CAL-PARAM"]),
    ("TST-PARAM", 30, ["CAL-PARAM", "CAL-IFACE"]),
    ("TST-IFACE", 40, ["TST-PARAM"]),
]
ITEMS = {
    "CAL-PARAM": ["test_amp_cal", "test_phase_cal"],
    "CAL-IFACE": ["test_noise", "test_touch"],
    "CAL-AWG": ["test_1k_dc"],
    "TST-PARAM": ["test_timebase", "test_amp_ac"],
    "TST-IFACE": ["test_line_trig", "test_runt_trig"],
    "TST-AWG": ["test_aux_chk", "test_afg_sine_osc"],
}

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

_CLIENT_SEQ = itertools.count(1)

def new_client_id(stage: str = "CAL") -> str:
    """合规且唯一的机台编号（README 7.2）。

    测试专用 L9 产线，避免与夹具 CLIENTS 占用的 L1 序号冲突。
    """
    return f"SZ-L9-{stage}-{next(_CLIENT_SEQ):02d}"

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
            "POST",
            "/api/admin/clients",
            {"client_id": cid, "bound_stations": [station], "ip_address": "10.1.60.1"},
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
            if pid == P_DPO and sid in ("CAL-AWG", "TST-AWG"):
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

    bad = f"PROC-TEST-BAD-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": bad, "process_name": "cycle demo"})
    admin("POST", "/api/admin/stations", {"station_id": "TST-CYCLE1", "station_name": "TST-CYCLE1"})
    admin("POST", "/api/admin/stations", {"station_id": "TST-CYCLE2", "station_name": "TST-CYCLE2"})
    cycle = [
        {"station_id": "TST-CYCLE1", "step_order": 10, "depends_on": ["TST-CYCLE2"]},
        {"station_id": "TST-CYCLE2", "step_order": 20, "depends_on": ["TST-CYCLE1"]},
    ]
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

    # 依赖顺序倒置：step_order 与 deps 矛盾（CYCLE1 排 10 却依赖排 20 的 CYCLE2）
    # → error 级，非 force 保存 409 拦截。回退深度按 step_order、进站卡控按 deps，
    # 两者矛盾会导致回退边界与卡控不一致，保存即拦截。
    reverse = [
        {"station_id": "TST-CYCLE1", "step_order": 10, "depends_on": ["TST-CYCLE2"]},
        {"station_id": "TST-CYCLE2", "step_order": 20, "depends_on": []},
    ]
    resp = admin("PUT", "/api/admin/routing/stations", reverse, params={"process_id": bad})
    check(
        resp.status_code == 409 and "reverse_step_dep" in resp.json()["message"],
        f"reverse dep must block save: {resp.status_code} {resp.text}",
    )
    resp = admin(
        "PUT", "/api/admin/routing/stations", reverse, params={"process_id": bad, "force": True}
    )
    check(resp.status_code == 200, f"force save reverse: {resp.text}")
    resp = admin("GET", "/api/admin/routing/validate", params={"process_id": bad})
    codes = [i["code"] for i in resp.json()["issues"]]
    check("reverse_step_dep" in codes, f"validate reverse dep: {codes}")

def test_delete_guards():
    resp = admin("DELETE", f"/api/admin/processes/{P_MSO}")
    check(resp.status_code == 409 and "process_has_models" in resp.json()["code"], f"process guard: {resp.text}")
    resp = admin("DELETE", f"/api/admin/stations/CAL-PARAM")
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

    sn = new_sn("GUARD")
    pass_station("CAL-PARAM", sn)
    resp = admin("DELETE", f"/api/admin/product-models/{M_DPO}")
    check(resp.status_code == 409 and resp.json()["code"] == "model_in_use", f"model guard: {resp.text}")

def test_station_item_guards():
    """station_items 无外键级联，只能靠应用层守住：

    - 测试项不得挂到拓扑外的工位（否则是"没人跑却计入统计"的孤儿必测项）
    - 工步被移出拓扑时，其测试项须连带清理
    - 工位字典被测试项引用时拒绝删除；删除流程则连带清理
    """
    pid = f"PROC-TEST-GUARD-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "item guard"})
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [
            {"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []},
            {"station_id": "CAL-IFACE", "step_order": 20, "depends_on": ["CAL-PARAM"]},
        ],
        params={"process_id": pid},
    )

    def create_item(station_id, case_id):
        return admin(
            "POST",
            "/api/admin/routing/items",
            {"process_id": pid, "station_id": station_id, "case_id": case_id, "item_name": case_id},
        )

    resp = create_item("CAL-PARAM", "tests/guard_a.py::test_one")
    check(resp.status_code == 201, f"item in topology must be 201: {resp.text}")

    resp = create_item("TST-PARAM", "tests/guard_b.py::test_two")
    check(
        resp.status_code == 400 and resp.json()["code"] == "station_not_in_process",
        f"item outside topology must be 400: {resp.text}",
    )

    create_item("CAL-IFACE", "tests/guard_c.py::test_three")
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []}],
        params={"process_id": pid},
    )
    resp = admin("GET", "/api/admin/routing/items", params={"process_id": pid})
    stations = {i["station_id"] for i in resp.json()}
    check("CAL-IFACE" not in stations, f"removed step items must be purged: {stations}")
    check("CAL-PARAM" in stations, f"kept step items must survive: {stations}")

    orphan_station = f"TST-GUARD{STAMP}"
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
    check("CAL-PARAM" not in stations, f"items must be purged with topology: {stations}")

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
    cid = new_client_id("CAL")
    resp = v1("resolve", {"client_id": cid, "ip_address": "10.1.60.99"})
    check(resp.status_code == 200, f"auto register: {resp.text}")
    check(resp.json()["data"]["bound"] is False, f"auto registered must be unbound: {resp.text}")

    resp = admin("PUT", f"/api/admin/clients/{cid}", {"ip_address": "10.1.60.100"})
    check(resp.status_code == 200, f"update ip: {resp.text}")
    check(resp.json()["ip_address"] == "10.1.60.100", f"ip updated: {resp.text}")
    check(not resp.json()["station_id"], f"must stay unbound: {resp.text}")

    resp = admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": ["CAL-PARAM"]})
    check(resp.status_code == 200 and resp.json()["bound_stations"] == ["CAL-PARAM"], f"bind: {resp.text}")
    resp = admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": []})
    check(resp.status_code == 200 and resp.json()["bound_stations"] == [], f"unbind: {resp.text}")

    resp = v1(
        "check-in",
        {"client_id": cid, "sn": new_sn("UNBOUND"), "product_model": M_DPO, "firmware": FW},
    )
    check(
        resp.status_code == 403 and resp.json()["code"] == "client_not_bound",
        f"unbound check-in must be 403: {resp.text}",
    )
    admin("DELETE", f"/api/admin/clients/{cid}")

def test_client_multi_station_binding():
    """一机多工位：绑定集合跨流程，check-in 按件所属流程解析唯一工位。

    覆盖：同流程双绑定为配置错误（保存即 400）/ 跨流程合法绑定唯一命中 /
    绑定后拓扑漂移由进站运行时校验兜底 / 持锁禁止改绑 / 释放后改绑 /
    绑定集合与流程无交集时 client_not_bound。
    """
    cid = new_client_id("CAL")
    # ① 同流程双绑定（CAL-AWG 与 CAL-PARAM 同属 P_MSO）→ 配置层直接拒绝
    resp = admin(
        "POST",
        "/api/admin/clients",
        {"client_id": cid, "bound_stations": ["CAL-AWG", "CAL-PARAM"]},
    )
    check(
        resp.status_code == 400 and resp.json()["code"] == "station_ambiguous",
        f"same-process binding must 400 on save: {resp.status_code} {resp.text}",
    )
    # ② 正常注册：单工位 CAL-AWG（仅 MSO 有）
    resp = admin(
        "POST",
        "/api/admin/clients",
        {"client_id": cid, "bound_stations": ["CAL-AWG"]},
    )
    check(resp.status_code == 201, f"create client: {resp.text}")

    # ③ PUT 改绑同样拦截同流程双绑定
    resp = admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": ["CAL-AWG", "CAL-PARAM"]})
    check(
        resp.status_code == 400 and resp.json()["code"] == "station_ambiguous",
        f"same-process rebind must 400: {resp.status_code} {resp.text}",
    )

    # ④ 跨流程合法绑定：新增单工位流程 P_AUX，改绑为 CAL-AWG(MSO) + PACK-01(AUX)
    p_aux = f"PROC-TEST-AUX-{STAMP}"
    m_aux = "AUX1000"
    admin("POST", "/api/admin/processes", {"process_id": p_aux, "process_name": "aux"})
    admin("POST", "/api/admin/stations", {"station_id": "PACK-01", "station_name": "PACK-01"})
    resp = admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "PACK-01", "step_order": 10, "depends_on": []}],
        params={"process_id": p_aux},
    )
    check(resp.status_code == 200, f"aux topology: {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {"product_model": m_aux, "process_id": p_aux, "target_fw_version": FW},
    )
    check(resp.status_code == 201, f"aux model: {resp.text}")

    resp = admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": ["CAL-AWG", "PACK-01"]})
    check(resp.status_code == 200, f"cross-process rebind must 200: {resp.status_code} {resp.text}")

    # ⑤ AUX 件进站：绑定集合 ∩ P_AUX = {PACK-01}，唯一命中并回写当前操作工位
    sn_aux = new_sn("MULTI-AUX")
    resp = v1("check-in", {"client_id": cid, "sn": sn_aux, "product_model": m_aux, "firmware": FW})
    check(resp.status_code == 200, f"AUX check-in: {resp.text}")
    check(resp.json()["data"]["station_id"] == "PACK-01", f"resolve PACK-01: {resp.text}")
    lock_token = resp.json()["data"]["lock_token"]

    # ⑥ 持锁期间禁止改绑（绑定集合变化才拦，集合未变时允许改其他字段）
    resp = admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": ["CAL-AWG"]})
    check(resp.status_code == 409, f"rebind while holding must 409: {resp.text}")

    # ⑦ 主动释放锁后改绑：绑定集合收敛为 CAL-AWG
    resp = v1("release", {"client_id": cid, "sn": sn_aux, "lock_token": lock_token, "reason": "test"})
    check(resp.status_code == 200, f"release: {resp.text}")
    resp = admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": ["CAL-AWG"]})
    check(resp.status_code == 200, f"rebind after release: {resp.text}")

    # ⑧ AUX 件进站：绑定集合 ∩ P_AUX = 空 → 403 client_not_bound
    resp = v1("check-in", {"client_id": cid, "sn": new_sn("MULTI-AUX2"), "product_model": m_aux, "firmware": FW})
    check(
        resp.status_code == 403 and resp.json()["code"] == "client_not_bound",
        f"no intersection must 403: {resp.status_code} {resp.text}",
    )

    # ⑨ MSO 件进站：工位解析为 CAL-AWG，但 MSO 流程要求首站 CAL-PARAM
    resp = v1("check-in", {"client_id": cid, "sn": new_sn("MULTI-MSO"), "product_model": M_MSO, "firmware": FW})
    check(
        resp.status_code == 403 and resp.json()["code"] == "missing_prereq",
        f"must 403 missing_prereq: {resp.status_code} {resp.text}",
    )

    # ⑩ 拓扑漂移兜底：绑定后流程又调整（直改库模拟），进站运行时校验拦下歧义
    with SessionLocal() as db:
        row = db.get(models.StationClient, cid)
        row.bound_stations = ["CAL-AWG", "CAL-PARAM"]
        db.commit()
    resp = v1("check-in", {"client_id": cid, "sn": new_sn("MULTI-MSO2"), "product_model": M_MSO, "firmware": FW})
    check(
        resp.status_code == 400 and resp.json()["code"] == "station_ambiguous",
        f"runtime ambiguous must 400: {resp.status_code} {resp.text}",
    )

def test_clone_process():
    target = f"PROC-TEST-CLONE-{STAMP}"
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
    for station in ("CAL-PARAM", "CAL-IFACE", "TST-PARAM", "TST-IFACE"):
        resp = checkin(station, sn)
        check(resp.status_code == 200, f"DPO check-in {station}: {resp.status_code} {resp.text}")
        case_ids = rules_of(resp)
        resp = checkout(station, sn, case_ids)
        check(resp.status_code == 201, f"DPO check-out {station}: {resp.status_code} {resp.text}")
        check(resp.json()["data"]["overall_result"] == "PASS", f"result {station}: {resp.text}")

    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.status_code == 200, f"product created: {resp.text}")
    check(resp.json()["process_id"] == P_DPO, "process binding")
    check(resp.json()["current_fw_version"] == FW, "firmware recorded")
    check(resp.json()["total_steps"] == 4, f"DPO should have 4 steps: {resp.json()}")
    check(resp.json()["is_completed"] is True, f"should be completed: {resp.json()}")
    check(
        sorted(resp.json()["passed_stations"]) == ["CAL-IFACE", "CAL-PARAM", "TST-IFACE", "TST-PARAM"],
        f"stamps: {resp.json()}",
    )

def test_mso_full_flow():
    """MSO 带 AWG：6 站完整流程，且校准三站未齐前不得进入测试阶段。"""
    sn = new_sn("MSO-OK")
    for station in ("CAL-PARAM", "CAL-IFACE", "CAL-AWG"):
        resp = checkin(station, sn, model=M_MSO)
        check(resp.status_code == 200, f"MSO check-in {station}: {resp.status_code} {resp.text}")
        resp = checkout(station, sn, rules_of(resp))
        check(resp.status_code == 201, f"MSO check-out {station}: {resp.status_code} {resp.text}")

    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["total_steps"] == 6, "MSO should have 6 steps")

    for station in ("TST-PARAM", "TST-IFACE", "TST-AWG"):
        resp = checkin(station, sn, model=M_MSO)
        check(resp.status_code == 200, f"MSO check-in {station}: {resp.status_code} {resp.text}")
        resp = checkout(station, sn, rules_of(resp))
        check(resp.status_code == 201, f"MSO check-out {station}: {resp.status_code} {resp.text}")

    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["is_completed"] is True, f"MSO completed: {resp.text}")

def test_mso_gate_requires_all_cal():
    """MSO：校准三站未齐 → TST-PARAM 拦截（方案中的闸门工位）。"""
    sn = new_sn("MSO-GATE")
    for station in ("CAL-PARAM", "CAL-IFACE"):
        resp = checkin(station, sn, model=M_MSO)
        check(resp.status_code == 200, f"check-in {station}: {resp.text}")
        checkout(station, sn, rules_of(resp))

    resp = checkin("TST-PARAM", sn, model=M_MSO)
    check(resp.status_code == 403, f"gate must be 403: {resp.status_code} {resp.text}")
    check(resp.json()["data"]["missing"] == ["CAL-AWG"], f"missing: {resp.json()}")

# ================= C. 需求 2：卡控 =================
def test_jump_station_blocked_403():
    """跳过 CAL-IFACE 直奔 TST-PARAM → 403。"""
    sn = new_sn("JUMP")
    pass_station("CAL-PARAM", sn)

    resp = checkin("TST-PARAM", sn)
    check(resp.status_code == 403, f"jump must be 403, got {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["code"] == "missing_prereq", f"code: {body}")
    check(body["exit_code"] == 10, f"exit_code: {body}")
    check("CAL-IFACE" in body["data"]["missing"], f"missing: {body}")

def test_retest_blocked_409():
    """已盖章工位严禁复测 → 409。"""
    sn = new_sn("RETEST")
    pass_station("CAL-PARAM", sn)

    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 409, f"retest must be 409, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "station_already_passed", f"code: {resp.json()}")

def test_wrong_process_blocked_400():
    """DPO 机型进入 AWG 工位（机台绑定工位与该件流程无交集）→ 403。"""
    sn = new_sn("WRONGP")
    resp = checkin("TST-AWG", sn, model=M_DPO)
    check(resp.status_code == 403, f"wrong process must be blocked, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "client_not_bound", f"code: {resp.json()}")

def test_firmware_mismatch_403():
    sn = new_sn("FW")
    resp = checkin("CAL-PARAM", sn, firmware="V3.10")
    check(resp.status_code == 403, f"fw mismatch must be 403, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "firmware_mismatch", f"code: {resp.json()}")

def test_case_id_mismatch_400():
    """上报的用例ID未覆盖必测清单 → 400。"""
    sn = new_sn("CASEID")
    resp = checkin("CAL-PARAM", sn, case_ids=[ITEMS["CAL-PARAM"][0]])
    check(resp.status_code == 400, f"case_id mismatch must be 400, got {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["code"] == "case_id_mismatch", f"code: {body}")
    check(body["exit_code"] == 15, f"exit_code: {body}")
    check(len(body["data"]["missing_case_ids"]) > 0, f"missing: {body}")

    resp = checkin("CAL-PARAM", sn, case_ids=list(ITEMS["CAL-PARAM"]))
    check(resp.status_code == 200, f"full case IDs should pass: {resp.text}")

def test_first_station_required():
    """未建档的机器不得从中间工位开局。"""
    sn = new_sn("MIDDLE")
    resp = checkin("TST-IFACE", sn)
    check(resp.status_code == 403, f"must start at first station: {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "missing_prereq", f"code: {resp.json()}")

def test_model_mismatch():
    sn = new_sn("MODELM")
    pass_station("CAL-PARAM", sn, model=M_DPO)
    resp = checkin("CAL-IFACE", sn, model=M_MSO)
    check(resp.status_code == 403 and resp.json()["code"] == "model_mismatch", f"{resp.status_code} {resp.text}")

# ================= D. 需求 1：落库 ACK =================
def test_ack_and_idempotent_replay():
    sn = new_sn("ACK")
    pass_station("CAL-PARAM", sn)
    resp = checkin("CAL-IFACE", sn)
    case_ids = rules_of(resp)
    cid = uuid.uuid4().hex

    first = checkout("CAL-IFACE", sn, case_ids, checkout_id=cid)
    check(first.status_code == 201, f"checkout: {first.status_code} {first.text}")
    body = first.json()
    check(body["data"]["acknowledged"] is True, f"ack flag: {body}")
    check(body["data"]["idempotent_replay"] is False, f"first should not be replay: {body}")
    record_id = body["data"]["record_id"]

    second = checkout("CAL-IFACE", sn, case_ids, checkout_id=cid)
    check(second.status_code == 201, f"replay: {second.status_code} {second.text}")
    check(second.json()["data"]["idempotent_replay"] is True, f"replay flag: {second.json()}")
    check(second.json()["data"]["record_id"] == record_id, "record_id must be identical")

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

    resp = admin("GET", "/api/admin/records", params={"sn": sn})
    check(resp.json()["total"] == 2, f"records should be 2: {resp.json()['total']}")

# ================= E. 漏测与锁定 =================
def test_missing_mandatory_400():
    sn = new_sn("MISSING")
    resp = checkin("CAL-PARAM", sn)
    case_ids = rules_of(resp)
    session_id = resp.json()["data"]["session_id"]
    resp = checkout("CAL-PARAM", sn, case_ids[:-1])
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

    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"re-enter after fail: {resp.text}")
    resp = checkout("CAL-PARAM", sn, case_ids, skip=(case_ids[-1],))
    check(resp.status_code == 400 and resp.json()["code"] == "missing_mandatory", f"skip: {resp.text}")

    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"re-enter: {resp.text}")
    resp = checkout("CAL-PARAM", sn, case_ids, fail=(case_ids[0],))
    check(resp.status_code == 201, f"fail should be recorded: {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["exit_code"] == 1 and body["data"]["overall_result"] == "FAIL", f"exit_code: {body}")

def test_fail_streak_lock_403():
    sn = new_sn("LOCK")
    for i in range(3):
        resp = checkin("CAL-PARAM", sn)
        check(resp.status_code == 200, f"check-in round {i}: {resp.text}")
        case_ids = rules_of(resp)
        resp = checkout("CAL-PARAM", sn, case_ids, fail=(case_ids[0],))
        check(resp.status_code == 201 and resp.json()["data"]["overall_result"] == "FAIL", f"round {i}: {resp.text}")
        locked = resp.json()["data"]["product_locked"]
        check(locked is (i == 2), f"round {i} lock flag: {resp.json()['data']}")

    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 403, f"locked must be 403, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "product_locked", f"code: {resp.json()}")
    check(resp.json()["exit_code"] == 16, f"exit_code: {resp.json()}")

    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "工程师确认后重投"})
    check(resp.status_code == 201, f"reset: {resp.text}")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"unlocked: {resp.text}")

def test_lock_conflict_409_and_timeout_takeover():
    sn = new_sn("CONFLICT")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")

    other = new_client_id("CAL")
    admin("POST", "/api/admin/clients", {"client_id": other, "bound_stations": ["CAL-PARAM"]})
    resp = v1(
        "check-in",
        {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW},
    )
    check(resp.status_code == 409, f"conflict must be 409, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "lock_conflict", f"code: {resp.json()}")

    resp = v1("heartbeat", {"client_id": CLIENTS["CAL-PARAM"], "sn": sn})
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
    resp = checkout("CAL-PARAM", sn, rules_of(resp))
    check(resp.status_code == 403, f"stale lock must be 403, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "lock_invalid", f"code: {resp.json()}")

def test_scrap_blocked():
    sn = new_sn("SCRAP")
    pass_station("CAL-PARAM", sn)
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "SCRAP", "reason": "主板损伤"})
    check(resp.status_code == 201, f"scrap: {resp.text}")
    resp = checkin("CAL-IFACE", sn)
    check(resp.status_code == 403 and resp.json()["code"] == "product_scrapped", f"{resp.status_code} {resp.text}")

# ================= F. 维修处置 =================
def test_repair_actions():
    sn = new_sn("REPAIR")
    for station in ("CAL-PARAM", "CAL-IFACE", "TST-PARAM"):
        pass_station(station, sn)

    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "TST-PARAM", "reason": "抽检漂移"},
    )
    check(resp.status_code == 201, f"retest: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check("TST-PARAM" not in resp.json()["passed_stations"], f"stamp removed: {resp.json()}")
    resp = admin("GET", "/api/admin/records", params={"sn": sn, "station_id": "TST-PARAM", "is_valid": False})
    check(resp.json()["total"] == 1, f"invalidated record: {resp.json()['total']}")
    # 追溯工步口径一致：作废记录不参与"最后结果"，印章与结果不得自相矛盾
    resp = admin("GET", f"/api/admin/records/trace/{sn}")
    step = next(s for s in resp.json()["steps"] if s["station_id"] == "TST-PARAM")
    check(
        step["passed"] is False and step["last_result"] is None,
        f"trace step must align with stamp: {step}",
    )

    pass_station("TST-PARAM", sn)

    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "ROLLBACK", "target_station": "CAL-IFACE", "reason": "批量异常"},
    )
    check(resp.status_code == 201, f"rollback: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(sorted(resp.json()["passed_stations"]) == ["CAL-PARAM"], f"rollback result: {resp.json()}")

    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "SCRAP", "reason": "报废"})
    check(resp.status_code == 201, f"scrap: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["current_status"] == "SCRAPPED", f"scrapped: {resp.json()}")
    # 报废件封死复活通道：RESET/RETEST/ROLLBACK 一律 409（防误操作击穿"永久拦截"语义）
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "复判合格"})
    check(
        resp.status_code == 409 and resp.json()["code"] == "product_scrapped",
        f"scrapped revival blocked: {resp.status_code} {resp.text}",
    )
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RETEST", "target_station": "CAL-PARAM"})
    check(resp.status_code == 409, f"scrapped retest blocked: {resp.status_code} {resp.text}")

    resp = admin("GET", "/api/admin/repairs", params={"sn": sn})
    check(resp.json()["total"] == 3, f"repair history: {resp.json()['total']}")

def test_repair_guards():
    sn = new_sn("RGUARD")
    pass_station("CAL-PARAM", sn)
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "TST-PARAM", "reason": "x"},
    )
    check(resp.status_code == 400 and resp.json()["code"] == "station_not_passed", f"{resp.text}")
    # 持锁中禁止处置
    checkin("CAL-IFACE", sn)
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "x"})
    check(resp.status_code == 409 and resp.json()["code"] == "product_holding_lock", f"{resp.text}")

# ================= G. 台账与统计 =================
def test_trace():
    sn = new_sn("TRACE")
    for station in ("CAL-PARAM", "CAL-IFACE"):
        pass_station(station, sn)

    resp = admin("GET", f"/api/admin/records/trace/{sn}")
    check(resp.status_code == 200, f"trace: {resp.text}")
    body = resp.json()
    check(len(body["steps"]) == 4, f"DPO trace steps: {len(body['steps'])}")
    check(body["product"]["process_id"] == P_DPO, "trace process")
    passed_steps = [s for s in body["steps"] if s["passed"]]
    check(len(passed_steps) == 2, f"passed steps: {passed_steps}")
    check(len(body["records"]) == 2, f"records: {len(body['records'])}")
    orders = [s["step_order"] for s in body["steps"]]
    check(orders == sorted(orders), f"step order: {orders}")

def test_metrics():
    resp = admin("GET", "/api/admin/metrics/overview", params={"days": 14})
    check(resp.status_code == 200, f"overview: {resp.text}")
    body = resp.json()
    check(body["wip"]["total"] > 0, f"wip: {body['wip']}")
    check(len(body["trend"]) == 14, f"trend: {len(body['trend'])}")
    check(any(r["key"] == "CAL-PARAM" for r in body["station_yield"]), f"station yield: {body['station_yield']}")
    check(any(r["key"] == P_DPO for r in body["process_yield"]), f"process yield: {body['process_yield']}")
    check(body["clients"]["total"] >= 6, f"clients: {body['clients']}")

    resp = admin("GET", "/api/admin/metrics/overview", params={"days": 7, "process_id": P_MSO})
    check(resp.status_code == 200 and len(resp.json()["trend"]) == 7, f"filtered: {resp.text}")

def test_no_bilingual():
    """后端只输出英文，语种由前端 i18n 决定。"""
    resp = api(
        "POST",
        "/api/v1/client/resolve",
        body={"client_id": CLIENTS["CAL-PARAM"]},
        api_key=API_KEY,
        headers={"Accept-Language": "zh-CN"},
    )
    zh = resp.json()["message"]
    resp = api(
        "POST",
        "/api/v1/client/resolve",
        body={"client_id": CLIENTS["CAL-PARAM"]},
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
    for station in ("CAL-PARAM", "CAL-IFACE", "TST-PARAM", "TST-IFACE"):
        pass_station(station, done_sn)

    resp = admin("GET", "/api/admin/products", params={"current_status": "COMPLETED", "sn": done_sn})
    check(resp.status_code == 200, f"completed filter: {resp.text}")
    check(resp.json()["total"] == 1, f"已完工应能筛出: {resp.json()['total']}")

    resp = admin("GET", "/api/admin/products", params={"current_status": "IDLE", "sn": done_sn})
    check(resp.status_code == 200, f"idle filter: {resp.text}")
    check(resp.json()["total"] == 0, f"已完工不得出现在待测试: {resp.json()['total']}")

    wip_sn = new_sn("WIPIDLE")
    pass_station("CAL-PARAM", wip_sn)
    resp = admin("GET", "/api/admin/products", params={"current_status": "IDLE", "sn": wip_sn})
    check(resp.json()["total"] == 1, f"未完工在制品应在待测试: {resp.json()['total']}")

def test_lock_lost_takeover():
    """机台失联（心跳断流 > grace）→ 新机台立即接管，旧持锁方写入被拒。"""
    sn = new_sn("LOST")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    stale_token = resp.json()["data"]["lock_token"]
    check(bool(stale_token), "lock_token must be issued")

    other = new_client_id("CAL")
    admin("POST", "/api/admin/clients", {"client_id": other, "bound_stations": ["CAL-PARAM"]})

    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 409, f"still locked: {resp.status_code} {resp.text}")

    # 心跳断流 200s（> grace 120s），但持锁总时长远未到硬超时 → 可接管
    _backdate(sn, lock_last_seen_at=200)
    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 200, f"takeover: {resp.status_code} {resp.text}")
    body = resp.json()["data"]
    check(body["takeover"] is True, f"takeover flag: {body}")
    check(body["takeover_from"] == CLIENTS["CAL-PARAM"], f"takeover_from: {body}")
    check(body["lock_token"] != stale_token, "新持锁方必须换发 token")

    resp = v1(
        "check-out",
        {
            "client_id": CLIENTS["CAL-PARAM"],
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
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    first_lease = resp.json()["data"]["timeout_sec"]

    resp = v1("heartbeat", {"client_id": CLIENTS["CAL-PARAM"], "sn": sn})
    body = resp.json()["data"]
    check(body["lease_remaining_sec"] <= first_lease, "心跳不得延长硬超时")
    check(body["heartbeat_count"] == 1, f"heartbeat_count: {body}")

    # 心跳新鲜（未失联）但持锁已超硬超时 → 出站判 lock_expired
    _backdate(sn, lock_acquired_at=1900, testing_started_at=1900)
    resp = checkout("CAL-PARAM", sn, ["test_amp_cal", "test_phase_cal"])
    check(resp.status_code == 403 and resp.json()["code"] == "lock_expired", f"{resp.status_code} {resp.text}")

def test_checkpoint_resume_and_merge():
    """崩溃续测：断点上报 → 重进站拿到已完成清单 → 出站补齐未提交用例。"""
    sn = new_sn("RESUME")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    data = resp.json()["data"]
    session_id = data["session_id"]
    token = data["lock_token"]

    resp = v1(
        "checkpoint",
        {
            "client_id": CLIENTS["CAL-PARAM"],
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
            "client_id": CLIENTS["CAL-PARAM"],
            "sn": sn,
            "session_id": session_id,
            "lock_token": token,
            "items": items_for(["test_amp_cal"]),
        },
    )
    check(resp.json()["data"]["merged_count"] == 1, f"idempotent: {resp.text}")

    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"re-checkin: {resp.text}")
    data = resp.json()["data"]
    check(data["session_id"] == session_id, "必须复用同一会话")
    check(data["attempt"] == 2, f"attempt: {data}")
    check(data["resume"]["resumed"] is True, f"resume: {data}")
    check(data["resume"]["completed_case_ids"] == ["test_amp_cal"], f"resume: {data}")
    check(data["resume"]["cursor"] == {"step": 1}, f"cursor: {data}")

    resp = checkout("CAL-PARAM", sn, ["test_phase_cal"], lock_token=data["lock_token"])
    check(resp.status_code == 201, f"checkout: {resp.status_code} {resp.text}")
    ack = resp.json()["data"]
    check(ack["checkpoint_merged_count"] == 1, f"merged: {ack}")
    resp = admin("GET", f"/api/admin/records/trace/{sn}")
    record = [r for r in resp.json()["records"] if r["station_id"] == "CAL-PARAM"][0]
    case_ids = sorted(i["case_id"] for i in record["executed_items"]["items"])
    check(case_ids == ["test_amp_cal", "test_phase_cal"], f"merged items: {case_ids}")

def test_sweeper_releases_orphan_lock():
    """回收任务：失联锁自动释放，且不计产品失败。"""
    from app.services.sweeper import run_once

    sn = new_sn("SWEEP")
    resp = checkin("CAL-PARAM", sn)
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

    other = new_client_id("CAL")
    admin("POST", "/api/admin/clients", {"client_id": other, "bound_stations": ["CAL-PARAM"]})
    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 200, f"re-checkin after sweep: {resp.text}")

def test_client_app_version_tracked():
    """机台上报的程序版本要落库：现场"同机型结果不可比"的常见根因是版本漂移。"""
    cid = new_client_id("CAL")
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

    admin("PUT", f"/api/admin/clients/{cid}", {"bound_stations": ["CAL-PARAM"]})
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

    manual = new_client_id("CAL")
    resp = admin(
        "POST",
        "/api/admin/clients",
        {"client_id": manual, "bound_stations": ["CAL-PARAM"], "app_version": "2.6.0"},
    )
    check(resp.status_code == 201 and resp.json()["app_version"] == "2.6.0", f"manual register: {resp.text}")

    # client_name：可填展示名，未填时回退为 client_id，保证列表永远有可读内容
    named = new_client_id("CAL")
    resp = admin(
        "POST",
        "/api/admin/clients",
        {"client_id": named, "bound_stations": ["CAL-PARAM"], "client_name": "Line1 Cal Bench A"},
    )
    check(
        resp.status_code == 201 and resp.json()["client_name"] == "Line1 Cal Bench A",
        f"explicit name: {resp.text}",
    )

    fallback = new_client_id("CAL")
    resp = admin("POST", "/api/admin/clients", {"client_id": fallback, "bound_stations": ["CAL-PARAM"]})
    check(
        resp.status_code == 201 and resp.json()["client_name"] == fallback,
        f"name must fall back to client_id: {resp.text}",
    )

    resp = admin("PUT", f"/api/admin/clients/{named}", {"client_name": ""})
    check(resp.status_code == 200 and resp.json()["client_name"] is None, f"clear name: {resp.text}")

    admin("DELETE", f"/api/admin/clients/{cid}")
    admin("DELETE", f"/api/admin/clients/{manual}")
    admin("DELETE", f"/api/admin/clients/{named}")
    admin("DELETE", f"/api/admin/clients/{fallback}")

def test_firmware_match_rule():
    """fw_match_rule：exact 要求完全一致；min 只要求不低于基线。

    min 规则下必须按数字段比较，否则 "V3.9" > "V3.20" 的字典序会放行旧固件。
    """
    pid = f"PROC-TEST-FW-{STAMP}"
    model = f"MODEL_FWRULE-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "fw rule"})
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []}],
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
        cid = new_client_id("CAL")
        admin("POST", "/api/admin/clients", {"client_id": cid, "bound_stations": ["CAL-PARAM"]})
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

    resp = admin("PUT", f"/api/admin/product-models/{model}", {"fw_match_rule": "exact"})
    check(resp.status_code == 200 and resp.json()["fw_match_rule"] == "exact", f"switch rule: {resp.text}")
    resp = try_firmware("V3.21", "EXACT")
    check(
        resp.status_code == 403 and resp.json()["code"] == "firmware_mismatch",
        f"exact must reject higher version: {resp.text}",
    )

    resp = admin("PUT", f"/api/admin/product-models/{model}", {"fw_match_rule": "bogus"})
    check(resp.status_code == 422, f"invalid rule must be 422: {resp.text}")

def test_app_version_baseline():
    """测试软件基线：低于/缺失/unknown 拒绝；min 与 exact 两种口径；清空基线 = 不卡控。

    app_version 来自上位机 config.toml [system].version，随 check-in 上报；
    版本不明（缺失/unknown）且基线已配置时视为不满足——版本不明不得上线。
    """
    pid = f"PROC-TEST-APP-{STAMP}"
    model = f"MODEL_APPVER-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "app version"})
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []}],
        params={"process_id": pid},
    )
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {
            "product_model": model,
            "process_id": pid,
            "target_fw_version": "V3.20",
            "target_app_version": "V1.0.0",
            "app_match_rule": "min",
        },
    )
    check(resp.status_code == 201, f"create model: {resp.text}")
    check(resp.json()["app_match_rule"] == "min", f"rule must persist: {resp.text}")
    check(resp.json()["target_app_version"] == "V1.0.0", f"baseline must persist: {resp.text}")

    def try_app(payload_extra, tag):
        # 每次换机台 + 换 SN，避免工位锁互相干扰
        cid = new_client_id("CAL")
        admin("POST", "/api/admin/clients", {"client_id": cid, "bound_stations": ["CAL-PARAM"]})
        body = {"client_id": cid, "sn": new_sn(f"APP-{tag}"), "product_model": model, "firmware": "V3.20"}
        body.update(payload_extra)
        return v1("check-in", body)

    resp = try_app({"app_version": "V1.0.0"}, "EQ")
    check(resp.status_code == 200, f"equal must pass under min: {resp.text}")
    resp = try_app({"app_version": "V1.0.1"}, "HI")
    check(resp.status_code == 200, f"higher must pass under min: {resp.text}")
    resp = try_app({"app_version": "V0.9.9"}, "LOW")
    check(
        resp.status_code == 403 and resp.json()["code"] == "app_version_mismatch",
        f"V0.9.9 is below V1.0.0 and must be blocked: {resp.text}",
    )
    resp = try_app({}, "MISSING")
    check(
        resp.status_code == 403 and resp.json()["code"] == "app_version_mismatch",
        f"missing app_version must be blocked when baseline set: {resp.text}",
    )
    resp = try_app({"app_version": "unknown"}, "UNKNOWN")
    check(
        resp.status_code == 403 and resp.json()["code"] == "app_version_mismatch",
        f"unknown app_version must be blocked when baseline set: {resp.text}",
    )

    resp = admin("PUT", f"/api/admin/product-models/{model}", {"app_match_rule": "exact"})
    check(resp.status_code == 200 and resp.json()["app_match_rule"] == "exact", f"switch rule: {resp.text}")
    resp = try_app({"app_version": "V1.0.1"}, "EXACT")
    check(
        resp.status_code == 403 and resp.json()["code"] == "app_version_mismatch",
        f"exact must reject higher version: {resp.text}",
    )

    # 清空基线 = 不卡控：缺失 app_version 也放行
    resp = admin("PUT", f"/api/admin/product-models/{model}", {"target_app_version": ""})
    check(resp.status_code == 200, f"clear baseline: {resp.text}")
    resp = try_app({}, "NOCHECK")
    check(resp.status_code == 200, f"empty baseline must not enforce: {resp.text}")

def test_process_status():
    """流程停用后不再接受新机型绑定，重新启用后恢复。"""
    pid = f"PROC-TEST-VER-{STAMP}"
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

def test_client_release_lock():
    """上位机主动放弃锁：释放锁与会话，但不计失败、不动画章，同工位可立即接管。"""
    sn = new_sn("RELEASE")
    pass_station("CAL-PARAM", sn)

    resp = checkin("CAL-IFACE", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    session_id = resp.json()["data"]["session_id"]
    lock_token = resp.json()["data"]["lock_token"]

    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        stamps_before = sorted(product.passed_stations)
        fail_before = product.fail_count

    resp = v1(
        "release",
        {
            "client_id": CLIENTS["CAL-IFACE"],
            "sn": sn,
            "lock_token": lock_token,
            "reason": "操作员取消测试",
        },
    )
    check(resp.status_code == 200, f"release: {resp.status_code} {resp.text}")
    data = resp.json()["data"]
    check(data["released"] is True, f"released: {data}")
    check(data["session_id"] == session_id, f"session id: {data}")

    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        check(product.current_status == models.STATUS_IDLE, f"status after release: {product.current_status}")
        check(product.lock_token is None, f"lock token must be cleared: {product.lock_token}")
        # 主动放弃不能计一次失败——这与漏测拦截(400)的语义有本质区别
        check(product.fail_count == fail_before, f"release must not count as failure: {product.fail_count}")
        check(sorted(product.passed_stations) == stamps_before, f"stamps must stay: {product.passed_stations}")

    resp = admin("GET", f"/api/admin/sessions/{session_id}")
    check(resp.status_code == 200, f"session detail: {resp.text}")
    check(resp.json()["status"] == models.SESSION_ABORTED, f"session status: {resp.json()}")
    check("操作员取消测试" in (resp.json()["end_reason"] or ""), f"end reason: {resp.json()}")

    other = new_client_id("CAL")
    admin("POST", "/api/admin/clients", {"client_id": other, "bound_stations": ["CAL-IFACE"]})
    resp = v1("check-in", {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW})
    check(resp.status_code == 200, f"re-check-in after release: {resp.status_code} {resp.text}")

    # 未持锁的机台调用 release：不报错，但绝不能释放他人持有的锁
    resp = v1("release", {"client_id": CLIENTS["CAL-PARAM"], "sn": sn, "reason": "无关机台"})
    check(resp.status_code == 200, f"release by non-holder: {resp.text}")
    check(resp.json()["data"]["released"] is False, f"must not release others: {resp.json()}")
    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        check(product.current_status == models.STATUS_TESTING, f"other's lock must survive: {product.current_status}")
        check(product.current_client == other, f"holder unchanged: {product.current_client}")

def test_id_format_enforced():
    """编号规范在写入口强制校验（README 7.2）：不合规一律 422，合规放行。
    client_id 不做格式约束（现场编号风格各异，仅要求非空）。"""
    rejected = [
        ("/api/admin/processes", {"process_id": "PROC_TEK_MSO"}),
        ("/api/admin/processes", {"process_id": "PROC-SCOPE-MSO"}),
        ("/api/admin/stations", {"station_id": "CAL_PARAM"}),
        ("/api/admin/stations", {"station_id": "CAL"}),
    ]
    for path, body in rejected:
        resp = admin("POST", path, body)
        check(resp.status_code == 422, f"must reject {body}: {resp.status_code} {resp.text}")

    resp = admin("POST", "/api/admin/processes", {"process_id": f"PROC-TEST-FMT-{STAMP}"})
    check(resp.status_code == 201, f"process must be accepted: {resp.text}")
    resp = admin("POST", "/api/admin/stations", {"station_id": f"TST-FMT{STAMP}"})
    check(resp.status_code == 201, f"station must be accepted: {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/clients",
        {"client_id": "SZ-L1-TST-99", "station_id": "TST-PARAM"},
    )
    check(resp.status_code == 201, f"client must be accepted: {resp.text}")

    admin("DELETE", "/api/admin/clients/SZ-L1-TST-99")
    admin("DELETE", f"/api/admin/stations/TST-FMT{STAMP}")
    admin("DELETE", f"/api/admin/processes/PROC-TEST-FMT-{STAMP}")

def test_force_release_and_sessions_api():
    """运维强制解锁 + 会话管理接口。"""
    sn = new_sn("FORCE")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    session_id = resp.json()["data"]["session_id"]

    resp = admin("POST", f"/api/admin/products/{sn}/force-release", {"reason": "机台蓝屏"})
    check(resp.status_code == 200, f"force-release: {resp.text}")
    check(resp.json()["previous_client"] == CLIENTS["CAL-PARAM"], f"previous: {resp.text}")

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

def test_import_blocks_running_session():
    """导入闸门：工位有 RUNNING 会话时，"新增必测"默认 409；收缩清单放行，dry-run 转告警，force 放行。"""
    sn = new_sn("IMPBLK")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")
    session_id = resp.json()["data"]["session_id"]

    add_new = {
        "process_id": P_DPO,
        "station_id": "CAL-PARAM",
        "mode": "replace",
        "items": [{"case_id": "tests/new_case.py::test_new", "item_name": "新用例", "is_mandatory": True}],
    }

    resp = admin("POST", "/api/admin/routing/items/import", add_new)
    check(
        resp.status_code == 409 and resp.json()["code"] == "running_session_block",
        f"add mandatory with RUNNING session must 409: {resp.status_code} {resp.text}",
    )
    check(resp.json()["data"]["running_sessions"] >= 1, f"data: {resp.text}")

    resp = admin("POST", "/api/admin/routing/items/import", {**add_new, "dry_run": True})
    check(resp.status_code == 200, f"dry-run must pass: {resp.text}")
    body = resp.json()
    check(
        any(w.startswith("would_block") for w in body["warnings"]),
        f"dry-run must warn would_block: {body['warnings']}",
    )
    resp = admin(
        "GET", "/api/admin/routing/items", params={"process_id": P_DPO, "station_id": "CAL-PARAM"}
    )
    ids = {i["case_id"] for i in resp.json()}
    check("tests/new_case.py::test_new" not in ids, f"dry-run must not write: {ids}")

    keep = ITEMS["CAL-PARAM"][0]
    resp = admin(
        "POST",
        "/api/admin/routing/items/import",
        {
            "process_id": P_DPO,
            "station_id": "CAL-PARAM",
            "mode": "replace",
            "items": [{"case_id": keep, "is_mandatory": True}],
        },
    )
    check(
        resp.status_code == 200 and resp.json()["deactivated"] == 1,
        f"shrink must pass: {resp.status_code} {resp.text}",
    )

    resp = admin("POST", "/api/admin/routing/items/import", add_new, params={"force": "true"})
    check(resp.status_code == 200 and resp.json()["created"] == 1, f"force must pass: {resp.text}")

    # 收尾：中止会话并恢复原清单，避免影响后续用例
    admin("POST", f"/api/admin/sessions/{session_id}/abort", {"reason": "test cleanup"})
    restore = {
        "process_id": P_DPO,
        "station_id": "CAL-PARAM",
        "mode": "replace",
        "items": [{"case_id": c, "is_mandatory": True} for c in ITEMS["CAL-PARAM"]],
    }
    resp = admin("POST", "/api/admin/routing/items/import", restore, params={"force": "true"})
    check(
        resp.status_code == 200 and resp.json()["total_active"] == len(ITEMS["CAL-PARAM"]),
        f"restore: {resp.status_code} {resp.text}",
    )

def test_process_export_import():
    """流程导出→改号导入副本：工位补建不覆盖 / 机型冲突跳过 / 重复导入 409 / 坏依赖 400。"""
    doc = admin("GET", f"/api/admin/routing/processes/{P_DPO}/export").json()
    check(doc["process"]["process_id"] == P_DPO, "export process")
    check(len(doc["steps"]) == 4, f"export steps: {len(doc['steps'])}")
    check(len(doc["items"]) >= 8, f"export items: {len(doc['items'])}")

    # 导出前给两个用例设置执行顺序，验证导出→导入往返不丢失
    dpo_items = admin("GET", "/api/admin/routing/items",
                      params={"process_id": P_DPO, "station_id": "CAL-PARAM"}).json()
    check(len(dpo_items) >= 2, f"dpo items for order: {len(dpo_items)}")
    admin("PUT", f"/api/admin/routing/items/{dpo_items[0]['item_id']}", {"sort_order": 20})
    admin("PUT", f"/api/admin/routing/items/{dpo_items[1]['item_id']}", {"sort_order": 10})
    doc = admin("GET", f"/api/admin/routing/processes/{P_DPO}/export").json()
    exported_orders = {i["case_id"]: i.get("sort_order") for i in doc["items"]
                       if i["station_id"] == "CAL-PARAM"}
    check(exported_orders.get(dpo_items[0]["case_id"]) == 20, f"export sort_order: {exported_orders}")

    imp = f"PROC-TEST-IMP-{STAMP}"
    doc["process"]["process_id"] = imp
    doc["process"]["process_name"] = "导入副本"
    resp = admin("POST", "/api/admin/routing/processes/import", doc)
    check(resp.status_code == 201, f"import: {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["steps_created"] == 4 and body["items_created"] == len(doc["items"]), f"counts: {body}")
    check(body["stations_created"] == 0, f"工位已存在不重复建: {body}")
    check(body["models_skipped"] == 1, f"机型冲突跳过: {body}")

    # 导入副本：显式序号与源一致（往返不丢失）；未编号项自动编号
    imp_orders = {i["case_id"]: i.get("sort_order")
                  for i in admin("GET", "/api/admin/routing/items",
                                 params={"process_id": imp, "station_id": "CAL-PARAM"}).json()}
    for cid, so in exported_orders.items():
        if so is not None:
            check(imp_orders.get(cid) == so, f"roundtrip order {cid}: {imp_orders}")
    check(all(v is not None for v in imp_orders.values()), f"import auto numbered: {imp_orders}")

    # 旧格式兼容：无 sort_order 的导出文件可导入，按导入顺序以步长 10 自动编号
    old = json.loads(json.dumps(doc))
    old["process"]["process_id"] = f"PROC-IMP-OLD-{STAMP}"
    for it in old["items"]:
        it.pop("sort_order", None)
    resp = admin("POST", "/api/admin/routing/processes/import", old)
    check(resp.status_code == 201, f"old-format import: {resp.status_code} {resp.text}")
    old_items = admin("GET", "/api/admin/routing/items",
                      params={"process_id": f"PROC-IMP-OLD-{STAMP}",
                              "station_id": "CAL-PARAM"}).json()
    old_orders = [r["sort_order"] for r in old_items]
    check(old_orders == sorted(old_orders) and old_orders[0] == 10 and len(set(old_orders)) == len(old_orders),
          f"old-format auto numbering: {old_orders}")

    # 清理副本上的序号标记，避免影响其他用例
    for it in dpo_items:
        admin("PUT", f"/api/admin/routing/items/{it['item_id']}", {"sort_order": None})

    res = admin("GET", "/api/admin/routing/validate", params={"process_id": imp})
    check(res.json()["ok"] is True, "副本拓扑校验通过")

    resp = admin("POST", "/api/admin/routing/processes/import", doc)
    check(resp.status_code == 409 and resp.json()["code"] == "process_already_exists", f"dup: {resp.text}")

    bad = json.loads(json.dumps(doc))
    bad["process"]["process_id"] = f"PROC-TEST-IMP2-{STAMP}"
    bad["steps"][0]["depends_on"] = ["CAL-AWG"]
    resp = admin("POST", "/api/admin/routing/processes/import", bad)
    check(resp.status_code == 400 and resp.json()["code"] == "topology_invalid", f"bad deps: {resp.text}")

    resp = admin("GET", "/api/admin/routing/processes/export-all")
    check(resp.status_code == 200, f"export all: {resp.status_code} {resp.text}")
    all_ids = [p["process"]["process_id"] for p in resp.json()["processes"]]
    check(P_DPO in all_ids and P_MSO in all_ids, f"export all ids: {all_ids}")

    admin("PUT", "/api/admin/routing/stations", [], params={"process_id": imp})
    resp = admin("DELETE", f"/api/admin/processes/{imp}")
    check(resp.status_code in (200, 204), f"cleanup: {resp.status_code} {resp.text}")

def test_model_app_fields_roundtrip():
    """ATE 基线/规则随流程导出→导入完整往返：字段不丢、口径不变。"""
    pid = f"PROC-TEST-EXP-{STAMP}"
    model = f"MODEL_EXP-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": pid, "process_name": "exp app fields"})
    admin(
        "POST",
        "/api/admin/product-models",
        {
            "product_model": model,
            "process_id": pid,
            "target_fw_version": "V3.20",
            "target_app_version": "V2.0.0",
            "app_match_rule": "min",
        },
    )
    admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []}],
        params={"process_id": pid},
    )
    admin(
        "POST",
        "/api/admin/routing/items",
        {"process_id": pid, "station_id": "CAL-PARAM", "case_id": "cal/CAL-PARAM",
         "item_name": "ATE 基线往返用例", "is_mandatory": True},
    )

    doc = admin("GET", f"/api/admin/routing/processes/{pid}/export").json()
    exported = doc["models"][0]
    check(exported["target_app_version"] == "V2.0.0", f"export baseline: {exported}")
    check(exported["app_match_rule"] == "min", f"export rule: {exported}")

    imp = f"PROC-EXP-IMP-{STAMP}"
    doc["process"]["process_id"] = imp
    doc["process"]["process_name"] = "exp app fields import"
    doc["models"][0]["product_model"] = f"MODEL-EXP-IMP-{STAMP}"
    resp = admin("POST", "/api/admin/routing/processes/import", doc)
    check(resp.status_code == 201 and resp.json()["models_created"] == 1, f"import: {resp.text}")

    rows = admin("GET", "/api/admin/product-models").json()
    imported = next(m for m in rows if m["product_model"] == f"MODEL-EXP-IMP-{STAMP}")
    check(imported["target_app_version"] == "V2.0.0", f"import baseline: {imported}")
    check(imported["app_match_rule"] == "min", f"import rule: {imported}")

def _login(username: str, password: str):
    return api("POST", "/api/auth/login", body={"username": username, "password": password})

def _user_id(username: str) -> int:
    rows = admin("GET", "/api/admin/users").json()
    return next(u["id"] for u in rows if u["username"] == username)

def test_role_permissions():
    """四角色权限矩阵：viewer 只读 / operator 产线操作 / manager 配置管理 / admin 全部。

    覆盖：登录响应带角色 / 越权 403 / 停用账号登录拒绝且旧 token 立即失效 /
    防自锁（最后一个有效 admin 不可删/停/降）。
    """
    # ① 用户管理（admin）：创建 + 重名 409 + 非法角色 400
    resp = admin("POST", "/api/admin/users",
                 {"username": "op1", "password": "op123456", "full_name": "产线员", "role": "operator"})
    check(resp.status_code == 201 and resp.json()["role"] == "operator", f"create operator: {resp.text}")
    resp = admin("POST", "/api/admin/users", {"username": "view1", "password": "view123456", "role": "viewer"})
    check(resp.status_code == 201, f"create viewer: {resp.text}")
    resp = admin("POST", "/api/admin/users", {"username": "view1", "password": "view123456", "role": "viewer"})
    check(resp.status_code == 409 and resp.json()["code"] == "username_exists", f"dup username: {resp.text}")
    resp = admin("POST", "/api/admin/users", {"username": "bad1", "password": "bad123456", "role": "hacker"})
    check(resp.status_code == 400 and resp.json()["code"] == "invalid_role", f"bad role: {resp.text}")

    # ② 登录响应携带角色
    resp = _login("view1", "view123456")
    check(resp.status_code == 200 and resp.json()["user"]["role"] == "viewer", f"login role: {resp.text}")
    view_tok = resp.json()["access_token"]
    op_tok = _login("op1", "op123456").json()["access_token"]

    # ③ viewer：读放行，一切写 403（用户管理也不可见）
    resp = api("GET", "/api/admin/processes", token=view_tok)
    check(resp.status_code == 200, f"viewer read: {resp.status_code}")
    resp = api("POST", "/api/admin/processes", body={"process_id": "PROC-X", "process_name": "x"}, token=view_tok)
    check(resp.status_code == 403 and resp.json()["code"] == "permission_denied", f"viewer write: {resp.text}")
    resp = api("POST", "/api/admin/clients", body={"client_id": "SZ-VIEW-01", "bound_stations": ["CAL-PARAM"]}, token=view_tok)
    check(resp.status_code == 403, f"viewer bind: {resp.status_code}")
    resp = api("GET", "/api/admin/users", token=view_tok)
    check(resp.status_code == 403, f"viewer users: {resp.status_code}")
    resp = api("GET", "/api/admin/processes")
    check(resp.status_code == 401, f"anonymous: {resp.status_code}")

    # ④ operator：机台绑定放行，工艺配置 403
    resp = api("POST", "/api/admin/clients",
               body={"client_id": new_client_id(), "bound_stations": ["CAL-PARAM"]}, token=op_tok)
    check(resp.status_code == 201, f"operator bind: {resp.status_code} {resp.text}")
    resp = api("POST", "/api/admin/processes", body={"process_id": "PROC-Y", "process_name": "y"}, token=op_tok)
    check(resp.status_code == 403, f"operator config: {resp.status_code}")
    resp = api("DELETE", "/api/admin/routing/items/1", token=op_tok)
    check(resp.status_code == 403, f"operator item delete: {resp.status_code}")

    # ⑤ 停用账号：登录拒绝 + 旧 token 立即失效
    vid = _user_id("view1")
    resp = admin("PUT", f"/api/admin/users/{vid}", {"is_active": False})
    check(resp.status_code == 200 and resp.json()["is_active"] is False, f"disable: {resp.text}")
    resp = _login("view1", "view123456")
    check(resp.status_code == 403 and resp.json()["code"] == "user_disabled", f"disabled login: {resp.text}")
    resp = api("GET", "/api/admin/processes", token=view_tok)
    check(resp.status_code == 401, f"disabled old token: {resp.status_code}")

    # ⑥ 防自锁：最后一个有效 admin 不可删 / 停用 / 降权（含操作自己）
    aid = _user_id("admin")
    resp = admin("DELETE", f"/api/admin/users/{aid}")
    check(resp.status_code == 409 and resp.json()["code"] == "cannot_delete_self", f"delete self: {resp.text}")
    resp = admin("PUT", f"/api/admin/users/{aid}", {"is_active": False})
    check(resp.status_code == 409 and resp.json()["code"] == "last_admin", f"disable last admin: {resp.text}")
    resp = admin("PUT", f"/api/admin/users/{aid}", {"role": "viewer"})
    check(resp.status_code == 409 and resp.json()["code"] == "last_admin", f"demote last admin: {resp.text}")
    resp = admin("POST", "/api/admin/users",
                 {"username": "admin2", "password": "admin23456", "role": "admin"})
    check(resp.status_code == 201, f"create admin2: {resp.text}")
    resp = admin("PUT", f"/api/admin/users/{_user_id('admin2')}", {"is_active": False})
    check(resp.status_code == 200, f"disable second admin: {resp.text}")

# ================= I2. manager 主管角色（admin 减用户管理） =================
def test_role_manager():
    """manager 主管角色：工艺配置/测试登记/报告规则可写，用户管理 403。

    覆盖：创建 manager / 配置写放行（流程+机型+测试登记）/ 用户管理读写 403 /
    产线操作超集放行（operator 能力）/ 非法角色校验回归（manager 是合法角色）。
    """
    resp = admin("POST", "/api/admin/users",
                 {"username": "mgr1", "password": "mgr123456", "full_name": "工艺主管", "role": "manager"})
    check(resp.status_code == 201 and resp.json()["role"] == "manager", f"create manager: {resp.text}")
    mgr = _login("mgr1", "mgr123456").json()["access_token"]

    # ① 配置写放行：工艺流程 + 机型 + 测试登记（原 require_admin 面）
    resp = api("POST", "/api/admin/processes",
               body={"process_id": f"PROC-MGR-TEST-{STAMP}", "process_name": "主管流程"}, token=mgr)
    check(resp.status_code == 201, f"manager process: {resp.status_code} {resp.text}")
    resp = api("POST", "/api/admin/product-models",
               body={"product_model": f"MGR-M-{STAMP}", "process_id": f"PROC-MGR-TEST-{STAMP}",
                     "target_fw_version": FW}, token=mgr)
    check(resp.status_code == 201, f"manager model: {resp.status_code} {resp.text}")
    resp = api("POST", "/api/admin/sn-registry", {"sn": new_sn("MGR"), "note": "主管登记"}, token=mgr)
    check(resp.status_code == 201, f"manager register: {resp.status_code} {resp.text}")

    # ② 用户管理：读写全部 403（职责分离的核心——manager 无提权入口）
    resp = api("GET", "/api/admin/users", token=mgr)
    check(resp.status_code == 403, f"manager users list: {resp.status_code}")
    resp = api("POST", "/api/admin/users",
               body={"username": "x-from-mgr", "password": "x123456", "role": "viewer"}, token=mgr)
    check(resp.status_code == 403, f"manager user create: {resp.status_code}")

    # ③ 产线操作超集：机台绑定放行（operator 能力）
    resp = api("POST", "/api/admin/clients",
               body={"client_id": new_client_id(), "bound_stations": ["CAL-PARAM"]}, token=mgr)
    check(resp.status_code == 201, f"manager bind: {resp.status_code} {resp.text}")

# ================= I. 测试产物 =================
def _artifact_upload(
    station,
    sn,
    session_id,
    case_id,
    content=b"cal-data",
    filename="amp.cal",
    lock_token=None,
    client_id=None,
):
    data = {
        "client_id": client_id or CLIENTS[station],
        "sn": sn,
        "session_id": session_id,
        "case_id": case_id,
    }
    if lock_token:
        data["lock_token"] = lock_token
    return client.post(
        "/api/v1/client/artifact",
        data=data,
        files={"file": (filename, content, "application/octet-stream")},
        headers={"X-API-Key": API_KEY},
    )

def test_artifacts():
    """测试产物全链路（真连 MinIO，复用 .env 的 MINIO_* 配置）：
    上传 → 出站并入 files → 管理端预签名 URL → 实际 GET 下载比对 → 清理测试对象。
    """
    import urllib.request

    from app.services import artifacts as artifact_store
    from app.services.reports import store as minio_store

    check(artifact_store.enabled(), "MinIO 未配置（.env 缺 MINIO_ENDPOINT/ACCESS_KEY），产物全链路无法执行")

    # 绕过系统代理直连（预签名 URL 的签名含 host，代理会破坏签名校验）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    uploaded_keys = []
    sn = new_sn("artifact")
    try:
        resp = checkin("CAL-PARAM", sn)
        check(resp.status_code == 200, f"check-in: {resp.status_code} {resp.text}")
        data = resp.json()["data"]
        session_id, lock_token = data["session_id"], data["lock_token"]

        # ① 上传成功：对象键按 artifacts/{model}/{sn}/{case_hash}/ 归档，文件名消毒
        case_hash = artifact_store.case_hash("test_amp_cal")
        resp = _artifact_upload("CAL-PARAM", sn, session_id, "test_amp_cal", b"amp-v1", "amp.cal", lock_token)
        check(resp.status_code == 200, f"artifact upload: {resp.status_code} {resp.text}")
        ref = resp.json()["data"]
        check(ref["filename"] == "amp.cal" and ref["size"] == 6, f"ref: {ref}")
        check(
            ref["object_key"].startswith(f"artifacts/{M_DPO}/{sn}/{case_hash}/amp.cal"),
            f"object_key: {ref['object_key']}",
        )
        key_amp = ref["object_key"]
        uploaded_keys.append(key_amp)

        # ② 路径穿越文件名被消毒
        resp = _artifact_upload("CAL-PARAM", sn, session_id, "test_amp_cal", b"x", "../../evil.txt", lock_token)
        check(resp.status_code == 200 and ".." not in resp.json()["data"]["object_key"], f"traversal: {resp.text}")
        uploaded_keys.append(resp.json()["data"]["object_key"])

        # ③ 同 case_id + 文件名重复上传 = 覆盖（对象键不变）
        resp = _artifact_upload("CAL-PARAM", sn, session_id, "test_amp_cal", b"amp-v2-longer", "amp.cal", lock_token)
        check(resp.status_code == 200 and resp.json()["data"]["object_key"] == key_amp, f"overwrite: {resp.text}")

        # ④ 空文件拒绝
        resp = _artifact_upload("CAL-PARAM", sn, session_id, "test_amp_cal", b"", "empty.cal", lock_token)
        check(resp.status_code == 400 and resp.json()["code"] == "artifact_empty", f"empty: {resp.text}")

        # ⑤ fencing：错误 lock_token → 403 lock_invalid
        resp = _artifact_upload("CAL-PARAM", sn, session_id, "test_amp_cal", b"x", "bad.cal", lock_token="wrong-token")
        check(resp.status_code == 403 and resp.json()["code"] == "lock_invalid", f"fencing: {resp.text}")

        # ⑥ 会话归属：他人机台的 session_id → 400 session_mismatch
        resp = _artifact_upload(
            "CAL-PARAM", sn, session_id, "test_amp_cal", b"x", "x.cal", client_id=CLIENTS["CAL-IFACE"]
        )
        check(resp.status_code == 400 and resp.json()["code"] == "session_mismatch", f"owner: {resp.text}")

        # ⑦ 普通断点上报（不含 files）→ 出站时服务端暂存引用自动并入
        resp = v1(
            "checkpoint",
            {
                "client_id": CLIENTS["CAL-PARAM"],
                "sn": sn,
                "session_id": session_id,
                "lock_token": lock_token,
                "items": [
                    {"case_id": "test_amp_cal", "result": "PASS",
                     "values": {"gain": 1.02}, "duration_ms": 800},
                ],
            },
        )
        check(resp.status_code == 200, f"checkpoint: {resp.text}")

        resp = _artifact_upload(
            "CAL-PARAM", sn, session_id, "test_phase_cal", b"phase-result", "phase.cal", lock_token
        )
        check(resp.status_code == 200, f"phase artifact: {resp.text}")
        uploaded_keys.append(resp.json()["data"]["object_key"])

        resp = checkout("CAL-PARAM", sn, ITEMS["CAL-PARAM"], lock_token=lock_token)
        check(resp.status_code == 201, f"check-out: {resp.status_code} {resp.text}")
        record_id = resp.json()["data"]["record_id"]

        resp = admin("GET", f"/api/admin/records/{record_id}")
        items = {it["case_id"]: it for it in resp.json()["executed_items"]["items"]}
        check(items["test_amp_cal"]["files"][0]["object_key"] == key_amp, f"staged files: {items['test_amp_cal']}")
        phase_files = items["test_phase_cal"].get("files") or []
        check(
            len(phase_files) == 1 and phase_files[0]["filename"] == "phase.cal" and phase_files[0]["size"] == 12,
            f"staged merged: {phase_files}",
        )

        # ⑧ 下载端点返回预签名 URL，实际 GET 内容 = 最后一次覆盖上传
        resp = admin("GET", f"/api/admin/records/{record_id}/files/0")
        check(resp.status_code == 200, f"download: {resp.status_code} {resp.text}")
        dl = resp.json()
        check(dl["filename"] == "amp.cal" and dl["url"], f"download: {dl}")
        with opener.open(dl["url"], timeout=30) as r:
            check(r.status == 200 and r.read() == b"amp-v2-longer", "presigned GET content mismatch")
        resp = admin("GET", f"/api/admin/records/{record_id}/files/9")
        check(resp.status_code == 404, f"index oob: {resp.status_code}")
        resp = api("GET", f"/api/admin/records/{record_id}/files/0")
        check(resp.status_code == 401, f"anonymous download: {resp.status_code}")
    finally:
        # 清理测试对象，不在共享桶留垃圾
        for key in uploaded_keys:
            try:
                minio_store.remove(key)
            except Exception:  # noqa: BLE001 清理失败不影响测试结论
                pass

def test_report_batch_requires_completed():
    """报告是入库凭据：手动批量入队收口在完工件，未盖满章的 SN 不得生成报告。

    入库治理 = "打不出报告 = 未完工 = 拒收"，因此报告生成必须只可能发生在
    全部盖章之后（自动扫描本来就按 is_completed 过滤，此处补手动入口的同款守卫）。
    """
    # 建一条 DPO 报告规则（脚本可空，仅用于通过 no_plugin 校验，聚焦完工守卫本身）
    resp = admin(
        "POST",
        "/api/admin/report-rules",
        {"rule": "DPO_DEFAULT", "models": [M_DPO], "auto_trigger": False},
    )
    check(resp.status_code == 201, f"create rule: {resp.text}")

    # 未完工件（只过一站）→ 拒绝入队
    wip_sn = new_sn("WIPRPT")
    pass_station("CAL-PARAM", wip_sn)
    resp = admin("POST", "/api/admin/report-jobs/batch", {"sns": [wip_sn]})
    check(resp.status_code == 200, f"batch wip: {resp.text}")
    body = resp.json()
    check(body["created"] == 0, f"未完工不得入队: {body}")
    check(
        body["skipped"] == [{"sn": wip_sn, "reason": "not_completed"}],
        f"skip 口径: {body['skipped']}",
    )

    # 完工件（走满 DPO 四站）→ 正常入队
    done_sn = new_sn("DONEPRPT")
    for station in ("CAL-PARAM", "CAL-IFACE", "TST-PARAM", "TST-IFACE"):
        pass_station(station, done_sn)
    resp = admin("POST", "/api/admin/report-jobs/batch", {"sns": [done_sn]})
    check(resp.status_code == 200, f"batch done: {resp.text}")
    check(resp.json()["created"] == 1, f"完工件应正常入队: {resp.text}")

def test_optional_station_sampling():
    """选检工位（QA 抽查）：不卡完工、抽查件正常进站出站、必跑依赖选检被阻断。

    场景：CAL-PARAM（必跑）→ QA-CHECK（选检）。未被抽到的件只过 CAL-PARAM
    即完工；被抽中的件走完 QA 后 is_completed 不变；必跑工位依赖选检工位
    保存即 409（否则选检变相必跑，抽查语义失效）。
    """
    p_opt = f"PROC-TEST-OPT-{STAMP}"
    resp = admin("POST", "/api/admin/processes", {"process_id": p_opt, "process_name": "选检流程"})
    check(resp.status_code == 201, f"create opt process: {resp.text}")
    resp = admin("POST", "/api/admin/stations", {"station_id": "QA-CHECK", "station_name": "QA抽查站位", "timeout_sec": 600})
    check(resp.status_code == 201, f"create qa station: {resp.text}")
    m_opt = "DPO4054C"
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {"product_model": m_opt, "process_id": p_opt, "target_fw_version": FW},
    )
    check(resp.status_code == 201, f"create opt model: {resp.text}")

    def put_topology(steps):
        return admin("PUT", "/api/admin/routing/stations", steps, params={"process_id": p_opt})

    # 必跑工位依赖选检工位 → 保存被阻断（结构性错误，同成环同级）
    bad = [
        {"station_id": "CAL-PARAM", "step_order": 10, "depends_on": ["QA-CHECK"], "is_optional": False},
        {"station_id": "QA-CHECK", "step_order": 20, "depends_on": [], "is_optional": True},
    ]
    resp = put_topology(bad)
    check(resp.status_code == 409 and resp.json()["code"] == "topology_invalid", f"dep guard: {resp.text}")
    check(
        any(i["code"] == "mandatory_dep_on_optional" for i in resp.json()["data"]["issues"]),
        f"guard code missing: {resp.text}",
    )

    # 正确拓扑：QA-CHECK 选检，且其后还有必跑工位（CAL-XRAY 字母序 < QA-CHECK，
    # 用于区分"字母序展示"与"工步序路径"）；保存后读回应带 is_optional
    resp = admin("POST", "/api/admin/stations", {"station_id": "CAL-XRAY", "station_name": "X光检查站位", "timeout_sec": 600})
    check(resp.status_code == 201, f"create xray station: {resp.text}")
    CLIENTS["CAL-XRAY"] = "SZ-L9-QA-02"
    resp = admin("POST", "/api/admin/clients", {"client_id": CLIENTS["CAL-XRAY"], "bound_stations": ["CAL-XRAY"]})
    check(resp.status_code == 201, f"create xray client: {resp.text}")
    good = [
        {"station_id": "CAL-PARAM", "step_order": 10, "depends_on": [], "is_optional": False},
        {"station_id": "QA-CHECK", "step_order": 20, "depends_on": ["CAL-PARAM"], "is_optional": True},
        {"station_id": "CAL-XRAY", "step_order": 30, "depends_on": ["CAL-PARAM"], "is_optional": False},
    ]
    resp = put_topology(good)
    check(resp.status_code == 200, f"save opt topology: {resp.text}")
    optional_flags = {s["station_id"]: s["is_optional"] for s in resp.json()}
    check(
        optional_flags == {"CAL-PARAM": False, "QA-CHECK": True, "CAL-XRAY": False},
        f"roundtrip flags: {resp.text}",
    )

    # 各站建 1 条必测项（否则 station_no_item）
    for sid, case in (
        ("CAL-PARAM", "test_opt_base"),
        ("QA-CHECK", "test_qa_sample"),
        ("CAL-XRAY", "test_xray_check"),
    ):
        resp = admin(
            "POST",
            "/api/admin/routing/items",
            {"process_id": p_opt, "station_id": sid, "case_id": case, "item_name": case},
        )
        check(resp.status_code == 201, f"create item {sid}: {resp.text}")

    # 导出契约携带选检标志
    resp = admin("GET", f"/api/admin/routing/processes/{p_opt}/export")
    steps = {s["station_id"]: s["is_optional"] for s in resp.json()["steps"]}
    check(
        steps == {"CAL-PARAM": False, "QA-CHECK": True, "CAL-XRAY": False},
        f"export flags: {resp.text}",
    )

    # 未被抽查的件：过两个必跑工位即完工（QA 缺席不卡）
    wip_sn = new_sn("OPTSKIP")
    pass_station("CAL-PARAM", wip_sn, model=m_opt)
    pass_station("CAL-XRAY", wip_sn, model=m_opt)
    resp = admin("GET", f"/api/admin/products/{wip_sn}")
    body = resp.json()
    check(body["is_completed"] is True, f"未抽查件应完工: {body}")
    check(body["total_steps"] == 2 and body["passed_count"] == 2, f"进度只数必跑: {body}")
    check(body["passed_path"] == ["CAL-PARAM", "CAL-XRAY"], f"未抽查件路径: {body.get('passed_path')}")

    # 被抽查的件：CAL-PARAM → QA-CHECK（抽检）→ CAL-XRAY，完工状态与进度口径不变
    resp = admin("POST", "/api/admin/clients", {"client_id": "SZ-L9-QA-01", "bound_stations": ["QA-CHECK"]})
    check(resp.status_code == 201, f"create qa client: {resp.text}")
    sampled_sn = new_sn("OPTQA")
    pass_station("CAL-PARAM", sampled_sn, model=m_opt)
    qa_client = "SZ-L9-QA-01"
    resp = v1(
        "check-in",
        {"client_id": qa_client, "sn": sampled_sn, "product_model": m_opt, "firmware": FW},
    )
    check(resp.status_code == 200, f"qa check-in: {resp.text}")
    case_ids = rules_of(resp)
    # QA 机不在 CLIENTS 夹具表，出站直接以 qa_client 构造
    resp = v1(
        "check-out",
        {
            "client_id": qa_client,
            "sn": sampled_sn,
            "checkout_id": uuid.uuid4().hex,
            "items": items_for(case_ids),
            "duration_ms": 60000,
        },
    )
    check(resp.status_code == 201, f"qa check-out: {resp.status_code} {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sampled_sn}")
    body = resp.json()
    check(body["is_completed"] is False, f"QA 过后还差 CAL-XRAY: {body}")
    # passed_path 按工步序排列；字母序会给出 ["CAL-PARAM", "CAL-XRAY", "QA-CHECK"]，两者可区分
    check(
        body["passed_path"] == ["CAL-PARAM", "QA-CHECK"],
        f"抽查件盖章路径顺序: {body.get('passed_path')}",
    )
    pass_station("CAL-XRAY", sampled_sn, model=m_opt)
    resp = admin("GET", f"/api/admin/products/{sampled_sn}")
    body = resp.json()
    check(body["is_completed"] is True, f"抽查件补完 CAL-XRAY 后完工: {body}")
    check(body["passed_count"] == 2 and body["total_steps"] == 2, f"抽查件进度口径: {body}")
    check(
        body["passed_path"] == ["CAL-PARAM", "QA-CHECK", "CAL-XRAY"],
        f"抽查件盖章路径顺序: {body.get('passed_path')}",
    )
    check(body["optional_stations"] == ["QA-CHECK"], f"选检清单: {body.get('optional_stations')}")
    # 追溯工步透出选检标志（前端据此显示"选检"标签）
    resp = admin("GET", f"/api/admin/records/trace/{sampled_sn}")
    trace_flags = {s["station_id"]: s["is_optional"] for s in resp.json()["steps"]}
    check(
        trace_flags == {"CAL-PARAM": False, "QA-CHECK": True, "CAL-XRAY": False},
        f"trace flags: {trace_flags}",
    )

# ================= I. SN 准入极性（SERVER_ROLE 双态） =================
def test_sn_registry_status_api():
    """V1 只读登记状态查询：登记 true / 未登记 false / 无 SN 参数 422，供上位机路由判定。"""
    resp = api("POST", "/api/admin/sn-registry", {"sn": "SIMRS-ROUTE-1", "note": "路由判定"},
               token=token())
    check(resp.status_code == 201, f"register: {resp.text}")
    resp = api("GET", "/api/v1/client/sn-registry/status", params={"sn": "SIMRS-ROUTE-1"},
               api_key=API_KEY)
    check(resp.status_code == 200 and resp.json()["data"]["registered"] is True,
          f"registered: {resp.text}")
    resp = api("GET", "/api/v1/client/sn-registry/status", params={"sn": "SIMRS-ROUTE-X"},
               api_key=API_KEY)
    check(resp.status_code == 200 and resp.json()["data"]["registered"] is False,
          f"unregistered: {resp.text}")
    resp = api("GET", "/api/v1/client/sn-registry/status", api_key=API_KEY)
    check(resp.status_code == 422, f"missing sn: {resp.status_code}")

def test_sn_registry_polarity():
    """SN 准入极性：SERVER_ROLE 环境变量切换，服务端硬卡控，SDK 零改动仅透传 403。

    覆盖：未登记型号 403 回归（型号校验先于 SN 极性）/ 登记 CRUD 与重复 409 /
    production 黑名单（登记 SN 进站 403，台账零痕迹）/ development 白名单（未登记 403、
    登记后进站成功）/ 注销仅解除登记（不触碰设备记录，重流线由人显式走 RESET）/
    注销后恢复 / 写操作 require_admin 收口。
    """
    sn = new_sn("REG")

    # ① 未登记型号 403 保持（及时卡控回归）
    resp = checkin("CAL-PARAM", sn, model="NO-SUCH-MODEL")
    check(
        resp.status_code == 403 and resp.json()["code"] == "model_not_registered",
        f"model_not_registered regression: {resp.status_code} {resp.text}",
    )

    # ② 登记 CRUD：201 + 登记人留痕 + 重复登记 409 + 清单可见
    resp = admin("POST", "/api/admin/sn-registry", {"sn": sn, "note": "EVT 工程样机"})
    check(
        resp.status_code == 201 and resp.json()["registered_by"] == "admin",
        f"register: {resp.status_code} {resp.text}",
    )
    resp = admin("POST", "/api/admin/sn-registry", {"sn": sn})
    check(
        resp.status_code == 409 and resp.json()["code"] == "sn_already_registered",
        f"duplicate register: {resp.status_code} {resp.text}",
    )
    resp = admin("GET", "/api/admin/sn-registry")
    check(
        resp.status_code == 200 and any(r["sn"] == sn for r in resp.json()),
        f"registry list: {resp.status_code} {resp.text}",
    )

    # ③ production 黑名单（默认角色）：登记 SN 进站 403，且不建档（台账零痕迹）
    resp = checkin("CAL-PARAM", sn, model=M_DPO)
    check(
        resp.status_code == 403 and resp.json()["code"] == "sn_is_test_device",
        f"production blacklist: {resp.status_code} {resp.text}",
    )
    with SessionLocal() as db:
        check(db.get(models.ProductStatus, sn) is None, "blacklisted check-in must leave no trace")

    # ④ development 白名单：未登记 SN 服务端 403 拒收；登记 SN 进站成功
    unregistered = new_sn("UNREG")
    old_role = settings.SERVER_ROLE
    try:
        settings.SERVER_ROLE = "development"
        resp = checkin("CAL-PARAM", unregistered, model=M_DPO)
        check(
            resp.status_code == 403 and resp.json()["code"] == "sn_not_registered_test_device",
            f"development whitelist unregistered: {resp.status_code} {resp.text}",
        )
        resp = checkin("CAL-PARAM", sn, model=M_DPO)
        check(
            resp.status_code == 200 and resp.json()["ok"] is True,
            f"development whitelist registered: {resp.status_code} {resp.text}",
        )
        lock_token = resp.json()["data"]["lock_token"]

        # ⑤ 注销只解除登记：不自动重置，设备测试记录原样保留（持锁也可注销）；
        #    重流线由工艺员显式走维修处置 RESET，系统不代劳
        resp = admin("DELETE", f"/api/admin/sn-registry/{sn}")
        check(resp.status_code == 204, f"unregister must not touch records: {resp.status_code} {resp.text}")
        resp = admin("GET", f"/api/admin/products/{sn}")
        body = resp.json()
        check(
            body["current_status"] == "TESTING",
            f"unregister must leave records untouched: {body}",
        )
        resp = v1("release", {"client_id": CLIENTS["CAL-PARAM"], "sn": sn, "lock_token": lock_token, "reason": "test"})
        check(resp.status_code == 200, f"release: {resp.status_code} {resp.text}")
    finally:
        settings.SERVER_ROLE = old_role

    # ⑥ 重新流线回归：注销后从首站正常进站（生产侧真实重流线走 RESET，此处回归准入恢复）
    resp = checkin("CAL-PARAM", sn, model=M_DPO)
    check(
        resp.status_code == 200 and resp.json()["ok"] is True,
        f"re-flow after unregister: {resp.status_code} {resp.text}",
    )

    # ⑦ 注销后恢复：无记录 SN 注销仅删登记，黑名单解除后正常进站（回归）
    sn2 = new_sn("REG2")
    resp = admin("POST", "/api/admin/sn-registry", {"sn": sn2, "note": "临时借测"})
    check(resp.status_code == 201, f"register sn2: {resp.status_code} {resp.text}")
    resp = checkin("CAL-PARAM", sn2, model=M_DPO)
    check(
        resp.status_code == 403 and resp.json()["code"] == "sn_is_test_device",
        f"sn2 blacklist: {resp.status_code} {resp.text}",
    )
    resp = admin("DELETE", f"/api/admin/sn-registry/{sn2}")
    check(resp.status_code == 204, f"unregister sn2: {resp.status_code} {resp.text}")
    resp = checkin("CAL-PARAM", sn2, model=M_DPO)
    check(
        resp.status_code == 200 and resp.json()["ok"] is True,
        f"restored check-in: {resp.status_code} {resp.text}",
    )

    # ⑧ 权限：写操作 require_admin 收口（viewer 403；清单只读对全员开放）
    admin("POST", "/api/admin/users",
          {"username": "view_reg", "password": "view123456", "full_name": "只读", "role": "viewer"})
    viewer_token = _login("view_reg", "view123456").json()["access_token"]
    resp = api("POST", "/api/admin/sn-registry", {"sn": "SN-VIEWER-TRY"}, token=viewer_token)
    check(resp.status_code == 403, f"viewer register denied: {resp.status_code} {resp.text}")
    resp = api("GET", "/api/admin/sn-registry", token=viewer_token)
    check(resp.status_code == 200, f"viewer list allowed: {resp.status_code} {resp.text}")

# ================= K. 批量维修处置（dry_run 预演 + 破坏面回显） =================
def test_repair_batch():
    """批量处置：dry_run 只校验不落库并回显破坏面；逐条独立生效留痕。

    覆盖：dry_run 混合成败（不存在/重复/空 SN）且零写入 / 执行后逐条生效 /
    RESET/SCRAP 禁批量 400 / 空清单与超上限 400 / 报废件在批量中按失败项返回。
    """
    sn_a, sn_b = new_sn("BATCH-A"), new_sn("BATCH-B")
    pass_station("CAL-PARAM", sn_a)
    pass_station("CAL-IFACE", sn_a)
    pass_station("CAL-PARAM", sn_b)

    # ① RESET/SCRAP 禁批量；空清单 400
    resp = admin("POST", "/api/admin/repairs/batch", {"items": [sn_a], "repair_action": "RESET", "reason": "x"})
    check(resp.status_code == 400 and resp.json()["code"] == "action_not_batchable", f"reset batch: {resp.text}")
    resp = admin("POST", "/api/admin/repairs/batch", {"items": [sn_a], "repair_action": "SCRAP", "reason": "x"})
    check(resp.status_code == 400 and resp.json()["code"] == "action_not_batchable", f"scrap batch: {resp.text}")
    resp = admin("POST", "/api/admin/repairs/batch", {"items": [], "repair_action": "RETEST"})
    check(resp.status_code == 400 and resp.json()["code"] == "items_required", f"empty items: {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/repairs/batch",
        {"items": [f"SN-X{i}" for i in range(51)], "repair_action": "RETEST", "target_station": "CAL-PARAM"},
    )
    check(resp.status_code == 400 and resp.json()["code"] == "batch_too_large", f"too large: {resp.text}")

    # ② dry_run：混合成败（不存在 + 重复 + 空 SN），零写入
    payload = {
        "items": [sn_a, sn_a, "", new_sn("NOPE"), sn_b],
        "repair_action": "RETEST",
        "target_station": "CAL-PARAM",
        "reason": "批量预演",
        "dry_run": True,
    }
    resp = admin("POST", "/api/admin/repairs/batch", payload)
    body = resp.json()
    check(resp.status_code == 200 and body["dry_run"] is True, f"dry_run: {resp.text}")
    check(body["total"] == 5 and body["ok_count"] == 2 and body["fail_count"] == 3, f"dry_run counts: {body}")
    ok_a = next(i for i in body["items"] if i["sn"] == sn_a)
    check(
        ok_a["current_status"] == "IDLE"
        and ok_a["revoked_stations"] == ["CAL-PARAM"]
        and ok_a["invalidating_records"] == 1
        and ok_a["product_model"] == M_DPO
        and ok_a["process_id"] == P_DPO,
        f"dry_run preview surface: {ok_a}",
    )
    check(
        any(i["code"] == "product_not_found" for i in body["items"])
        and any(i["code"] == "duplicate_sn" for i in body["items"])
        and any(i["code"] == "field_required" for i in body["items"]),
        f"dry_run failures: {body}",
    )
    # 零写入：印章未动、无维修履历
    resp = admin("GET", f"/api/admin/products/{sn_a}")
    check(resp.json()["passed_stations"] == ["CAL-IFACE", "CAL-PARAM"], f"dry_run no mutation: {resp.json()}")
    resp = admin("GET", "/api/admin/repairs", params={"sn": sn_a})
    check(resp.json()["total"] == 0, f"dry_run no history: {resp.json()['total']}")

    # ③ 执行：逐条生效（sn_a 成功、不存在项失败），失败项不影响已成功项
    payload["dry_run"] = False
    resp = admin("POST", "/api/admin/repairs/batch", payload)
    body = resp.json()
    check(body["ok_count"] == 2 and body["fail_count"] == 3, f"execute counts: {body}")
    resp = admin("GET", f"/api/admin/products/{sn_a}")
    check(resp.json()["passed_stations"] == ["CAL-IFACE"], f"batch retest applied: {resp.json()}")
    resp = admin("GET", "/api/admin/repairs", params={"sn": sn_a})
    check(resp.json()["total"] == 1 and resp.json()["items"][0]["repair_action"] == "RETEST", f"history: {resp.text}")
    resp = admin("GET", "/api/admin/records", params={"sn": sn_a, "station_id": "CAL-PARAM", "is_valid": False})
    check(resp.json()["total"] == 1, f"batch invalidated records: {resp.text}")

    # ④ 报废件在批量中按失败项返回（复活守卫同样生效于批量）
    sn_c = new_sn("BATCH-C")
    pass_station("CAL-PARAM", sn_c)
    resp = admin("POST", "/api/admin/repairs", {"sn": sn_c, "repair_action": "SCRAP", "reason": "报废"})
    check(resp.status_code == 201, f"scrap sn_c: {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/repairs/batch",
        {"items": [sn_c], "repair_action": "RETEST", "target_station": "CAL-PARAM", "reason": "x"},
    )
    body = resp.json()
    check(
        body["fail_count"] == 1 and body["items"][0]["code"] == "product_scrapped",
        f"scrapped in batch: {body}",
    )

# ================= L. 撤销维修处置（窄窗口快照还原） =================
def test_repair_undo():
    """撤销处置：按处置前快照精确还原（印章/失败计数/台账记录）。

    覆盖：RETEST 后撤销全字段还原（含 fail_count）/ 二次撤销 409 already_undone /
    处置后产线有动作 → 409 repair_state_changed / 非最新条 → 409 not_latest_disposition /
    SCRAP → 409 action_not_undoable。
    """
    sn = new_sn("UNDO")
    pass_station("CAL-PARAM", sn)
    pass_station("CAL-IFACE", sn)
    with SessionLocal() as db:
        product = db.get(models.ProductStatus, sn)
        product.fail_count = 2
        db.commit()

    # RETEST CAL-IFACE：印章撤除 + fail_count 清零 + 台账记录作废
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "CAL-IFACE", "reason": "误操作演练"},
    )
    check(resp.status_code == 201, f"retest: {resp.text}")
    repair_id = resp.json()["repair_id"]
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.json()["passed_stations"] == ["CAL-PARAM"], f"after retest: {resp.json()}")
    resp = admin("GET", "/api/admin/records", params={"sn": sn, "station_id": "CAL-IFACE", "is_valid": False})
    check(resp.json()["total"] == 1, f"invalidated: {resp.text}")

    # 撤销：印章/失败计数/台账记录按快照精确还原
    resp = admin("POST", f"/api/admin/repairs/{repair_id}/undo")
    check(
        resp.status_code == 200 and resp.json()["undone_by"] == "admin" and resp.json()["undone_at"],
        f"undo: {resp.text}",
    )
    resp = admin("GET", f"/api/admin/products/{sn}")
    body = resp.json()
    check(
        sorted(body["passed_stations"]) == ["CAL-IFACE", "CAL-PARAM"] and body["fail_count"] == 2,
        f"undo restore: {body}",
    )
    resp = admin("GET", "/api/admin/records", params={"sn": sn, "station_id": "CAL-IFACE", "is_valid": True})
    check(resp.json()["total"] == 1, f"record revalidated: {resp.text}")

    # 二次撤销 → 409 already_undone
    resp = admin("POST", f"/api/admin/repairs/{repair_id}/undo")
    check(resp.status_code == 409 and resp.json()["code"] == "already_undone", f"re-undo: {resp.text}")

    # 非最新条 → 409 not_latest_disposition（连续两次处置，只能撤最新的）
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "CAL-PARAM", "reason": "第二次处置"},
    )
    check(resp.status_code == 201, f"second disposition: {resp.text}")
    middle_id = resp.json()["repair_id"]
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "RETEST", "target_station": "CAL-IFACE", "reason": "第三次处置"},
    )
    check(resp.status_code == 201, f"third disposition: {resp.text}")
    resp = admin("POST", f"/api/admin/repairs/{middle_id}/undo")
    check(
        resp.status_code == 409 and resp.json()["code"] == "not_latest_disposition",
        f"undo older: {resp.status_code} {resp.text}",
    )

    # 处置后产线有动作（重新过站产生新会话）→ 409 repair_state_changed
    sn2 = new_sn("UNDO2")
    pass_station("CAL-PARAM", sn2)
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn2, "repair_action": "RETEST", "target_station": "CAL-PARAM", "reason": "越窗演练"},
    )
    check(resp.status_code == 201, f"retest sn2: {resp.text}")
    pass_station("CAL-PARAM", sn2)  # 处置后产线重新动作
    resp = admin("GET", "/api/admin/repairs", params={"sn": sn2})
    sn2_repair = resp.json()["items"][0]["repair_id"]
    resp = admin("POST", f"/api/admin/repairs/{sn2_repair}/undo")
    check(
        resp.status_code == 409 and resp.json()["code"] == "repair_state_changed",
        f"undo after activity: {resp.status_code} {resp.text}",
    )

    # SCRAP 不可撤销（409 action_not_undoable）
    resp = admin("POST", "/api/admin/repairs", {"sn": sn2, "repair_action": "SCRAP", "reason": "报废"})
    check(resp.status_code == 201, f"scrap sn2: {resp.text}")
    resp = admin("GET", "/api/admin/repairs", params={"sn": sn2})
    scrap_id = resp.json()["items"][0]["repair_id"]
    resp = admin("POST", f"/api/admin/repairs/{scrap_id}/undo")
    check(
        resp.status_code == 409 and resp.json()["code"] == "action_not_undoable",
        f"undo scrap: {resp.status_code} {resp.text}",
    )

# ================= M. 维修履历筛选 =================
def test_repair_list_filters():
    """履历筛选：操作人模糊 + 日期区间（本地日界）+ 动作/撤销状态组合。"""
    today = time.strftime("%Y-%m-%d")
    dates = {"date_from": today, "date_to": today}
    # 操作人筛选：admin 的处置都能查到
    resp = admin("GET", "/api/admin/repairs", params={"technician": "admin", **dates})
    body = resp.json()
    check(resp.status_code == 200 and body["total"] >= 1, f"technician filter: {resp.text}")
    check(
        all(r["technician_id"] == "admin" for r in body["items"]),
        f"technician filter rows: {body}",
    )
    # 今日区间 + 动作组合筛选
    resp = admin("GET", "/api/admin/repairs", params={"repair_action": "RETEST", **dates})
    check(
        all(r["repair_action"] == "RETEST" for r in resp.json()["items"]),
        f"action+date filter: {resp.text}",
    )
    # 撤销状态筛选：true=仅已撤销 / false=仅有效
    resp = admin("GET", "/api/admin/repairs", params={"undone": "true", **dates})
    check(
        resp.status_code == 200
        and resp.json()["total"] >= 1
        and all(r["undone_at"] for r in resp.json()["items"]),
        f"undone=true filter: {resp.text}",
    )
    resp = admin("GET", "/api/admin/repairs", params={"undone": "false", **dates})
    check(
        resp.status_code == 200 and all(not r["undone_at"] for r in resp.json()["items"]),
        f"undone=false filter: {resp.text}",
    )
    # 非法日期格式 400
    resp = admin("GET", "/api/admin/repairs", params={"date_from": "2026/01/01"})
    check(
        resp.status_code == 400 and resp.json()["code"] == "invalid_date_format",
        f"invalid date: {resp.text}",
    )

# ================= I4. 依赖重测与用例执行顺序 =================
def test_dependency_retest_cascade():
    """RETEST 依赖重测工位：未确认 409 + 下游闭包清单 / 确认后一并清除下游印章。"""
    proc = f"PROC-CASC-TEST-{STAMP}"
    resp = admin("POST", "/api/admin/processes", {"process_id": proc, "process_name": "级联验证"})
    check(resp.status_code == 201, f"create proc: {resp.text}")
    topo = [
        {"station_id": "CAL-PARAM", "step_order": 10, "depends_on": [],
         "invalidates_downstream": True},
        {"station_id": "CAL-IFACE", "step_order": 20, "depends_on": ["CAL-PARAM"]},
        {"station_id": "TST-PARAM", "step_order": 30, "depends_on": ["CAL-IFACE"]},
        # 依赖链之外：工步顺序在后但 depends_on 不经过 CAL-PARAM → 不级联
        {"station_id": "CAL-AWG", "step_order": 40, "depends_on": []},
    ]
    resp = admin("PUT", "/api/admin/routing/stations", topo, params={"process_id": proc})
    check(resp.status_code == 200, f"topo: {resp.text}")
    for it in (
        {"process_id": proc, "station_id": "CAL-PARAM", "case_id": "tests/casc.py::test_cal",
         "item_name": "重校准", "sort_order": 1},
        {"process_id": proc, "station_id": "CAL-IFACE", "case_id": "tests/casc.py::test_iface",
         "item_name": "接口测量"},
        {"process_id": proc, "station_id": "TST-PARAM", "case_id": "tests/casc.py::test_param",
         "item_name": "指标测量"},
        {"process_id": proc, "station_id": "CAL-AWG", "case_id": "tests/casc.py::test_awg",
         "item_name": "AWG 测量"},
    ):
        resp = admin("POST", "/api/admin/routing/items", it)
        check(resp.status_code == 201, f"create item: {resp.status_code} {resp.text}")
    model = f"M-CASC-{STAMP}"
    resp = admin("POST", "/api/admin/product-models",
                 {"product_model": model, "process_id": proc, "target_fw_version": FW})
    check(resp.status_code == 201, f"create model: {resp.text}")

    sn = new_sn("CASC")
    for station in ("CAL-PARAM", "CAL-IFACE", "TST-PARAM", "CAL-AWG"):
        pass_station(station, sn, model=model)

    resp = admin("POST", "/api/admin/repairs",
                 {"sn": sn, "repair_action": "RETEST", "target_station": "CAL-PARAM",
                  "reason": "重校准"})
    check(
        resp.status_code == 409 and resp.json()["code"] == "downstream_invalidation_required",
        f"cascade 409: {resp.text}",
    )
    check(
        resp.json()["data"]["stations"] == ["CAL-IFACE", "TST-PARAM"],
        f"closure list: {resp.text}",
    )

    resp = admin("POST", "/api/admin/repairs",
                 {"sn": sn, "repair_action": "RETEST", "target_station": "CAL-PARAM",
                  "reason": "重校准", "confirm_downstream": True})
    check(resp.status_code == 201, f"cascade retest: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(
        resp.json()["passed_stations"] == ["CAL-AWG"],
        f"closure only: dependency-free station survives: {resp.json()}",
    )

    # 批量：未确认 → 全部失败（依赖重测 409）；确认 → 级联生效
    sn2 = new_sn("CASC2")
    for station in ("CAL-PARAM", "CAL-IFACE", "TST-PARAM", "CAL-AWG"):
        pass_station(station, sn2, model=model)
    resp = admin("POST", "/api/admin/repairs/batch",
                 {"items": [sn2], "repair_action": "RETEST", "target_station": "CAL-PARAM",
                  "reason": "批量重校准", "dry_run": True})
    check(resp.status_code == 200, f"batch precheck: {resp.text}")
    body = resp.json()
    check(
        body["fail_count"] == 1 and body["items"][0]["code"] == "downstream_invalidation_required",
        f"batch cascade 409: {body}",
    )
    resp = admin("POST", "/api/admin/repairs/batch",
                 {"items": [sn2], "repair_action": "RETEST", "target_station": "CAL-PARAM",
                  "reason": "批量重校准", "confirm_downstream": True})
    check(resp.status_code == 200 and resp.json()["ok_count"] == 1, f"batch cascade ok: {resp.text}")
    batch_id = resp.json()["batch_id"]
    check(batch_id, f"batch_id returned: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn2}")
    check(resp.json()["passed_stations"] == ["CAL-AWG"], f"batch cascaded stamps: {resp.json()}")

    # 批量撤销：整批按快照还原 → 印章与记录恢复；重复撤销 → already_undone
    resp = admin("POST", "/api/admin/repairs/batch-undo", {"batch_id": batch_id})
    check(resp.status_code == 200 and resp.json()["ok_count"] == 1, f"batch undo: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn2}")
    check(
        sorted(resp.json()["passed_stations"]) ==
        sorted(["CAL-PARAM", "CAL-IFACE", "TST-PARAM", "CAL-AWG"]),
        f"undo restored stamps: {resp.json()}",
    )
    resp = admin("POST", "/api/admin/repairs/batch-undo", {"batch_id": batch_id})
    check(
        resp.status_code == 200 and resp.json()["items"][0]["code"] == "already_undone",
        f"repeat undo: {resp.text}",
    )


def test_item_sort_order():
    """用例执行顺序：下发清单按 sort_order 排序（字母序被覆盖）；重复序号 warning。"""
    proc = f"PROC-ORDER-TEST-{STAMP}"
    resp = admin("POST", "/api/admin/processes", {"process_id": proc, "process_name": "排序验证"})
    check(resp.status_code == 201, f"create proc: {resp.text}")
    topo = [
        {"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []},
        {"station_id": "CAL-IFACE", "step_order": 20, "depends_on": ["CAL-PARAM"]},
    ]
    resp = admin("PUT", "/api/admin/routing/stations", topo, params={"process_id": proc})
    check(resp.status_code == 200, f"topo: {resp.text}")
    for it in (
        {"process_id": proc, "station_id": "CAL-PARAM", "case_id": "tests/x.py::test_a",
         "item_name": "测量A", "sort_order": 2},
        {"process_id": proc, "station_id": "CAL-PARAM", "case_id": "tests/x.py::test_b",
         "item_name": "校准B", "sort_order": 1},
        {"process_id": proc, "station_id": "CAL-IFACE", "case_id": "tests/x.py::test_c",
         "item_name": "重复序C", "sort_order": 1},
        {"process_id": proc, "station_id": "CAL-IFACE", "case_id": "tests/x.py::test_d",
         "item_name": "重复序D", "sort_order": 1},
        {"process_id": proc, "station_id": "CAL-IFACE", "case_id": "tests/x.py::test_e",
         "item_name": "自动续排E"},
    ):
        resp = admin("POST", "/api/admin/routing/items", it)
        check(resp.status_code == 201, f"create item: {resp.status_code} {resp.text}")

    model = f"M-ORDER-{STAMP}"
    resp = admin("POST", "/api/admin/product-models",
                 {"product_model": model, "process_id": proc, "target_fw_version": FW})
    check(resp.status_code == 201, f"create model: {resp.text}")

    sn = new_sn("ORDER")
    resp = checkin("CAL-PARAM", sn, model=model)
    check(resp.status_code == 200, f"checkin: {resp.text}")
    rules = resp.json()["data"]["rules"]
    check(
        [r["case_id"] for r in rules] == ["tests/x.py::test_b", "tests/x.py::test_a"],
        f"sort_order respected: {rules}",
    )
    checkout("CAL-PARAM", sn, [r["case_id"] for r in rules])
    resp = checkin("CAL-IFACE", sn, model=model)
    rules = resp.json()["data"]["rules"]
    check(
        [r["case_id"] for r in rules] ==
        ["tests/x.py::test_c", "tests/x.py::test_d", "tests/x.py::test_e"],
        f"auto append: {rules}",
    )
    resp = admin("GET", "/api/admin/routing/validate", params={"process_id": proc})
    codes = [i["code"] for i in resp.json()["issues"]]
    check("item_order_duplicate" in codes, f"dup order warning: {resp.text}")

    row_b = next(r for r in admin("GET", "/api/admin/routing/items",
                                 params={"process_id": proc, "station_id": "CAL-PARAM"}).json()
                 if r["case_id"] == "tests/x.py::test_b")
    resp = admin("PUT", f"/api/admin/routing/items/{row_b['item_id']}", {"sort_order": None})
    check(resp.status_code == 200 and resp.json()["sort_order"] is None, f"reset null: {resp.text}")
    sn2 = new_sn("ORDER2")
    resp = checkin("CAL-PARAM", sn2, model=model)
    rules = resp.json()["data"]["rules"]
    check(
        [r["case_id"] for r in rules] == ["tests/x.py::test_a", "tests/x.py::test_b"],
        f"null resets to tail: {rules}",
    )

# ================= J. 存量机型受控直通（passthrough 渐进上线） =================
def test_passthrough_mode():
    """直通机型进站 ACK 零写入 + 模式切换卡控。

    覆盖：直通机型进站 200 passthrough=true 且零痕迹（不建档，豁免拓扑/绑定/用例）/
    身份先于直通（登记试验 SN 在直通机型上仍 403）/ 切换 409 两向（关直通无拓扑
    model_topology_missing、开直通有在制 model_has_wip）/ 清线确认（无 confirm_drain
    → 409 drain_confirm_required，带确认位切换成功）。
    """
    sn = new_sn("PASS")
    p_pass = f"PROC-TEST-PASS-{STAMP}"
    m_pass = "MSO-PASS-01"

    # ① 直通机型（建流程 + 机型，无需拓扑）：进站 ACK 零写入
    resp = admin("POST", "/api/admin/processes", {"process_id": p_pass, "process_name": "直通验证"})
    check(resp.status_code == 201, f"create process: {resp.text}")
    resp = admin(
        "POST",
        "/api/admin/product-models",
        {"product_model": m_pass, "process_id": p_pass, "target_fw_version": FW, "passthrough": True},
    )
    check(resp.status_code == 201 and resp.json()["passthrough"] is True, f"create passthrough model: {resp.text}")

    # 直通豁免拓扑/机台绑定/用例清单：直接 ACK，station_id 为空
    resp = checkin("CAL-PARAM", sn, model=m_pass)
    body = resp.json()["data"]
    check(
        resp.status_code == 200 and body["passthrough"] is True and body["station_id"] == "",
        f"passthrough check-in: {resp.status_code} {resp.text}",
    )
    # 零痕迹：不建档（GET products/{sn} → 404）
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(resp.status_code == 404, f"passthrough must leave no trace: {resp.status_code} {resp.text}")

    # ② 身份先于直通：登记试验 SN + 直通机型 → 仍 403（直通不豁免身份）
    sn_reg = new_sn("PASS-REG")
    resp = admin("POST", "/api/admin/sn-registry", {"sn": sn_reg})
    check(resp.status_code == 201, f"register: {resp.text}")
    resp = checkin("CAL-PARAM", sn_reg, model=m_pass)
    check(
        resp.status_code == 403 and resp.json()["code"] == "sn_is_test_device",
        f"identity precedes passthrough: {resp.status_code} {resp.text}",
    )
    resp = admin("DELETE", f"/api/admin/sn-registry/{sn_reg}")
    check(resp.status_code == 204, f"unregister: {resp.status_code} {resp.text}")

    # ③ 关直通：无拓扑 → 409 model_topology_missing
    resp = admin("PUT", f"/api/admin/product-models/{m_pass}", {"passthrough": False, "confirm_drain": True})
    check(
        resp.status_code == 409 and resp.json()["code"] == "model_topology_missing",
        f"topology required: {resp.status_code} {resp.text}",
    )
    # 补拓扑后：无清线确认位 → 409 drain_confirm_required
    resp = admin(
        "PUT",
        "/api/admin/routing/stations",
        [{"station_id": "CAL-PARAM", "step_order": 1, "depends_on": []}],
        params={"process_id": p_pass},
    )
    check(resp.status_code == 200, f"add step: {resp.text}")
    resp = admin("PUT", f"/api/admin/product-models/{m_pass}", {"passthrough": False})
    check(
        resp.status_code == 409 and resp.json()["code"] == "drain_confirm_required",
        f"drain confirm required: {resp.status_code} {resp.text}",
    )
    # 带确认位 → 切换成功（严格卡控）
    resp = admin("PUT", f"/api/admin/product-models/{m_pass}", {"passthrough": False, "confirm_drain": True})
    check(resp.status_code == 200 and resp.json()["passthrough"] is False, f"switch to strict: {resp.text}")

    # ④ 开直通：有在制 → 409 model_has_wip
    wip_sn = new_sn("PASS-WIP")
    with SessionLocal() as db:
        db.add(
            models.ProductStatus(
                sn=wip_sn, product_model=m_pass, current_fw_version=FW,
                current_status="IDLE", is_completed=False,
            )
        )
        db.commit()
    resp = admin("PUT", f"/api/admin/product-models/{m_pass}", {"passthrough": True})
    check(
        resp.status_code == 409 and resp.json()["code"] == "model_has_wip",
        f"wip blocks passthrough: {resp.status_code} {resp.text}",
    )
    # 清掉在制后开直通成功
    with SessionLocal() as db:
        db.delete(db.get(models.ProductStatus, wip_sn))
        db.commit()
    resp = admin("PUT", f"/api/admin/product-models/{m_pass}", {"passthrough": True})
    check(resp.status_code == 200 and resp.json()["passthrough"] is True, f"switch to passthrough: {resp.text}")

TESTS = [
    test_sn_registry_status_api,
    test_health,
    test_masters_seed,
    test_topology_and_items,
    test_validate_ok_and_cycle,
    test_delete_guards,
    test_station_item_guards,
    test_client_unbound_state,
    test_client_multi_station_binding,
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
    test_app_version_baseline,
    test_process_status,
    test_client_release_lock,
    test_id_format_enforced,
    test_force_release_and_sessions_api,
    test_import_blocks_running_session,
    test_process_export_import,
    test_model_app_fields_roundtrip,
    test_role_permissions,
    test_role_manager,
    test_artifacts,
    test_report_batch_requires_completed,
    test_optional_station_sampling,
    test_sn_registry_polarity,
    test_passthrough_mode,
    test_repair_batch,
    test_repair_undo,
    test_repair_list_filters,
    test_dependency_retest_cascade,
    test_item_sort_order,
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

