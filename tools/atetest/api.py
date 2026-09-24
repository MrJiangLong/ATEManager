"""ATE Manager 接入 API（传输层 + 会话状态机，纯类接口，不含任何 pytest 钩子）。

单文件、仅标准库依赖。pytest 侧的钩子与夹具写在 conftest.py 中，调用本模块：

    import common.shared.api as ate

    run = ate.AteRun(ate.create_client())     # ATE_URL 留空 → 未对接，全部方法自动旁路
    run.start(request.session.items, sn, model, fw)   # 会话夹具 yield 前：进站 + 续测跳过标记
    run.collect(item, rep)                    # 现有 pytest_runtest_makereport 里调用（逐用例收集）
    run.gate()                                # 每用例收尾：checkpoint + 失锁自检
    run.finish()                              # 会话夹具 yield 后：必测判定 + 出站（幂等重试）

所有方法可能抛两类异常，conftest 负责处置：
    ate.AteExit   会话状态机的"干净终止"决策（防呆拦截/锁失效/网络不可达/必测未全过），
                  捕获后转 pytest.exit(e.message, returncode=e.returncode)
    ate.ApiError  非 ATE 决策的错误（原样冒泡即可）

自动化行为：进站防跳站/防复测/固件/用例ID 卡控；锁冲突自适应等待；断网断点缓存补传；
锁被接管/运维中止 → 状态机抛 AteExit 停机；必测未全过 → 不出站写重跑清单，重跑只执行
未通过项；全绿 → 出站同 checkout_id 幂等重试，拿到 ACK 才放行流转。
机台编号 = 当前电脑名（socket.gethostname()），首次运行自动注册（未绑定态），
需在 Web 端「机台管理」补录工位绑定。连接参数见下方常量区；ATE_URL 留空 = 不对接。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

""" ═══════════════════════════ 1. 配置 ═════════════════════════════════════ """

""" 服务端地址；留空 = 不对接（AteRun 全部方法旁路） """
ATE_URL = "http://127.0.0.1:8000"

""" 与服务端 V1_API_KEY 一致 """
ATE_KEY = "a1bb70ab8ac61c0be56c950b0bb2ce56959c5f96d86f4da3"

""" 机台编号 = 当前电脑名，首次进站自动注册 """
CLIENT_ID = socket.gethostname()

""" 断点目录（按 SN 分文件） """
STATE_DIR = Path(__file__).resolve().parent.parent / ".state"

""" ═══════════════════════════ 2. 传输层 ═════════════════════════════════════ """

LOGGER = logging.getLogger("ate.api")
LOGGER.addHandler(logging.NullHandler())

""" 绕过系统代理（http_proxy 等环境变量）：直连产线内网服务端，代理只会劫持流量、
把代理错误误判成服务端故障（5xx 可重试）/ 掩盖真实网络拓扑。 """
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_config_version() -> str:
    """从工程 config.toml 的 [system].version 读取上位机程序版本。

    config.toml 与本文件同属 common/ 包（../config/config.toml）。用逐行窄解析
    只提取这一个键——api.py 保持零第三方依赖，不引入 tomlkit；文件不存在、
    [system] 段缺失或解析失败一律返回空串，由调用方落默认值。
    """
    cfg = Path(__file__).resolve().parent.parent / "config" / "config.toml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return ""
    in_system = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_system = line == "[system]"
            continue
        if in_system:
            m = re.match(r'version\s*=\s*(.+)$', line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return ""


""" 上位机程序版本：服务端按机台留档，用于排查"同机型结果不可比"的版本漂移。
来源 = config.toml 的 [system].version；读不到 → "unknown"（宁可暴露未知，
也不静默填一个假版本让追溯失真）。 """
APP_VERSION = _read_config_version() or "unknown"
DEFAULT_TIMEOUT = 15.0

RESULT_PASS, RESULT_FAIL, RESULT_SKIP = "PASS", "FAIL", "SKIP"
OUTCOME_MAP = {"passed": RESULT_PASS, "failed": RESULT_FAIL, "skipped": RESULT_SKIP, "error": RESULT_FAIL}

""" 断网时本地最多缓存多少条未上报断点 """
MAX_PENDING_ITEMS = 500

""" 需要立即停机（结果作废、必须重新进站）的错误码 """
FATAL_LOCK_CODES = ("lock_invalid", "lock_expired")

""" 防呆拦截：重试无意义，必须人工介入 """
GATE_CODES = (
    "missing_prereq", "station_already_passed", "station_not_in_process",
    "station_ambiguous", "case_id_mismatch", "missing_mandatory",
    "model_mismatch", "model_not_registered", "firmware_mismatch",
    "product_locked", "product_scrapped", "client_not_bound", "client_not_registered",
)


class ApiError(Exception):
    """服务端错误响应包 + 网络故障的统一封装（网络不可达 = status 0 / network_error）。

    分类属性决定调用方的处置：is_retryable → 重试；is_lock_invalid → 立即停机；
    is_gate_blocked → 人工介入；is_lock_conflict → 可按 lock_idle_sec() 自适应等待。
    """

    def __init__(self, status: int, code: str, message: str,
                 exit_code: int = 0, data: Optional[Any] = None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = int(status)
        self.code = code
        self.exit_code = int(exit_code)
        self.data = data or {}

    @classmethod
    def network(cls, exc: BaseException) -> "ApiError":
        return cls(0, "network_error", str(exc) or exc.__class__.__name__)

    @property
    def is_network(self) -> bool:
        return self.status == 0

    @property
    def is_lock_invalid(self) -> bool:
        return self.code in FATAL_LOCK_CODES

    @property
    def is_lock_conflict(self) -> bool:
        return self.status == 409 and self.code == "lock_conflict"

    @property
    def is_gate_blocked(self) -> bool:
        return self.code in GATE_CODES

    @property
    def is_retryable(self) -> bool:
        return self.is_network or self.status >= 500

    def lock_idle_sec(self) -> Optional[int]:
        """lock_conflict 时服务端回传的"锁已失联多久"，用于估算接管等待时间。"""
        value = (self.data or {}).get("lock_idle_sec")
        return int(value) if isinstance(value, (int, float)) else None


def _decode_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(0, "invalid_response", f"响应不是合法 JSON：{exc}") from exc


def _safe_decode(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


class HttpClient:
    """最小 HTTP 客户端（urllib）。仅对"网络故障/5xx"做指数退避重试；4xx 直接抛出。"""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = DEFAULT_TIMEOUT,
                 retries: int = 2, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(0, retries)
        self.token = token

    def request(self, method: str, path: str, body: Optional[dict] = None,
                params: Optional[dict] = None, token: Optional[str] = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None

        last_error: Optional[ApiError] = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            if self.api_key:
                req.add_header("X-API-Key", self.api_key)
            bearer = token or self.token
            if bearer:
                req.add_header("Authorization", f"Bearer {bearer}")

            try:
                with _OPENER.open(req, timeout=self.timeout) as resp:
                    return _decode_json(resp.read())
            except urllib.error.HTTPError as exc:
                payload = _safe_decode(exc.read())
                error = ApiError(exc.code, payload.get("code", "http_error"),
                                 payload.get("message", str(exc)),
                                 payload.get("exit_code", 0), payload.get("data"))
                if not error.is_retryable or attempt == self.retries:
                    raise error
                last_error = error
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                error = ApiError.network(exc)
                if attempt == self.retries:
                    raise error
                last_error = error

            backoff = min(0.2 * (2 ** attempt), 2.0)
            LOGGER.warning("%s %s 失败（%s），%.1fs 后重试", method, path, last_error, backoff)
            time.sleep(backoff)

        raise last_error or ApiError(0, "unknown_error", "request failed")

    def data(self, method: str, path: str, **kw) -> dict:
        """取响应包的 data 段；服务层保证 ok=true 时 data 必然存在。"""
        return self.request(method, path, **kw).get("data") or {}


def map_outcome(outcome: str) -> str:
    """pytest rep.outcome → 服务端 result 枚举（未知一律按 SKIP，避免漏测误判为通过）。"""
    return OUTCOME_MAP.get((outcome or "").strip().lower(), RESULT_SKIP)


""" ═══════════════════════════ 3. 领域层 ═════════════════════════════════════ """


@dataclass
class SessionState:
    """一次进站的上下文。必须持久化——崩溃重启后靠它恢复并续测。"""

    sn: str = ""
    session_id: str = ""
    lock_token: str = ""
    attempt: int = 1
    station_id: str = ""
    """ 出站幂等凭据 """
    checkout_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    completed_case_ids: List[str] = field(default_factory=list)
    mandatory_case_ids: List[str] = field(default_factory=list)
    cursor: Dict[str, Any] = field(default_factory=dict)
    """ 断网期间待补传的断点 """
    pending_items: List[dict] = field(default_factory=list)

    def save(self, path: Path) -> None:
        """原子落盘（临时文件 + os.replace）。写入失败绝不中断测试——续测只是优化。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            LOGGER.warning("断点文件写入失败（不影响测试）：%s", exc)

    @classmethod
    def load(cls, path: Path) -> Optional["SessionState"]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        """ type: ignore[attr-defined] """
        known = {f for f in cls.__dataclass_fields__}
        try:
            return cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError:
            return None


