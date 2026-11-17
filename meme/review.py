"""审核：让多模态模型看图，把结论压成一份 JSON，再按策略判收不收。

设计上只留**类别开关 + 阈值**，不写任何关键词表：梗图的文本本来就是
错别字、谐音、黑话，关键词黑名单只会一边误杀好图一边漏掉真问题图，
而真正看得懂画面的是模型。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .models import Review

#: 模型可以打的所有标签。新增标签要同步改 _conf_schema 的 reject_flags 文案。
KNOWN_FLAGS = (
    "sexual",  # 裸露、性暗示
    "minor",  # 出现未成年
    "gore",  # 血腥、尸体、伤口
    "violence",  # 打斗、虐待（不见血）
    "politics",  # 现实政治人物/事件/宣传
    "hate",  # 歧视、攻击性符号
    "ads_or_watermark",  # 广告、二维码、明显水印
    "text_heavy",  # 文字占比过高（更像截图公告而不是梗图）
    "other",
)

#: 默认直接拒收的类别。色情走 nsfw_level 阈值，不在这里重复。
DEFAULT_REJECT_FLAGS = ("gore", "politics", "ads_or_watermark", "minor", "hate")

_LEVEL_NAMES = ("安全", "轻微暗示", "明显擦边", "接近色情", "露骨色情")

PROMPT_HEAD = """你是群聊里的梗图审核员。看这张图，只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。

字段：
- "is_meme": 布尔。它是「能让人会心一笑的梗图/表情包/沙雕图」则为 true；
  纯截图公告、长文、风景照、自拍、聊天记录截图、色情图则为 false。
- "nsfw_level": 0-4 的整数。0 安全，1 轻微暗示，2 明显擦边，3 接近色情，4 露骨色情。
- "flags": 字符串数组，元素只能从下面这些里选，没有就给空数组：
  {flags}
- "caption_text": 图中出现的文字，原样抄写（保留原语言，包括错别字和乱码）。
  没有文字就给空字符串。最多 120 个字符。
- "language": 图中文字的主要语言，用 ISO 代码，如 "zh"、"en"、"ja"；
  没有文字或无法判断就给空字符串。
- "reason": 一句话说明判断理由，不超过 30 字。

只输出 JSON。"""


def build_review_prompt(
    reject_flags: tuple[str, ...] = DEFAULT_REJECT_FLAGS,
    nsfw_max_level: int = 1,
) -> str:
    """把策略写进提示词。

    白名单式地列出所有标签，比让模型自由发挥再回来做字符串匹配稳得多：
    它一旦写成 ``政治敏感`` 或 ``political``，本地就永远匹配不上
    ``politics``，图会当成干净的放出去。
    """
    head = PROMPT_HEAD.format(flags="、".join(KNOWN_FLAGS))
    rules = [
        "\n注意：",
        f"- 色情等级超过 {nsfw_max_level} 的图会被丢弃，请如实打级。",
        "- 带二维码、公众号水印、引流文字的一律标 ads_or_watermark。",
        "- 只是「有点低俗但不露」的，nsfw_level 给 1，不要直接拉满。",
    ]
    if reject_flags:
        rules.append("- 以下类别会被直接丢弃：" + "、".join(reject_flags))
    return head + "\n".join(rules)


def extract_json_object(text: str) -> dict | None:
    """从模型的回话里挖出第一个完整的 JSON 对象。

    模型很爱在前面加「好的，这是结果：」或者用 ```json 包起来，直接
    ``json.loads`` 会失败，而失败在 fail_closed 模式下等于把所有图都拒了。
    这里按大括号配对扫描，比正则可靠（字符串里也可能出现大括号）。
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : idx + 1]
                    try:
                        parsed = json.loads(chunk)
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def _coerce_level(value) -> int:
    try:
        level = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(4, level))


def parse_review(text: str) -> Review:
    """模型回话 → Review。解析不出来就标 ``parsed=False``，绝不假装通过。"""
    data = extract_json_object(text)
    if data is None:
        return Review(parsed=False, error="模型没有返回可解析的 JSON")
    flags = data.get("flags") or []
    if isinstance(flags, str):
        flags = [f.strip() for f in flags.split(",")]
    cleaned = []
    for flag in flags:
        name = str(flag).strip().lower().replace("-", "_").replace(" ", "_")
        if name:
            cleaned.append(name)
    return Review(
        is_meme=bool(data.get("is_meme", False)),
        nsfw_level=_coerce_level(data.get("nsfw_level", 0)),
        flags=tuple(dict.fromkeys(cleaned)),
        reason=str(data.get("reason", "") or "")[:120],
        caption_text=str(data.get("caption_text", "") or "").strip()[:200],
        language=str(data.get("language", "") or "").strip().lower()[:16],
        parsed=True,
    )


@dataclass(frozen=True)
class ReviewPolicy:
    """收不收的判据。全部来自插件配置，改配置就能改口径。"""

    nsfw_max_level: int = 1
    reject_flags: tuple[str, ...] = DEFAULT_REJECT_FLAGS
    require_is_meme: bool = True
    fail_closed: bool = True

    @classmethod
    def from_config(cls, config) -> "ReviewPolicy":
        raw_flags = config.get("reject_flags", DEFAULT_REJECT_FLAGS) or ()
        if isinstance(raw_flags, str):
            raw_flags = [f.strip() for f in raw_flags.split(",") if f.strip()]
        flags = tuple(str(f).strip().lower() for f in raw_flags if str(f).strip())
        return cls(
            nsfw_max_level=_coerce_level(config.get("nsfw_max_level", 1)),
            reject_flags=flags,
            require_is_meme=bool(config.get("require_is_meme", True)),
            fail_closed=bool(config.get("moderation_fail_closed", True)),
        )


def judge(review: Review, policy: ReviewPolicy) -> tuple[bool, str]:
    """``(收不收, 理由)``。理由会原样回给投稿人，所以要短、要说人话。"""
    if not review.parsed:
        if policy.fail_closed:
            return False, "审核没跑成功，先不收（可在配置里关掉「审核失败即拒收」）"
        return True, "审核没跑成功，已按配置放行"

    hits = [flag for flag in review.flags if flag in policy.reject_flags]
    if hits:
        return False, "命中禁发类别：" + "、".join(hits)

    if review.nsfw_level > policy.nsfw_max_level:
        level = _LEVEL_NAMES[review.nsfw_level]
        return False, f"色情等级 {review.nsfw_level}（{level}）高于上限 {policy.nsfw_max_level}"

    if policy.require_is_meme and not review.is_meme:
        return False, "看着不像梗图（像是截图/照片/公告）"

    return True, review.reason or "通过"
