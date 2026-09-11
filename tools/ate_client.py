"""ATE Manager 上位机接入 SDK（仅依赖 Python 标准库）+ 端到端演示脚本。

定位
    本文件是**参考实现与联调工具**，不是产线测试执行器：它不驱动仪器、不执行真实用例，
    只把「上位机 ↔ 服务端」的契约跑通。产线 pytest 工程（conftest）可直接 `import` 后复用，
    或照抄其调用顺序。完整接口契约、错误码与时序见doc/API.md。

兼容性
    Python 3.8+，仅用标准库（urllib / json / threading / dataclasses），
    产线工控机无需安装任何第三方依赖。Windows GBK 控制台打印中文自动降级，不会抛异常。

被测件身份从哪来
    sn / product_model / firmware 一律由底层 SCPI `*IDN?` 直读解析
    （见 `identity_from_report()`），全流程无人工扫码、无手工建档。

用例ID（case_id）
    与服务端 `station_items` 保持一致，取 pytest nodeid：
        tests/test_cal_param.py::TestAmp::test_amp_cal

最小调用序列
    cli = AteClient(base_url, api_key, client_id="SZ-L1-CAL-01", state_file=Path(".state.json"))
    cli.resolve()                                   # 机台身份上报（可选）
    state = cli.check_in(sn, model, firmware, case_ids=case_ids)
    for case_id in case_ids:                        # 每跑完一个用例即上报断点
        if case_id in state.completed_case_ids:     # 崩溃续测：跳过已完成
            continue
        cli.checkpoint([run(case_id)])
    ack = cli.check_out(items, duration_ms=...)     # 必须收到 ACK 才允许拔线

运行演示
    python tools/ate_client.py --api-key <V1_API_KEY> demo        # 全部场景
    python tools/ate_client.py --api-key <V1_API_KEY> normal      # 正常全流程
    python tools/ate_client.py --api-key <V1_API_KEY> gate        # 防跳站拦截（需求 2）
    python tools/ate_client.py --api-key <V1_API_KEY> resume      # 崩溃 → 断点续测
    python tools/ate_client.py --api-key <V1_API_KEY> takeover    # 崩溃 → 备用机台接管
    python tools/ate_client.py --api-key <V1_API_KEY> sweep       # 孤儿锁回收（留一把锁在服务端）
    python tools/ate_client.py --api-key <V1_API_KEY> resolve     # 只做机台身份上报
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ApiError",
    "AteClient",
    "HttpClient",
    "SessionState",
    "CAL_PARAM_CASES",
    "admin_login",
    "identity_from_report",
    "map_outcome",
    "to_ate_items",
]

LOGGER = logging.getLogger("ate.client")
LOGGER.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------
# 常量：与服务端 errors.py / models.py 保持一致
# ---------------------------------------------------------------------
APP_VERSION = "ate-client/1.0"
DEFAULT_TIMEOUT = 15.0
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_SKIP = "SKIP"

# rep.outcome（pytest） → 服务端 result 枚举
OUTCOME_MAP = {"passed": RESULT_PASS, "failed": RESULT_FAIL, "skipped": RESULT_SKIP, "error": RESULT_FAIL}

# 网络抖动时本地最多缓存多少条未上报断点（防止长时间断网把内存/磁盘写满）
MAX_PENDING_ITEMS = 500

# 需要立即停机（结果作废、必须重新进站）的错误码
FATAL_LOCK_CODES = ("lock_invalid", "lock_expired")
# 防呆拦截：重试无意义，必须人工介入
GATE_CODES = (
    # 工艺/流程不匹配
    "missing_prereq",
    "station_already_passed",
    "station_not_in_process",
    "case_id_mismatch",
    "missing_mandatory",
    # 被测件身份与状态
    "model_mismatch",
    "model_not_registered",
    "firmware_mismatch",
    "product_locked",
    "product_scrapped",
    # 机台侧配置缺失：同样只能由运维处理，重试没有意义
    "client_not_bound",
    "client_not_registered",
)

CAL_PARAM_CASES = [
    "tests/test_cal_param.py::TestAmp::test_amp_cal",
    "tests/test_cal_param.py::TestPhase::test_phase_cal",
    "tests/test_cal_param.py::TestAmpDc::test_amp_dc_1m",
    "tests/test_cal_param.py::TestBandwidth::test_bw_hi_z",
    "tests/test_cal_param.py::TestFastEdge::test_fast_edge_1m",
]


# ---------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------
class ApiError(Exception):
    """服务端统一响应包异常（EnvelopeOut）以及网络故障的统一封装。

    网络不可达时用 `status=0` + `code="network_error"` 表示，
    调用方只需 `except ApiError` 即可覆盖全部失败路径。
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        exit_code: int = 0,
        data: Optional[Any] = None,
    ):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = int(status)
        self.code = code
        self.exit_code = int(exit_code)
        self.data = data or {}

    @classmethod
    def network(cls, exc: BaseException) -> "ApiError":
        return cls(0, "network_error", str(exc) or exc.__class__.__name__)

    # ---- 分类：决定"重试 / 停机 / 报障" ----
    @property
    def is_network(self) -> bool:
        return self.status == 0

    @property
    def is_lock_invalid(self) -> bool:
        """锁已被接管或已失效：必须停机，本次结果作废，需重新进站。"""
        return self.code in FATAL_LOCK_CODES

    @property
    def is_lock_conflict(self) -> bool:
        """锁被其他机台持有：可等待 grace 后重试，或由运维强制解锁后接管。"""
        return self.status == 409 and self.code == "lock_conflict"

    @property
    def is_gate_blocked(self) -> bool:
        """防呆拦截：重试无意义，需人工介入（换工序 / 刷固件 / 送修）。"""
        return self.code in GATE_CODES

    @property
    def is_retryable(self) -> bool:
        """网络抖动或服务端 5xx 才可重试；4xx 一律不重试。"""
        return self.is_network or self.status >= 500

    def lock_idle_sec(self) -> Optional[int]:
        """lock_conflict 时服务端回传的"已失联多久"，可用于估算接管等待时间。"""
        value = (self.data or {}).get("lock_idle_sec")
        return int(value) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------
