"""出厂报告 PDF 外部转换 Worker（部署在装有 WPS/Office 的 Windows 测试机上）。

职责：轮询 ATE Server 领取待转换的 xlsx（MinIO 预签名 URL）→ WPS/Office COM 转 PDF
→ 回传服务端（multipart），服务端负责归档 MinIO 与 MES 上传。

部署（两种方式任选）：
    A. 源码运行：机器装 Python 3.8+ 与 pywin32（pip install pywin32），
       `python report_pdf_worker.py`
    B. exe 免环境：PyInstaller 打包（见 build_exe.md），配置写在 exe 旁边的
       worker_config.json，改地址/密钥无需重新打包。

与服务端的约定（REPORT_PDF_MODE=external 时启用）：
    GET  /api/admin/report-jobs/pdf-claim   X-API-Key 鉴权，无任务返回 404
    POST /api/admin/report-jobs/pdf-result  multipart: job_id/artifact_index/success/error/file
"""

import json
import os
import sys
import tempfile
import time
import uuid

import urllib.error
import urllib.request

DEFAULT_CONFIG = {
    "server_url": "http://10.1.1.4:8000",
    "api_key": "",
    "poll_interval": 10,
    "work_dir": "",
    "convert_timeout": 120,
}


def _app_dir() -> str:
    """exe（PyInstaller）取 exe 所在目录；源码运行取脚本目录 —— 配置随部署位置走。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(_app_dir(), "worker_config.json")


def load_config() -> dict:
    """读取 exe/脚本旁的 worker_config.json，缺失时生成默认配置；work_dir 留空取系统临时目录。"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (OSError, ValueError) as exc:
            print(
                f"[config] worker_config.json 解析失败，使用默认配置：{exc}",
                flush=True,
            )
    else:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            print(
                f"[config] 已生成默认配置 {CONFIG_FILE}，请填写后重启",
                flush=True,
            )
        except OSError:
            pass
    if not cfg.get("work_dir"):
        cfg["work_dir"] = os.path.join(
            tempfile.gettempdir(), "report_pdf_worker"
        )
    return cfg


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOGGER_PREFIX = "[pdf-worker]"
SERVER_URL = ""
API_KEY = ""
POLL_INTERVAL = 10
CONVERT_TIMEOUT = 120
WORK_DIR = ""


def _log(message):
    """带时间戳与前缀的行日志。"""
    print(f"{time.strftime('%H:%M:%S')} {LOGGER_PREFIX} {message}", flush=True)


def _request(method, path, body=None, raw=None, content_type=None):
    """向服务端发请求：body 走 JSON，raw 走原始字节（multipart），自动带 X-API-Key。"""
    url = f"{SERVER_URL.rstrip('/')}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with OPENER.open(req, timeout=600) as resp:
        payload = resp.read()
        if headers.get("Accept") == "application/json":
            return json.loads(payload.decode("utf-8"))
        return payload


def claim_task():
    """领取一个待转换任务；无任务返回 None。"""
    try:
        return _request("GET", "/api/admin/report-jobs/pdf-claim")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def download_xlsx(url, target_path):
    """按服务端下发的预签名 URL 下载 xlsx 到本地路径。"""
    req = urllib.request.Request(url, method="GET")
    with OPENER.open(req, timeout=300) as resp, open(target_path, "wb") as fh:
        fh.write(resp.read())


