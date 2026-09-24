"""正式产线全场景仿真 runner v3：对真实服务端按多机台流转执行全部场景。

v3 新增（第一档补齐）：S8 固件防呆 / S9 未绑定机台 / S10 选做 FAIL 不判停 /
S11 进程崩溃续测（Popen 中途 kill，凭断点文件 resume）。
每个场景 = 一次 pytest 子进程（不同机台/身份/注入），断言退出码是否符合预期。
用法：python run_scenarios.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

# Windows GBK 控制台打印中文/特殊字符会崩，统一 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("ATE_BASE_URL", "http://127.0.0.1:8000")
# SN 前缀带时间戳：runner 可重复执行，不会撞上一次的防复测/已建档状态
SN_PREFIX = "R" + time.strftime("%m%d%H%M%S")
_results = []

# 每台机台只收集本站用例（与 Web 端 station_items 清单一致）
STATION_TESTS = {
    "JC-CAL1": ["tests/test_cal_param.py"],
    "JC-IFACE": ["tests/test_cal_iface.py"],
    "JC-TST1": ["tests/test_tst_param.py"],
    "JC-TIFACE": ["tests/test_tst_iface.py"],
}


def _admin_http():
    import api
    req = urllib.request.Request(f"{BASE}/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    tok = json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]
    return api.HttpClient(BASE, api.ATE_KEY, token=tok)


def trace_summary(sn):
    """查服务端追溯：盖章工位 / 失败计数 / 各记录 attempt。"""
    h = _admin_http()
    try:
        t = h.request("GET", f"/api/admin/records/trace/{sn}")
    except Exception as exc:
        return f"trace 查询失败: {exc}"
    p = t.get("product") or {}
    recs = [(r.get("station_id"), r.get("overall_result"),
             (r.get("executed_items") or {}).get("attempt")) for r in t.get("records") or []]
    return f"盖章={p.get('passed_stations')} fail_count={p.get('fail_count')} 记录={recs}"


def run_case(label, sn, client, expect, *, model="SELFTEST-MODEL", fw="V3.20",
             fail="", skip="", args=None):
    """跑一个场景（pytest 子进程），断言退出码。sn 自动加时间戳前缀。"""
    sn = SN_PREFIX + sn
    env = os.environ.copy()
    env.update({
        "ATE_TEST_SN": sn, "ATE_TEST_CLIENT": client,
        "ATE_TEST_MODEL": model, "ATE_TEST_FW": fw,
        "ATE_FAIL": fail, "ATE_SKIP": skip,
    })
    tests = args if args is not None else STATION_TESTS.get(client, ["tests/test_cal_param.py"])
    cmd = [sys.executable, "-m", "pytest", "-p", "no:typeguard", "-q"] + tests
    p = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok = p.returncode == expect
    _results.append((ok, label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: exit={p.returncode} (期望 {expect})", flush=True)
    if not ok:
        for line in (p.stdout or "").strip().splitlines()[-8:]:
            print("      " + line, flush=True)
    return p


# ── S1 正常全流程：CS211 四站顺序盖章（每站只跑本站用例）──
run_case("S1.1 CAL-PARAM（5 用例）", "11", "JC-CAL1", 0)
run_case("S1.2 CAL-IFACE（3 用例，前置已过）", "11", "JC-IFACE", 0)
run_case("S1.3 TST-PARAM（5 用例）", "11", "JC-TST1", 0)
run_case("S1.4 TST-IFACE（3 用例）", "11", "JC-TIFACE", 0)

# ── S2 防复测：已盖章工位重复进站 ──
run_case("S2 防复测（CAL-PARAM 已盖章）", "11", "JC-CAL1", 1)

# ── S3 跳工位：未过 CAL-PARAM 直接进 CAL-IFACE ──
run_case("S3 跳工位（缺前置）", "12", "JC-IFACE", 1)

# ── S4 漏测-进站 collection 残缺：只收集 1/3 个用例 ──
run_case("S4 漏测（collection 缺用例）", "14", "JC-IFACE", 1,
         args=["tests/test_cal_iface.py::TestNoise::test_noise"])

# ── S5 漏测-SKIP：必测用例被跳过 → 出站漏测拦截 ──
run_case("S5.1 CS213 先过 CAL-PARAM", "13", "JC-CAL1", 0)
run_case("S5.2 SKIP 必测（test_noise）", "13", "JC-IFACE", 1, skip="test_noise")

# ── S6 机型未注册 ──
run_case("S6 机型未注册", "15", "JC-CAL1", 1, model="MSO4054B")

# ── S7 失败重跑闭环：TST-PARAM 失败 → 不出站 → 修复重跑 → 走完 ──
run_case("S7.1 CS216 CAL-PARAM", "16", "JC-CAL1", 0)
run_case("S7.2 CS216 CAL-IFACE", "16", "JC-IFACE", 0)
run_case("S7.3 TST-PARAM 必测失败（不出站，写重跑清单）", "16", "JC-TST1", 2, fail="test_timebase")
run_case("S7.4 TST-PARAM 修复重跑（仅重跑失败项）", "16", "JC-TST1", 0)
run_case("S7.5 CS216 TST-IFACE", "16", "JC-TIFACE", 0)

# ── S8 固件防呆：固件不满足基线（V3.20 exact）──
run_case("S8 固件防呆（V0.9 < V3.20）", "18", "JC-CAL1", 1, fw="V0.9")

# ── S9 未绑定机台：resolve 自动注册后进站被拦 ──
run_case("S9 未绑定机台进站（JC-NEVER）", "19", "JC-NEVER", 1)

# ── S10 选做用例 FAIL 不判停：必测全过 + 选做 FAIL → 出站放行 ──
# 注意：pytest 有失败用例时进程退出码恒为 1，出站是否被放行要看服务端记录
run_case("S10 选做 FAIL（pytest 退出码 1）", "20", "JC-CAL1", 1, fail="test_optional")
_h = _admin_http()
try:
    _t = _h.request("GET", f"/api/admin/records/trace/{SN_PREFIX}20")
    _rec = (_t.get("records") or [{}])[0]
    _opt = [i for i in (_rec.get("executed_items") or {}).get("items") or []
            if "test_optional" in i.get("case_id", "")]
    _ok = _rec.get("overall_result") == "PASS" and _opt and _opt[0].get("result") == "FAIL"
    _detail = f"overall={_rec.get('overall_result')} optional={_opt and _opt[0].get('result')}"
except Exception as exc:
    _ok, _detail = False, f"产品未建档（{exc}）"
_results.append((_ok, "S10 选做 FAIL 出站放行且明细留档"))
print(f"[{'PASS' if _ok else 'FAIL'}] S10 出站放行且选做 FAIL 留档: {_detail}", flush=True)

# ── S11 进程崩溃续测：跑到一半 kill，凭断点文件 resume（attempt+1）──
print("\n=== S11 进程崩溃续测 ===", flush=True)
env = os.environ.copy()
env.update({"ATE_TEST_SN": SN_PREFIX + "21", "ATE_TEST_CLIENT": "JC-CAL1",
            "ATE_TEST_MODEL": "SELFTEST-MODEL", "ATE_SLOW": "2"})
cmd = [sys.executable, "-m", "pytest", "-p", "no:typeguard", "-q"] + STATION_TESTS["JC-CAL1"]
p = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(4.5)          # check-in + 1~2 个用例（每用例 2s）后强杀
p.kill()
p.wait()
print(f"  进程已强杀（exit={p.returncode}），会话应处于 RUNNING + 断点已存", flush=True)
time.sleep(1)
run_case("S11.2 崩溃后续测出站", "21", "JC-CAL1", 0)
_t = _admin_http().request("GET", f"/api/admin/records/trace/{SN_PREFIX}21")
_attempts = [(r.get("executed_items") or {}).get("attempt") for r in _t.get("records") or []]
_ok = any(a and a >= 2 for a in _attempts)
_results.append((_ok, "S11 续测 attempt≥2"))
print(f"[{'PASS' if _ok else 'FAIL'}] S11 续测 attempt≥2: 记录 attempt={_attempts}", flush=True)

# ── 服务端对账：关键台件的状态 ──
print("\n=== 服务端对账 ===", flush=True)
for suf in ("11", "13", "16", "20", "21"):
    sn = SN_PREFIX + suf
    print(f"  {sn}: {trace_summary(sn)}", flush=True)

# ── 汇总 ──
fails = [m for ok, m in _results if not ok]
print(f"\n====== 场景汇总：{len(_results) - len(fails)}/{len(_results)} 通过 ======", flush=True)
for m in fails:
    print(f"  FAIL: {m}", flush=True)
sys.exit(1 if fails else 0)