class HttpClient:
    """最小 HTTP 客户端（urllib，零第三方依赖）。

    重试策略：仅对"网络故障"与"服务端 5xx"做指数退避重试；
    4xx 是业务判定（防呆拦截/锁冲突），重试没有意义，直接抛给调用方。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 2,
        token: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(0, retries)
        self.token = token

    # 统一出口：返回完整 EnvelopeOut 字典
    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        token: Optional[str] = None,
    ) -> dict:
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
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return _decode_json(resp.read())
            except urllib.error.HTTPError as exc:
                payload = _safe_decode(exc.read())
                error = ApiError(
                    exc.code,
                    payload.get("code", "http_error"),
                    payload.get("message", str(exc)),
                    payload.get("exit_code", 0),
                    payload.get("data"),
                )
                if not error.is_retryable or attempt == self.retries:
                    raise error
                last_error = error
            except (urllib.error.URLError, OSError, TimeoutError) as exc:  # 连接失败/超时/解析失败
                error = ApiError.network(exc)
                if attempt == self.retries:
                    raise error
                last_error = error

            backoff = min(0.2 * (2**attempt), 2.0)
            LOGGER.warning("%s %s 失败（%s），%.1fs 后重试", method, path, last_error, backoff)
            time.sleep(backoff)

        raise last_error or ApiError(0, "unknown_error", "request failed")

    def data(self, method: str, path: str, **kw) -> dict:
        """取响应包的 data 段；服务层保证 ok=true 时 data 必然存在。"""
        return self.request(method, path, **kw).get("data") or {}


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


# ---------------------------------------------------------------------
# 与 pytest / 真实工程的衔接
# ---------------------------------------------------------------------
def map_outcome(outcome: str) -> str:
    """pytest `rep.outcome` → 服务端 result 枚举（未知一律按 SKIP，避免漏测误判为通过）。"""
    return OUTCOME_MAP.get((outcome or "").strip().lower(), RESULT_SKIP)


def identity_from_report(report: dict) -> Tuple[str, str, str]:
    """从 `*IDN?` 解析出的 report 字典里取 (sn, model, firmware)。"""
    return (
        (report.get("serialNumber") or "").strip(),
        (report.get("model") or "").strip(),
        (report.get("version") or "").strip(),
    )


def to_ate_items(report_data: Sequence[dict]) -> List[dict]:
    """把 conftest 中 `pytest_runtest_makereport` 收集到的列表转成出站 items。

    report["data"] 元素结构（与产线工程一致）：
        {"nodeid", "duration", "description", "result", "exception", "images", "sheets"}
    """
    items: List[dict] = []
    for row in report_data or []:
        duration = float(row.get("duration") or 0)
        items.append(
            {
                "case_id": row.get("nodeid", ""),
                "result": map_outcome(row.get("result") or ""),
                "values": {"duration_s": round(duration, 3)},
                "duration_ms": int(duration * 1000),
                "message": (row.get("exception") or "")[:500] or None,
                "item_name": row.get("description") or None,
            }
        )
    return items


# ---------------------------------------------------------------------
# 会话状态（崩溃后靠它续测）
# ---------------------------------------------------------------------
@dataclass
class SessionState:
    """一次进站的上下文。必须持久化——崩溃重启后靠它恢复并续测。"""

    sn: str = ""
    session_id: str = ""
    lock_token: str = ""
    attempt: int = 1
    station_id: str = ""
    checkout_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    completed_case_ids: List[str] = field(default_factory=list)
    cursor: Dict[str, Any] = field(default_factory=dict)
    pending_items: List[dict] = field(default_factory=list)

    def save(self, path: Path) -> None:
        """原子落盘。写入失败绝不能中断测试——续测只是优化，丢了大不了重跑。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:  # 杀毒/索引/备份占用文件在 Windows 上很常见
            LOGGER.warning("断点文件写入失败（不影响测试）：%s", exc)

    @classmethod
    def load(cls, path: Path) -> Optional["SessionState"]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        try:
            return cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError:
            return None


