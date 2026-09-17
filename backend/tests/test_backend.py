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
# MSO 6 站 / DPO 4 站（物理剔除 AWG）
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

# MSO 流程生效的用例ID总数（CAL-PARAM 2 + CAL-IFACE 2 + CAL-AWG 1 + TST-PARAM 2 + TST-IFACE 2 + TST-AWG 2）
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

    # 人为制造成环
    bad = f"PROC-TEST-BAD-{STAMP}"
    admin("POST", "/api/admin/processes", {"process_id": bad, "process_name": "cycle demo"})
    admin("POST", "/api/admin/stations", {"station_id": "TST-CYCLE1", "station_name": "TST-CYCLE1"})
    admin("POST", "/api/admin/stations", {"station_id": "TST-CYCLE2", "station_name": "TST-CYCLE2"})
    cycle = [
        {"station_id": "TST-CYCLE1", "step_order": 10, "depends_on": ["TST-CYCLE2"]},
        {"station_id": "TST-CYCLE2", "step_order": 20, "depends_on": ["TST-CYCLE1"]},
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

    # 存在在制品时拒绝删除
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

    # 工位是跨流程共享字典，被测试项引用时拒绝删除
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
    cid = new_client_id("CAL")
    resp = v1("resolve", {"client_id": cid, "ip_address": "10.1.60.99"})
    check(resp.status_code == 200, f"auto register: {resp.text}")
    check(resp.json()["data"]["bound"] is False, f"auto registered must be unbound: {resp.text}")

    # 未绑定机台也要能改 IP：不传 station_id 即保持解绑
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
    #    → 403 missing_prereq（工位解析与工艺闸门分层工作，解析成功≠放行）
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

    # 首工位动态建档
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

    # 补齐剩余三站
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

    # 完整清单放行
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

    # 网络重传：同一 checkout_id
    second = checkout("CAL-IFACE", sn, case_ids, checkout_id=cid)
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
    resp = checkin("CAL-PARAM", sn)
    case_ids = rules_of(resp)
    session_id = resp.json()["data"]["session_id"]
    # 漏掉一个必测项
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

    # SKIP 视为漏测
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"re-enter after fail: {resp.text}")
    resp = checkout("CAL-PARAM", sn, case_ids, skip=(case_ids[-1],))
    check(resp.status_code == 400 and resp.json()["code"] == "missing_mandatory", f"skip: {resp.text}")

    # 已执行但判定 FAIL → 真实失败，201 + exit_code 1，不拦截
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

    # RETEST 处置后解锁
    resp = admin("POST", "/api/admin/repairs", {"sn": sn, "repair_action": "RESET", "reason": "工程师确认后重投"})
    check(resp.status_code == 201, f"reset: {resp.text}")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"unlocked: {resp.text}")


