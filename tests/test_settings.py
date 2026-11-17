"""网页调参：白名单、取值范围、部分成功。"""
from __future__ import annotations

import pytest

from meme.settings import (
    FIELDS,
    FLAG_CHOICES,
    apply_settings,
    coerce,
    export_settings,
)


def test_every_field_is_exposed_with_metadata():
    payload = export_settings({"nsfw_max_level": 1})
    keys = [field["key"] for field in payload["fields"]]
    assert keys == [field.key for field in FIELDS]
    assert all(field["label"] for field in payload["fields"])


@pytest.mark.parametrize(
    "raw,expected",
    [(1, 1), ("2", 2), (2.7, 2), (0, 0), (4, 4)],
)
def test_coerce_int(raw, expected):
    value, error = coerce("nsfw_max_level", raw)
    assert (value, error) == (expected, "")


@pytest.mark.parametrize("raw", [-1, 5, "abc", None])
def test_coerce_int_rejects_out_of_range_or_garbage(raw):
    value, error = coerce("nsfw_max_level", raw)
    assert value is None
    assert error


def test_coerce_float():
    assert coerce("weight_api", "0.5") == (0.5, "")
    assert coerce("weight_api", 500)[0] is None


@pytest.mark.parametrize(
    "raw,expected",
    [(True, True), (False, False), ("true", True), ("0", False), (1, True), ("是", True)],
)
def test_coerce_bool(raw, expected):
    assert coerce("require_is_meme", raw)[0] is expected


def test_coerce_choice_accepts_listed_values_only():
    assert coerce("caption_zh_mode", "redraw") == ("redraw", "")
    value, error = coerce("caption_zh_mode", "重画")
    assert value is None and "off/append/redraw" in error


def test_coerce_flags_filters_unknown():
    assert coerce("reject_flags", ["gore", "politics"])[0] == ["gore", "politics"]
    value, error = coerce("reject_flags", ["gore", "神秘类别"])
    assert value is None and "神秘类别" in error


def test_coerce_flags_accepts_comma_string():
    assert coerce("reject_flags", "gore, ads_or_watermark")[0] == ["gore", "ads_or_watermark"]


def test_coerce_rejects_unknown_key():
    value, error = coerce("moderation_provider_id", "x")
    assert value is None and "不认识" in error


def test_apply_settings_writes_only_valid_entries():
    config = {"nsfw_max_level": 1, "caption_zh_mode": "append"}
    applied, errors = apply_settings(
        config,
        {
            "nsfw_max_level": 2,
            "caption_zh_mode": "nope",
            "reject_flags": ["gore"],
            "bogus_key": 1,
        },
    )
    assert applied == {"nsfw_max_level": 2, "reject_flags": ["gore"]}
    assert len(errors) == 2
    assert config["nsfw_max_level"] == 2
    assert config["caption_zh_mode"] == "append"  # 非法值没被写进去
    assert "bogus_key" not in config


def test_apply_settings_is_all_or_nothing_per_key():
    """一个项填错不该把整批都丢掉 —— 那会让人以为页面坏了。"""
    config = {}
    applied, errors = apply_settings(config, {"cooldown_seconds": 30, "send_cooldown_seconds": "x"})
    assert applied == {"cooldown_seconds": 30}
    assert len(errors) == 1


def test_apply_settings_empty_payload():
    config = {}
    assert apply_settings(config, {}) == ({}, [])


def test_export_settings_lists_readonly_provider_fields():
    payload = export_settings({"moderation_provider_id": "openai_1/grok", "redraw_model": "m"})
    assert payload["readonly"]["moderation_provider_id"] == "openai_1/grok"
    assert payload["readonly"]["redraw_model"] == "m"
    assert {meta["key"] for meta in payload["readonlyMeta"]} >= {"moderation_provider_id"}


def test_export_settings_copies_flag_list():
    flags = ["gore"]
    payload = export_settings({"reject_flags": flags})
    payload["values"]["reject_flags"].append("politics")
    assert flags == ["gore"]  # 不能把配置里的列表直接交出去


def test_flag_choices_match_review_known_flags():
    from meme.review import KNOWN_FLAGS

    assert {name for name, _label in FLAG_CHOICES} == set(KNOWN_FLAGS)