class HeartbeatThread(threading.Thread):
    """后台保活：按服务端下发的间隔刷新 `lock_last_seen_at`。

    心跳只证明"活着"，不保存进度；锁一旦被接管（403 lock_invalid）立即退出线程。
    """

    def __init__(self, client: "AteClient", interval: float):
        super().__init__(daemon=True, name=f"ate-heartbeat-{client.client_id}")
        self.client = client
        self.interval = max(1.0, interval)
        # 不能命名为 _stop：会覆盖 threading.Thread 内部同名方法，导致 join() 崩溃
        self._stop_event = threading.Event()
        self.last_error: Optional[str] = None
        self.lost_lock = False

    def _notify_lost(self, reason: str) -> None:
        """置停机标志并回调宿主——两者都只是"通知"，是否真的停由上位机决定。"""
        self.lost_lock = True
        LOGGER.warning("lost lock: %s -> stop testing now", reason)
        callback = getattr(self.client, "on_lost_lock", None)
        if callback:
            try:
                callback(reason)
            except Exception:  # 回调异常绝不能拖垮心跳线程
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
            except Exception as exc:  # 兜底：心跳线程永远不能拖垮主流程
                self.last_error = str(exc)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)


# ---------------------------------------------------------------------
# SDK
# ---------------------------------------------------------------------
class AteClient:
    """通道一客户端：`/api/v1/*`，X-API-Key 鉴权。

    典型用法（推荐用 with，保证心跳线程一定被回收）：

        with AteClient(url, key, client_id="SZ-L1-CAL-01", state_file=Path(".s.json")) as cli:
            state = cli.check_in(sn, model, fw, case_ids=cases)
            ...
            cli.check_out(items)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        client_id: str,
        app_version: str = APP_VERSION,
        state_file: Optional[Path] = None,
        admin_token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 2,
        heartbeat_factor: float = 0.5,
        on_lost_lock: Optional[Callable[[str], None]] = None,
    ):
        self.http = HttpClient(base_url, api_key, timeout=timeout, retries=retries, token=admin_token)
        self.client_id = client_id
        self.app_version = app_version
        self.state_file = state_file
        self.admin_token = admin_token
        self.heartbeat_factor = heartbeat_factor
        # 锁失效回调：运维中止会话 / 锁被接管时触发，上位机应据此停止后续用例
        self.on_lost_lock = on_lost_lock
        self.state: Optional[SessionState] = None
        self._hb: Optional[HeartbeatThread] = None

    @property
    def lost_lock(self) -> bool:
        """锁是否已被服务端判失效（被接管 / 运维中止会话）。True → 必须停止测试。

        典型用法（pytest 工程里每个用例之间自检）：

            if cli.lost_lock:
                pytest.exit("lock lost: aborted by operator", returncode=3)
        """
        return bool(self._hb and self._hb.lost_lock)

    # ---------------- 上下文管理 ----------------
    def __enter__(self) -> "AteClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop_heartbeat()

    # ---------------- 通道一：生命周期 ----------------
    def resolve(self, ip_address: str = "") -> dict:
        """机台身份上报与工位反查（首次调用自动注册，工位需由 Web 端补录）。"""
        data = self.http.data(
            "POST",
            "/api/v1/client/resolve",
            body={"client_id": self.client_id, "ip_address": ip_address, "app_version": self.app_version},
        )
        if not data.get("bound"):
            LOGGER.warning("机台 %s 尚未绑定工位，请在 Web 端「机台管理」补录", self.client_id)
        return data

    def check_in(
        self,
        sn: str,
        product_model: str,
        firmware: str,
        case_ids: Optional[Sequence[str]] = None,
        resume: bool = True,
    ) -> SessionState:
        """进站：建档 + 防跳站/防复测/固件/用例ID 卡控 + 领取工位锁。

        返回 SessionState（含 session_id / lock_token / 已完成的用例清单）。
        崩溃后再次调用会自动携带 `resume_session_id`，attempt+1 并跳过已完成用例。
        """
        previous = SessionState.load(self.state_file) if (resume and self.state_file) else None
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
            cursor=dict(resume_info.get("cursor") or {}),
            pending_items=list(previous.pending_items) if previous and previous.sn == sn else [],
        )
        self._persist()

        if data.get("takeover"):
            LOGGER.warning("本次进站接管了 %s 持有的锁", data.get("takeover_from"))
        if self.state.completed_case_ids:
            LOGGER.info(
                "续测 attempt=%d，跳过 %d 个已测用例", self.state.attempt, len(self.state.completed_case_ids)
            )

        interval = float(data.get("heartbeat_interval_sec") or 30) * self.heartbeat_factor
        self._hb = HeartbeatThread(self, interval)
        self._hb.start()
        return self.state

    def heartbeat(self) -> dict:
        """单次保活。通常由后台线程按 heartbeat_interval_sec 调用。"""
        if not self.state:
            return {}
        return self.http.data(
            "POST",
            "/api/v1/client/heartbeat",
            body={
                "client_id": self.client_id,
                "sn": self.state.sn,
                "lock_token": self.state.lock_token,
            },
        )

    def checkpoint(self, items: Sequence[dict], cursor: Optional[dict] = None) -> int:
        """增量上报已完成用例（按 case_id 幂等覆盖），返回服务端合并后的总数。

        网络失败不抛异常：items 进入本地待补传队列，下次上报或出站时自动补发。
        """
        if not self.state or not items:
            return 0

        buffered = list(self.state.pending_items) + [
            dict(item) for item in items if item.get("case_id")
        ]
        if len(buffered) > MAX_PENDING_ITEMS:
            buffered = buffered[-MAX_PENDING_ITEMS:]

        try:
            data = self.http.data(
                "POST",
                "/api/v1/client/checkpoint",
                body={
                    "client_id": self.client_id,
                    "sn": self.state.sn,
                    "session_id": self.state.session_id,
                    "lock_token": self.state.lock_token,
                    "items": buffered,
                    "cursor": cursor if cursor is not None else self.state.cursor,
                },
            )
        except ApiError as exc:
            if exc.is_lock_invalid:
                raise
            # 断网/5xx：本地缓存，不阻断测试
            self.state.pending_items = buffered
            self._persist()
            LOGGER.warning("断点上报失败（%s），已缓存 %d 条待补传", exc.code, len(buffered))
            return len(self.state.completed_case_ids)

        self.state.pending_items = []
        self.state.completed_case_ids = list(data.get("completed_case_ids") or [])
        if cursor is not None:
            self.state.cursor = dict(cursor)
        self._persist()
        return int(data.get("merged_count") or 0)

    def check_out(
        self,
        items: Sequence[dict],
        duration_ms: int = 0,
        reason: str = "",
    ) -> dict:
        """出站落库并返回 ACK。必须校验 `acknowledged` 才允许拔线流转（需求 1）。

        本地待补传的断点会自动并入本次提交，避免"已跑完但没上报"被判为漏测。
        网络重试请复用同一 `checkout_id`，服务端幂等回放，不会产生第二条账目。
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

        try:
            data = self.http.data(
                "POST",
                "/api/v1/client/check-out",
                body={
                    "client_id": self.client_id,
                    "sn": self.state.sn,
                    "checkout_id": self.state.checkout_id,
                    "lock_token": self.state.lock_token,
                    "items": merged,
                    "duration_ms": int(duration_ms or 0),
                    "reason": reason,
                },
            )
        finally:
            # 只清本地断点文件：内存中的 state 保留，便于调用方读取 attempt / checkout_id 做诊断
            self._clear_persisted()

        LOGGER.debug("出库 record_id=%s result=%s", data.get("record_id"), data.get("overall_result"))
        return data

    def release(self, reason: str = "") -> dict:
        """主动放弃工位锁（优雅退出 / 放弃本次测试）。不计产品失败。"""
        if not self.state:
            return {}
        self.stop_heartbeat()
        try:
            return self.http.data(
                "POST",
                "/api/v1/client/release",
                body={
                    "client_id": self.client_id,
                    "sn": self.state.sn,
                    "lock_token": self.state.lock_token,
                    "reason": reason,
                },
            )
        finally:
            self._clear_persisted()

    def ack(self, sn: Optional[str] = None, checkout_id: Optional[str] = None) -> bool:
        """上传闭环校验：确认服务端已落库。命中返回 True。"""
        target_sn = sn or (self.state.sn if self.state else "")
        target_id = checkout_id or (self.state.checkout_id if self.state else "")
        if not target_sn or not target_id:
            return False
        try:
            self.http.request("GET", "/api/v1/client/ack", params={"sn": target_sn, "checkout_id": target_id})
            return True
        except ApiError:
            return False

    def stop_heartbeat(self) -> None:
        """停止心跳线程。出站/释放前必须调用，避免与服务端状态竞争。"""
        if self._hb is not None:
            self._hb.stop()
            self._hb = None

    # ---------------- 通道二（运维/演示用，JWT） ----------------
    def admin_force_release(self, sn: str, reason: str) -> dict:
        """运维强制解锁：仅释放锁与会话，不改变印章与失败计数。"""
        return self.http.request(
            "POST",
            f"/api/admin/products/{sn}/force-release",
            body={"reason": reason},
            token=self.admin_token,
        )

    # ---------------- 内部 ----------------
    def _persist(self) -> None:
        if self.state_file and self.state:
            self.state.save(self.state_file)

    def _clear_persisted(self) -> None:
        """删除本地断点文件。一旦成功出库或主动释放，下次进站就是全新会话。"""
        if self.state_file:
            try:
                self.state_file.unlink()
            except OSError:
                pass


