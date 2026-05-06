"""COS 文件上传工具"""

import hashlib
import os
import logging
from io import BytesIO
from datetime import datetime

from qcloud_cos import CosConfig, CosS3Client

try:
    from . import cos_config as cfg
except ImportError:
    import cos_config as cfg

logger = logging.getLogger("bridge")

_client = None


def _get_client() -> CosS3Client:
    global _client
    if _client is None:
        if not cfg.COS_SECRET_ID or not cfg.COS_SECRET_KEY:
            raise RuntimeError("COS 未配置：请先填写 cos_config.py 中的 SecretId/SecretKey")
        config = CosConfig(
            Region=cfg.COS_REGION,
            SecretId=cfg.COS_SECRET_ID,
            SecretKey=cfg.COS_SECRET_KEY,
        )
        _client = CosS3Client(config)
    return _client


def upload_file(file_data: bytes, filename: str, content_type: str = "application/octet-stream") -> str:
    """
    上传文件到 COS，返回公开访问 URL。
    file_data: 文件字节内容
    filename: 原始文件名（不含路径）
    content_type: MIME 类型
    """
    client = _get_client()

    # 按日期分子目录
    date_prefix = datetime.now().strftime("%Y%m%d")
    object_key = f"{cfg.COS_PREFIX}{date_prefix}/{filename}"

    # 计算 MD5
    file_hash = hashlib.md5(file_data).hexdigest()

    try:
        response = client.put_object(
            Bucket=cfg.COS_BUCKET,
            Body=file_data,
            Key=object_key,
            ContentType=content_type,
            Metadata={"md5": file_hash},
        )

        # 设置为公开读
        client.put_object_acl(
            Bucket=cfg.COS_BUCKET,
            Key=object_key,
            ACL='public-read',
        )

        # 构造 URL
        if cfg.COS_CUSTOM_DOMAIN:
            url = f"{cfg.COS_CUSTOM_DOMAIN.rstrip('/')}/{object_key}"
        else:
            url = f"https://{cfg.COS_BUCKET}.cos.{cfg.COS_REGION}.myqcloud.com/{object_key}"

        logger.info(f"[COS] 上传成功: {filename} -> {url}")
        return url

    except Exception as e:
        logger.error(f"[COS] 上传失败: {filename}: {e}")
        raise


def upload_local_file(local_path: str) -> dict:
    """
    从本地路径上传文件到 COS。
    返回 { url, filename, size, content_type }
    """
    if not os.path.isfile(local_path):
        logger.warning(f"[COS] 本地文件不存在: {local_path}")
        return None

    filename = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)

    # 猜测 MIME 类型
    import mimetypes
    content_type, _ = mimetypes.guess_type(local_path)
    if not content_type:
        content_type = "application/octet-stream"

    with open(local_path, "rb") as f:
        file_data = f.read()

    url = upload_file(file_data, filename, content_type)
    return {
        "url": url,
        "filename": filename,
        "size": file_size,
        "content_type": content_type,
    }