def submit_result(task, success, pdf_path=None, error=None):
    """multipart 回传转换结果：成功带 PDF 文件，失败带 error 文本。"""
    boundary = uuid.uuid4().hex
    parts = []
    fields = {
        "job_id": task["job_id"],
        "artifact_index": str(task["artifact_index"]),
        "success": "true" if success else "false",
        "error": error or "",
    }
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
                "utf-8"
            )
        )
    if success and pdf_path:
        filename = os.path.basename(pdf_path)
        with open(pdf_path, "rb") as fh:
            content = fh.read()
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'
            ).encode("utf-8")
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    _request(
        "POST",
        "/api/admin/report-jobs/pdf-result",
        raw=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def excel_to_pdf(excel_path):
    """WPS/Excel COM 转 PDF：优先 WPS 表格（et），回退 MS Excel。
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    pdf_path = os.path.splitext(excel_path)[0] + ".pdf"
    app = None
    try:
        try:
            app = win32com.client.DispatchEx("Ket.Application")
        except Exception:
            app = win32com.client.DispatchEx("Excel.Application")

        app.Visible = False
        app.DisplayAlerts = False
        workbook = app.Workbooks.Open(
            os.path.abspath(excel_path), ReadOnly=True
        )
        try:
            workbook.ExportAsFixedFormat(0, os.path.abspath(pdf_path))
        finally:
            workbook.Close(False)
    finally:
        if app:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoFreeUnusedLibraries()
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    if not os.path.exists(pdf_path):
        raise RuntimeError("pdf not generated")
    return pdf_path


def _kill_com_hosts():
    """转换超时后强杀 COM 宿主进程（WPS/Office），释放卡死的隐藏窗口；仅超时异常路径使用。"""
    for exe in ("et.exe", "wps.exe", "excel.exe", "ket.exe"):
        os.system(f"taskkill /F /IM {exe} >nul 2>&1")


def convert_with_timeout(excel_path, timeout):
    """带超时的 COM 转换：转换跑在守护线程，超时即放弃并强杀 COM 宿主（COM 对象无法跨线程强停）。"""
    import threading

    result = {}

    def target():
        try:
            result["pdf"] = excel_to_pdf(excel_path)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _kill_com_hosts()
        raise TimeoutError(
            f"COM convert timeout after {timeout}s (COM host killed)"
        )
    if "error" in result:
        raise result["error"]
    return result["pdf"]


def _acquire_single_instance() -> bool:
    """Windows 全局互斥锁防多实例并发领任务；返回 False 表示已有实例在运行。"""
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    ctypes.windll.kernel32.CreateMutexW(
        None, False, "Global\\ReportPdfWorker_SingleInstance"
    )
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def run_forever():
    """主循环：领任务 → 下载 xlsx → 带超时转换 → 回传结果；服务端不可达时退避重试不退出。"""
    global SERVER_URL, API_KEY, POLL_INTERVAL, WORK_DIR, CONVERT_TIMEOUT
    if not _acquire_single_instance():
        _log("another instance is already running, exit")
        return
    cfg = load_config()
    SERVER_URL = cfg["server_url"]
    API_KEY = cfg.get("api_key", "")
    POLL_INTERVAL = int(cfg.get("poll_interval", 10))
    WORK_DIR = cfg["work_dir"]
    CONVERT_TIMEOUT = int(cfg.get("convert_timeout", 120))
    os.makedirs(WORK_DIR, exist_ok=True)
    _log(
        f"started, server={SERVER_URL}, api_key={'set' if API_KEY else 'NOT SET'}, work_dir={WORK_DIR}"
    )
    while True:
        task = None
        try:
            task = claim_task()
        except Exception as exc:
            _log(f"claim failed (server unreachable? will retry): {exc}")
        if not task:
            time.sleep(POLL_INTERVAL)
            continue
        job_id, index = task["job_id"], task["artifact_index"]
        _log(f"claimed {job_id}#{index}")
        xlsx_path = os.path.join(WORK_DIR, f"{job_id}_{index}.xlsx")
        try:
            download_xlsx(task["download_url"], xlsx_path)
        except Exception as exc:
            _log(f"download failed {job_id}#{index}: {exc}")
            try:
                submit_result(task, False, error=f"download failed: {exc}")
            except Exception as submit_exc:
                _log(f"submit result failed: {submit_exc}")
            continue
        try:
            pdf_path = convert_with_timeout(xlsx_path, CONVERT_TIMEOUT)
            submit_result(task, True, pdf_path=pdf_path)
            _log(f"converted {job_id}#{index}")
        except Exception as exc:
            _log(f"convert failed {job_id}#{index}: {exc}")
            try:
                submit_result(task, False, error=str(exc))
            except Exception as submit_exc:
                _log(f"submit result failed: {submit_exc}")
        finally:
            keep_dir = os.path.join(WORK_DIR, "_keep")
            try:
                os.makedirs(keep_dir, exist_ok=True)
                for path in (xlsx_path, os.path.splitext(xlsx_path)[0] + ".pdf"):
                    if os.path.exists(path):
                        dst = os.path.join(
                            keep_dir,
                            f"{time.strftime('%H%M%S')}_{os.path.basename(path)}",
                        )
                        os.replace(path, dst)
            except OSError as keep_exc:
                _log(f"keep failed: {keep_exc}")
                for path in (xlsx_path, os.path.splitext(xlsx_path)[0] + ".pdf"):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass


if __name__ == "__main__":
    run_forever()
