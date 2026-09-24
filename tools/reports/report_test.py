"""端到端验证：完工自动触发 → 插件生成 → MinIO → Worker PDF → MES（本地文件夹接收）。

用法：
    python tools/reports/report_test.py [SN]
    不带 SN：自动找「已完工、尚无报告任务」的候选
    带 SN（如 CNUD001375）：删除该 SN 的历史报告任务记录，让完工扫描重新自动入队
前提：
    - 后端 http://127.0.0.1:8000 运行中（带最新代码），MinIO 可达
    - tek_mso 规则存在且绑定 DPO1074；工厂库可达
    - 本机装有 WPS/Office（external PDF 模式转换用）

流程与校验：
    1. 起本地 MES 接收器：HTTP 收到 multipart 后把 PDF 落盘到 tools/reports/_mes_sink/，
       并把规则 mes_url 指向它（结束后恢复原值——含 Ctrl-C/异常，绝不残留脏配置）
    2. 确保 PDF Worker 在跑（没有则拉起，单实例互斥保证不重复）
    3. 等待：调度器扫描自动入队 → 插件生成 → MinIO 归档 → Worker 转换回传
       → MES 上传 → 归档清理
    4. 校验：任务 success、MES 文件夹收到两份合法 PDF、MES 产物已从 MinIO/本地
       清理（purged=true）、数据报告仍可下载

说明：带 SN 重触发模式只删除该 SN 的历史报告任务记录（完工扫描重新入队的必要
条件），不触碰产品/测试记录等其他数据；产品必须处于完工态（is_completed=True），
否则扫描永不入队——脚本开始前会校验并明确终止，而不是干等超时。
"""

import email
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from email import policy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8000"
MODEL = "DPO1074"
SINK_DIR = ROOT / "tools" / "reports" / "_mes_sink"
DEADLINE_SEC = 600
POLL_SEC = 5