class HeartbeatThread(threading.Thread):
    """后台保活：按服务端下发间隔刷新 lock_last_seen_at。

    失锁（holding_lock=false 或 403 lock_invalid）→ 置 lost_lock 并触发宿主回调后退出。
    """

    def __init__(self, client: "AteClient", interval: float):
        super().__init__(daemon=True, name=f"ate-heartbeat-{client.client_id}")
        self.client = client
        self.interval = max(1.0, interval)
        """ 不能命名为 _stop：会覆盖 threading.Thread 内部同名方法，导致 join() 崩溃 """
        self._stop_event = threading.Event()
        self.last_error: Optional[str] = None
        self.lost_lock = False

    def _notify_lost(self, reason: str) -> None:
        self.lost_lock = True
        LOGGER.warning("lost lock: %s -> stop testing now", reason)
        callback = getattr(self.client, "on_lost_lock", None)
        if callback:
            try:
                callback(reason)
            except Exception:
                """ 回调异常绝不能拖垮心跳线程 """
                LOGGER.exception("on_lost_lock callback raised")

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                data = self.client.heartbeat()
                if not data.get("holding_lock"):
                    self._notify_lost("server says holding_lock=false (lock taken over or session aborted)")
                    break
            except ApiError as exc:
                self.last_error = str(exc)
                if exc.is_lock_invalid:
                    self._notify_lost(f"heartbeat rejected: {exc.code}")
                    break
                LOGGER.debug("心跳失败：%s", exc)
            except Exception as exc:
                """ 兜底：心跳线程永远不能拖垮主流程 """
                self.last_error = str(exc)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)


