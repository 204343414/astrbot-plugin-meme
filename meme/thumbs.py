"""缩略图：网页图库一屏要放几十张，原图直接 base64 会变成几兆的 JSON。

缓存键是 `sha256 + 尺寸`，所以同一张图只会缩放一次；`PIL` 缺失时退回原图，
网页最多是慢一点，不会白屏。
"""
from __future__ import annotations

import base64
import io
import logging
import os

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 只在缺依赖的环境里走 False
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None

#: 图库里用的缩略图尺寸（长边）。300 在卡面上已经够清楚，体积约 15KB。
GALLERY_THUMB = 300
#: 点开大图时用的尺寸。再大对网页没意义，而且 JSON 会开始变重。
PREVIEW_SIZE = 1000


def thumb_cache_path(cache_dir: str, sha256: str, size: int) -> str:
    bucket = os.path.join(cache_dir, (sha256 or "0")[:2] or "00")
    return os.path.join(bucket, f"{sha256}_{int(size)}.jpg")


def make_thumb(data: bytes, size: int = GALLERY_THUMB, quality: int = 72) -> bytes:
    """把图片字节缩成 JPEG。任何失败都返回原字节（宁可大，不要没有）。"""
    if Image is None or not data:
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((size, size), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality, optimize=True)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.info("[Meme] 生成缩略图失败，用原图代替：%s", exc)
        return data


def read_thumb(
    file_path: str,
    sha256: str,
    cache_dir: str,
    size: int = GALLERY_THUMB,
) -> bytes:
    """读缓存的缩略图，没有就现做一份存下来。"""
    cache_path = thumb_cache_path(cache_dir, sha256, size)
    try:
        with open(cache_path, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    with open(file_path, "rb") as fh:
        original = fh.read()
    thumb = make_thumb(original, size=size)
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(thumb)
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return thumb


def data_url(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()