def admin_login(base_url: str, username: str, password: str) -> str:
    """通道二登录，返回 JWT（供 force-release 等运维接口使用）。"""
    data = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(f"{base_url.rstrip('/')}/api/auth/login", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


# ---------------------------------------------------------------------
# 演示（假用例，仅用于验证服务端契约）
# ---------------------------------------------------------------------
class SimulatedCrash(Exception):
    """模拟上位机进程崩溃：不发 release、不 check-out，锁遗留在服务端。"""


def fake_run_case(case_id: str, fail: bool = False) -> dict:
    return {
        "case_id": case_id,
        "result": RESULT_FAIL if fail else RESULT_PASS,
        "values": {"voltage_v": 3.3012, "limit_v": 3.3},
        "duration_ms": 1500,
        "message": "测量值超差" if fail else None,
    }


def run_station(
    cli: AteClient,
    *,
    sn: str,
    model: str,
    firmware: str,
    case_ids: Sequence[str],
    crash_after: Optional[int] = None,
) -> dict:
    """跑完一个工位：进站 → 逐用例上报断点 → 出站。返回 ACK。"""
    state = cli.check_in(sn, model, firmware, case_ids=case_ids)
    done = list(state.completed_case_ids)
    executed: List[str] = []
    try:
        for case_id in case_ids:
            if case_id in done:
                continue  # 崩溃续测：跳过崩溃前已跑完的
            cli.checkpoint([fake_run_case(case_id)], cursor={"step": len(done) + 1})
            done.append(case_id)
            executed.append(case_id)
            print(f"  · 上报断点 {case_id.split('::')[-1]} ({len(done)}/{len(case_ids)})", flush=True)
            if crash_after is not None and len(done) >= crash_after:
                raise SimulatedCrash("模拟上位机崩溃")
    except SimulatedCrash:
        print("  x 崩溃！断点已保存在服务端，重启后可续测", flush=True)
        raise
    except ApiError as exc:
        if exc.is_lock_invalid:
            print(f"  x 锁已失效（{exc.code}）：立即停机，结果作废，需重新进站", flush=True)
        raise
    return cli.check_out([fake_run_case(c) for c in executed], duration_ms=len(executed) * 1500)


def demo_normal(cli: AteClient, sn: str, model: str, firmware: str) -> None:
    print(f"\n=== 场景 1：正常全流程（SN={sn}）===", flush=True)
    station = cli.resolve().get("station_id")
    print(f"  机台 {cli.client_id} 绑定工位：{station}", flush=True)
    run_station(cli, sn=sn, model=model, firmware=firmware, case_ids=CAL_PARAM_CASES)
    print("  OK 出站成功，锁已释放", flush=True)


def demo_gate(base_url: str, api_key: str, sn: str, model: str, firmware: str) -> None:
    """需求 2：跳站卡控——未做前工序，直接进第二站应被 403 拦截。"""
    print(f"\n=== 场景 2：防跳站拦截（需求 2，SN={sn}）===", flush=True)
    second = AteClient(base_url, api_key, client_id="SZ-L1-CAL-02")
    try:
        second.check_in(sn, model, firmware, case_ids=CAL_PARAM_CASES)
        print("  !! 未被拦截，防跳站失效", flush=True)
    except ApiError as exc:
        print(f"  OK 已拦截：{exc.code} —— pytest 终止、治具不开电", flush=True)
        missing = (exc.data or {}).get("missing")
        if missing:
            print(f"     缺失前置工位：{missing}", flush=True)
    finally:
        second.stop_heartbeat()

    print(f"  → 先回到首站 {CAL_PARAM_CASES and 'CAL-PARAM'} 完成前工序", flush=True)
    first = AteClient(base_url, api_key, client_id="SZ-L1-CAL-01")
    try:
        run_station(first, sn=sn, model=model, firmware=firmware, case_ids=CAL_PARAM_CASES)
        print("  OK 首站通过，第二站闸门应放行", flush=True)
        run_station(
            second,
            sn=sn,
            model=model,
            firmware=firmware,
            case_ids=[
                "tests/test_cal_iface.py::TestNoise::test_noise",
                "tests/test_cal_iface.py::TestTouch::test_touch",
                "tests/test_cal_iface.py::TestAux::test_aux",
            ],
        )
        print("  OK 第二站正常出站", flush=True)
    finally:
        first.stop_heartbeat()
        second.stop_heartbeat()


def demo_resume(base_url: str, api_key: str, sn: str, model: str, firmware: str, state_file: Path) -> None:
    """崩溃 → 重启 → 断点续测（attempt+1，跳过已完成用例）。"""
    print(f"\n=== 场景 3：崩溃 → 断点续测（SN={sn}）===", flush=True)
    crashed = AteClient(base_url, api_key, client_id="SZ-L1-CAL-01", state_file=state_file)
    try:
        run_station(crashed, sn=sn, model=model, firmware=firmware, case_ids=CAL_PARAM_CASES, crash_after=1)
    except SimulatedCrash:
        pass
    finally:
        crashed.stop_heartbeat()

    print("  → 模拟重启：新建客户端，从本地断点文件恢复", flush=True)
    restarted = AteClient(base_url, api_key, client_id="SZ-L1-CAL-01", state_file=state_file)
    try:
        ack = run_station(restarted, sn=sn, model=model, firmware=firmware, case_ids=CAL_PARAM_CASES)
        attempt = restarted.state.attempt if restarted.state else 0
        print(
            f"  OK 续测出库 attempt={attempt}，checkpoint 补齐 {ack.get('checkpoint_merged_count', 0)} 个用例",
            flush=True,
        )
    finally:
        restarted.stop_heartbeat()


def demo_takeover(
    cli: AteClient, sn: str, model: str, firmware: str, backup_client_id: str
) -> None:
    """崩溃 → 运维强制解锁 → 备用机台接管；旧 token 出站被 fencing 拒绝。"""
    print(f"\n=== 场景 4：崩溃 → 备用机台接管（SN={sn}）===", flush=True)
    try:
        run_station(cli, sn=sn, model=model, firmware=firmware, case_ids=CAL_PARAM_CASES, crash_after=2)
    except SimulatedCrash:
        pass

    try:
        cli.admin_force_release(sn, "演示：原机台宕机，切换备用机台")
        print("  OK 运维已强制解锁", flush=True)
    except ApiError as exc:
        print(f"  !  强制解锁失败（{exc.code}），继续尝试接管", flush=True)

    backup = AteClient(cli.http.base_url, cli.http.api_key, client_id=backup_client_id)
    try:
        backup.check_in(sn, model, firmware, case_ids=CAL_PARAM_CASES)
        print(f"  OK 备用机台 {backup_client_id} 接管成功", flush=True)
    except ApiError as exc:
        print(f"  x  接管失败：{exc}", flush=True)
        return
    finally:
        backup.stop_heartbeat()

    print("  → 原机台恢复后拿着旧 token 出站（应被拒绝）", flush=True)
    try:
        cli.check_out([fake_run_case(c) for c in CAL_PARAM_CASES])
        print("  !! 未拒绝，fencing 失效", flush=True)
    except ApiError as exc:
        print(f"  OK 已拒绝：{exc.code} —— 数据未被污染", flush=True)


def demo_sweep(cli: AteClient, sn: str, model: str, firmware: str) -> None:
    """孤儿锁回收：持锁后不发心跳，等待服务端 Sweeper 自动释放。"""
    print(f"\n=== 场景 5：孤儿锁回收（SN={sn}）===", flush=True)
    cli.check_in(sn, model, firmware, case_ids=CAL_PARAM_CASES)
    print("  已持锁且停止心跳；等待 Sweeper 回收（周期 30s / 失联宽限 120s）", flush=True)
    print("  可在 Web 端「测试会话 → 僵尸锁」观察，或查询 GET /api/admin/sessions/zombie-locks", flush=True)
    cli.stop_heartbeat()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会抛 UnicodeEncodeError，需自适应。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ate_client",
        description="ATE Manager 上位机接入 SDK 演示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="完整契约见 doc/API.md",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="服务地址（默认 %(default)s）")
    parser.add_argument("--api-key", default="", help="X-API-Key（后端 V1_API_KEY）")
    parser.add_argument("--client-id", default="SZ-L1-CAL-01", help="本机台 ID")
    parser.add_argument("--backup-client-id", default="SZ-L1-CAL-09", help="备用机台 ID（接管场景）")
    parser.add_argument("--model", default="DPO4054B", help="被测机型（*IDN? 直读）")
    parser.add_argument("--firmware", default="V3.20", help="固件版本（*IDN? 直读）")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="admin123")
    parser.add_argument("--admin-token", default="", help="已有 JWT 时直接传入，跳过登录")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 SDK 调试日志")
    parser.add_argument(
        "scenario",
        nargs="?",
        default="demo",
        choices=["demo", "normal", "gate", "resume", "takeover", "sweep", "resolve"],
        help="演示场景（默认 demo）",
    )
    return parser