class AteClient:
    """通道一客户端：/api/v1/*，X-API-Key 鉴权。生命周期：resolve → check_in →
    heartbeat(后台) / checkpoint(逐用例) → check_out / release。"""

    def __init__(self, base_url: str, api_key: str = "", *, client_id: str,
                 app_version: str = APP_VERSION, state_file: Optional[Path] = None,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = 2,
                 heartbeat_factor: float = 0.5,
                 on_lost_lock: Optional[Callable[[str], None]] = None):
        self.http = HttpClient(base_url, api_key, timeout=timeout, retries=retries)
        self.client_id = client_id
        self.app_version = app_version
        self.state_file = state_file
        self.heartbeat_factor = heartbeat_factor
        self.on_lost_lock = on_lost_lock
        self.state: Optional[SessionState] = None
        self._hb: Optional[HeartbeatThread] = None

    @property
    def lost_lock(self) -> bool:
        """锁是否已被服务端判失效（被接管/运维中止）。True → 必须停止测试。"""
        return bool(self._hb and self._hb.lost_lock)

    def __enter__(self) -> "AteClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop_heartbeat()

    """ ---- 生命周期 ---- """

    def _detect_local_ip(self) -> str:
        """探测本机访问 ATE 服务端所用的内网 IP。

        UDP socket connect 不会真正发包，仅让协议栈按路由表选出出口地址；
        探测目标就是服务端本身，选出的必然是与其同网段、真实可达的 IP
        （而不是 127.0.0.1 之类的回环/占位值）。探测失败返回空串。
        """
        try:
            parsed = urllib.parse.urlparse(self.http.base_url)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, port))
                return s.getsockname()[0]
        except OSError:
            return ""

    def resolve(self, ip_address: str = "") -> dict:
        """机台身份上报与工位反查（首次调用自动注册，工位需由 Web 端补录）。

        ip_address 缺省时自动探测本机与服务端同网段的真实 IP 并上报，
        保证 Web 端「机台管理」的接入 IP 列与实际一致。
        """
        data = self.http.data(
            "POST", "/api/v1/client/resolve",
            body={"client_id": self.client_id,
                  "ip_address": ip_address or self._detect_local_ip(),
                  "app_version": self.app_version},
        )
        if not data.get("bound"):
            LOGGER.warning("机台 %s 尚未绑定工位，请在 Web 端「机台管理」补录", self.client_id)
        return data

    def check_in(self, sn: str, product_model: str, firmware: str,
                 case_ids: Optional[Sequence[str]] = None, resume: bool = True) -> SessionState:
        """进站：建档 + 防跳站/防复测/固件/用例ID 卡控 + 领取工位锁。

        断点文件存在且 SN 一致时自动携带 resume_session_id（attempt+1，跳过已完成）。
        """
        state_path = self._state_path_for(sn)
        """ 重复进站前先回收旧心跳线程，否则两个线程会同时给同一 state 打心跳 """
        self.stop_heartbeat()
        previous = SessionState.load(state_path) if (resume and state_path is not None) else None
        payload: Dict[str, Any] = {
            "client_id": self.client_id,
            "sn": sn,
            "product_model": product_model,
            "firmware": firmware,
            "app_version": self.app_version,
        }
        if case_ids is not None:
            payload["case_ids"] = list(case_ids)
        if previous is not None and previous.sn == sn and previous.session_id:
            payload["resume_session_id"] = previous.session_id

        data = self.http.data("POST", "/api/v1/client/check-in", body=payload)
        resume_info = data.get("resume") or {}

        self.state = SessionState(
            sn=sn,
            session_id=data.get("session_id") or "",
            lock_token=data.get("lock_token") or "",
            attempt=int(data.get("attempt") or 1),
            station_id=data.get("station_id") or "",
            checkout_id=(previous.checkout_id if previous and previous.sn == sn else uuid.uuid4().hex),
            completed_case_ids=list(resume_info.get("completed_case_ids") or []),
            mandatory_case_ids=[r.get("case_id") for r in (data.get("rules") or [])
                                if r.get("is_mandatory", True)],
            cursor=dict(resume_info.get("cursor") or {}),
            pending_items=list(previous.pending_items) if previous and previous.sn == sn else [],
        )
        self._persist()

        if data.get("takeover"):
            LOGGER.warning("本次进站接管了 %s 持有的锁", data.get("takeover_from"))

        interval = float(data.get("heartbeat_interval_sec") or 30) * self.heartbeat_factor
        self._hb = HeartbeatThread(self, interval)
        self._hb.start()
        return self.state

    def heartbeat(self) -> dict:
        if not self.state:
            return {}
        return self.http.data(
            "POST", "/api/v1/client/heartbeat",
            body={"client_id": self.client_id, "sn": self.state.sn, "lock_token": self.state.lock_token},
        )

    def checkpoint(self, items: Sequence[dict]) -> int:
        """增量上报已完成用例（按 case_id 幂等覆盖），返回服务端合并后的总数。

        网络失败不抛异常：items 进入本地待补传队列落盘，下次上报或出站时自动补发。
        锁失效（lock_invalid）必须抛出——结果作废，宿主应据此停机。
        """
        if not self.state or not items:
            return 0

        buffered = list(self.state.pending_items) + [dict(i) for i in items if i.get("case_id")]
        if len(buffered) > MAX_PENDING_ITEMS:
            buffered = buffered[-MAX_PENDING_ITEMS:]

        try:
            data = self.http.data(
                "POST", "/api/v1/client/checkpoint",
                body={"client_id": self.client_id, "sn": self.state.sn,
                      "session_id": self.state.session_id, "lock_token": self.state.lock_token,
                      "items": buffered, "cursor": self.state.cursor},
            )
        except ApiError as exc:
            if exc.is_lock_invalid:
                raise
            """ 断网/5xx：本地缓存，不阻断测试 """
            self.state.pending_items = buffered
            self._persist()
            LOGGER.warning("断点上报失败（%s），已缓存 %d 条待补传", exc.code, len(buffered))
            return len(self.state.completed_case_ids)

        self.state.pending_items = []
        self.state.completed_case_ids = list(data.get("completed_case_ids") or [])
        self._persist()
        return int(data.get("merged_count") or 0)

    def check_out(self, items: Sequence[dict], duration_ms: int = 0, reason: str = "") -> dict:
        """出站落库并返回 ACK（须校验 acknowledged 才允许拔线流转）。

        本地待补传断点自动并入本次提交；失败时保留断点文件（checkout_id 不丢，
        凭同一 ID 重试可被服务端幂等回放，换新 ID 重发会产生双账）。
        """
        if not self.state:
            raise RuntimeError("check_out before check_in")
        self.stop_heartbeat()

        merged = list(items)
        seen = {i.get("case_id") for i in merged}
        for pending in self.state.pending_items:
            if pending.get("case_id") and pending["case_id"] not in seen:
                merged.append(pending)
                seen.add(pending["case_id"])

        data = self.http.data(
            "POST", "/api/v1/client/check-out",
            body={"client_id": self.client_id, "sn": self.state.sn,
                  "checkout_id": self.state.checkout_id, "lock_token": self.state.lock_token,
                  "items": merged, "duration_ms": int(duration_ms or 0), "reason": reason},
        )
        self._clear_persisted()
        return data

    def release(self, reason: str = "") -> dict:
        """主动放弃工位锁（优雅退出/放弃本次测试）。不计产品失败。"""
        if not self.state:
            return {}
        self.stop_heartbeat()
        data = self.http.data(
            "POST", "/api/v1/client/release",
            body={"client_id": self.client_id, "sn": self.state.sn,
                  "lock_token": self.state.lock_token, "reason": reason},
        )
        """ 与 check_out 同口径：成功才清断点文件 """
        self._clear_persisted()
        return data

    def ack(self, sn: Optional[str] = None, checkout_id: Optional[str] = None) -> bool:
        """上传闭环校验：确认服务端已落库。命中返回 True。"""
        target_sn = sn or (self.state.sn if self.state else "")
        target_id = checkout_id or (self.state.checkout_id if self.state else "")
        if not target_sn or not target_id:
            return False
        try:
            self.http.request("GET", "/api/v1/client/ack",
                              params={"sn": target_sn, "checkout_id": target_id})
            return True
        except ApiError:
            return False

    def stop_heartbeat(self) -> None:
        if self._hb is not None:
            self._hb.stop()
            self._hb = None

    """ ---- 内部 ---- """

    def _state_path_for(self, sn: Optional[str] = None) -> Optional[Path]:
        """断点文件路径：state_file 传目录 → 按 SN 分文件；传 .json 文件 → 单文件模式。"""
        if not self.state_file:
            return None
        if self.state_file.suffix:
            return self.state_file
        return self.state_file / f".ate_state_{sn or 'unknown'}.json"

    def _persist(self) -> None:
        path = self._state_path_for(self.state.sn if self.state else None)
        if path and self.state:
            self.state.save(path)

    def _clear_persisted(self) -> None:
        path = self._state_path_for(self.state.sn if self.state else None)
        if path:
            try:
                path.unlink()
            except OSError:
                pass


