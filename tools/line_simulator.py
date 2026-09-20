r"""产线模拟器：多线程模拟 N 台机台对 Web 服务做端到端验证。

与 ate_client.py 的区别
    ate_client.py  单线程、假用例 —— 用来看懂/验证服务端契约
    line_simulator 多线程、真实并发 —— 用来验证"多机台同时跑"时服务端的
                    锁竞争、防呆拦截、崩溃续测、失联接管、孤儿锁回收是否正确

v2：补齐产线状态
    旧版只覆盖"单工位全 PASS 出库 + 随机崩溃接管"，产线上的进度分布与
    异常态全部缺失。本版补齐（工艺拓扑与必测项均从服务端动态拉取，
    不在本机硬编码）：

    · 流程推进：按真实拓扑逐站流转，SN 出站后自动投递到下一工位
      → 自然产生"只跑了一个 / 完成一半 / 全流程完工"
    · 停线推进：--stop-rate 控制过站后停止推进（下班 / 停线 / 待处理）
    · 异常态注入（互斥轮盘赌，一件至多命中一种）：
        --fail-rate     不良件 → 判 FAIL；顽固不良件连续失败 → 工程锁定 LOCKED
        --missing-rate  出站漏报一个必测项 → 400 missing_mandatory
        --bad-fw-rate   进站固件非基线 → 403 firmware_mismatch
        --abandon-rate  进站后主动放弃（release）→ 停在半途，不计失败
    · 跳站：--jump-rate 控制推进时跳过下一工位 → 403 missing_prereq
    · 判据改造：注入的防呆拦截属"预期命中"，只有非预期异常才计 failed

用法
    # 1) 准备数据（务必用本地测试库，勿对正式库执行）
    scripts\sim-local.bat

    # 2) 另开终端跑模拟器
    python tools\line_simulator.py --api-key <V1_API_KEY>

    # 常用参数
    --clients 2            每工位的机台数（自动注册并绑定该工位）
    --units 12             本轮投产的被测件数量
    --model DPO4054B       机型（自动取其流程与基线固件）
    --station CAL-PARAM    单工位模式（不做流程推进）
    --mode chaos           随机注入崩溃（normal 仅关闭崩溃注入，
                           其余 --fail-rate 等注入速率仍然生效）
    --crash-rate 0.12      崩溃概率
    --fail-rate 0.08       不良件比例（→ LOCKED / 重测通过）
    --missing-rate 0.05    漏报必测项比例（→ missing_mandatory）
    --bad-fw-rate 0.05     非基线固件比例（→ firmware_mismatch）
    --jump-rate 0.05       跳站比例（→ missing_prereq）
    --abandon-rate 0.05    中途停机比例（→ 停在半途）
    --stop-rate 0.25       过站后停止推进比例（→ 完成一半）

输出
    逐台机台日志 + 结果分布 + 完成度分布 + 服务端侧校验
"""

from __future__ import annotations

import argparse
import collections
import json
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from ate_client import (AteClient,ApiError,admin_login)

DEFAULT_MODEL = "DPO4054B"
NON_BASELINE_FW = "V3.10"

# 注入型防呆拦截：属"预期命中"，不计入失败
EXPECTED_GATE_CODES = (
    "firmware_mismatch",
    "missing_prereq",
    "missing_mandatory",
    "case_id_mismatch",
    "model_mismatch",
)

