"""管线：审核 → 判收 → 中文化 → 重画重审 → 落盘入库。

这里钉死的是「一条链」的行为：审核只花一次钱、拒收的图不会再送到模型面前、
重画失败不能把一张已经过审的图弄丢。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from meme.chinese import MODE_APPEND, MODE_OFF, MODE_REDRAW
from meme.pipeline import MemePipeline
from meme.review import ReviewPolicy
from meme.store import MemeStore

JPEG_HEAD = b"\xff\xd8\xff\xe0"


def img(tag: str = "a", size: int = 64) -> bytes:
    return JPEG_HEAD + tag.encode() + b"\x00" * size


GOOD_REVIEW = {
    "is_meme": True,
    "nsfw_level": 0,
    "flags": [],
    "caption_text": "when the code works",
    "language": "en",
    "reason": "普通梗图",
}


class FakeModerator:
    """按顺序吐出预设回话，并记住自己被调了几次。"""

    def __init__(self, *payloads):
        self.payloads = list(payloads) or [json.dumps(GOOD_REVIEW, ensure_ascii=False)]
        self.calls = 0
        self.seen_urls = []

    async def __call__(self, data_url: str) -> str:
        self.calls += 1
        self.seen_urls.append(data_url)
        index = min(self.calls - 1, len(self.payloads) - 1)
        payload = self.payloads[index]
        if isinstance(payload, Exception):
            raise payload
        return payload


class FakeTranslator:
    def __init__(self, text="当代码跑通的时候"):
        self.text = text
        self.calls = 0
        self.seen = []

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.seen.append(prompt)
        return self.text


class FakeRedrawer:
    def __init__(self, result=None, raises=None, data_tag="zh"):
        self.result = result if result is not None else img(data_tag)
        self.raises = raises
        self.calls = 0
        self.prompts = []

    async def __call__(self, data: bytes, prompt: str) -> bytes:
        self.calls += 1
        self.prompts.append(prompt)
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture()
def store(tmp_path):
    instance = MemeStore(str(tmp_path / "meme.db"), str(tmp_path / "media"))
    instance.init()
    return instance


def make_pipeline(store, **kwargs) -> MemePipeline:
    params = dict(
        moderator=FakeModerator(),
        translator=FakeTranslator(),
        caption_mode=MODE_APPEND,
        max_bytes=1024 * 1024,
    )
    params.update(kwargs)
    return MemePipeline(store, ReviewPolicy(), **params)


def run(coro):
    return asyncio.run(coro)


# --- 正常入库 ---------------------------------------------------------------

def test_clean_submission_is_stored_with_translation(store):
    moderator = FakeModerator()
    translator = FakeTranslator()
    pipeline = make_pipeline(store, moderator=moderator, translator=translator)

    outcome = run(pipeline.ingest(img(), contributor="U1"))

    assert outcome.ok is True
    assert outcome.meme is not None
    assert outcome.meme.caption == "when the code works"
    assert outcome.meme.caption_zh == "当代码跑通的时候"
    assert outcome.meme.contributor == "U1"
    assert os.path.isfile(outcome.meme.file_path)
    assert moderator.calls == 1
    assert translator.calls == 1
    # 多模态是直接吃 data URL 的，不经过图床
    assert moderator.seen_urls[0].startswith("data:image/jpeg;base64,")


def test_chinese_meme_is_not_translated(store):
    payload = dict(GOOD_REVIEW, caption_text="下次一定", language="zh")
    translator = FakeTranslator()
    pipeline = make_pipeline(
        store, moderator=FakeModerator(json.dumps(payload)), translator=translator
    )
    outcome = run(pipeline.ingest(img(), contributor="U1"))
    assert outcome.ok is True
    assert outcome.meme.caption_zh == ""
    assert translator.calls == 0


def test_append_mode_never_redraws(store):
    redrawer = FakeRedrawer()
    pipeline = make_pipeline(store, redrawer=redrawer, caption_mode=MODE_APPEND)
    outcome = run(pipeline.ingest(img(), contributor="U1"))
    assert outcome.redrawn is False
    assert redrawer.calls == 0


def test_off_mode_skips_translation_entirely(store):
    translator = FakeTranslator()
    pipeline = make_pipeline(store, translator=translator, caption_mode=MODE_OFF)
    outcome = run(pipeline.ingest(img(), contributor="U1"))
    assert outcome.ok is True
    assert outcome.meme.caption_zh == ""
    assert translator.calls == 0


# --- 拒收 ------------------------------------------------------------------

def test_rejected_image_is_remembered_and_never_reviewed_twice(store):
    bad = dict(GOOD_REVIEW, flags=["politics"], reason="政治")
    moderator = FakeModerator(json.dumps(bad))
    pipeline = make_pipeline(store, moderator=moderator)

    first = run(pipeline.ingest(img("x"), contributor="U1"))
    second = run(pipeline.ingest(img("x"), contributor="U1"))

    assert first.ok is False
    assert "politics" in first.reason
    assert second.ok is False
    assert "之前没收" in second.reason
    assert moderator.calls == 1
    assert store.rejected_count() == 1
    assert store.stats()["total"] == 0


def test_nsfw_over_threshold_is_rejected(store):
    payload = dict(GOOD_REVIEW, nsfw_level=3)
    pipeline = make_pipeline(store, moderator=FakeModerator(json.dumps(payload)))
    outcome = run(pipeline.ingest(img("n"), contributor="U1"))
    assert outcome.ok is False
    assert "色情等级 3" in outcome.reason


def test_not_a_meme_is_rejected(store):
    payload = dict(GOOD_REVIEW, is_meme=False)
    pipeline = make_pipeline(store, moderator=FakeModerator(json.dumps(payload)))
    outcome = run(pipeline.ingest(img("s"), contributor="U1"))
    assert outcome.ok is False
    assert "不像梗图" in outcome.reason


def test_moderation_error_blocks_when_fail_closed(store):
    pipeline = make_pipeline(
        store, moderator=FakeModerator(RuntimeError("provider 挂了"))
    )
    outcome = run(pipeline.ingest(img("e"), contributor="U1"))
    assert outcome.ok is False
    assert "审核" in outcome.reason
    assert store.stats()["total"] == 0


def test_moderation_error_passes_when_fail_open(store):
    pipeline = MemePipeline(
        store,
        ReviewPolicy(fail_closed=False),
        moderator=FakeModerator(RuntimeError("provider 挂了")),
        caption_mode=MODE_APPEND,
    )
    outcome = run(pipeline.ingest(img("e2"), contributor="U1"))
    assert outcome.ok is True


def test_missing_moderator_is_not_a_pass(store):
    pipeline = MemePipeline(store, ReviewPolicy(), moderator=None)
    outcome = run(pipeline.ingest(img("m"), contributor="U1"))
    assert outcome.ok is False


def test_unparseable_moderation_answer_is_fail_closed(store):
    pipeline = make_pipeline(store, moderator=FakeModerator("这张图挺可爱的"))
    outcome = run(pipeline.ingest(img("u"), contributor="U1"))
    assert outcome.ok is False


# --- 输入校验 ---------------------------------------------------------------

def test_non_image_bytes_are_refused(store):
    pipeline = make_pipeline(store)
    outcome = run(pipeline.ingest(b"%PDF-1.4 not an image", contributor="U1"))
    assert outcome.ok is False
    assert "不是图片" in outcome.reason


def test_empty_payload_is_refused(store):
    pipeline = make_pipeline(store)
    assert run(pipeline.ingest(b"", contributor="U1")).ok is False


def test_oversized_image_is_refused(store):
    pipeline = make_pipeline(store, max_bytes=10)
    outcome = run(pipeline.ingest(img("big", 200), contributor="U1"))
    assert outcome.ok is False
    assert "太大" in outcome.reason


def test_png_is_accepted_with_its_own_extension(store):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    pipeline = make_pipeline(store)
    outcome = run(pipeline.ingest(png, contributor="U1"))
    assert outcome.ok is True
    assert outcome.meme.file_path.endswith(".png")
    assert outcome.meme.mime == "image/png"


# --- 去重与缓存 -------------------------------------------------------------

def test_same_image_twice_is_a_duplicate_not_a_second_row(store):
    pipeline = make_pipeline(store)
    first = run(pipeline.ingest(img("dup"), contributor="U1"))
    second = run(pipeline.ingest(img("dup"), contributor="U2"))
    assert first.ok and second.ok
    assert second.duplicate is True
    assert first.meme.id == second.meme.id
    assert store.stats()["total"] == 1


def test_review_is_cached_so_the_model_is_paid_once(store):
    moderator = FakeModerator()
    pipeline = make_pipeline(store, moderator=moderator)
    run(pipeline.ingest(img("cache"), contributor="U1"))
    # 第二张不同图 → 再调一次；再跑一次同样的图 → 命中缓存不再调
    run(pipeline.ingest(img("cache2"), contributor="U1"))
    assert moderator.calls == 2
    review = run(pipeline.review_bytes(img("cache"), "image/jpeg"))
    assert review.caption_text == "when the code works"
    assert moderator.calls == 2


# --- 重画 ------------------------------------------------------------------

def test_redraw_mode_stores_the_chinese_version(store):
    moderator = FakeModerator()
    redrawer = FakeRedrawer()
    pipeline = make_pipeline(
        store, moderator=moderator, redrawer=redrawer, caption_mode=MODE_REDRAW
    )
    outcome = run(pipeline.ingest(img("orig"), contributor="U1"))

    assert outcome.redrawn is True
    assert redrawer.calls == 1
    with open(outcome.meme.file_path, "rb") as fh:
        stored = fh.read()
    assert stored.startswith(b"\xff\xd8\xff")
    assert stored != img("orig")
    # 原图 + 重画版各审一次
    assert moderator.calls == 2


def test_redraw_prompt_carries_the_guard_rails(store):
    redrawer = FakeRedrawer()
    pipeline = make_pipeline(store, redrawer=redrawer, caption_mode=MODE_REDRAW)
    run(pipeline.ingest(img("p"), contributor="U1"))
    prompt = redrawer.prompts[0]
    assert "只翻译留言和主要语言变成中文" in prompt
    assert "其他不要改动" in prompt or "构图" in prompt


def test_redraw_failure_falls_back_to_caption(store):
    redrawer = FakeRedrawer(raises=RuntimeError("画图接口 502"))
    pipeline = make_pipeline(store, redrawer=redrawer, caption_mode=MODE_REDRAW)
    outcome = run(pipeline.ingest(img("f"), contributor="U1"))
    assert outcome.ok is True
    assert outcome.redrawn is False
    assert outcome.meme.caption_zh == "当代码跑通的时候"


def test_redraw_returning_garbage_falls_back(store):
    redrawer = FakeRedrawer(result=b"not an image at all")
    pipeline = make_pipeline(store, redrawer=redrawer, caption_mode=MODE_REDRAW)
    outcome = run(pipeline.ingest(img("g"), contributor="U1"))
    assert outcome.ok is True
    assert outcome.redrawn is False


def test_redrawn_version_that_fails_recheck_is_discarded(store):
    """重画版被判违规 → 退回原图，而不是把违规图入库。"""
    bad = dict(GOOD_REVIEW, nsfw_level=4, flags=["sexual"])
    moderator = FakeModerator(json.dumps(GOOD_REVIEW), json.dumps(bad))
    redrawer = FakeRedrawer()
    pipeline = make_pipeline(
        store, moderator=moderator, redrawer=redrawer, caption_mode=MODE_REDRAW
    )
    outcome = run(pipeline.ingest(img("rc"), contributor="U1"))

    assert outcome.ok is True
    assert outcome.redrawn is False
    with open(outcome.meme.file_path, "rb") as fh:
        assert fh.read() == img("rc")


def test_recheck_can_be_disabled(store):
    bad = dict(GOOD_REVIEW, nsfw_level=4)
    moderator = FakeModerator(json.dumps(GOOD_REVIEW), json.dumps(bad))
    pipeline = make_pipeline(
        store,
        moderator=moderator,
        redrawer=FakeRedrawer(),
        caption_mode=MODE_REDRAW,
        redraw_recheck=False,
    )
    outcome = run(pipeline.ingest(img("nr"), contributor="U1"))
    assert outcome.redrawn is True
    assert moderator.calls == 1


# --- 选图 ------------------------------------------------------------------

def test_pipeline_pick_uses_origin_weights(store):
    run(make_pipeline(store).ingest(img("w"), contributor="U1"))
    picked = store.pick({"api": 0, "submitted": 1})
    assert picked is not None and picked.origin == "submitted"