def api(method, path, token=None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delete_job_rows(sn):
    """删除该 SN 的历史报告任务记录，让完工扫描重新自动入队（不动产品/测试数据）。"""
    sys.path.insert(0, str(ROOT / "backend"))
    os.chdir(ROOT / "backend")
    env = {}
    for line in (ROOT / "backend" / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip())
    for k, v in env.items():
        os.environ.setdefault(k, v)
    from app import models
    from app.database import SessionLocal

    db = SessionLocal()
    n = db.query(models.ReportJob).filter(models.ReportJob.sn == sn).delete(synchronize_session=False)
    db.commit()
    db.close()
    return n


class FolderSink:
    """本地文件夹版 MES：把 multipart 里的文件部分落盘到 SINK_DIR/<时间戳>/。"""

    def __init__(self):
        self.saved = []  # [(sn, [filename, ...])]
        self._server = None

    def start(self):
        outer = self
        run_dir = SINK_DIR / time.strftime("%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                ctype = self.headers.get("Content-Type", "")
                sn = ""
                saved = []
                try:
                    msg = email.message_from_bytes(
                        b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body,
                        policy=policy.default,
                    )
                    for part in msg.iter_parts():
                        if part.get_filename():
                            data = part.get_payload(decode=True) or b""
                            out = run_dir / part.get_filename()
                            out.write_bytes(data)
                            saved.append(out.name)
                        elif part.get_param("name") == "sn":
                            sn = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                except Exception as exc:  # 落盘失败必须让引擎判失败
                    payload = json.dumps({"success": False, "message": str(exc)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                outer.saved.append((sn, saved))
                payload = json.dumps({"success": True, "message": f"saved {saved}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/mes"


def _restore_rule(token, original_url):
    """把规则 mes_url 恢复为运行前原值（幂等，可安全重复调用）。"""
    try:
        api("PUT", "/api/admin/report-rules/tek_mso", token, body={
            "rule": "tek_mso", "models": ["DPO1074"], "mes_url": original_url,
            "auto_trigger": True, "enabled": True,
        })
        print("    规则 mes_url 已恢复")
    except Exception as exc:
        print(f"    [警告] mes_url 恢复失败，请手工检查规则：{exc}")


def main():
    retrigger_sn = sys.argv[1] if len(sys.argv) > 1 else None
    cleanup = {}  # teardown 上下文；SIGINT/异常路径也要恢复规则，绝不残留脏配置

    def _do_teardown():
        worker = cleanup.get("worker")
        if cleanup.pop("worker_alive", False) and worker and worker.poll() is None:
            worker.terminate()
            print("    已停掉本脚本拉起的 PDF Worker")
        sink = cleanup.get("sink")
        if sink:
            sink.stop()
            cleanup["sink"] = None
        if "original_url" in cleanup:
            _restore_rule(cleanup.pop("token"), cleanup.pop("original_url"))

    def _sigint_handler(signum, frame):
        print("\n[中断] 恢复规则配置后退出…")
        _do_teardown()
        sys.exit(130)
    signal.signal(signal.SIGINT, _sigint_handler)

    # ---- 1. 登录 ----
    token = api("POST", "/api/auth/login", body={
        "username": os.environ.get("E2E_USER", "admin"),
        "password": os.environ.get("E2E_PASS", "admin123"),
    })["access_token"]
    print("[1] 登录成功")

    # ---- 2. 确定目标 SN（重触发前校验完工态：未完工扫描永不入队，避免干等超时）----
    if retrigger_sn:
        sn = retrigger_sn
        trace = api("GET", f"/api/admin/records/trace/{sn}", token)
        product = trace.get("product") or {}
        if not product.get("is_completed"):
            print(f"[终止] {sn} 不在完工态（is_completed=False）："
                  "完工扫描只对已完工产品入队。请先让其重新走完流程（或检查是否被维修处置回退）。")
            sys.exit(2)
        removed = delete_job_rows(sn)
        print(f"[2] 重触发模式：删除 {sn} 的历史报告任务记录 {removed} 条，"
              f"等待完工扫描重新自动入队…")
    else:
        page = api("GET", f"/api/admin/report-jobs/candidates?model={MODEL}&page_size=100", token)
        cands = [c for c in page["items"] if not c["has_job"]]
        if not cands:
            print(f"[终止] 型号 {MODEL} 下没有「已完工且尚无报告任务」的产品；"
                  f"可用 `python tools/reports/report_test.py <SN>` 对既有完工产品重触发。")
            sys.exit(2)
        sn = cands[0]["sn"]
    print(f"[2] 目标 SN：{sn}，等待完工自动触发…")

    # ---- 3. 本地文件夹 MES（规则改写进入受保护段，任何退出路径都恢复）----
    rules = api("GET", "/api/admin/report-rules", token)
    rule = next(r for r in rules if r["rule"] == "tek_mso")
    original_url = rule["mes_url"]
    sink = FolderSink()
    sink.start()
    api("PUT", "/api/admin/report-rules/tek_mso", token, body={
        "rule": "tek_mso", "models": rule["models"], "mes_url": sink.url,
        "auto_trigger": rule["auto_trigger"], "enabled": True,
    })
    cleanup.update({"sink": sink, "original_url": original_url, "token": token})
    print(f"[3] 规则 mes_url → 本地文件夹接收器 {sink.url}（原值 {original_url}，结束后恢复）")

    try:
        # ---- 4. 确保 PDF Worker 在跑 ----
        worker = subprocess.Popen(
            [sys.executable,
             str(ROOT / "tools" / "reports" / "pdf_worker" / "report_pdf_worker.py")],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        worker_alive = worker.poll() is None
        cleanup.update({"worker": worker, "worker_alive": worker_alive})
        print(f"[4] PDF Worker：{'本脚本拉起' if worker_alive else '已有实例在运行（单实例互斥生效，转换由其完成）'}")
        if not worker_alive:
            print("    （本脚本拉起的进程被单实例互斥顶掉；只要已有 Worker 在轮询即可继续）")

        # ---- 5. 轮询整条链路 ----
        deadline = time.time() + DEADLINE_SEC
        last = ""
        while time.time() < deadline:
            items = api("GET", f"/api/admin/report-jobs?sn={sn}", token)["items"]
            if items:
                job = items[0]
                cur = f"{job['status']}/{job['mes_status']}"
                if cur != last:
                    pdfs = "/".join(
                        f"{a['type']}:{a['pdf_status']}" for a in (job["artifacts"] or [])
                    )
                    print(f"    任务 {cur}  [{pdfs}]  mes_msg={job['mes_message']}")
                    last = cur
                if job["status"] == "success" and job["mes_status"] == "success":
                    break
                if job["status"] == "failed" or job["mes_status"] == "failed":
                    break
            elif last != "no-job":
                print("    （等待完工扫描自动入队…）")
                last = "no-job"
            time.sleep(POLL_SEC)
        else:
            print("[失败] 超时，任务未达 success/MES success")
            sys.exit(3)

        job = api("GET", f"/api/admin/report-jobs?sn={sn}", token)["items"][0]

        # ---- 6. 校验 ----
        print("[6] 校验：")
        errors = []
        arts = {a["type"]: a for a in (job["artifacts"] or [])}
        if job["status"] != "success":
            errors.append(f"任务终态 {job['status']}")
        if job["mes_status"] != "success":
            errors.append(f"MES 状态 {job['mes_status']}: {job['mes_message']}")
        for t in ("cal_report", "certificate"):
            a = arts.get(t)
            if not a:
                errors.append(f"缺少产物 {t}")
                continue
            if not a.get("purged"):
                errors.append(f"{t} 未标记 purged")
            if a.get("object_key") or a.get("pdf_object_key"):
                errors.append(f"{t} MinIO 键未清空")
        if not sink.saved:
            errors.append("MES 接收器没有收到任何上传")
        else:
            for recv_sn, files in sink.saved:
                print(f"    MES 收到（sn={recv_sn}）：{files}")
        pdf_files = list(SINK_DIR.rglob("*.pdf"))
        if len(pdf_files) < 2:
            errors.append(f"MES 文件夹 PDF 不足 2 份：{len(pdf_files)}")
        for p in pdf_files:
            if p.read_bytes()[:4] != b"%PDF":
                errors.append(f"{p.name} 不是合法 PDF")
        # 数据报告仍可下载
        try:
            idx = [a["type"] for a in job["artifacts"]].index("data_report")
            dl = api("GET", f"/api/admin/report-jobs/{job['job_id']}/download/{idx}?variant=xlsx", token)
            r = urllib.request.urlopen(dl["url"], timeout=30)
            ok = r.status == 200 and r.read(4) not in (b"", None)
            print(f"    数据报告下载：HTTP {r.status}（仍保留）")
            if not ok:
                errors.append("数据报告下载异常")
        except Exception as exc:
            errors.append(f"数据报告下载失败：{exc}")
        # MES 产物 MinIO 已清（预签名下载应 404）
        for t, idx in (("cal_report", 1), ("certificate", 2)):
            try:
                dl = api("GET", f"/api/admin/report-jobs/{job['job_id']}/download/{idx}?variant=pdf", token)
                urllib.request.urlopen(dl["url"], timeout=30)
                errors.append(f"{t} 的 MinIO 对象仍存在，未清理")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    errors.append(f"{t} MinIO 下载返回 {exc.code}，预期 404")
            print(f"    {t} MinIO 对象：已清理（404）")

        if errors:
            print("[结果] 存在问题：")
            for e in errors:
                print("  -", e)
            sys.exit(4)
        print(f"[结果] 全链路通过。MES 落盘目录：{SINK_DIR}")
        print(f"       产物：{[a['filename'] for a in job['artifacts']]}")
    finally:
        _do_teardown()


if __name__ == "__main__":
    main()