# ---------------------------------------------------------------------
# 管理端（注册机台 / 强制解锁 / 拉取工艺 / 校验结果）
# ---------------------------------------------------------------------
class Admin:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None, params=None):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def ensure_client(self, client_id: str, station_id: str, ip: str) -> None:
        try:
            self._req("POST", "/api/admin/clients",
                      {"client_id": client_id, "bound_stations": [station_id], "ip_address": ip})
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise

    def force_release(self, sn: str, reason: str) -> None:
        self._req("POST", f"/api/admin/products/{sn}/force-release", {"reason": reason})

    def models(self) -> list:
        return self._req("GET", "/api/admin/product-models")

    def topology(self, process_id: str) -> dict:
        return self._req("GET", "/api/admin/routing/topology", params={"process_id": process_id})

    def overview(self) -> dict:
        return self._req("GET", "/api/admin/metrics/overview", params={"days": 14})

    def abnormal_sessions(self) -> int:
        return self._req("GET", "/api/admin/sessions", params={"abnormal_only": True, "page_size": 1})["total"]

    def zombie_locks(self) -> int:
        return self._req("GET", "/api/admin/sessions/zombie-locks", params={"page_size": 1})["total"]

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
class ProcessPlan:
    """流程拓扑 + 各工位必测用例：来自 GET /api/admin/routing/topology。"""

    def __init__(self, topo: dict):
        self.process_id = topo["process_id"]
        self.steps = sorted(topo.get("steps") or [], key=lambda s: s.get("step_order") or 0)
        self.stations: List[str] = [s["station_id"] for s in self.steps]
        self.items: Dict[str, List[str]] = {}
        for it in topo.get("items") or []:
            if not it.get("is_active", True):
                continue
            self.items.setdefault(it["station_id"], []).append(it["case_id"])

    def cases(self, station_id: str, limit: int = 0) -> List[str]:
        ids = self.items.get(station_id) or []
        return ids[:limit] if limit and limit < len(ids) else ids

    def next_station(self, station_id: str, jump: bool = False) -> Optional[str]:
        """下一工位；jump=True 时跳过一站（用于触发 missing_prereq）。"""
        if station_id not in self.stations:
            return None
        idx = self.stations.index(station_id) + (2 if jump else 1)
        return self.stations[idx] if idx < len(self.stations) else None

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
@dataclass
class Job:
    sn: str
    station_id: str
    defective: bool = False
    stubborn: bool = False
    bad_firmware: bool = False
    missing_item: bool = False
    abandon: bool = False
    attempts: int = 0
    retried: bool = False

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
class Sim:
    """队列 / 计数 / 进度 / 同步。pending=0 即全产线空闲，线程退出。"""

    def __init__(self, plan: ProcessPlan, rates: dict, single_station: bool, items_limit: int):
        self.plan = plan
        self.rates = rates
        self.single_station = single_station
        self.items_limit = items_limit
        self.queues: Dict[str, "queue.Queue[Job]"] = {sid: queue.Queue() for sid in plan.stations}
        self.counters: "collections.Counter" = collections.Counter()
        self.progress: Dict[str, set] = {}
        self.pending = 0
        self._lock = threading.Lock()

    # ---- 计数 ----
    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] += n

    def mark_passed(self, sn: str, station_id: str) -> None:
        with self._lock:
            self.progress.setdefault(sn, set()).add(station_id)

    # ---- 队列 ----
    def submit(self, job: Job, station_id: Optional[str] = None) -> None:
        """新任务进入产线：pending +1。"""
        with self._lock:
            self.pending += 1
        self.queues[station_id or job.station_id].put(job)

    def requeue(self, job: Job, station_id: str) -> None:
        """重投（崩溃接管 / 失败重测）：任务未终结，pending 不变。"""
        self.queues[station_id].put(job)

    def finish(self) -> None:
        """任务终结：pending -1。"""
        with self._lock:
            self.pending -= 1

    def is_active(self) -> bool:
        with self._lock:
            return self.pending > 0