def main() -> int:
    _force_utf8_console()
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    state_file = Path(f".ate_session_{args.client_id}.json")
    cli = AteClient(
        args.base_url,
        args.api_key,
        client_id=args.client_id,
        state_file=state_file,
        admin_token=args.admin_token,
    )

    if args.scenario == "resolve":
        print(json.dumps(cli.resolve(), ensure_ascii=False, indent=2))
        return 0

    # 接管场景需要运维权限做强制解锁
    if not args.admin_token:
        try:
            cli.admin_token = admin_login(args.base_url, args.admin_user, args.admin_password)
            cli.http.token = cli.admin_token
        except Exception as exc:
            print(f"[warn] 管理端登录失败（{exc}），接管场景可能失败", flush=True)

    stamp = time.strftime("%H%M%S")
    try:
        if args.scenario in ("demo", "normal"):
            demo_normal(cli, f"C077{stamp}", args.model, args.firmware)
        if args.scenario in ("demo", "gate"):
            demo_gate(args.base_url, args.api_key, f"C076{stamp}", args.model, args.firmware)
        if args.scenario in ("demo", "resume"):
            demo_resume(args.base_url, args.api_key, f"C078{stamp}", args.model, args.firmware, state_file)
        if args.scenario in ("demo", "takeover"):
            demo_takeover(cli, f"C079{stamp}", args.model, args.firmware, args.backup_client_id)
        if args.scenario == "sweep":
            demo_sweep(cli, f"C080{stamp}", args.model, args.firmware)
    except ApiError as exc:
        print(f"\n[ERROR] {exc}", flush=True)
        return 1
    finally:
        cli.stop_heartbeat()
    return 0


if __name__ == "__main__":
    sys.exit(main())
