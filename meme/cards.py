"""卡片：一张梗图 + 两个按钮。

为什么非要卡片
--------------
QQ 的富媒体消息（纯图片）**不能带按钮** —— 平台不允许富媒体和 keyboard 同条
消息。想让「再来一张 / 我要投稿」出现在图下面，只有一条路：发 **Markdown 卡片**，
把图嵌进 Markdown 里。

而卡片里的图又必须"能被腾讯取到"，两条来源：

1. ``hub.publish_image_material(bytes)`` → ``qqbot-media://image/v1/xxx``：
   字节直接进腾讯自己的素材库，不需要公网地址、不需要隧道、不需要 DNS。
   **首选。** 代价是每个机器人只有 100 张额度（90 天），所以必须回收。
2. ``hub.publish_image_checked(bytes)`` → 图床的公网 URL：老路子，要自建图床
   和公网域名。

本模块只负责**拼卡片 JSON**，不碰网络 —— 发不出去的降级链在 ``main.py``
（卡片带图 → 卡片无图 + 单独图片 → 纯图片），这样规则和 IO 不搅在一起。
"""
from __future__ import annotations

from typing import Any

from .models import MemeItem

#: hub 的 action id。卡片按钮靠它回到我们的回调，改名字要同步改 main.py 的注册。
ACTION_AGAIN = "meme.again"
ACTION_HOWTO = "meme.howto"

#: QQ 卡片里图片 alt 必须带尺寸（``#宽px #高px``），少了就是裂图。
#: 720 是实测在手机和桌面都不糊也不大的档位；竖图按高度收。
MAX_SIDE = 720

#: 按钮文字要短（用户明确提过）：超过这个长度会被 QQ 截断或挤成两行。
MAX_LABEL = 6

HOWTO_MARKDOWN = """**📮 怎么投稿**

1. 长按图片 → **引用**
2. 同一条消息里 **@我**
3. 输入 `/meme 投稿`

投过的图会进公共库，**别的群也能刷到**。
只想投给本群，就用 `/meme 收`。"""


def display_size(
    width: int, height: int, max_side: int = MAX_SIDE
) -> tuple[int, int]:
    """等比缩到长边 ≤ ``max_side``。尺寸写错只影响显示大小，不会裂图。"""
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return max_side, max_side
    if width <= 0 or height <= 0:
        return max_side, max_side
    longest = max(width, height)
    if longest <= max_side:
        return width, height
    scale = max_side / float(longest)
    return max(1, round(width * scale)), max(1, round(height * scale))


def image_markdown(url: str, width: int = MAX_SIDE, height: int = MAX_SIDE) -> str:
    """``![梗图 #720px #480px](qqbot-media://...)``。

    alt 里带尺寸不是装饰，是 QQ 的硬要求；alt 文字本身会显示在裂图位置，
    所以写「梗图」而不是留空。
    """
    width, height = display_size(width, height)
    return f"![梗图 #{width}px #{height}px]({url})"


def caption_line(item: MemeItem, limit: int = 160) -> str:
    return item.caption_line(limit)


def _buttons(again: bool, howto: bool) -> list[list[dict[str, Any]]]:
    row: list[dict[str, Any]] = []
    if again:
        row.append({"id": "again", "label": "再来一张"[:MAX_LABEL], "action_id": ACTION_AGAIN})
    if howto:
        row.append({"id": "howto", "label": "我要投稿"[:MAX_LABEL], "action_id": ACTION_HOWTO})
    return [row] if row else []


def build_meme_card(
    item: MemeItem,
    image_url: str = "",
    *,
    caption_limit: int = 160,
    show_again: bool = True,
    show_howto: bool = True,
    width: int = MAX_SIDE,
    height: int = MAX_SIDE,
    note: str = "",
) -> dict[str, Any]:
    """一张卡片。

    ``image_url`` 为空时发**无图卡片**：图会由调用方紧跟一条图片消息补上
    （素材库和图床都不可用时的兜底，比"发不出图"强）。
    """
    blocks: list[str] = []
    if image_url:
        blocks.append(image_markdown(image_url, width, height))
    head = f"**#{item.id}**"
    line = caption_line(item, caption_limit)
    if line:
        head += f" {line}"
    blocks.append(head)
    if item.used_count:
        blocks.append(f"<font color='grey'>发过 {item.used_count} 次</font>")
    if note:
        blocks.append(note)
    return {
        "markdown": "\n".join(blocks),
        "rows": _buttons(show_again, show_howto),
        # 谁都能点：这张卡是发在群里的，锁成发起者会让别人点了没反应
        "owner_mode": "everyone",
    }


def build_howto_card(*, show_again: bool = True) -> dict[str, Any]:
    """「我要投稿」按钮弹出的说明卡。

    带上「再来一张」是因为很多人点完投稿发现手里没图，回不去了。
    """
    return {
        "markdown": HOWTO_MARKDOWN,
        "rows": _buttons(show_again, False),
        "owner_mode": "everyone",
    }
