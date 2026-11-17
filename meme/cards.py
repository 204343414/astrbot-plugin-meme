"""卡片：一张梗图 + 两个按钮。

为什么非要卡片
--------------
QQ 的富媒体消息（纯图片）**不能带按钮** —— 平台不允许富媒体和 keyboard 同条
消息。想让「再来一张 / 我要投稿」出现在图下面，只有一条路：发 **Markdown 卡片**，
把图嵌进 Markdown 里。

而卡片里的图又必须"能被腾讯取到"，两条来源：

1. ``hub.publish_image_material(bytes)`` → ``qqbot-media://image/v1/xxx``：
   字节直接进腾讯自己的素材库，不需要公网地址、不需要隧道、不需要 DNS。
   **首选。** 代价是每个机器人只有 100 张额度，所以必须回收。
   额度规则（官方文档，2026-10 核实）：
     * 上限 100 张，超了是**新的上传被拒**（40035007），旧图不会被挤掉；
     * 有效期是**滑动**的：最后一次被消息引用起 90 天内没再用到才失效；
     * 删除**不影响已发送的消息**（发送时平台已转存副本），所以回收是安全的。
2. ``hub.publish_image_checked(bytes)`` → 图床的公网 URL：老路子，要自建图床
   和公网域名。

本模块只负责**拼卡片 JSON**，不碰网络 —— 发不出去的降级链在 ``main.py``
（卡片带图 → 卡片无图 + 单独图片 → 纯图片），这样规则和 IO 不搅在一起。
"""
from __future__ import annotations

from typing import Any

from .models import MemeItem

#: 卡片在 Hub 那边的可点有效期。Hub 默认 3600 秒（1 小时），但那意味着聊天记录里
#: 一小时前的卡片，点上去只会得到一句「卡片已过期」—— 群友以为是按钮坏了。
#: 按钮本身有一次性/归属校验，放宽到 24 小时（Hub 允许的上限）只是让旧卡还能用。
CARD_TTL_SECONDS = 86400

#: hub 的 action id。卡片按钮靠它回到我们的回调，改名字要同步改 main.py 的注册。
ACTION_AGAIN = "meme.again"
ACTION_HOWTO = "meme.howto"
#: 自动推送：订阅入口、三档、退订。三档分成三个 action，是因为卡片按钮的 data
#: 由 Hub 自己注入 nonce，我们没法在按钮里塞参数。
ACTION_AUTO_SUB = "meme.auto_sub"
ACTION_AUTO_OFF = "meme.auto_off"
ACTION_AUTO_LOW = "meme.auto_low"
ACTION_AUTO_MID = "meme.auto_mid"
ACTION_AUTO_HIGH = "meme.auto_high"
ACTION_AUTO_LEVELS = {
    ACTION_AUTO_LOW: "low",
    ACTION_AUTO_MID: "mid",
    ACTION_AUTO_HIGH: "high",
}
#: 档位 → 中文名（发给群里的文案用）。
LEVEL_LABELS = {"low": "低", "mid": "中", "high": "高"}

#: 自动推送发出的卡片上那行提示。用户点名要这句，逐字照抄。
AUTO_NOTE = "当前为 每日自动推送中..."

#: QQ 卡片里图片 alt 必须带尺寸（``#宽px #高px``），少了就是裂图。
#: 720 是实测在手机和桌面都不糊也不大的档位；竖图按高度收。
MAX_SIDE = 720

#: 按钮文字要短（用户明确提过）：超过这个长度会被 QQ 截断或挤成两行。
MAX_LABEL = 6

HOWTO_MARKDOWN = """**📮 怎么投稿**

1. 长按图片 → **引用**
2. 同一条消息里 **@我**
3. 输入 `/meme 投稿`

投过的图会进图库，**所有群共用同一份**。"""


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


def _buttons(again: bool, howto: bool, auto: str = "") -> list[list[dict[str, Any]]]:
    """底部按钮行。

    ``auto`` 有三种取值：
      * ``""``（默认）—— 不带自动推送按钮（比如说明卡）；
      * ``"sub"`` —— 未订阅，按钮是「自动订阅」；
      * ``"off"`` —— 已订阅，按钮是「取消自动」（点了要过管理员校验）。

    最多三个按钮，正好是 QQ 一行放得下的宽度；文字必须 ≤ ``MAX_LABEL``。
    """
    row: list[dict[str, Any]] = []
    if again:
        row.append({"id": "again", "label": "再来一张"[:MAX_LABEL], "action_id": ACTION_AGAIN})
    if howto:
        row.append({"id": "howto", "label": "我要投稿"[:MAX_LABEL], "action_id": ACTION_HOWTO})
    if auto == "sub":
        row.append({"id": "auto", "label": "自动订阅"[:MAX_LABEL], "action_id": ACTION_AUTO_SUB})
    elif auto == "off":
        row.append({"id": "auto", "label": "取消自动"[:MAX_LABEL], "action_id": ACTION_AUTO_OFF})
    return [row] if row else []


