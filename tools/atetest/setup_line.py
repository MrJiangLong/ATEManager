"""幂等搭建场景仿真沙箱：独立流程 + 拓扑 + 用例ID + 虚拟机台。

沙箱流程 PROC-SELFTEST-DPO-BASE 与真实工艺配置（PROC-SCOPE-DPO-BASE）完全隔离——
工位是跨流程共享字典（复用 CAL-PARAM 等），但 station_items 按 (process, station)
隔离，机台按"件所属流程 ∩ 绑定"解析，两边互不干扰。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import api

BASE = os.environ.get("ATE_BASE_URL", "http://127.0.0.1:8000")
PROCESS_ID = "PROC-SELFTEST-DPO-BASE"
SANDBOX_MODEL = "SELFTEST-MODEL"


def login():
    req = urllib.request.Request(f"{BASE}/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]


def main():
    tok = login()
    h = api.HttpClient(BASE, api.ATE_KEY, token=tok)

    def attempt(label, fn):
        try:
            fn()
            print(f"  {label} OK")
        except Exception as exc:
            print(f"  {label} 跳过（{exc}）")

    for sid, name in (("TST-PARAM", "测试-指标测试站位"), ("TST-IFACE", "测试-接口测试站位")):
        attempt(f"工位 {sid}", lambda s=sid, n=name: h.request(
            "POST", "/api/admin/stations", body={"station_id": s, "station_name": n, "timeout_sec": 900}))

    def proc():
        h.request("POST", "/api/admin/processes",
                  body={"process_id": PROCESS_ID, "process_name": "场景仿真沙箱流程"})
    attempt(f"流程 {PROCESS_ID}", proc)

    def model():
        h.request("POST", "/api/admin/product-models",
                  body={"product_model": SANDBOX_MODEL, "process_id": PROCESS_ID,
                        "target_fw_version": "V3.20"})
    attempt(f"机型 {SANDBOX_MODEL}", model)

    def topo():
        h.request("PUT", "/api/admin/routing/stations", body=[
            {"station_id": "CAL-PARAM", "step_order": 10, "depends_on": []},
            {"station_id": "CAL-IFACE", "step_order": 20, "depends_on": ["CAL-PARAM"]},
            {"station_id": "TST-PARAM", "step_order": 30, "depends_on": ["CAL-PARAM", "CAL-IFACE"]},
            {"station_id": "TST-IFACE", "step_order": 40, "depends_on": ["TST-PARAM"]},
        ], params={"process_id": PROCESS_ID})
    attempt("拓扑 4 工步（沙箱流程）", topo)

    items = [
        ("CAL-PARAM", "tests/test_cal_param.py::TestAmp::test_amp_cal", "CHn幅度校准", True),
        ("CAL-PARAM", "tests/test_cal_param.py::TestPhase::test_phase_cal", "CHn_相位校准", True),
        ("CAL-PARAM", "tests/test_cal_param.py::TestAmpDc::test_amp_dc_1m", "CHn_幅度DC_1MΩ测试", True),
        ("CAL-PARAM", "tests/test_cal_param.py::TestBandwidth::test_bw_hi_z", "CHn_带宽测试_高阻", True),
        ("CAL-PARAM", "tests/test_cal_param.py::TestFastEdge::test_fast_edge_1m", "CHn_快沿_1MΩ", True),
        ("CAL-IFACE", "tests/test_cal_iface.py::TestNoise::test_noise", "基线噪声测试", True),
        ("CAL-IFACE", "tests/test_cal_iface.py::TestTouch::test_touch", "触屏测试", True),
        ("CAL-IFACE", "tests/test_cal_iface.py::TestAux::test_aux", "AUX", True),
        ("TST-PARAM", "tests/test_tst_param.py::TestTimebase::test_timebase", "时基精度测试", True),
        ("TST-PARAM", "tests/test_tst_param.py::TestAmp::test_amp_ac", "CHn_幅度AC测试", True),
        ("TST-PARAM", "tests/test_tst_param.py::TestAmpDc::test_amp_dc_1m", "CHn_幅度DC_1MΩ测试", True),
        ("TST-PARAM", "tests/test_tst_param.py::TestBandwidth::test_bw_full", "CHn_带宽测试_全通道", True),
        ("TST-PARAM", "tests/test_tst_param.py::TestBandwidth::test_bw_single", "CHn_带宽测试_单通道", True),
        ("TST-IFACE", "tests/test_tst_iface.py::TestNoise::test_noise", "基线噪声测试", True),
        ("TST-IFACE", "tests/test_tst_iface.py::TestLineTrig::test_line_trig", "市电触发测试", True),
        ("TST-IFACE", "tests/test_tst_iface.py::TestRuntTrig::test_runt_trig", "欠幅脉冲触发测试", True),
        # 选做用例（非必测）：验证"选做 FAIL 不判停、只记录"
        ("CAL-PARAM", "tests/test_cal_param.py::TestOptional::test_optional", "选做示例用例", False),
    ]
    for sid, case_id, name, mandatory in items:
        attempt(f"用例 {case_id.rsplit('::', 1)[-1]} → {sid}", lambda s=sid, c=case_id, n=name, mm=mandatory: h.request(
            "POST", "/api/admin/routing/items",
            body={"process_id": PROCESS_ID, "station_id": s,
                  "case_id": c, "item_name": n, "is_mandatory": mm}))

    for cid, bound in (("JC-CAL1", ["CAL-PARAM"]), ("JC-IFACE", ["CAL-IFACE"]),
                       ("JC-TST1", ["TST-PARAM"]), ("JC-TIFACE", ["TST-IFACE"]),
                       ("JC-UNBOUND", [])):
        def reg(c=cid, b=bound):
            try:
                h.request("POST", "/api/admin/clients",
                          body={"client_id": c, "bound_stations": b, "ip_address": "127.0.0.1"})
            except Exception as exc:
                if "already" not in str(exc):
                    raise
                h.request("PUT", f"/api/admin/clients/{c}", body={"bound_stations": b})
        attempt(f"机台 {cid} 绑定 {bound or '(未绑定)'}", reg)

    print("产线数据就绪")


if __name__ == "__main__":
    main()
