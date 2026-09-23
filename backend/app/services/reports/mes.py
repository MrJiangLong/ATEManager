"""MES 上传传输层：multipart 上传产物 PDF。

字段名与份数由插件产物（mes_field）声明，本模块不做任何产品解释；
默认 URL 指向现行统一的 MES 校准数据接口。"""

import json
from typing import Optional

import httpx

from ...config import settings
from ...logging import get_logger

logger = get_logger("report.mes")

def upload_mes(files: dict, sn: str, url: Optional[str] = None) -> tuple:
    """按 {字段名: 本地 PDF 路径} 上传，返回 (success, message)。"""
    target = url or settings.REPORT_MES_URL
    try:
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                target,
                data={"sn": sn},
                files={
                    name: (path.name, path.read_bytes(), "application/octet-stream")
                    for name, path in files.items()
                },
            )
        text = resp.text
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {text[:500]}"
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return False, f"invalid response: {text[:500]}"
        success = body.get("success")
        if isinstance(success, bool):
            ok = success
        elif isinstance(success, str):
            ok = success.strip().lower() == "true"
        else:
            ok = False
        return ok, str(body.get("message") or text[:500])
    except Exception as exc:
        logger.warning("MES 上传失败：%s", exc)
        return False, str(exc)
