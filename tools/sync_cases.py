r"""用一份 JSON 全量同步用例ID（nodeid）到服务端 station_items。

用法：
    scripts\sync-cases.bat --file cases.json             全量同步
    scripts\sync-cases.bat --file cases.json --dry-run   只预览差异，不落库
    scripts\sync-cases.bat --file cases.json --abort-running         先停涉及的工位再同步
    scripts\sync-cases.bat --file cases.json --abort-running --settle-sec 90

JSON 格式（多流程，一次更新多条；同名工位在不同流程是两份独立清单）：
{
  "processes": {
    "PROC_TEK_MSO": {
      "CAL_PARAM": [
        "tests/test_cal_param.py::TestAmp::test_amp_cal",
        {"case_id": "tests/test_cal_param.py::TestPhase::test_phase_cal",
         "item_name": "CHn_相位校准", "is_mandatory": true}
      ],
      "TST_AWG": ["tests/test_tst_awg.py::TestDac::test_1k_dc"]
    },
    "PROC_TEK_DPO": {
      "CAL_PARAM": ["tests/test_cal_param.py::TestAmp::test_amp_cal"]
    }
  }
}

单条流程也用同一格式，只写一个 key 即可：
{"processes": {"PROC_TEK_MSO": {"CAL_PARAM": [...]}}}

有会话在跑时怎么办（--abort-running）
    换清单会让进行中会话的断点"缺新必测项"，故默认拒绝同步。
    --abort-running 会先批量中止这些会话（只关会话 + 放锁，不计失败），
    然后等待 --settle-sec 秒让上位机通过心跳感知停机（默认 = 心跳间隔 + 余量），
    确认 RUNNING 归零后才同步。被中止的件回到 IDLE 未盖章，需重新进站跑一遍。

语义 —— JSON 就是最终状态，不做"增量推断"：
    JSON 里有            -> 新增 / 更新 / 重新启用（幂等，可反复执行）
    JSON 里没有           -> 停用（is_active=false，软删可回滚，不会再拦进站）
    JSON 里没列的工位     -> 不动，只同步列出的工位

为什么是停用而不是删除：上位机改名/删用例后，旧 nodeid 若仍是必测项，进站会
case_id_mismatch 全线拦截；停用既解除拦截，又保留历史记录可追溯。

凭据（不要写进命令行历史）：ATE_ADMIN_USER / ATE_ADMIN_PASSWORD 环境变量。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

DEFAULT_BASE = "http://127.0.0.1:8000"
# 服务端 LOCK_HEARTBEAT_INTERVAL_SEC 默认值：上位机靠心跳感知停机，等待窗口须大于它
HEARTBEAT_SEC = 30


def http_json(method: str, url: str, token: str, payload: Dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[ERROR] {method} {url} -> {exc.code}: {exc.read().decode(errors='replace')}")


def login(base: str, user: str, password: str) -> str:
    """通道二登录取 JWT（写规则必须走管理员通道，v1 的 X-API-Key 无此权限）。"""
    data = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/api/auth/login", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())["access_token"]
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[ERROR] login failed: {exc.code} {exc.read().decode(errors='replace')}")


def parse_items(raw) -> List[dict]:
    """支持 `["nodeid", ...]` 与 `[{case_id, item_name, is_mandatory}, ...]` 两种写法。"""
    items: List[dict] = []
    for entry in raw or []:
        if isinstance(entry, str):
            case_id = entry.strip()
            if case_id:
                items.append({"case_id": case_id, "item_name": "", "is_mandatory": True})
        elif isinstance(entry, dict):
            case_id = str(entry.get("case_id") or "").strip()
            if not case_id:
                continue
            items.append(
                {
                    "case_id": case_id,
                    "item_name": str(entry.get("item_name") or "").strip(),
                    "is_mandatory": bool(entry.get("is_mandatory", True)),
                }
            )
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="用 JSON 全量同步用例ID")
    parser.add_argument("--file", required=True, help="用例清单 JSON 路径")
    parser.add_argument("--base-url", default=os.environ.get("ATE_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--user", default=os.environ.get("ATE_ADMIN_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("ATE_ADMIN_PASSWORD", ""))
    parser.add_argument("--dry-run", action="store_true", help="只预览差异，不落库")
    parser.add_argument("--abort-running", action="store_true", help="先中止涉及的 RUNNING 会话")
    parser.add_argument("--settle-sec",type=int,default=0,help="中止后等待的秒数（让上位机心跳感知停机）；0 = 取心跳间隔 + 15s")
    args = parser.parse_args()

    if not args.password:
        print("[ERROR] missing admin password: set ATE_ADMIN_PASSWORD or pass --password")
        return 1

    spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
    raw_processes = spec.get("processes")
    if not isinstance(raw_processes, dict) or not raw_processes:
        print('[ERROR] JSON must be: {"processes": {"<process_id>": {"<station_id>": [...]}}}')
        return 1
    processes: Dict[str, dict] = {
        pid: stations
        for pid, stations in raw_processes.items()
        if isinstance(stations, dict) and stations
    }
    if not processes:
        print("[ERROR] no non-empty station map under 'processes'")
        return 1

    token = login(args.base_url, args.user, args.password)

    # 1) 逐流程校验工位归属（同名工位在不同流程是两份独立清单，必须按流程分别校验）
    for process_id in sorted(processes):
        stations = processes[process_id]
        steps = http_json(
            "GET", f"{args.base_url}/api/admin/routing/stations?process_id={process_id}", token
        )
        valid = {s["station_id"] for s in steps} if isinstance(steps, list) else set()
        unknown = [s for s in stations if s not in valid]
        if unknown:
            print(f"[ERROR] not steps of {process_id}: {', '.join(sorted(unknown))}")
            print(f"        valid stations: {', '.join(sorted(valid))}")
            return 1
        missing = sorted(valid - set(stations))
        if missing:
            print(f"[warn] {process_id}: stations not in JSON (left untouched): {', '.join(missing)}")

    # 2) 中止范围按流程圈定：abort 接口会用 SN 的机型反查所属流程，不会误伤别的流程
    running_items = http_json(
        "GET", f"{args.base_url}/api/admin/sessions?status=RUNNING&page_size=200", token
    ).get("items") or []
    running_stations = {s.get("station_id") for s in running_items}
    busy_processes = sorted(
        pid for pid, stations in processes.items() if running_stations & set(stations)
    )
    if busy_processes:
        if args.abort_running:
            touched: List[str] = []
            for process_id in busy_processes:
                res = http_json(
                    "POST",
                    f"{args.base_url}/api/admin/sessions/abort-running",
                    token,
                    {"process_id": process_id, "reason": "case list sync", "dry_run": args.dry_run},
                )
                touched += [f"{i['sn']}@{i['station_id']}" for i in (res.get("items") or [])]
            print(f"[{'dry-run' if args.dry_run else 'abort' }] {len(touched)} session(s): "
                  f"{', '.join(touched[:8])}{' ...' if len(touched) > 8 else ''}")
            if not args.dry_run:
                wait = args.settle_sec or (HEARTBEAT_SEC + 15)
                print(f"[abort ] waiting {wait}s for clients to notice via heartbeat...")
                time.sleep(wait)
                still_items = http_json(
                    "GET", f"{args.base_url}/api/admin/sessions?status=RUNNING&page_size=200", token
                ).get("items") or []
                still_stations = {s.get("station_id") for s in still_items}
                still = sorted(
                    pid for pid, stations in processes.items() if still_stations & set(stations)
                )
                if still:
                    print(f"[ERROR] still RUNNING in {', '.join(still)} after abort+wait.")
                    print("        the client does not honor holding_lock=false; fix the client and retry.")
                    return 1
        else:
            level = "warn" if args.dry_run else "ERROR"
            print(f"[{level}] RUNNING session(s) in: {', '.join(busy_processes)}")
            print("        their checkpoint holds the old case IDs -> missing_mandatory at check-out")
            print("        + one failure counted (3x -> LOCKED).")
            if not args.dry_run:
                print("        wait for them to finish, or add --abort-running to stop them first.")
                return 1

    for process_id in sorted(processes):
        for station_id in sorted(processes[process_id]):
            payload = {
                "process_id": process_id,
                "station_id": station_id,
                "items": parse_items(processes[process_id][station_id]),
                "mode": "replace",
                "dry_run": args.dry_run,
            }
            res = http_json("POST", f"{args.base_url}/api/admin/routing/items/import", token, payload)
            print(
                "[%-8s] %-30s +%-3d ~%-3d =%-3d -%-3d active=%d%s"
                % (
                    "dry-run" if args.dry_run else "synced",
                    f"{process_id}/{station_id}",
                res.get("created", 0),
                res.get("updated", 0),
                res.get("unchanged", 0),
                res.get("deactivated", 0),
                res.get("total_active", 0),
                "  (nothing written)" if args.dry_run else "",
            )
        )
            if res.get("deactivated"):
                sample = ", ".join((res.get("orphans") or [])[:3])
                print(f"           deactivated (not in JSON): {sample}"
                      f"{' ...' if res['deactivated'] > 3 else ''}")
            for w in res.get("warnings") or []:
                print(f"           warn: {w}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
