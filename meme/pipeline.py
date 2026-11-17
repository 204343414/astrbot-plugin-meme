"""统一管线：四个来源都从这一个口进来。

    字节 → 查重 → 审核（有缓存就跳过）→ 判收不收
         → 需要就翻中文 / 重画 → 重画后再审一次 → 落盘入库

放在这里而不是散在命令里的原因很实际：审核和入库的口径一旦有两份实现，
第三方 API 拉的图迟早会绕开审核直接进库，那不是配置问题，是结构问题。
"""
from __future__ import annotations

import inspect
import logging
import os
from typing import Awaitable, Callable

from .chinese import (
    DEFAULT_REDRAW_PROMPT,
    MODE_APPEND,
    MODE_OFF,  # noqa: F401 - 文档用，说明 off 是合法模式
    MODE_REDRAW,
    build_redraw_prompt,
    build_translate_prompt,
    clean_translation,
    needs_translation,
)
from .media import ext_for_mime, human_size, sha256_of, sniff_image, to_data_url
from .models import SCOPE_GROUP, IngestResult, MemeItem, Review
from .review import ReviewPolicy, judge, parse_review
from .store import MemeStore

logger = logging.getLogger(__name__)

#: 多模态审核：吃 data URL / 图片字节，吐模型原始回话（字符串）。
Moderator = Callable[[str], Awaitable[str]]
#: 翻译：吃原文，吐中文。
Translator = Callable[[str], Awaitable[str]]
#: 重绘：吃原图字节 + 提示词，吐新图字节。
Redrawer = Callable[[bytes, str], Awaitable[bytes]]


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class MemePipeline:
    def __init__(
        self,
        store: MemeStore,
        policy: ReviewPolicy,
        *,
        moderator: Moderator | None = None,
        translator: Translator | None = None,
        redrawer: Redrawer | None = None,
        caption_mode: str = MODE_APPEND,
        default_scope: str = SCOPE_GROUP,
        max_bytes: int = 8 * 1024 * 1024,
        redraw_recheck: bool = True,
        caption_limit: int = 160,
    ) -> None:
        self.store = store
        self.policy = policy
        self.moderator = moderator
        self.translator = translator
        self.redrawer = redrawer
        self.caption_mode = caption_mode
        self.default_scope = default_scope
        self.max_bytes = max_bytes
        self.redraw_recheck = redraw_recheck
        self.caption_limit = caption_limit

    # --- 审核 -------------------------------------------------------------

    async def review_bytes(self, data: bytes, mime: str = "image/jpeg") -> Review:
        sha256 = sha256_of(data)
        cached = self.store.cached_review(sha256)
        if cached is not None:
            return cached
        if self.moderator is None:
            return Review(parsed=False, error="未配置审核 Provider")
        try:
            raw = await _maybe_await(self.moderator(to_data_url(data, mime)))
        except Exception as exc:  # 审核挂了不能当成「干净」
            logger.warning("[Meme] 审核调用失败：%s", exc)
            return Review(parsed=False, error=f"审核调用失败：{type(exc).__name__}")
        review = parse_review(raw if isinstance(raw, str) else str(raw))
        if review.parsed:
            self.store.cache_review(sha256, review)
        return review

    # --- 主流程 -----------------------------------------------------------

    async def ingest(
        self,
        data: bytes,
        *,
        origin: str = "submitted",
        contributor: str = "",
        group_id: str = "",
        scope: str = "",
        source_url: str = "",
        tags: tuple[str, ...] = (),
    ) -> IngestResult:
        if not data:
            return IngestResult(ok=False, reason="空文件")
        sniffed = sniff_image(data)
        if sniffed is None:
            return IngestResult(ok=False, reason="这不是图片（或者格式不支持：只收 jpg/png/gif/webp）")
        mime, ext = sniffed
        if self.max_bytes and len(data) > self.max_bytes:
            return IngestResult(
                ok=False,
                reason=f"图太大了（{human_size(len(data))}，上限 {human_size(self.max_bytes)}）",
            )

        sha256 = sha256_of(data)
        existing = self.store.by_sha(sha256)
        if existing is not None:
            if existing.status == "active":
                return IngestResult(
                    ok=True,
                    meme=existing,
                    duplicate=True,
                    reason=f"这张早就在库里了（#{existing.id}）",
                )
            return IngestResult(ok=False, reason="这张图之前被下架了")

        rejected = self.store.rejection_reason(sha256)
        if rejected:
            return IngestResult(ok=False, reason=f"这张之前没收：{rejected}")

        review = await self.review_bytes(data, mime)
        accepted, why = judge(review, self.policy)
        # 拉黑意图与"这张收不收"是两件事：模型可能既判违规又判恶意，
        # 也可能只判恶意（罕见）。两个结论都往上带，由命令层决定要不要执行。
        ban = bool(getattr(review, "ban_submitter", False))
        if not accepted:
            self.store.reject(sha256, why, contributor)
            return IngestResult(
                ok=False, reason=why, review=review,
                banned=ban, ban_reason=getattr(review, "ban_reason", "") or why,
            )

        caption = review.caption_text
        caption_zh = ""
        # off 模式下连翻译请求都不发：既省钱也不会在库里留下没人用的译文
        translate_enabled = self.caption_mode in (MODE_APPEND, MODE_REDRAW)
        if translate_enabled and needs_translation(review) and self.translator is not None:
            try:
                translated = await _maybe_await(
                    self.translator(build_translate_prompt(caption))
                )
                caption_zh = clean_translation(translated if isinstance(translated, str) else "")
            except Exception as exc:
                logger.warning("[Meme] 翻译失败，退回原图：%s", exc)

        redrawn = False
        if (
            self.caption_mode == MODE_REDRAW
            and self.redrawer is not None
            and needs_translation(review)
        ):
            redrawn, data, mime, ext, sha256 = await self._try_redraw(
                data, mime, ext, sha256, caption
            )

        path = self.store.save_media(sha256, data, ext)
        meme_id, created = self.store.add(
            sha256=sha256,
            file_path=path,
            origin=origin,
            scope=scope or self.default_scope,
            group_id=group_id,
            mime=mime,
            caption=caption,
            caption_zh=caption_zh,
            tags=tags,
            nsfw_level=review.nsfw_level,
            contributor=contributor,
            source_url=source_url,
        )
        meme = self.store.get(meme_id) if meme_id else None
        if meme is None:
            return IngestResult(ok=False, reason="入库失败（数据库异常）", review=review)
        return IngestResult(
            ok=True,
            meme=meme,
            review=review,
            redrawn=redrawn,
            duplicate=not created,
            banned=ban,
            ban_reason=getattr(review, "ban_reason", "") if ban else "",
            reason=("重画成中文版后入库" if redrawn else "已入库"),
        )

    async def _try_redraw(
        self, data: bytes, mime: str, ext: str, sha256: str, caption: str
    ) -> tuple[bool, bytes, str, str, str]:
        """重画一版中文的，并（默认）重审。

        任何一步失败都退回原图 + 字幕模式：重画只是加分项，不该让一张
        审核通过的图因为画图接口抽风而投不出去。
        """
        prompt = build_redraw_prompt(DEFAULT_REDRAW_PROMPT)
        try:
            new_data = await _maybe_await(self.redrawer(data, prompt))
        except Exception as exc:
            logger.warning("[Meme] 重画失败，改用字幕模式：%s", exc)
            return (False, data, mime, ext, sha256)
        if not isinstance(new_data, (bytes, bytearray)) or not new_data:
            logger.warning("[Meme] 重画没有返回图片，改用字幕模式")
            return (False, data, mime, ext, sha256)
        new_data = bytes(new_data)
        sniffed = sniff_image(new_data)
        if sniffed is None:
            logger.warning("[Meme] 重画返回的不是图片，改用字幕模式")
            return (False, data, mime, ext, sha256)
        new_mime, new_ext = sniffed
        new_sha = sha256_of(new_data)

        if self.redraw_recheck:
            review = await self.review_bytes(new_data, new_mime)
            accepted, why = judge(review, self.policy)
            if not accepted:
                logger.warning("[Meme] 重画版没过审（%s），退回原图", why)
                return (False, data, mime, ext, sha256)
        return (True, new_data, new_mime, new_ext, new_sha)

    # --- 选图 -------------------------------------------------------------

    def pick(self, group_id: str, weights: dict, rng=None) -> MemeItem | None:
        return self.store.pick(group_id, weights, rng)


def seed_bundled(store: MemeStore, bundled_dir: str, tags: tuple[str, ...] = ("内置",)) -> int:
    """把 ``assets/bundled`` 里的种子图登记进库（已存在就跳过）。

    内置图是我们自己放进去的、上线前人工看过，因此不再花一次审核的钱。
    文件名（去掉后缀）会当成图注，方便 ``/meme 库`` 里认人。
    """
    if not bundled_dir or not os.path.isdir(bundled_dir):
        return 0
    added = 0
    for name in sorted(os.listdir(bundled_dir)):
        path = os.path.join(bundled_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        sniffed = sniff_image(data)
        if sniffed is None:
            continue
        mime, ext = sniffed
        sha256 = sha256_of(data)
        if store.by_sha(sha256) is not None:
            continue
        stem = os.path.splitext(name)[0]
        stored = store.save_media(sha256, data, ext)
        _meme_id, created = store.add(
            sha256=sha256,
            file_path=stored,
            origin="bundled",
            scope="global",
            mime=mime,
            caption=stem if not stem.isascii() else "",
            tags=tags,
        )
        if created:
            added += 1
    return added
