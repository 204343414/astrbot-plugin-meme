"""中文化：什么时候该翻、怎么翻、警告词怎么写。"""
from __future__ import annotations

from meme.chinese import (
    DEFAULT_REDRAW_PROMPT,
    MODE_APPEND,
    MODE_OFF,
    MODE_REDRAW,
    build_redraw_prompt,
    build_translate_prompt,
    clean_translation,
    compose_caption,
    is_chinese,
    needs_translation,
    should_redraw,
)
from meme.models import Review


def _review(language="en", text="when the code finally works", parsed=True):
    return Review(is_meme=True, language=language, caption_text=text, parsed=parsed)


# --- 语言判定 --------------------------------------------------------------

def test_chinese_language_codes():
    for value in ("zh", "zh-CN", "zh_hans", "Chinese", "中文", "简体中文", "CN"):
        assert is_chinese(value) is True


def test_non_chinese_language_codes():
    for value in ("en", "ja", "ko", "ru", ""):
        assert is_chinese(value) is False


def test_english_meme_needs_translation():
    assert needs_translation(_review()) is True


def test_chinese_meme_does_not_need_translation():
    assert needs_translation(_review(language="zh", text="下次一定")) is False


def test_meme_without_text_is_left_alone():
    """没文字就别翻——翻了只能瞎编。"""
    assert needs_translation(_review(text="")) is False


def test_unparsed_review_never_triggers_translation():
    assert needs_translation(_review(parsed=False)) is False


def test_should_redraw_only_in_redraw_mode():
    assert should_redraw(MODE_REDRAW, _review()) is True
    assert should_redraw(MODE_APPEND, _review()) is False
    assert should_redraw(MODE_OFF, _review()) is False
    assert should_redraw(MODE_REDRAW, _review(language="zh", text="好耶")) is False


# --- 提示词 ----------------------------------------------------------------

def test_translate_prompt_carries_the_text():
    prompt = build_translate_prompt("  nobody:  \n me:")
    assert "nobody" in prompt
    assert "简体中文" in prompt
    assert "只输出译文" in prompt


def test_redraw_prompt_keeps_user_wording_and_adds_guards():
    prompt = build_redraw_prompt(DEFAULT_REDRAW_PROMPT)
    assert DEFAULT_REDRAW_PROMPT in prompt
    # 这几条约束就是「其他不要改动」的落地
    assert "构图" in prompt
    assert "不要新增" in prompt


def test_redraw_prompt_is_customisable():
    prompt = build_redraw_prompt("只把气泡里的字换成中文", "画面是一位猫")
    assert "只把气泡里的字换成中文" in prompt
    assert "画面是一位猫" in prompt


# --- 清洗模型输出 -----------------------------------------------------------

def test_clean_translation_strips_quotes_and_labels():
    assert clean_translation('译文：“当代码终于跑通时”') == "当代码终于跑通时"


def test_clean_translation_keeps_last_line_when_model_echoes():
    text = "when the code works\n当代码跑通的时候"
    assert clean_translation(text) == "当代码跑通的时候"


def test_clean_translation_handles_empty():
    assert clean_translation("") == ""
    assert clean_translation(None) == ""


# --- 图注 ------------------------------------------------------------------

def test_caption_puts_chinese_first():
    assert compose_caption("see you", "回头见") == "回头见 / see you"


def test_caption_without_translation_is_original():
    assert compose_caption("see you", "") == "see you"


def test_caption_truncates():
    line = compose_caption("a" * 300, "", limit=10)
    assert line.endswith("…")
    assert len(line) == 10


def test_caption_empty_when_nothing_to_say():
    assert compose_caption("", "") == ""
