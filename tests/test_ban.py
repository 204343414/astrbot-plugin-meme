"""恶意投稿 → 永久禁止投稿。这个文件里最重要的不是"能不能拉黑"，是**别误杀**。

用户的原话：「拉黑只针对那种明显恶意想让 bot 被封号的那种内容，如果只是
地狱笑话或者擦边和微恶俗还是通过的」。

所以两边都要钉：
* 正例（真时政/露骨色情/真实血腥/违法宣传）→ 拉黑；
* 反例（地狱笑话、擦边、微恶俗、政治谐音梗、怪图）→ **不拉黑，最多拒收**；
* 模型把布尔写成 ``"true"`` / ``"是"`` 要认，写成 ``"false"`` / 空 / 缺失一律不认。
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest

from meme.pipeline import MemePipeline
from meme.review import ReviewPolicy, build_review_prompt, parse_review

Image = pytest.importorskip("PIL.Image")


def jpeg_bytes(color=(10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (80, 80), color).save(buffer, "JPEG")
    return buffer.getvalue()


def review_json(**kwargs) -> str:
    base = {
        "is_meme": True,
        "nsfw_level": 0,
        "flags": [],
        "caption_text": "",
        "language": "zh",
        "reason": "ok",
        "ban_submitter": False,
        "ban_reason": "",
    }
    base.update(kwargs)
    return json.dumps(base, ensure_ascii=False)


class FakeModerator:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def __call__(self, data_url):
        self.calls += 1
        return self.payload


class FakeTranslator:
    async def __call__(self, prompt):
        return ""


# --- 提示词里必须有的边界 ---------------------------------------------------

def test_prompt_lists_what_counts_as_malicious():
    prompt = build_review_prompt(ban_enabled=True)
    assert "ban_submitter" in prompt
    for word in ("时政", "露骨色情", "血腥", "钓封号"):
        assert word in prompt


def test_prompt_lists_what_must_not_count():
    prompt = build_review_prompt(ban_enabled=True)
    for word in ("地狱笑话", "擦边", "微恶俗", "谐音梗"):
        assert word in prompt
    assert "拿不准就 false" in prompt


def test_prompt_without_ban_switch_has_no_ban_rules():
    prompt = build_review_prompt(ban_enabled=False)
    assert "地狱笑话" not in prompt


# --- 解析 -------------------------------------------------------------------

@pytest.mark.parametrize("value", [True, "true", "True", "是", 1, "yes"])
def test_ban_flag_accepts_explicit_true(value):
    review = parse_review(review_json(ban_submitter=value, ban_reason="露骨色情"))
    assert review.ban_submitter is True
    assert review.ban_reason == "露骨色情"


@pytest.mark.parametrize("value", [False, "false", "False", "", None, 0, "否", "no", "maybe"])
def test_ban_flag_rejects_everything_else(value):
    assert parse_review(review_json(ban_submitter=value)).ban_submitter is False


def test_missing_ban_field_is_not_a_ban():
    payload = json.loads(review_json())
    payload.pop("ban_submitter")
    assert parse_review(json.dumps(payload, ensure_ascii=False)).ban_submitter is False


def test_review_round_trips_through_cache():
    from meme.models import Review

    review = Review(ban_submitter=True, ban_reason="露骨色情")
    again = Review.from_json(review.to_json())
    assert again.ban_submitter is True and again.ban_reason == "露骨色情"


# --- 管线把结论带出来 -------------------------------------------------------

def pipeline_with(tmp_path, payload) -> tuple[MemePipeline, FakeModerator]:
    from tests.test_store_helpers import build_store  # 见下（同文件内定义）

    store = build_store(tmp_path)
    moderator = FakeModerator(payload)
    return (
        MemePipeline(store, ReviewPolicy(), moderator=moderator, translator=FakeTranslator()),
        moderator,
    )


@pytest.mark.parametrize(
    "flags,reason",
    [
        (["politics"], "真实时政敏感图，故意传播"),
        (["sexual"], "露骨色情"),
        (["gore"], "真实血腥画面"),
    ],
)
def test_malicious_submission_reports_ban(tmp_path, flags, reason):
    pipeline, _moderator = pipeline_with(
        tmp_path,
        review_json(is_meme=False, nsfw_level=4, flags=flags,
                    reason=reason, ban_submitter=True, ban_reason=reason),
    )
    outcome = asyncio.run(pipeline.ingest(jpeg_bytes(), origin="submitted"))
    assert outcome.ok is False
    assert outcome.banned is True
    assert outcome.ban_reason == reason


@pytest.mark.parametrize(
    "caption,reason",
    [
        ("哈哈哈哈这个骷髅好可爱", "地狱笑话"),
        ("露肩自拍，擦边", "轻微擦边"),
        ("屎尿屁谐音梗", "微恶俗"),
        ("颜色地图玩梗", "政治谐音梗"),
    ],
)
def test_harmless_edge_cases_are_not_banned(tmp_path, caption, reason):
    """地狱笑话 / 擦边 / 微恶俗 / 谐音梗：可以拒收，但绝不拉黑。"""
    pipeline, _moderator = pipeline_with(
        tmp_path,
        review_json(caption_text=caption, reason=reason, ban_submitter=False, nsfw_level=1),
    )
    outcome = asyncio.run(pipeline.ingest(jpeg_bytes(), origin="submitted"))
    assert outcome.banned is False


def test_accepted_image_can_still_carry_ban_flag(tmp_path):
    """模型判"这张能收"但同时说这人在钓封号：结论要一起带上去。"""
    pipeline, _moderator = pipeline_with(
        tmp_path, review_json(ban_submitter=True, ban_reason="连环投同类违规图")
    )
    outcome = asyncio.run(pipeline.ingest(jpeg_bytes(), origin="submitted"))
    assert outcome.ok is True
    assert outcome.banned is True


def test_ban_switch_off_keeps_prompt_but_pipeline_still_reports(tmp_path):
    """配置关掉拉黑时，提示词不带那张表（模型就不会建议）；真收到建议也照实上报，
    最终执行与否由命令层看配置决定。"""
    from meme.review import ReviewPolicy as Policy

    from tests.test_store_helpers import build_store

    store = build_store(tmp_path)
    policy = Policy(ban_enabled=False)
    pipeline = MemePipeline(store, policy, moderator=FakeModerator(
        review_json(is_meme=False, flags=["politics"], ban_submitter=True, ban_reason="时政")
    ))
    outcome = asyncio.run(pipeline.ingest(jpeg_bytes(), origin="submitted"))
    assert outcome.banned is True
    assert policy.ban_enabled is False