def build_meme_card(
    item: MemeItem,
    image_url: str = "",
    *,
    caption_limit: int = 160,
    show_again: bool = True,
    show_howto: bool = True,
    auto: str = "",
    width: int = MAX_SIDE,
    height: int = MAX_SIDE,
    note: str = "",
    auto_note: str = "",
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
    if auto_note:
        blocks.append(f"<font color='grey'>{auto_note}</font>")
    if note:
        blocks.append(note)
    return {
        "markdown": "\n".join(blocks),
        "rows": _buttons(show_again, show_howto, auto),
        # 谁都能点：这张卡是发在群里的，锁成发起者会让别人点了没反应
        "owner_mode": "everyone",
        "ttl_seconds": CARD_TTL_SECONDS,
    }


def build_howto_card(*, show_again: bool = True) -> dict[str, Any]:
    """「我要投稿」按钮弹出的说明卡。

    带上「再来一张」是因为很多人点完投稿发现手里没图，回不去了。
    """
    return {
        "markdown": HOWTO_MARKDOWN,
        "rows": _buttons(show_again, False),
        "owner_mode": "everyone",
        "ttl_seconds": CARD_TTL_SECONDS,
    }


def build_auto_pick_card(pcts: dict[str, int]) -> dict[str, Any]:
    """「自动订阅」点下去后的三档选择卡。

    按钮文字跟着配置走：面板把低档改成 5%，这里就显示「低 5%」。这样群里看到的
    数字永远和实际概率一致，不用改代码也不用解释「为什么写 10 其实发了 5」。
    """
    low = int(pcts.get("low", 10))
    mid = int(pcts.get("mid", 25))
    high = int(pcts.get("high", 50))
    markdown = (
        "**🤖 自动搬石**\n"
        "开启后，这个群每小时有概率自动冒一张梗图：\n"
        f"· 低：{low}% / 小时（平均两三天一张）\n"
        f"· 中：{mid}% / 小时（平均一天六张左右）\n"
        f"· 高：{high}% / 小时（平均一天十来张）\n"
        "<font color='grey'>随时可以在卡片上点「取消自动」。夜间和每天条数上限可在面板设置。</font>"
    )
    row = [
        {"id": "low", "label": f"低{low}%"[:MAX_LABEL], "action_id": ACTION_AUTO_LOW},
        {"id": "mid", "label": f"中{mid}%"[:MAX_LABEL], "action_id": ACTION_AUTO_MID},
        {"id": "high", "label": f"高{high}%"[:MAX_LABEL], "action_id": ACTION_AUTO_HIGH},
    ]
    return {
        "markdown": markdown,
        "rows": [row],
        "owner_mode": "everyone",
        "ttl_seconds": CARD_TTL_SECONDS,
    }


def build_auto_done_card(
    level: str = "",
    pct: int = 0,
    *,
    off: bool = False,
    show_again: bool = True,
    show_howto: bool = True,
) -> dict[str, Any]:
    """选完档位 / 退订之后的回执卡。

    回执里带上「再开一次要几步」，因为退订是个不可逆操作 —— 群友点错了得知道怎么回来。
    """
    if off or not level:
        markdown = (
            "**🔕 已关闭自动搬石**\n"
            "这个群不会再自动发梗图了（`/meme` 照常能手动摇）。\n"
            "<font color='grey'>想再开：随便发一张梗图，点卡片上的「自动订阅」。</font>"
        )
        auto = "sub"
    else:
        label = LEVEL_LABELS.get(level, level)
        markdown = (
            f"**✅ 已开启自动搬石：{label}（{pct}%/小时）**\n"
            "每小时整点后随机挑一分钟抽一次骰子，命中就自动发一张。\n"
            "<font color='grey'>抽不中是正常的 —— 概率本来就不高。</font>"
        )
        auto = "off"
    return {
        "markdown": markdown,
        "rows": _buttons(show_again, show_howto, auto),
        "owner_mode": "everyone",
        "ttl_seconds": CARD_TTL_SECONDS,
    }
