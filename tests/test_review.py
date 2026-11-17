"""审核口径：解析模型回话 + 按策略判收不收。"""
from __future__ import annotations

import pytest

from meme.review import (
    DEFAULT_REJECT_FLAGS,
    KNOWN_FLAGS,
    ReviewPolicy,
    build_review_prompt,
    extract_json_object,
    judge,
    parse_review,
)
from meme.models import Review


# --- 从回话里挖 JSON --------------------------------------------------------

def test_extracts_plain_json():
    assert extract_json_object('{"is_meme": true}') == {"is_meme": True}


def test_ignores_politeness_prefix():
    text = '好的，这是审核结果：\n{"is_meme": true, "nsfw_level": 0}\n以上。'
    assert extract_json_object(text)["is_meme"] is True


def test_ignores_markdown_fence():
    text = '```json\n{"is_meme": false, "flags": ["gore"]}\n```'
    assert extract_json_object(text)["flags"] == ["gore"]


def test_braces_inside_strings_do_not_break_scanning():
    text = '{"reason": "看这里 {不是对象}", "is_meme": true}'
    assert extract_json_object(text)["reason"] == "看这里 {不是对象}"


def test_returns_none_without_json():
    assert extract_json_object("我觉得还行") is None
    assert extract_json_object("") is None


def test_broken_json_returns_none_not_exception():
    assert extract_json_object('{"is_meme": tru') is None


# --- 解析字段 ---------------------------------------------------------------

def test_parse_reads_every_field():
    review = parse_review(
        '{"is_meme": true, "nsfw_level": 1, "flags": ["Gore", " ads-or-watermark "],'
        ' "caption_text": "when you finally fix it", "language": "EN", "reason": "正常梗图"}'
    )
    assert review.parsed is True
    assert review.is_meme is True
    assert review.nsfw_level == 1
    assert review.flags == ("gore", "ads_or_watermark")
    assert review.caption_text == "when you finally fix it"
    assert review.language == "en"


def test_parse_clamps_and_coerces_level():
    assert parse_review('{"nsfw_level": 9}').nsfw_level == 4
    assert parse_review('{"nsfw_level": -2}').nsfw_level == 0
    assert parse_review('{"nsfw_level": "2"}').nsfw_level == 2
    assert parse_review('{"nsfw_level": "一大堆"}').nsfw_level == 0


def test_parse_flags_from_string():
    assert parse_review('{"flags": "gore, politics"}').flags == ("gore", "politics")


def test_unparseable_text_is_marked_not_passed():
    review = parse_review("这张图挺可爱的")
    assert review.parsed is False
    assert review.is_meme is False
    assert review.error


# --- 提示词 ----------------------------------------------------------------

def test_prompt_lists_every_flag_so_model_cannot_invent_names():
    prompt = build_review_prompt()
    for flag in KNOWN_FLAGS:
        assert flag in prompt


def test_prompt_states_configured_threshold():
    prompt = build_review_prompt(nsfw_max_level=2)
    assert "超过 2" in prompt
    assert "politics" in prompt


# --- 判定 ------------------------------------------------------------------

def _ok(**kwargs):
    base = dict(is_meme=True, nsfw_level=0, flags=(), language="zh")
    base.update(kwargs)
    return Review(**base)


def test_clean_meme_passes():
    accepted, why = judge(_ok(), ReviewPolicy())
    assert accepted is True


def test_rejected_flag_blocks():
    accepted, why = judge(_ok(flags=("politics",)), ReviewPolicy())
    assert accepted is False
    assert "politics" in why


def test_flag_outside_policy_is_allowed():
    accepted, _why = judge(_ok(flags=("text_heavy",)), ReviewPolicy())
    assert accepted is True


@pytest.mark.parametrize(
    "level,expected",
    [(0, True), (1, True), (2, False), (3, False), (4, False)],
)
def test_nsfw_threshold(level, expected):
    accepted, _why = judge(_ok(nsfw_level=level), ReviewPolicy(nsfw_max_level=1))
    assert accepted is expected


def test_nsfw_threshold_is_configurable():
    accepted, _why = judge(_ok(nsfw_level=2), ReviewPolicy(nsfw_max_level=2))
    assert accepted is True


def test_not_a_meme_is_rejected_when_required():
    accepted, why = judge(_ok(is_meme=False), ReviewPolicy(require_is_meme=True))
    assert accepted is False
    assert "梗图" in why


def test_not_a_meme_passes_when_not_required():
    accepted, _why = judge(_ok(is_meme=False), ReviewPolicy(require_is_meme=False))
    assert accepted is True


def test_fail_closed_blocks_when_review_never_parsed():
    review = Review(parsed=False, error="超时")
    accepted, why = judge(review, ReviewPolicy(fail_closed=True))
    assert accepted is False
    assert "审核" in why


def test_fail_open_allows_when_review_never_parsed():
    review = Review(parsed=False, error="超时")
    accepted, _why = judge(review, ReviewPolicy(fail_closed=False))
    assert accepted is True


def test_policy_from_config_reads_every_knob():
    policy = ReviewPolicy.from_config(
        {
            "nsfw_max_level": "2",
            "reject_flags": ["gore", " politics "],
            "require_is_meme": False,
            "moderation_fail_closed": False,
        }
    )
    assert policy.nsfw_max_level == 2
    assert policy.reject_flags == ("gore", "politics")
    assert policy.require_is_meme is False
    assert policy.fail_closed is False


def test_policy_from_empty_config_uses_defaults():
    policy = ReviewPolicy.from_config({})
    assert policy.nsfw_max_level == 1
    assert policy.reject_flags == DEFAULT_REJECT_FLAGS
    assert policy.require_is_meme is True
    assert policy.fail_closed is True
