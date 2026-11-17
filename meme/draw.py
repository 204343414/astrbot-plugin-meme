"""重画时怎么定尺寸：按原图比例挑画布，画完再把它掰回原比例。

为什么不能直接写死 1024x1024：画图模型只认少数几档固定尺寸（1024x1024、
1536x1024、1024x1536 这种），而梗图大量是横的（16:9 截图）或竖的（手机
长图）。写死方形会有两个后果：一是模型把横幅硬塞进方框，构图被挤变形；
二是画回来比原图更方，「把梗图中文化」变成了「把梗图重新构图」。

所以做两件事：

1. :func:`choose_draw_size` —— 先按原图宽高比挑最接近的那一档，别让模型
   从方框开始想；
2. :func:`fit_to_reference` —— 画完再比对一次比例，差得明显就居中裁回
   原图比例、缩到原图宽度。这样发出去的图，比例跟投稿的那张一致。

两步都只用 PIL（插件本来就要它做缩略图），PIL 缺失时全部原样返回 ——
尺寸是锦上添花，不该让一张图发不出去。
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 只在缺依赖的环境里走 False
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None

#: 画图接口认识的几档画布。改这里就等于改「能画多大」。
SQUARE = "1024x1024"
LANDSCAPE = "1536x1024"
PORTRAIT = "1024x1536"
DRAW_SIZES = (SQUARE, LANDSCAPE, PORTRAIT)

#: 比这个还宽的图（16:9 大致 1.78）用横幅画布。
_WIDE = 1.5
#: 比这个还高的图用竖幅画布。
_TALL = 1 / 1.5

#: 画回来的比例和原图差多少以内就不动它（百分比）。
FIT_TOLERANCE = 0.05


def image_size(data: bytes, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """图片的宽高；认不出来就返回 ``default``。"""
    if Image is None or not data:
        return default
    try:
        with Image.open(io.BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001
        return default


def choose_draw_size(
    width: int, height: int, configured: str = "auto", default: str = SQUARE
) -> str:
    """``configured`` 是 ``auto``（或空）时按原图比例选画布，否则听配置的。

    选法很直白：宽高比 ≥1.5 用横幅、≤1/1.5 用竖幅，剩下的一律方形。
    中间这一段（比如 4:3）用方形而不是硬凑横幅，是因为 4:3 离方形更近，
    硬塞横幅反而会把画面拉宽。
    """
    text = str(configured or "").strip().lower()
    if text and text != "auto":
        return str(configured).strip()
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return default
    if width <= 0 or height <= 0:
        return default
    ratio = width / height
    if ratio >= _WIDE:
        return LANDSCAPE
    if ratio <= _TALL:
        return PORTRAIT
    return SQUARE


def _parse_size(size: str) -> tuple[int, int] | None:
    text = str(size or "").strip().lower()
    if "x" not in text:
        return None
    left, _, right = text.partition("x")
    try:
        width, height = int(left), int(right)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def fit_to_reference(
    drawn: bytes, width: int, height: int, tolerance: float = FIT_TOLERANCE
) -> bytes:
    """把画回来的图掰成原图的比例。

    比例本来就差不多（差不超过 ``tolerance``）时**原字节返回**：不重编码就
    不会掉画质，也不会把 png 变成 jpg。

    差得多时居中裁一块最大可用区域（尽量不裁掉画面中心），再把宽度缩到
    原图宽度 —— 手机上刷到的图，宽度对齐看起来最自然。
    """
    if Image is None or not drawn or width <= 0 or height <= 0:
        return drawn
    try:
        with Image.open(io.BytesIO(drawn)) as img:
            img.load()
            target = width / height
            current = img.width / img.height
            if abs(current / target - 1.0) <= tolerance:
                return drawn

            if current > target:
                # 画回来更宽 → 左右各切一点
                new_w = max(1, int(round(img.height * target)))
                left = (img.width - new_w) // 2
                box = (left, 0, left + new_w, img.height)
            else:
                new_h = max(1, int(round(img.width / target)))
                top = (img.height - new_h) // 2
                box = (0, top, img.width, top + new_h)
            cropped = img.crop(box)

            if cropped.width != width:
                resized_h = max(1, int(round(cropped.height * (width / cropped.width))))
                cropped = cropped.resize((width, resized_h), Image.LANCZOS)

            buffer = io.BytesIO()
            fmt = "PNG" if (img.format or "").upper() == "PNG" else "JPEG"
            if fmt == "JPEG" and cropped.mode not in ("RGB", "L"):
                cropped = cropped.convert("RGB")
            if fmt == "JPEG":
                cropped.save(buffer, fmt, quality=92, optimize=True)
            else:
                cropped.save(buffer, fmt, optimize=True)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Meme] 重画结果裁剪失败，按原样用：%s", exc)
        return drawn