""" ═══════════════════ 4. 会话状态机（pytest 无关，纯类接口） ═══════════════════ """


class AteExit(Exception):
    """会话状态机的"干净终止"决策（防呆拦截/锁失效/网络不可达/必测未全过等）。

    宿主捕获后转 pytest.exit(e.message, returncode=e.returncode)。
    """

    def __init__(self, message: str, returncode: int):
        super().__init__(message)
        self.message = message
        self.returncode = returncode


def create_client(url: Optional[str] = None) -> Optional[AteClient]:
    """创建 ATE 客户端。
    优先使用传入的主机/IP（拼接为 http://{url}:8112）；缺省时回退到配置区的 ATE_URL。
    最终服务端地址为空（未对接）时返回 None。
    """
    base_url = f"http://{url}:8112" if url else ATE_URL
    if not base_url:
        return None
    return AteClient(
        base_url, ATE_KEY, client_id=CLIENT_ID, state_file=STATE_DIR,
        on_lost_lock=lambda reason: threading.interrupt_main(),
    )


def _items_from(rows: List[dict]) -> List[dict]:
    """结果行 → checkpoint / 出站 items（message 截断 500）。"""
    return [{"case_id": r["nodeid"], "result": map_outcome(r["result"]),
             "duration_ms": int(float(r["duration"] or 0) * 1000),
             "message": (r["exception"] or "")[:500] or None}
            for r in rows]


