"""端到端验证：完工自动触发 → 插件生成 → MinIO → Worker PDF → MES（本地文件夹接收）。

用法：
    python tools/report_test.py [SN]
    不带 SN：自动找「已完工、尚无报告任务」的候选
    带 SN（如 CNUD001375）：删除该 SN 的历史报告任务记录，让完工扫描重新自动入队

前提：
    - 后端 http://127.0.0.1:8000 运行中（带最新代码），MinIO 可达
    - tek_mso 规则存在且绑定 DPO1074；工厂库可达
    - 本机装有 WPS/Office（external PDF 模式转换用）

流程与校验：
    1. 起本地 MES 接收器：HTTP 收到 multipart 后把 PDF 落盘到 tools/_mes_sink/，
       并把规则 mes_url 指向它（结束后恢复原值）
    2. 确保 PDF Worker 在跑（没有则拉起，单实例互斥保证不重复）
    3. 等待：调度器扫描自动入队 → 插件生成 → MinIO 归档 → Worker 转换回传
       → MES 上传 → 归档清理
    4. 校验：任务 success、MES 文件夹收到两份合法 PDF、MES 产物已从 MinIO/本地
       清理（purged=true）、数据报告仍可下载

说明：带 SN 重触发模式只删除该 SN 的历史报告任务记录（完工扫描重新入队的必要
条件），不触碰产品/测试记录等其他数据。
"""

import email
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from email import policy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
MODEL = "DPO1074"
SINK_DIR = ROOT / "tools" / "_mes_sink"
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
        self.saved = []  # [(filename, bytes, sn)]
        self._server = None
        self._thread = None

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
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/mes"


def main():
    import subprocess

    retrigger_sn = sys.argv[1] if len(sys.argv) > 1 else None

    # ---- 1. 登录 ----
    token = api("POST", "/api/auth/login", body={
        "username": os.environ.get("E2E_USER", "admin"),
        "password": os.environ.get("E2E_PASS", "admin123"),
    })["access_token"]
    print("[1] 登录成功")

    # ---- 2. 确定目标 SN ----
    if retrigger_sn:
        sn = retrigger_sn
        removed = delete_job_rows(sn)
        print(f"[2] 重触发模式：删除 {sn} 的历史报告任务记录 {removed} 条，"
              f"等待完工扫描重新自动入队…")
    else:
        page = api("GET", f"/api/admin/report-jobs/candidates?model={MODEL}&page_size=100", token)
        cands = [c for c in page["items"] if not c["has_job"]]
        if not cands:
            print(f"[终止] 型号 {MODEL} 下没有「已完工且尚无报告任务」的产品；"
                  f"可用 `python tools/e2e_report_test.py <SN>` 对既有完工产品重触发。")
            sys.exit(2)
        sn = cands[0]["sn"]
    print(f"[2] 目标 SN：{sn}，等待完工自动触发…")

    # ---- 3. 本地文件夹 MES ----
    rules = api("GET", "/api/admin/report-rules", token)
    rule = next(r for r in rules if r["rule"] == "tek_mso")
    original_url = rule["mes_url"]
    sink = FolderSink()
    sink.start()
    api("PUT", "/api/admin/report-rules/tek_mso", token, body={
        "rule": "tek_mso", "models": rule["models"], "mes_url": sink.url,
        "auto_trigger": rule["auto_trigger"], "enabled": True,
    })
    print(f"[3] 规则 mes_url → 本地文件夹接收器 {sink.url}（原值 {original_url}，结束后恢复）")

    # ---- 4. 确保 PDF Worker 在跑 ----
    worker = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "pdf_worker" / "report_pdf_worker.py")],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    worker_alive = worker.poll() is None
    print(f"[4] PDF Worker：{'本脚本拉起' if worker_alive else '已有实例在运行（单实例互斥生效）'}")

    # ---- 5. 轮询整条链路 ----
    deadline = time.time() + DEADLINE_SEC
    last = ""
    job_id = None
    while time.time() < deadline:
        items = api("GET", f"/api/admin/report-jobs?sn={sn}", token)["items"]
        if items:
            job = items[0]
            job_id = job["job_id"]
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
        time.sleep(POLL_SEC)
    else:
        print("[失败] 超时，任务未达 success/MES success")
        _teardown(api, token, original_url, sink, worker, worker_alive)
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
        pdfs = [f for _, fs in sink.saved for f in fs if t.replace("cal_report", "cal") in f.lower()
                or Path(f).name.lower().endswith(".pdf")]
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

    _teardown(api, token, original_url, sink, worker, worker_alive)

    if errors:
        print("[结果] 存在问题：")
        for e in errors:
            print("  -", e)
        sys.exit(4)
    print(f"[结果] 全链路通过。MES 落盘目录：{SINK_DIR}")
    print(f"       产物：{[a['filename'] for a in job['artifacts']]}")


def _teardown(api, token, original_url, sink, worker, worker_alive):
    api("PUT", "/api/admin/report-rules/tek_mso", token, body={
        "rule": "tek_mso", "models": ["DPO1074"], "mes_url": original_url,
        "auto_trigger": True, "enabled": True,
    })
    sink.stop()
    if worker_alive and worker and worker.poll() is None:
        worker.terminate()
        print("    已停掉本脚本拉起的 PDF Worker")


if __name__ == "__main__":
    main()