def test_lock_conflict_409_and_timeout_takeover():
    sn = new_sn("CONFLICT")
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"check-in: {resp.text}")

    # 另一台同工位机台抢占 → 409
    other = new_client_id("CAL")
    admin("POST", "/api/admin/clients", {"client_id": other, "bound_stations": ["CAL-PARAM"]})
    resp = v1(
        "check-in",
        {"client_id": other, "sn": sn, "product_model": M_DPO, "firmware": FW},
    )
    check(resp.status_code == 409, f"conflict must be 409, got {resp.status_code} {resp.text}")
    check(resp.json()["code"] == "lock_conflict", f"code: {resp.json()}")

    # 心跳续期
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

    # RETEST：收回 TST-PARAM 印章，作废旧记录
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

    # 重测通过
    pass_station("TST-PARAM", sn)

    # ROLLBACK：回退到 CAL-IFACE（清除其及之后所有印章）
    resp = admin(
        "POST",
        "/api/admin/repairs",
        {"sn": sn, "repair_action": "ROLLBACK", "target_station": "CAL-IFACE", "reason": "批量异常"},
    )
    check(resp.status_code == 201, f"rollback: {resp.text}")
    resp = admin("GET", f"/api/admin/products/{sn}")
    check(sorted(resp.json()["passed_stations"]) == ["CAL-PARAM"], f"rollback result: {resp.json()}")

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
    pass_station("CAL-PARAM", sn)
    # 目标工位未通过
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
    # 工步按 step_order 排序
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

    # 未失联 → 仍然 409
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

    # 旧持锁方持旧 token 出站 → 403，绝不污染数据
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

    # 崩溃前只上报了 1 个用例
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

    # 崩溃重启：同机台重进站自动续测
    resp = checkin("CAL-PARAM", sn)
    check(resp.status_code == 200, f"re-checkin: {resp.text}")
    data = resp.json()["data"]
    check(data["session_id"] == session_id, "必须复用同一会话")
    check(data["attempt"] == 2, f"attempt: {data}")
    check(data["resume"]["resumed"] is True, f"resume: {data}")
    check(data["resume"]["completed_case_ids"] == ["test_amp_cal"], f"resume: {data}")
    check(data["resume"]["cursor"] == {"step": 1}, f"cursor: {data}")

    # 只跑剩余用例即可出站，未提交的由 checkpoint 补齐
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

    # 解锁后新机台可直接进站
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

    # 进站同样刷新版本（此前 CheckInIn 收了 app_version 却没落库）
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

    # 手工注册也允许登记版本
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
    pass_station("CAL-PARAM", sn)  # 先正常通过首站，留下印章作为"不应被改动"的基线

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

    # 同工位另一台机台应能立即进站，证明锁确实释放了
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
        ("/api/admin/processes", {"process_id": "PROC_TEK_MSO"}),  # 用了下划线
        ("/api/admin/processes", {"process_id": "PROC-SCOPE-MSO"}),  # 缺构型段
        ("/api/admin/stations", {"station_id": "CAL_PARAM"}),  # 用了下划线
        ("/api/admin/stations", {"station_id": "CAL"}),  # 缺测试域
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

    # 更新接口不校验：三者投产即冻结，换编号走「新建 + 停用」而不是原地改名
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

    # 新增必测项 → 409 running_session_block
    resp = admin("POST", "/api/admin/routing/items/import", add_new)
    check(
        resp.status_code == 409 and resp.json()["code"] == "running_session_block",
        f"add mandatory with RUNNING session must 409: {resp.status_code} {resp.text}",
    )
    check(resp.json()["data"]["running_sessions"] >= 1, f"data: {resp.text}")

    # dry-run：不落库，转为 would_block 告警
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

    # 只收缩清单（停用其一）→ 放行（收缩只会放宽出站校验，不会误伤在跑会话）
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

    # force=true 强行新增 → 放行
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

    imp = f"PROC-TEST-IMP-{STAMP}"
    doc["process"]["process_id"] = imp
    doc["process"]["process_name"] = "导入副本"
    resp = admin("POST", "/api/admin/routing/processes/import", doc)
    check(resp.status_code == 201, f"import: {resp.status_code} {resp.text}")
    body = resp.json()
    check(body["steps_created"] == 4 and body["items_created"] == len(doc["items"]), f"counts: {body}")
    check(body["stations_created"] == 0, f"工位已存在不重复建: {body}")
    check(body["models_skipped"] == 1, f"机型冲突跳过: {body}")

    res = admin("GET", "/api/admin/routing/validate", params={"process_id": imp})
    check(res.json()["ok"] is True, "副本拓扑校验通过")

    resp = admin("POST", "/api/admin/routing/processes/import", doc)
    check(resp.status_code == 409 and resp.json()["code"] == "process_already_exists", f"dup: {resp.text}")

    bad = json.loads(json.dumps(doc))
    bad["process"]["process_id"] = f"PROC-TEST-IMP2-{STAMP}"
    bad["steps"][0]["depends_on"] = ["CAL-AWG"]
    resp = admin("POST", "/api/admin/routing/processes/import", bad)
    check(resp.status_code == 400 and resp.json()["code"] == "topology_invalid", f"bad deps: {resp.text}")

    # 全量导出：包含全部流程，每项可单独导入
    resp = admin("GET", "/api/admin/routing/processes/export-all")
    check(resp.status_code == 200, f"export all: {resp.status_code} {resp.text}")
    all_ids = [p["process"]["process_id"] for p in resp.json()["processes"]]
    check(P_DPO in all_ids and P_MSO in all_ids, f"export all ids: {all_ids}")

    # 清理：清空工步（连带清用例）→ 删流程（机型全部 skipped，副本上无绑定）
    admin("PUT", "/api/admin/routing/stations", [], params={"process_id": imp})
    resp = admin("DELETE", f"/api/admin/processes/{imp}")
    check(resp.status_code in (200, 204), f"cleanup: {resp.status_code} {resp.text}")


TESTS = [
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
    test_process_status,
    test_client_release_lock,
    test_id_format_enforced,
    test_force_release_and_sessions_api,
    test_import_blocks_running_session,
    test_process_export_import,
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