class SimulatedCrash(Exception):
    """模拟上位机崩溃：不发 release、不 check-out，锁留在服务端。"""

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
class Station:
    def __init__(self, *, client_id: str, station_id: str, base_url: str, api_key: str,
                 sim: Sim, admin: Admin, model: str, base_fw: str, crash_rate: float,
                 resume: bool, state_dir: Path, rng: random.Random, max_retry: int = 9,
                 handoff: bool = False):
        self.client_id = client_id
        self.station_id = station_id
        self.sim = sim
        self.admin = admin
        self.model = model
        self.base_fw = base_fw
        self.crash_rate = crash_rate
        self.resume = resume
        self.handoff = handoff  # 崩溃后直接交同工位其他机台（稳定触发接管）
        self.rng = rng
        self.max_retry = max_retry
        self.cli = AteClient(
            base_url, api_key, client_id=client_id,
            app_version="ate-simulator/2.0",
            state_file=state_dir / f".sim_{client_id}.json",
        )
        self.log_lines: List[str] = []

    def log(self, msg: str) -> None:
        line = f"[{self.client_id}@{self.station_id}] {msg}"
        self.log_lines.append(line)
        print(line, flush=True)

    # ---------------- 单个被测件在某个工位的完整流程 ----------------
    def process(self, job: Job) -> str:
        cases = self.sim.plan.cases(job.station_id, self.sim.items_limit)
        if not cases:
            self.log(f"{job.sn} 工位无必测用例，跳过")
            self.sim.bump("skipped")
            return "skip"

        fw = NON_BASELINE_FW if job.bad_firmware else self.base_fw
        state = self.cli.check_in(job.sn, self.model, fw, case_ids=cases)
        if state.attempt > 1:
            self.sim.bump("resumed")

        if job.abandon:
            self.cli.release("模拟器：中途停机/下班，主动放弃锁")
            self.log(f"{job.sn} 中途停机（锁已释放，件停在半途）")
            self.sim.bump("abandoned")
            return "abandoned"

        bad_case = cases[-1] if job.defective else None
        # 漏测注入必须"压根没跑这一项"：服务端 MERGE_CHECKPOINT_ON_CHECKOUT
        skip_case = cases[-1] if job.missing_item else None
        executed: List[str] = []
        for case_id in cases:
            if case_id in state.completed_case_ids:
                continue
            if case_id == skip_case:
                self.log(f"{job.sn} 漏测注入：跳过 {case_id.split('::')[-1]}")
                continue
            time.sleep(self.rng.uniform(0.05, 0.2))
            self.cli.checkpoint(
                [{"case_id": case_id,
                  "result": "FAIL" if case_id == bad_case else "PASS",
                  "values": {"sim": True}, "duration_ms": 150,
                  "message": "模拟器注入不良" if case_id == bad_case else None}],
                cursor={"step": len(executed) + 1},
            )
            executed.append(case_id)
            if self.rng.random() < self.crash_rate:
                raise SimulatedCrash(f"{case_id} 之后进程崩溃")

        out_items = [{"case_id": c, "result": "FAIL" if c == bad_case else "PASS",
                      "values": {"sim": True}, "duration_ms": 150,
                      "message": "模拟器注入不良" if c == bad_case else None} for c in executed]

        ack = self.cli.check_out(out_items, duration_ms=len(executed) * 150)
        overall = (ack.get("overall_result") or "").upper()
        self.log(f"{job.sn} 出库 record={ack.get('record_id')} result={overall or 'PASS'} "
                 f"attempt={state.attempt} 补齐={ack.get('checkpoint_merged_count')}")
        return "fail" if overall == "FAIL" else "pass"

    # ---------------- 终态之后的流转 ----------------
    def advance(self, job: Job) -> None:
        """PASS 出站后推进到下一工位；按概率停线或跳站。"""
        if self.sim.single_station:
            return
        if self.rng.random() < self.sim.rates["stop"]:
            self.sim.bump("stopped")
            self.log(f"{job.sn} 过站后停止推进（完成度停在 {job.station_id}）")
            return
        jump = self.rng.random() < self.sim.rates["jump"]
        nxt = self.sim.plan.next_station(job.station_id, jump=jump)
        if nxt is None:
            self.sim.bump("completed")
            self.log(f"{job.sn} 全流程完工")
            return
        job.station_id = nxt
        self.sim.submit(job, nxt)

    def retry_after_fail(self, job: Job) -> bool:
        """失败件重测：顽固不良件会连续 FAIL 直到服务端判 product_locked。

        返回 True 表示已重投、任务仍在产线上（调用方不得再 finish，
        否则 pending 会提前归零、任务被丢弃，永远累计不到 FAIL_LIMIT）。
        """
        job.attempts += 1
        if not job.stubborn:
            job.defective = False
        if job.attempts >= self.max_retry:
            self.sim.bump("unexpected")
            self.log(f"{job.sn} 重测次数超限，放弃")
            return False
        self.log(f"{job.sn} 判定 FAIL，重新进站重测（第 {job.attempts + 1} 次）")
        self.sim.requeue(job, job.station_id)
        return True

    # ---------------- 异常分类 ----------------
    def on_api_error(self, job: Job, exc: ApiError) -> None:
        code = exc.code or ""
        if code == "product_locked":
            self.sim.bump("locked")
            self.log(f"{job.sn} 工程锁定 LOCKED（连续失败达阈值，需维修处置）")
            self.sim.finish()
        elif code in EXPECTED_GATE_CODES:
            self.sim.bump("gated")
            self.sim.bump(f"gated_{code}")
            self.log(f"{job.sn} 防呆拦截（{code}）")
            self.sim.finish()
        elif exc.is_lock_conflict:
            try:
                self.admin.force_release(job.sn, "模拟器：原机台崩溃，换机台接管")
                self.sim.bump("takeover")
                self.log(f"{job.sn} 锁冲突 → 已强制解锁，准备接管")
                job.attempts += 1
                if job.attempts < self.max_retry:
                    self.sim.requeue(job, job.station_id)
                else:
                    self.sim.bump("unexpected")
                    self.sim.finish()
            except Exception as rel_err:
                self.sim.bump("unexpected")
                self.log(f"{job.sn} 接管失败：{rel_err}")
                self.sim.finish()
        elif exc.is_lock_invalid:
            job.attempts += 1
            if job.attempts < self.max_retry:
                self.sim.requeue(job, job.station_id)
            else:
                self.sim.bump("unexpected")
                self.sim.finish()
        else:
            self.sim.bump("unexpected")
            self.log(f"{job.sn} 未预期异常：{exc.status} {code}")
            self.sim.finish()

    def on_crash(self, job: Job, local: "queue.Queue[Job]", exc: SimulatedCrash) -> None:
        self.sim.bump("crashed")
        self.log(f"{job.sn} 崩溃（{exc}），锁已遗留在服务端")
        job.attempts += 1
        if not self.resume or job.attempts >= self.max_retry:
            self.sim.bump("gave_up")
            self.log(f"{job.sn} 崩溃后放弃（不续测 / 超重试上限）")
            self.sim.finish()
        elif self.handoff or job.retried:
            self.sim.requeue(job, job.station_id)  # 交同工位其他机台 → 触发接管
        else:
            job.retried = True
            local.put(job)

    # ---------------- 主循环 ----------------
    def handle(self, job: Job, local: "queue.Queue[Job]") -> None:
        try:
            outcome = self.process(job)
        except SimulatedCrash as exc:
            self.on_crash(job, local, exc)
            return
        except ApiError as exc:
            self.on_api_error(job, exc)
            return
        except Exception as exc:  # 兜底：单个件失败不能拖垮整台机台
            self.sim.bump("unexpected")
            self.log(f"{job.sn} 未预期异常：{exc}")
            self.sim.finish()
            return

        if outcome == "pass":
            self.sim.mark_passed(job.sn, job.station_id)
            self.sim.bump("pass_out")
            self.advance(job)
            self.sim.finish()
        elif outcome == "fail":
            self.sim.bump("fail_out")
            # 重投表示任务仍在产线，不能再 finish
            if not self.retry_after_fail(job):
                self.sim.finish()
        else:
            self.sim.finish()

    def run(self) -> None:
        local: "queue.Queue[Job]" = queue.Queue()
        q = self.sim.queues[self.station_id]
        while self.sim.is_active():
            try:
                job = local.get_nowait()
            except queue.Empty:
                try:
                    job = q.get_nowait()
                except queue.Empty:
                    time.sleep(0.1)
                    continue
            try:
                self.handle(job, local)
            finally:
                self.cli.stop_heartbeat()

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
def roll_injection(rng: random.Random, rates: dict) -> dict:
    roll = rng.random()
    acc = 0.0
    for key in ("bad_fw", "abandon", "missing", "fail"):
        acc += rates.get(key, 0.0)
        if roll < acc:
            return {
                "bad_fw": {"bad_firmware": True},
                "abandon": {"abandon": True},
                "missing": {"missing_item": True},
                "fail": {"defective": True, "stubborn": rng.random() < 0.5},
            }[key]
    return {}

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="ATE 产线模拟器（多机台并发 + 产线状态仿真）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="", help="X-API-Key（后端 V1_API_KEY）")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="admin123")
    parser.add_argument("--clients", type=int, default=2, help="每个工位的机台数")
    parser.add_argument("--units", type=int, default=12, help="投产被测件数量")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="机型（自动取其流程与基线固件）")
    parser.add_argument("--station", default="", help="单工位模式：只跑该工位，不做流程推进")
    parser.add_argument("--items", type=int, default=0, help="每工位用例数（0=全量必测项）")
    parser.add_argument("--mode", choices=["normal", "chaos"], default="chaos")
    parser.add_argument("--crash-rate", type=float, default=0.12, help="每个测试项之后的崩溃概率")
    parser.add_argument("--no-chaos-resume", action="store_true", help="崩溃后不续测，直接换件")
    parser.add_argument("--crash-handoff", action="store_true",
                        help="崩溃后直接交同工位其他机台（稳定触发接管，而非本机台续测）")
    parser.add_argument("--fail-rate", type=float, default=0.08, help="不良件比例（→ LOCKED/重测）")
    parser.add_argument("--missing-rate", type=float, default=0.05, help="漏报必测项比例")
    parser.add_argument("--bad-fw-rate", type=float, default=0.05, help="非基线固件比例")
    parser.add_argument("--jump-rate", type=float, default=0.05, help="跳站比例")
    parser.add_argument("--abandon-rate", type=float, default=0.05, help="中途停机比例")
    parser.add_argument("--stop-rate", type=float, default=0.25, help="过站后停止推进比例")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    rng = random.Random(args.seed or None)
    crash_rate = 0.0 if args.mode == "normal" else args.crash_rate
    rates = {"fail": args.fail_rate, "missing": args.missing_rate, "bad_fw": args.bad_fw_rate,
             "jump": args.jump_rate, "stop": args.stop_rate, "abandon": args.abandon_rate}

    try:
        token = admin_login(args.base_url, args.admin_user, args.admin_password)
    except Exception as exc:
        print(f"[ERROR] 管理端登录失败：{exc}")
        return 1
    admin = Admin(args.base_url, token)

    model_row = next((m for m in admin.models() if m.get("product_model") == args.model), None)
    if model_row is None:
        print(f"[ERROR] 服务端未找到机型 {args.model}")
        return 1
    process_id = model_row["process_id"]
    base_fw = model_row.get("target_fw_version") or "V3.20"
    plan = ProcessPlan(admin.topology(process_id))
    if not plan.stations:
        print(f"[ERROR] 流程 {process_id} 无工步，请先执行 seed")
        return 1

    stations = [args.station] if args.station else plan.stations
    sim = Sim(plan, rates, single_station=bool(args.station), items_limit=args.items)
    print("=" * 70)
    print(f"产线模拟器 v2：机型 {args.model}（{process_id}，基线固件 {base_fw}）")
    print(f"  流程 {' → '.join(plan.stations)}")
    print(f"  机台 {args.clients}/工位 × {len(stations)} 工位 · 投产 {args.units} 件 · mode={args.mode}")
    print("=" * 70)

    workers: List[Station] = []
    state_dir = Path(".sim_state")
    state_dir.mkdir(exist_ok=True)
    for si, sid in enumerate(stations):
        for i in range(1, args.clients + 1):
            # client_id 必须符合后端 ID 规则 <SITE>-<LINE>-<STAGE>-<NN>[-<USE>]：
            # 用固定仿真线号 L9，工位的 DOMAIN 段放进可选后缀，保证跨工位唯一且可读
            stage, _, domain = sid.partition("-")
            cid = f"SZ-L9-{stage}-{i:02d}-{domain}" if domain else f"SZ-L9-{stage}-{i:02d}"
            admin.ensure_client(cid, sid, f"10.9.{si + 1}.{10 + i}")
            workers.append(Station(
                client_id=cid, station_id=sid, base_url=args.base_url, api_key=args.api_key,
                sim=sim, admin=admin, model=args.model, base_fw=base_fw,
                crash_rate=crash_rate, resume=not args.no_chaos_resume,
                state_dir=state_dir, rng=random.Random(rng.random()),
                handoff=args.crash_handoff,
            ))
    print(f"[setup] 已就绪 {len(workers)} 台机台\n")

    stamp = int(time.time()) % 100000
    for i in range(args.units):
        job = Job(sn=f"SIM{stamp}{i:03d}", station_id=stations[0],
                  **roll_injection(rng, rates))
        sim.submit(job, stations[0])

    # 4) 并发跑
    started = time.time()
    threads = [threading.Thread(target=w.run) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    c = sim.counters
    print("\n" + "-" * 70)
    print("结果分布：")
    print(f"  出库 PASS {c['pass_out']} · 出库 FAIL {c['fail_out']} · 工程锁定 {c['locked']}")
    print(f"  防呆拦截 {c['gated']}"
          f"（固件 {c['gated_firmware_mismatch']} / 跳站 {c['gated_missing_prereq']}"
          f" / 漏测 {c['gated_missing_mandatory']}）")
    print(f"  中途停机 {c['abandoned']} · 过站停线 {c['stopped']} · 全流程完工 {c['completed']}")
    print(f"  崩溃 {c['crashed']} · 续测 {c['resumed']} · 接管 {c['takeover']} · "
          f"放弃 {c['gave_up']} · 跳过 {c['skipped']}")
    print(f"  未预期失败 {c['unexpected']}")

    dist = collections.Counter(len(v) for v in sim.progress.values())
    total_stations = len(plan.stations)
    unfinished = args.units - sum(dist.values())
    print("\n完成度分布（已过工位数 / 件数）：")
    for n in range(total_stations + 1):
        label = "完工" if n == total_stations else f"{n} 站"
        print(f"  {label}: {dist.get(n, 0)}")
    print(f"  一件未过: {unfinished}")
    print(f"\n耗时 {elapsed:.1f}s")

    try:
        ov = admin.overview()
        print(f"\n服务端校验：活跃锁 {ov['locks']['active']} · 僵尸锁 {ov['locks']['zombie']} · "
              f"运行会话 {ov['locks']['sessions_running']} · 异常会话 {admin.abnormal_sessions()} · "
              f"失联待接管 {admin.zombie_locks()}")
    except Exception as exc:
        print(f"\n[warn] 服务端校验失败：{exc}")

    for f in state_dir.glob("*.json"):
        try:
            f.unlink()
        except Exception:
            pass
    try:
        state_dir.rmdir()
    except Exception:
        pass

    ok = c["unexpected"] == 0 and (c["pass_out"] + c["fail_out"]) > 0
    print(f"\n结论：{'通过' if ok else '未通过'}（非预期失败 {c['unexpected']}）")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())

