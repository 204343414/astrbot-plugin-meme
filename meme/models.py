"""梗图的数据形状：库里的一行、一次审核的结论。

三个 dataclass 就够表达全部状态，避免把 dict 传来传去：字段名打错在
Python 里不会报错，只会静默变成 ``None``，而梗图管线里 ``None`` 会一路
传到发送环节才炸。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields

#: 一张图的来源。权重配置按这个枚举取值，新增来源要同步改 _conf_schema。
ORIGINS = ("bundled", "api", "submitted", "generated")

#: 投稿作用域：group = 只在本群可见（默认，避版权扩散），global = 所有群可见。
SCOPE_GROUP = "group"
SCOPE_GLOBAL = "global"
SCOPES = (SCOPE_GROUP, SCOPE_GLOBAL)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


@dataclass(frozen=True)
class MemeItem:
    """库里的一行。"""

    id: int = 0
    sha256: str = ""
    origin: str = "submitted"
    scope: str = SCOPE_GROUP
    group_id: str = ""
    file_path: str = ""
    mime: str = "image/jpeg"
    #: 图里出现的原文（审核时由多模态模型抄写下来的）
    caption: str = ""
    #: 原文的中文译文（只有需要翻译时才非空）
    caption_zh: str = ""
    tags: tuple[str, ...] = ()
    nsfw_level: int = 0
    status: str = STATUS_ACTIVE
    contributor: str = ""
    source_url: str = ""
    created_at: float = 0.0
    used_at: float = 0.0
    used_count: int = 0

    @classmethod
    def from_row(cls, row) -> "MemeItem":
        data = dict(row)
        raw_tags = data.get("tags") or ""
        if isinstance(raw_tags, str):
            tags = tuple(t for t in (s.strip() for s in raw_tags.split(",")) if t)
        else:
            tags = tuple(raw_tags)
        allowed = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in allowed}
        kwargs["tags"] = tags
        return cls(**kwargs)

    def caption_line(self, limit: int = 160) -> str:
        """发消息时挂在图下面的那一行文字。

        有中文译文就「原文 / 译文」，否则退回原文；都空就是空字符串，
        调用方据此决定要不要发纯图。
        """
        parts = [p.strip() for p in (self.caption, self.caption_zh) if p and p.strip()]
        if not parts:
            return ""
        line = " / ".join(parts)
        if limit and len(line) > limit:
            line = line[: max(0, limit - 1)] + "…"
        return line


@dataclass
class Review:
    """一次多模态审核的结论。

    ``parsed=False`` 表示模型没给出可解析的 JSON —— 这和「审核通过」完全是
    两件事，混在一起会让 fail_closed 失效。
    """

    is_meme: bool = False
    nsfw_level: int = 0
    flags: tuple[str, ...] = ()
    reason: str = ""
    caption_text: str = ""
    language: str = ""
    parsed: bool = True
    error: str = ""
    #: 模型认为「这个投稿人是故意的、想让 bot 因违规被封号」。真正的动作在
    #: 服务端执行（见 main.py 的 _ban_submitter），模型只负责举手。
    ban_submitter: bool = False
    ban_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "is_meme": self.is_meme,
            "nsfw_level": self.nsfw_level,
            "flags": list(self.flags),
            "reason": self.reason,
            "caption_text": self.caption_text,
            "language": self.language,
            "parsed": self.parsed,
            "error": self.error,
            "ban_submitter": self.ban_submitter,
            "ban_reason": self.ban_reason,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Review":
        data = data or {}
        flags = data.get("flags") or ()
        if isinstance(flags, str):
            flags = tuple(f.strip() for f in flags.split(",") if f.strip())
        return cls(
            is_meme=bool(data.get("is_meme", False)),
            nsfw_level=int(data.get("nsfw_level", 0) or 0),
            flags=tuple(str(f) for f in flags),
            reason=str(data.get("reason", "") or ""),
            caption_text=str(data.get("caption_text", "") or ""),
            language=str(data.get("language", "") or ""),
            parsed=bool(data.get("parsed", True)),
            error=str(data.get("error", "") or ""),
            ban_submitter=bool(data.get("ban_submitter", False)),
            ban_reason=str(data.get("ban_reason", "") or "")[:60],
        )

    @classmethod
    def from_json(cls, text: str) -> "Review":
        try:
            return cls.from_dict(json.loads(text))
        except (TypeError, ValueError):
            return cls(parsed=False, error="审核缓存损坏")


@dataclass
class IngestResult:
    """投稿/入库的最终结论，给命令层拼回话用。"""

    ok: bool = False
    meme: MemeItem | None = None
    reason: str = ""
    review: Review | None = None
    redrawn: bool = False
    cached: bool = False
    duplicate: bool = False
    #: 审核模型点了「拉黑」工具。是否真的执行由命令层决定（管理员豁免等）。
    banned: bool = False
    ban_reason: str = ""
    extra: dict = field(default_factory=dict)
