"""测试产物归档：MinIO 对象存储（与出厂报告共用同一桶配置，仅前缀不同）。

任何机台客户端在会话内按 case_id 上传产物文件（结果文件、日志等），
出站时引用随 executed_items 落库，台账/追溯可下载。

对象键布局：{MINIO_ARTIFACT_PREFIX}/{model}/{sn}/{case_hash}/{filename}
- case_hash = sha1(case_id) 前 12 位十六进制：case_id（如 pytest nodeid
  含 / 与 ::）不进路径，哈希定长且无路径字符；case_id 本身在
  executed_items.items[].case_id 留档，追溯不受影响
- 同 case_id + filename 重复上传即覆盖（同 case_id 恒定同哈希）
- 文件永久保留（不随报告保留期清理）
- MinIO 未配置时上传/下载端点直接拒绝（产物只有对象存储一种归档方式）
"""

import hashlib
import re
from pathlib import Path

from ..config import settings

_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-()]")

def enabled() -> bool:
    from .reports import store

    return store.enabled()

def _segment(value: str) -> str:
    """键段消毒：前缀/机型/SN 来自配置或现场自报，绝不允许携带路径分隔符。"""
    cleaned = _UNSAFE.sub("_", (value or "").strip()).strip("._")
    if not cleaned:
        raise ValueError("invalid_object_key_segment: empty segment")
    return cleaned

def case_hash(case_id: str) -> str:
    """case_id 的定长路径别名：sha1 前 12 位十六进制。"""
    return hashlib.sha1((case_id or "").encode("utf-8")).hexdigest()[:12]

def object_key(model: str, sn: str, case_id: str, filename: str) -> str:
    safe_name = _segment(Path(filename or "artifact.bin").name)
    return "/".join(
        (
            _segment(settings.MINIO_ARTIFACT_PREFIX),
            _segment(model),
            _segment(sn),
            case_hash(case_id),
            safe_name,
        )
    )

def save(content: bytes, key: str) -> None:
    from .reports import store

    store.upload_bytes(content, key)

def presigned_url(key: str, hours: int = 24) -> str:
    from .reports import store

    return store.presigned_url(key, hours)
