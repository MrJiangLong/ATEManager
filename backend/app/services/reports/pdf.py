"""PDF 转换：libreoffice（服务端 headless）/ external（外部 Worker 经 API 领取）。

外部 Worker 模式下引擎只把产物标记为 pdf_status=pending，
转换由部署在装有 WPS/Office 的 Windows 机器上的 worker 完成（见 tools/pdf_worker/report_pdf_worker.py），
转换结果经 /api/admin/report-jobs/pdf-result 回写。
"""

import subprocess
from pathlib import Path

from ...config import settings
from ...logging import get_logger

logger = get_logger("report.pdf")

def convert_libreoffice(xlsx_path: Path, out_dir: Path) -> Path:
    """soffice headless 转换，返回生成的 PDF 路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.SOFFICE_CMD, "--headless", "--norestore",
        "--convert-to", "pdf", "--outdir", str(out_dir), str(xlsx_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    pdf_path = out_dir / (xlsx_path.stem + ".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"libreoffice convert failed: {result.stderr or result.stdout}")
    return pdf_path

def mode() -> str:
    return (settings.REPORT_PDF_MODE or "off").lower()
