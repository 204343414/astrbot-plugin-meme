"""把英文梗图变中文。

两条路，配置里二选一：

* ``append`` —— 只把图里的字翻出来，挂在图下面那条消息里发（默认）。
  便宜、快、不改变画面，也不会把图改烂。
* ``redraw`` —— 真的调画图模型重画一版中文的。贵、慢，且模型不保证
  「其他不要改动」，所以重画完必须再审一次（见 pipeline）。

英文/日文/韩文都由同一套逻辑处理：判定「图的文字不是中文」→ 翻译 or 重画。
"""
from __future__ import annotations

from .models import Review

MODE_OFF = "off"
MODE_APPEND = "append"
MODE_REDRAW = "redraw"
MODES = (MODE_OFF, MODE_APPEND, MODE_REDRAW)

#: 用户点名要的那句话，原样做默认值。
DEFAULT_REDRAW_PROMPT = "把当前meme梗图，只翻译留言和主要语言变成中文，其他不要改动。"

#: 判「已经是中文」的语言码。模型爱写 zh-CN / Chinese / 中文，都算。
_ZH_LANGS = {
    "zh",
    "zh-cn",
    "zh_cn",
    "zh-hans",
    "zh-hant",
    "zh-tw",
    "cn",
    "zho",
    "chi",
    "chinese",
    "chinesesimplified",
    "中文",
    "汉语",
    "简体中文",
    "繁體中文",
}


def is_chinese(language: str) -> bool:
    token = (language or "").strip().lower().replace(" ", "")
    if not token:
        return False
    if token in _ZH_LANGS:
        return True
    return token.startswith("zh")


def needs_translation(review: Review) -> bool:
    """有文字、且文字不是中文 → 才需要动它。

    没有文字就别翻（翻了只能瞎编），已经中文也别翻（翻了会改掉原味）。
    """
    if not review.parsed:
        return False
    if not review.caption_text.strip():
        return False
    return not is_chinese(review.language)


def build_translate_prompt(caption_text: str) -> str:
    """只翻文字，避免模型顺手「润色」成一句正经话——梗图的语感全在糙里。"""
    return (
        "把下面这段梗图里的文字翻成简体中文，保留原有的语感、口头禅和脏话强度，"
        "不要解释、不要加引号、不要输出任何多余内容，只输出译文：\n"
        f"{caption_text.strip()}"
    )


def build_redraw_prompt(template: str = "", extra: str = "") -> str:
    """重画用的提示词：用户那句话 + 不能改动画面的硬约束。

    光给「翻译成中文」这一句，画图模型常常顺手重画构图、换脸、改配色，
    出来的已经不是同一张梗图了。所以约束要写死。
    """
    base = (template or DEFAULT_REDRAW_PROMPT).strip()
    guard = (
        "约束：只替换图中的文字为简体中文；人物、构图、画风、配色、表情、"
        "水印位置都保持原样；不要新增任何文字或元素；中文要写对，不要出现乱码。"
    )
    return f"{base}\n\n{guard}" + (f"\n{extra.strip()}" if extra.strip() else "")


def clean_translation(text: str) -> str:
    """把模型爱加的前后缀削掉，只留译文本身。"""
    value = (text or "").strip()
    if not value:
        return ""
    for quote in ('"', "“", "”", "'", "「", "」"):
        value = value.replace(quote, "")
    for prefix in ("译文：", "译文:", "翻译：", "翻译:", "中文："):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    value = value.strip()
    # 模型偶尔会把原句一起回过来：只留最后一行
    if "\n" in value:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if lines:
            value = lines[-1]
    return value[:200]


def should_redraw(mode: str, review: Review) -> bool:
    return mode == MODE_REDRAW and needs_translation(review)


def compose_caption(caption: str, caption_zh: str, limit: int = 160) -> str:
    """图下面那一行字。中文译文永远排在前面（群友看的是中文）。"""
    parts = [p.strip() for p in (caption_zh, caption) if p and p.strip()]
    if not parts:
        return ""
    line = " / ".join(dict.fromkeys(parts))
    if limit and len(line) > limit:
        line = line[: max(0, limit - 1)] + "…"
    return line
