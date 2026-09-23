"""报告成品归档：MinIO 对象存储（上传 / 预签名下载 URL）。"""

import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional

from ...config import settings

_lock = threading.Lock()
_client = None

def enabled() -> bool:
    return bool(settings.MINIO_ENDPOINT and settings.MINIO_ACCESS_KEY)

def _get_client():
    global _client
    with _lock:
        if _client is None:
            from minio import Minio

            _client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                secure=settings.MINIO_SECURE,
            )
        return _client

def object_key(model: str, sn: str, filename: str) -> str:
    """对象键布局：reports/{model}/{sn}/{filename}。"""
    return f"{settings.MINIO_REPORT_PREFIX}/{model}/{sn}/{filename}"

def upload(local_path: Path, key: str) -> None:
    client = _get_client()
    if not client.bucket_exists(settings.MINIO_BUCKET):
        client.make_bucket(settings.MINIO_BUCKET)
    client.fput_object(settings.MINIO_BUCKET, key, str(local_path))

def stat_etag(key: str) -> Optional[str]:
    """对象当前 ETag（MinIO 对上传文件即 MD5）；转换结果新鲜度校验用。"""
    try:
        return _get_client().stat_object(settings.MINIO_BUCKET, key).etag
    except Exception:
        return None


def upload_bytes(content: bytes, key: str) -> None:
    """小对象（模板等）从内存直传，免去临时文件。"""
    from io import BytesIO

    client = _get_client()
    if not client.bucket_exists(settings.MINIO_BUCKET):
        client.make_bucket(settings.MINIO_BUCKET)
    client.put_object(
        settings.MINIO_BUCKET, key, BytesIO(content), length=len(content),
        content_type="application/octet-stream",
    )

def presigned_url(key: str, hours: int = 24) -> str:
    return _get_client().presigned_get_object(
        settings.MINIO_BUCKET, key, expires=timedelta(hours=hours)
    )

def download_to(key: str, local_path: Path) -> None:
    _get_client().fget_object(settings.MINIO_BUCKET, key, str(local_path))

def remove(key: str) -> None:
    """删除对象（脚本换名归档、模板清理时用；对象不存在不报错）。"""
    from minio.error import S3Error

    try:
        _get_client().remove_object(settings.MINIO_BUCKET, key)
    except S3Error as exc:
        if exc.code not in ("NoSuchKey", "NoSuchObject"):
            raise