class AteRun:
    """一次 pytest 运行的 ATE 会话状态机：进站 → 逐用例收集+断点 → 收尾判定/出站。

    用法（conftest）：
        run = AteRun(create_client())        # 未对接时 client 为 None，全部方法旁路
        run.start(request.session.items, sn, model, fw)  # 会话夹具 yield 前
        run.collect(item, rep)               # 现有 pytest_runtest_makereport 中调用
        run.gate()                           # 每用例收尾（function 级夹具 yield 后）
        run.finish()                         # 会话夹具 yield 后

    异常约定：AteExit = 状态机的干净终止决策（宿主转 pytest.exit）；
    ApiError = 非 ATE 决策的错误（原样冒泡）。
    """

    def __init__(self, client: Optional[AteClient]):
        """ None = 未对接（ATE_URL 留空），全部方法旁路 """
        self.client = client

        """ 未上报的结果行（checkpoint 成功后清空） """
        self.rows: List[dict] = []

        """ nodeid → outcome（收尾判定用，整轮保留） """
        self.results: Dict[str, str] = {}

        self.sn = ""
        self.mandatory: set = set()
        self.t0 = 0.0

    """ ---- 进站 ---- """

    def start(self, items: list, sn: str, model: str, fw: str) -> set:
        """进站 + 计算续测跳过清单。返回需跳过的 nodeid 集合（宿主负责打 skip 标记）。

        items = request.session.items；身份由 *IDN? 读出后传入。
        """
        if self.client is None:
            return set()
        sn = (sn or "").strip()
        model = (model or "").strip()
        if not sn or not model:
            raise AteExit("ATE 进站失败：*IDN? 未读到序列号/机型", returncode=1)
        self.sn = sn

        """ 电脑名机台首次使用自动注册（未绑定态）；未绑定会被 check-in 防呆拦截并提示 """
        try:
            self.client.resolve()
        except ApiError as exc:
            if exc.is_network:
                raise AteExit(f"ATE 进站失败：服务端不可达（{exc}），请检查网络", returncode=4)
            raise

        """ 进站必须携全量 collection（含已完成用例），否则 case_id_mismatch 拦截；
        pytest -k 的部分运行同样被拦（防漏测属预期），调试请跑全量 """
        case_ids = [item.nodeid for item in items]
        state = None
        for _ in range(3):
            try:
                state = self.client.check_in(sn, model, fw, case_ids=case_ids)
                break
            except ApiError as exc:
                if exc.is_lock_conflict:
                    """ 自适应等待：心跳新鲜 = 正被其他机台测试 → 立即退出；
                    僵尸锁 = 只等 Sweeper 回收所需的精确时间 """
                    idle = exc.lock_idle_sec()
                    if idle is not None and idle < 30:
                        raise AteExit("ATE 进站失败：该件正在其他机台测试中，请等待其完成或走维修处置",
                                      returncode=4)
                    wait = 15 if idle is None else min(max(120 - idle + 10, 10), 130)
                    time.sleep(wait)
                    continue
                if exc.is_gate_blocked:
                    raise AteExit(f"ATE 进站被防呆拦截: {exc}", returncode=1)
                if exc.is_network:
                    raise AteExit(f"ATE 进站失败：服务端不可达（{exc}），请检查网络", returncode=4)
                raise
        if state is None:
            raise AteExit("ATE 进站失败：锁一直未释放，请先做重测处置或强制解锁", returncode=4)

        self.mandatory = set(state.mandatory_case_ids)
        self.t0 = time.monotonic()

        """ 续测跳过：只跳过"上次已 PASS"的；FAIL 的（重跑清单里）本轮重跑，
        通过后 checkpoint 按 case_id 覆盖，FAIL 被新结果顶掉 """
        done_pass = set(state.completed_case_ids) - set(self._retry_failed())
        return done_pass

    """ ---- 逐用例 ---- """

    def collect(self, item, rep) -> None:
        """由 pytest_runtest_makereport 钩子调用（放在钩子末尾之前均可，内部自行过滤）。"""
        if self.client is None:
            return

        """ setup/teardown 失败也必须收集为 FAIL：异常用例若"凭空消失"，
        出站会触发 missing_mandatory 把整个工位卡死 """
        if rep.when == "call" or (rep.when in ("setup", "teardown") and rep.failed):
            self.rows.append({
                "nodeid": item.nodeid,
                "duration": rep.duration,
                "result": rep.outcome,
                "exception": str(rep.longrepr) if rep.failed else "",
            })
            self.results[item.nodeid] = rep.outcome

    def gate(self) -> None:
        """每用例收尾：checkpoint + 失锁自检。"""
        if self.client is None:
            return
        if self.rows:
            try:
                """ 断网自动进待补传队列，勿自行包重试；lock_invalid 必须抛出
                （结果作废）——状态机转为 AteExit 让宿主干净停机，否则后续用例
                都会带原始异常栈报 ERROR """
                self.client.checkpoint(_items_from(self.rows))
            except ApiError as exc:
                if exc.is_lock_invalid:
                    raise AteExit("ATE 锁已失效：会话被运维中止", returncode=3)
                raise
            self.rows.clear()
        if self.client.lost_lock:
            raise AteExit("ATE 锁已失效：会话被运维中止", returncode=3)

    """ ---- 收尾 ---- """

    def finish(self) -> None:
        """收尾判定：必测有 FAIL → 不出站，写重跑清单退出；全绿 → 出站（幂等重试）。"""
        if self.client is None:
            return
        failed_now = {nid for nid, res in self.results.items() if res != "passed"}
        if failed_now & self.mandatory:
            rp = self._retry_path()
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(json.dumps(sorted(failed_now)), encoding="utf-8")
            raise AteExit(f"ATE 必测未全过: {sorted(failed_now & self.mandatory)}；"
                          f"已保留断点不出站，重跑将只执行未通过项", returncode=2)
        self._retry_path().unlink(missing_ok=True)

        """ 全量结果由服务端从会话断点合并；此处提交本轮行为空即可（断点已齐），
        但为防御断点意外缺失，仍把内存里未上报的行带上 """
        for attempt in range(6):
            try:
                ack = self.client.check_out(_items_from(self.rows),
                                            duration_ms=int((time.monotonic() - self.t0) * 1000))
                if not ack.get("acknowledged"):
                    raise AteExit("ATE 未拿到落库回执，禁止流转", returncode=1)
                return
            except ApiError as exc:
                if exc.is_gate_blocked:
                    raise AteExit(f"ATE 出站被防呆拦截: {exc}", returncode=1)
                if self.client.lost_lock or exc.is_lock_invalid:
                    raise AteExit("ATE 会话已被运维中止，停止测试", returncode=3)
                time.sleep(min(2 ** attempt, 30))
            except Exception:
                if self.client.lost_lock:
                    raise AteExit("ATE 会话已被运维中止，停止测试", returncode=3)
                time.sleep(min(2 ** attempt, 30))
        raise AteExit("ATE 出站重试后仍未成功，请检查网络或联系运维", returncode=4)

    """ ---- 重跑清单（与 SDK 断点文件同目录，conftest 无需关心） ---- """

    def _retry_path(self) -> Path:
        return STATE_DIR / f".retry_{self.sn}.json"

    def _retry_failed(self) -> List[str]:
        """上一轮失败清单（不出站留下的）。无文件 = 无需重跑。"""
        p = self._retry_path()
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []