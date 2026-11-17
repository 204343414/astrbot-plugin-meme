"""图片字节的小工具：识别格式、转 data URL、算指纹。

只做纯计算，不碰网络也不碰 AstrBot，所以能被单测直接喂假字节。
"""
from __future__ import annotations

import base64
import hashlib

#: 允许入库的格式 → (mime, 后缀)。动图保留，视频一律不收。
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """看文件头判断是不是图片，返回 ``(mime, 后缀)``；不是图片返回 None。

    相信扩展名是危险的：QQ 转存过来的文件经常叫 ``xxx.jpg`` 而内容其实是
    视频或网页，入库之后再拿出来当图发就会一直失败。
    """
    if not data:
        return None
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    # WEBP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def to_data_url(data: bytes, mime: str = "image/jpeg") -> str:
    """多模态 provider 吃的 ``data:`` URL，省掉一次图床往返。"""
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def ext_for_mime(mime: str) -> str:
    for _magic, known_mime, ext in _MAGIC:
        if known_mime == mime:
            return ext
    return ".webp" if mime == "image/webp" else ".jpg"


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"
