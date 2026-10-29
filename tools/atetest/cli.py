# -*- coding: utf-8 -*-
"""ate-cli：非 Python 程序的命令行接入通道（stdout 纯 JSON，退出码即契约）。

任何语言/独立程序都可以通过"子进程 + 读 stdout + 看退出码"接入 ATE，
无需实现 HTTP 与心跳：会话状态（session/lock/断点）由本 CLI 落盘复用。

典型用法（一个工位 = 一次会话，多个子流程 = 多个 checkpoint）：

    ate-cli --state ./ate-state.json check-in --sn C020001 --model DPO4054B --fw V3.20
    ate-cli --state ./ate-state.json heartbeat --loop          # 后台保活
    ate-cli --state ./ate-state.json checkpoint --case cal_dc --result PASS --values '{"gain":1.02}'
    ate-cli --state ./ate-state.json artifact --case cal_dc --file ./logs/dc.log
    ate-cli --state ./ate-state.json check-out --duration 120000

约定：
    - stdout 只输出 JSON（日志走 stderr），一行一个对象
    - 退出码：0 = 放行；10 = 业务拦截（重号/跳站/固件/漏测…）；12 = 网络不可达；1 = 其它错误
    - 服务端拦截时 stdout 形如 {"ok": false, "code": "missing_prereq", "message": "...", "data": {...}}
    - API Key 走环境变量 ATE_API_KEY（命令行参数会出现在进程列表）
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api as ate

""" 跨平台编码：Windows 默认 GBK 会让中文响应/请求变成非 UTF-8 字节，宿主解析即失败。
CLI 与宿主的契约固定为 UTF-8（JSON 标准编码），三端统一。 """
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ERROR = 1
EXIT_BLOCKED = 10
EXIT_NETWORK = 12
EXIT_LOCK_LOST = 11
EXIT_SIGINT = 130

DEFAULT_STATE = "ate-state.json"
CLI_VERSION = "1.0.0"


"""serve 模式下的响应收集器：处理器照常 emit，由 serve 循环统一补 id/exit 后输出。"""
_SINK: Dict[str, Any] = {"buf": None}
"""stdout 写锁：心跳线程（失锁事件）与主线程（命令响应）可能并发写，必须串行化。"""
_WRITE_LOCK = threading.Lock()


def _write_line(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    with _WRITE_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def emit(payload: Dict[str, Any]) -> None:
    """stdout 只输出 JSON（单行），供宿主程序稳定解析。"""
    sink = _SINK["buf"]
    if sink is not None:
        sink.append(payload)
        return
    _write_line(payload)


def log(text: str) -> None:
    """诊断信息一律走 stderr，避免污染 stdout 的 JSON。"""
    sys.stderr.write(text.rstrip() + "\n")
    sys.stderr.flush()


def _result(value: str) -> str:
    v = (value or "").strip().upper()
    if v in ("PASS", "FAIL", "SKIP"):
        return v
    if v in ("PASSED", "FAILED", "SKIPPED"):
        return v[:-2]
    raise SystemExit(_fail("invalid_result", f"result 只接受 PASS/FAIL/SKIP，收到 {value!r}"))


def _fail(code: str, message: str, exit_code: int = EXIT_ERROR, data: Any = None) -> int:
    emit({"ok": False, "code": code, "message": message, "data": data or {}})
    return exit_code


def _items_from_json(raw: Any) -> List[dict]:
    """--items 传字符串（CLI）；serve 模式可直接传数组/对象。"""
    if not raw:
        return []
    if isinstance(raw, (list, dict)):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise SystemExit(_fail("invalid_items", f"--items 必须是 JSON 数组：{exc}"))
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise SystemExit(_fail("invalid_items", "--items 必须是 JSON 数组"))
    items = []
    for it in parsed:
        if not isinstance(it, dict) or not it.get("case_id"):
            raise SystemExit(_fail("invalid_items", "每条 item 必须含 case_id"))
        items.append(it)
    return items


PROBE_PATH = "/api/health"
PROBE_TIMEOUT_SEC = 1.0      # 就绪探测固定超时：不可达即 fail-fast，不做可调


def probe_server(client: "ate.AteClient", timeout: float = 1.0) -> "tuple[bool, int]":
    """服务端探针：GET /api/health（短超时、无重试、不发业务请求）。

    返回 (连得上, HTTP 状态码)：
    - 连得上 = 拿到了任何 HTTP 响应（含 4xx/5xx）——只证明网络通
    - 健康 = 状态码 < 500（服务端自检通过，业务命令才可能成功）
    连接层失败（拒绝/超时/DNS）→ (False, 0)
    """
    url = client.http.base_url.rstrip("/") + PROBE_PATH
    try:
        with ate._OPENER.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return True, int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        return True, int(getattr(exc, "code", 500))
    except Exception:
        return False, 0


def build_client(args: argparse.Namespace) -> "ate.AteClient":
    """serve 模式复用同一客户端（心跳线程、连接、内存会话），一次性子命令按参数新建。"""
    shared = getattr(args, "_client", None)
    if shared is not None:
        return shared
    state_path = Path(args.state).expanduser() if args.state else None
    return ate.AteClient(
        args.url,
        args.key,
        client_id=args.client,
        app_version=args.app,
        state_file=state_path,
        timeout=args.timeout,
    )


def load_session(client: "ate.AteClient", args: argparse.Namespace) -> "ate.SessionState":
    """从 --state 文件恢复会话（每个子命令都是独立进程，状态必须落盘）。"""
    if client.state and client.state.session_id:
        return client.state
    if not args.state:
        raise SystemExit(_fail("no_session", "缺少 --state（或环境变量 ATE_STATE_FILE）"))
    state = ate.SessionState.load(Path(args.state).expanduser())
    if state is None or not state.session_id:
        raise SystemExit(_fail("no_session", f"会话不存在或已结束：{args.state}（请先 check-in）"))
    client.state = state
    return state


def cmd_resolve(args: argparse.Namespace) -> int:
    client = build_client(args)
    data = client.resolve()
    emit({"ok": True, "command": "resolve", "data": data})
    return EXIT_OK


def _read_cases_file(path: str) -> List[str]:
    """从文件读用例序列：JSON 数组，或每行一个（# 开头为注释）。"""
    text = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SystemExit(_fail("invalid_cases", f"--cases-file JSON 解析失败：{exc}"))
        return [str(c).strip() for c in data if str(c).strip()]
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _as_cases(value: Any) -> List[str]:
    """用例序列兼容单值/数组（CLI: --case 多次；serve: "case": ["a","b"]）。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _cases_from_args(args: argparse.Namespace) -> List[str]:
    """--case 与 --cases-file 合并（nodeid 长/数量多时用文件，避免命令行长度上限）。"""
    cases = _as_cases(getattr(args, "case", None))
    path = getattr(args, "cases_file", None)
    if path:
        cases.extend(_read_cases_file(path))
    return cases


def cmd_case_list(args: argparse.Namespace) -> int:
    cases = _cases_from_args(args)
    if not cases:
        return _fail("invalid_cases", "至少需要一个 --case", EXIT_USAGE)
    client = build_client(args)
    data = client.report_case_list(args.client, args.model, cases)
    emit({"ok": True, "command": "case-list", "data": data})
    return EXIT_OK


def cmd_check_in(args: argparse.Namespace) -> int:
    if not (args.sn and args.model and args.fw):
        return _fail("missing_args", "check-in 需要 --sn/--model/--fw", EXIT_USAGE)
    client = build_client(args)
    state = client.check_in(
        args.sn, args.model, args.fw,
        case_ids=_cases_from_args(args) or None,
        resume=not getattr(args, "no_resume", False),
    )
    if getattr(args, "no_heartbeat", False):
        """ 宿主自行按需发 heartbeat：停掉 SDK 自动启动的后台线程 """
        client.stop_heartbeat()
    emit({
        "ok": True,
        "command": "check-in",
        "data": {
            "sn": state.sn,
            "session_id": state.session_id,
            "lock_token": state.lock_token,
            "station_id": state.station_id,
            "attempt": state.attempt,
            "passthrough": state.passthrough,
            "checkout_id": state.checkout_id,
            "completed_case_ids": list(state.completed_case_ids or []),
            "mandatory_case_ids": list(state.mandatory_case_ids or []),
        },
    })
    return EXIT_OK


def cmd_heartbeat(args: argparse.Namespace) -> int:
    client = build_client(args)
    load_session(client, args)
    if not args.loop:
        data = client.heartbeat()
        emit({"ok": True, "command": "heartbeat", "data": data})
        return EXIT_OK
    """ 循环保活：每次 beat 输出一行 JSON；失锁/会话终止即退出（宿主据此停机） """
    interval = max(5.0, float(args.interval))
    log(f"heartbeat loop started (interval={interval}s, state={args.state})")
    while True:
        try:
            data = client.heartbeat()
        except ate.ApiError as exc:
            emit({"ok": False, "command": "heartbeat", "code": exc.code, "message": str(exc)})
            if exc.is_network:
                time.sleep(interval)
                continue
            return EXIT_LOCK_LOST
        emit({"ok": True, "command": "heartbeat", "data": data})
        if data.get("holding_lock") is False:
            return EXIT_LOCK_LOST
        time.sleep(interval)


def cmd_checkpoint(args: argparse.Namespace) -> int:
    client = build_client(args)
    load_session(client, args)
    items_raw = getattr(args, "items", None)
    items_file = getattr(args, "items_file", None)
    if items_file:
        try:
            items_raw = json.loads(Path(items_file).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _fail("invalid_items", f"--items-file 读取失败：{exc}", EXIT_USAGE)
    batch = _items_from_json(items_raw)
    case_id = _as_cases(args.case)
    if case_id:
        item: Dict[str, Any] = {"case_id": case_id[0], "result": _result(args.result)}
        if args.duration is not None:
            item["duration_ms"] = int(args.duration)
        if args.values:
            if isinstance(args.values, (dict, list)):
                item["values"] = args.values
            else:
                try:
                    item["values"] = json.loads(args.values)
                except ValueError as exc:
                    return _fail("invalid_values", f"--values 必须是 JSON：{exc}", EXIT_USAGE)
        batch.append(item)
    if not batch:
        return _fail("missing_args", "checkpoint 需要 --case（或 --items）", EXIT_USAGE)
    merged = client.checkpoint(batch)
    emit({"ok": True, "command": "checkpoint", "data": {"merged_count": merged, "reported": len(batch)}})
    return EXIT_OK


def cmd_artifact(args: argparse.Namespace) -> int:
    client = build_client(args)
    load_session(client, args)
    path = Path(args.file).expanduser()
    if not path.is_file():
        return _fail("artifact_missing", f"文件不存在：{path}", EXIT_USAGE)
    data = client.upload_artifact(args.case, path=path)
    emit({"ok": True, "command": "artifact", "data": data})
    return EXIT_OK


def cmd_check_out(args: argparse.Namespace) -> int:
    client = build_client(args)
    load_session(client, args)
    items_raw = getattr(args, "items", None)
    items_file = getattr(args, "items_file", None)
    if items_file:
        try:
            items_raw = json.loads(Path(items_file).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _fail("invalid_items", f"--items-file 读取失败：{exc}", EXIT_USAGE)
    items = _items_from_json(items_raw)
    data = client.check_out(items, duration_ms=getattr(args, "duration", None) or 0,
                            reason=getattr(args, "reason", None) or "")
    emit({
        "ok": True,
        "command": "check-out",
        "data": {
            "acknowledged": data.get("acknowledged", True),
            "checkout_id": data.get("checkout_id"),
            "record_id": data.get("record_id"),
            "overall_result": data.get("overall_result"),
            "station_passed": data.get("station_passed"),
        },
    })
    return EXIT_OK


def cmd_ack(args: argparse.Namespace) -> int:
    client = build_client(args)
    state = None
    if args.state:
        state = ate.SessionState.load(Path(args.state).expanduser())
    if state is not None:
        client.state = state
    ok = client.ack(sn=args.sn or None, checkout_id=args.checkout_id or None)
    emit({"ok": bool(ok), "command": "ack", "data": {"acknowledged": bool(ok)}})
    return EXIT_OK if ok else EXIT_BLOCKED


def cmd_sn_status(args: argparse.Namespace) -> int:
    """SN 登记状态（只读）：宿主据 registered 决定连测试服还是生产服。"""
    sn = (getattr(args, "sn", "") or "").strip()
    if not sn:
        return _fail("missing_args", "sn-status 需要 --sn", EXIT_USAGE)
    registered = ate.sn_registered(args.url, args.key, sn, timeout=args.timeout)
    emit({"ok": True, "command": "sn-status", "data": {"sn": sn, "registered": bool(registered)}})
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """聚合状态：本地会话 + 服务端持锁情况（一次调用判断能否继续）。"""
    client = build_client(args)
    state = None
    if client.state and client.state.session_id:
        state = client.state
    elif args.state:
        state = ate.SessionState.load(Path(args.state).expanduser())
        client.state = state if (state and state.session_id) else None
        state = client.state
    if state is None:
        emit({"ok": True, "command": "status", "data": {"active": False}})
        return EXIT_OK
    data: Dict[str, Any] = {
        "active": True,
        "sn": state.sn,
        "station_id": state.station_id,
        "attempt": state.attempt,
        "session_id": state.session_id,
        "checkout_id": state.checkout_id,
        "completed_count": len(state.completed_case_ids or []),
        "pending_count": len(state.pending_items or []),
        "lost_lock": client.lost_lock,
    }
    try:
        beat = client.heartbeat()
        data.update({
            "holding_lock": beat.get("holding_lock"),
            "remaining_sec": beat.get("remaining_sec"),
            "lease_remaining_sec": beat.get("lease_remaining_sec"),
        })
    except ate.ApiError as exc:
        """ 会话已终结（已出站/被接管/不存在）：active=False，宿主应清理本地状态 """
        data.update({"active": False, "holding_lock": False, "code": exc.code})
    emit({"ok": True, "command": "status", "data": data})
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    client = build_client(args)
    load_session(client, args)
    data = client.release(reason=args.reason or "")
    emit({"ok": True, "command": "release", "data": data})
    return EXIT_OK


def cmd_state(args: argparse.Namespace) -> int:
    if not args.state:
        return _fail("no_session", "缺少 --state", EXIT_USAGE)
    path = Path(args.state).expanduser()
    if args.clear:
        try:
            path.unlink()
        except OSError:
            pass
        emit({"ok": True, "command": "state", "data": {"cleared": str(path)}})
        return EXIT_OK
    state = ate.SessionState.load(path)
    if state is None:
        emit({"ok": True, "command": "state", "data": {"exists": False, "path": str(path)}})
        return EXIT_OK
    emit({
        "ok": True,
        "command": "state",
        "data": {
            "exists": True,
            "path": str(path),
            "sn": state.sn,
            "session_id": state.session_id,
            "station_id": state.station_id,
            "attempt": state.attempt,
            "passthrough": state.passthrough,
            "checkout_id": state.checkout_id,
            "completed_case_ids": list(state.completed_case_ids or []),
            "pending_count": len(state.pending_items or []),
        },
    })
    return EXIT_OK


""" serve 模式可路由的命令（一次性子命令与常驻模式共用同一批处理器） """
SERVE_COMMANDS = {
    "resolve": cmd_resolve,
    "case-list": cmd_case_list,
    "check-in": cmd_check_in,
    "heartbeat": cmd_heartbeat,
    "checkpoint": cmd_checkpoint,
    "artifact": cmd_artifact,
    "check-out": cmd_check_out,
    "ack": cmd_ack,
    "release": cmd_release,
    "status": cmd_status,
    "sn-status": cmd_sn_status,
    "state": cmd_state,
}

""" 请求字段 → 处理器参数（serve 用 JSON 覆盖全局参数） """
_SERVE_FIELDS = (
    "sn", "model", "fw", "case", "result", "duration", "values", "items",
    "reason", "checkout_id", "client", "app", "timeout", "url",
)


""" serve 请求可能不携带的参数：给与子命令一致的安全默认值 """
_SERVE_DEFAULTS = {
    "sn": "", "model": "", "fw": "", "case": None, "result": "PASS",
    "duration": None, "values": None, "items": None, "reason": None,
    "checkout_id": None, "no_resume": False, "clear": False,
    "cases_file": None, "items_file": None, "no_heartbeat": False,
}


def _serve_namespace(req: Dict[str, Any], base: argparse.Namespace) -> argparse.Namespace:
    ns = argparse.Namespace(**vars(base))
    for key, value in _SERVE_DEFAULTS.items():
        if not hasattr(ns, key):
            setattr(ns, key, value)
    ns.loop = False
    ns.no_resume = bool(req.get("no_resume", False))
    ns.clear = bool(req.get("clear", False))
    ns.interval = float(req.get("interval") or 30.0)
    for key in _SERVE_FIELDS:
        if key in req and req[key] is not None:
            setattr(ns, key, req[key])
    return ns


def cmd_serve(args: argparse.Namespace) -> int:
    """常驻模式：stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应（含 id/exit）。

    好处：进程只起一次（省掉每次 ~170ms 的启动开销），会话、心跳线程、连接全部常驻；
    适合"高频上报但不想写 HTTP"的宿主程序。协议：
        {"id": 1, "cmd": "check-in", "sn": "X", "model": "Y", "fw": "V"}
        {"id": 2, "cmd": "checkpoint", "case": "cal_dc", "result": "PASS"}
        {"id": 3, "cmd": "check-out"}
        {"cmd": "quit"}
    """
    client = build_client(args)
    setattr(args, "_client", client)

    def _on_lost_lock(reason: str) -> None:
        """心跳线程判定失锁时**主动推送**事件（不等宿主下一次请求）——宿主据此立即停机。"""
        _write_line({
            "event": "lock_lost",
            "code": "lock_invalid",
            "message": reason,
            "exit": EXIT_LOCK_LOST,
        })

    client.on_lost_lock = _on_lost_lock
    ready: Dict[str, Any] = {"ready": True, "process_ready": True,
                             "state": args.state, "commands": sorted(SERVE_COMMANDS)}
    if args.state:
        leftover = ate.SessionState.load(Path(args.state).expanduser())
        if leftover is not None and leftover.session_id:
            ready.update({"resumable": True, "sn": leftover.sn, "station_id": leftover.station_id})

    """ 就绪 = 进程就绪「且」服务端可达且自检通过；探测失败即 fail-fast（退出码 12），
    由 Supervisor 决定重启/报警——CLI 不做等待与重探。 """
    reachable, status = probe_server(client, PROBE_TIMEOUT_SEC)

    healthy = reachable and status < 500
    ready["server_reachable"] = reachable
    ready["server_healthy"] = healthy
    if not healthy:
        ready["ready"] = False
        ready["hint"] = (
            "服务端不可达：进程退出（退出码 12），请检查服务端与网络后重启 CLI"
            if not reachable else
            f"服务端可达但自检未通过（HTTP {status}）：进程退出（退出码 12），请检查服务端状态"
        )
    emit({"ok": True, "command": "serve", "data": ready})

    if not healthy:
        """ fail-fast：没有服务对象就退出，不留"活着但干不了活"的哑进程 """
        log("server unreachable/unhealthy (HTTP %s): exit %d" % (status, EXIT_NETWORK))
        client.stop_heartbeat()
        return EXIT_NETWORK

    clean_quit = False        # True = 主动 quit（不触发兜底释放）
    stop_event = threading.Event()
    inbox = queue.Queue()

    def _pump_stdin() -> None:
        """独立线程读 stdin，主线程用带超时的 queue.get()。
        主线程直接阻塞在 stdin 的 C 层 read 上时（stdin 是宿主管道），
        Ctrl+C 往往要等到有数据才被处理——表现为"按了没反应"。"""
        try:
            for raw in sys.stdin:
                inbox.put(raw)
        finally:
            inbox.put(None)

    def _on_signal(signum, frame) -> None:
        stop_event.set()

    for _sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None),
                 getattr(signal, "SIGBREAK", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass

    threading.Thread(target=_pump_stdin, name="ate-cli-stdin", daemon=True).start()

    interrupted_by_keyboard = False
    try:
        while not stop_event.is_set():
            try:
                raw = inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if raw is None:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError as exc:
                emit({"ok": False, "code": "invalid_json", "message": str(exc), "exit": EXIT_USAGE})
                continue
            if not isinstance(req, dict):
                emit({"ok": False, "code": "invalid_request", "message": "每行必须是一个 JSON 对象", "exit": EXIT_USAGE})
                continue
            cmd = str(req.get("cmd") or req.get("command") or "").strip().lower()
            rid = req.get("id")
            if cmd in ("quit", "exit"):
                release = bool(req.get("release", False))
                has_session = bool(client.state and client.state.session_id)
                released = False
                if release and has_session:
                    try:
                        client.release(reason=str(req.get("reason") or "cli quit"))
                        released = True
                    except ate.ApiError as exc:
                        log(f"quit release failed: {exc}")
                client.stop_heartbeat()   # 优雅停止：先停心跳再退出
                payload: Dict[str, Any] = {"ok": True, "id": rid, "command": cmd, "exit": EXIT_OK,
                                           "data": {"active_session": bool(has_session and not released),
                                                    "released": released}}
                if has_session and not released:
                    payload["data"]["hint"] = "会话未出站：锁将在超时后失效；建议发送 {\"cmd\":\"quit\",\"release\":true} 或先 check-out"
                emit(payload)
                clean_quit = True
                break
            if cmd in ("", "ping"):
                emit({"ok": True, "id": rid, "command": "ping", "exit": EXIT_OK})
                continue
            handler = SERVE_COMMANDS.get(cmd)
            if handler is None:
                emit({
                    "ok": False, "id": rid, "code": "unknown_command", "message": cmd,
                    "data": {"commands": sorted(SERVE_COMMANDS)}, "exit": EXIT_USAGE,
                })
                continue
            buf: List[Dict[str, Any]] = []
            _SINK["buf"] = buf
            code = EXIT_OK
            try:
                code = int(handler(_serve_namespace(req, args)) or EXIT_OK)
            except SystemExit as exc:
                code = int(exc.code or EXIT_ERROR) if isinstance(exc.code, int) else EXIT_ERROR
            except ate.ApiError as exc:
                emit({
                    "ok": False, "status": exc.status, "code": exc.code, "message": str(exc),
                    "data": exc.data,
                })
                code = EXIT_NETWORK if exc.is_network else (EXIT_LOCK_LOST if exc.is_lock_invalid else (int(exc.exit_code) or EXIT_BLOCKED))
            except Exception as exc:
                log(f"serve handler error: {exc.__class__.__name__}: {exc}")
                emit({"ok": False, "code": "cli_error", "message": str(exc), "data": {}})
                code = EXIT_ERROR
            finally:
                _SINK["buf"] = None
            if not buf:
                buf.append({"ok": code == EXIT_OK, "command": cmd, "data": {}})
            for payload in buf:
                payload.setdefault("id", rid)
                payload["exit"] = code
                if client.lost_lock:
                    payload["lock_lost"] = True   # 锁已被接管：宿主必须停止后续流程
                emit(payload)
    except KeyboardInterrupt:
        """ 控制台 Ctrl+C：主线程在 queue.get() 上，中断可即时送达；
        统一走下面的收尾（释放会话 + 退出码 130），不留僵尸锁 """
        stop_event.set()
        interrupted_by_keyboard = True

    """ 异常退出兜底（stdin 关闭 / Ctrl+C / SIGTERM）：释放会话，避免僵尸锁。
    主动 quit 不走这里——quit 默认保留会话并已提示，是否释放由宿主决定。 """
    signalled = stop_event.is_set() or interrupted_by_keyboard
    if not clean_quit and client.state and client.state.session_id:
        try:
            client.release(reason="cli interrupted" if signalled else "cli stdin closed")
            _write_line({
                "event": "released_on_signal" if signalled else "released_on_eof",
                "sn": client.state.sn,
                "exit": EXIT_SIGINT if signalled else EXIT_OK,
            })
        except Exception as exc:
            log(f"release on exit failed: {exc}")
    client.stop_heartbeat()
    if signalled:
        log("interrupted (Ctrl+C/SIGTERM): 会话已释放，退出码 130")
        return EXIT_SIGINT
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ate-cli",
        description="ATE 命令行接入（stdout 纯 JSON，退出码 0/10/11/12 即契约）",
    )
    parser.add_argument("--url", default=os.getenv("ATE_URL", ate.ATE_URL), help="服务端地址（env ATE_URL）")
    parser.add_argument("--key", default=os.getenv("ATE_API_KEY", ate.ATE_KEY), help="X-API-Key（建议用 env ATE_API_KEY）")
    parser.add_argument("--client", default=os.getenv("ATE_CLIENT_ID", ate.CLIENT_ID), help="机台编号（env ATE_CLIENT_ID）")
    parser.add_argument("--app", default=os.getenv("ATE_APP_VERSION", ate.APP_VERSION), help="上位机版本（env ATE_APP_VERSION）")
    parser.add_argument("--state", default=os.getenv("ATE_STATE_FILE", DEFAULT_STATE), help="会话状态文件（env ATE_STATE_FILE）")
    parser.add_argument("--timeout", type=float, default=ate.DEFAULT_TIMEOUT, help="单次请求超时（秒）")
    parser.add_argument("--version", action="version", version=f"ate-cli {CLI_VERSION}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("resolve", help="机台身份上报与工位反查").set_defaults(func=cmd_resolve)

    p = sub.add_parser("case-list", help="录制态草稿上报：本次执行序列（check-in 之前调用）")
    p.add_argument("--model", required=True)
    p.add_argument("--case", action="append", help="用例 ID（可重复）")
    p.add_argument("--cases-file", help="用例序列文件（JSON 数组或每行一个），用于 nodeid 长/数量多")
    p.set_defaults(func=cmd_case_list)

    p = sub.add_parser("check-in", help="进站：防呆卡控 + 领锁（返回 session_id/lock_token）")
    p.add_argument("--sn", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--fw", required=True)
    p.add_argument("--case", action="append", help="本次会话用例序列（可重复）")
    p.add_argument("--cases-file", help="用例序列文件（JSON 数组或每行一个）")
    p.add_argument("--no-resume", action="store_true", help="忽略本地断点，按全新会话进站")
    p.add_argument("--no-heartbeat", action="store_true", help="不自动启动心跳线程（宿主自行按需 heartbeat）")
    p.set_defaults(func=cmd_check_in)

    p = sub.add_parser("heartbeat", help="保活（--loop 常驻，失锁即退出码 11）")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=30.0, help="循环间隔秒（最小 5）")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("checkpoint", help="增量上报已完成用例（每个子流程落一次断点）")
    p.add_argument("--case", help="用例 ID")
    p.add_argument("--result", default="PASS", help="PASS/FAIL/SKIP（默认 PASS）")
    p.add_argument("--duration", type=int, help="耗时毫秒")
    p.add_argument("--values", help="测量值 JSON，如 '{\"gain\":1.02}'")
    p.add_argument("--items", help="批量 JSON 数组（替代 --case）")
    p.add_argument("--items-file", help="批量 items 的 JSON 文件（规避命令行长度上限）")
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("artifact", help="上传产物（日志/波形/截图）")
    p.add_argument("--case", required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_artifact)

    p = sub.add_parser("check-out", help="出站落库（服务端补齐断点 + 漏测拦截 + 盖章）")
    p.add_argument("--items", help="出站一次性提交的 items JSON 数组（可选）")
    p.add_argument("--items-file", help="出站 items 的 JSON 文件（可选）")
    p.add_argument("--duration", type=int, help="整站总耗时毫秒")
    p.add_argument("--reason", help="备注")
    p.set_defaults(func=cmd_check_out)

    p = sub.add_parser("ack", help="出站闭环校验（网络抖动后确认已落库）")
    p.add_argument("--sn")
    p.add_argument("--checkout-id")
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("release", help="主动放弃工位锁（异常退出）")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("serve", help="常驻模式：stdin/stdout JSON-lines（进程只起一次）")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("status", help="聚合状态：本地会话 + 服务端持锁情况")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sn-status", help="SN 登记状态（只读；用于测试服/生产服路由判定）")
    p.add_argument("--sn", required=True)
    p.set_defaults(func=cmd_sn_status)

    p = sub.add_parser("state", help="查看/清除本地会话状态")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_state)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except ate.ApiError as exc:
        """ 业务拦截：把服务端 code/data 原样交给宿主，退出码用服务端 exit_code """
        emit({
            "ok": False,
            "command": args.command,
            "status": exc.status,
            "code": exc.code,
            "message": str(exc),
            "data": exc.data,
        })
        if exc.is_network:
            return EXIT_NETWORK
        if exc.is_lock_invalid:
            return EXIT_LOCK_LOST
        return int(exc.exit_code) or EXIT_BLOCKED
    except KeyboardInterrupt:
        log("interrupted (Ctrl+C): 已中止")
        return EXIT_SIGINT
    except Exception as exc:
        log(f"unexpected error: {exc.__class__.__name__}: {exc}")
        return _fail("cli_error", str(exc))


if __name__ == "__main__":
    sys.exit(main())
