"""重画尺寸：按原图比例挑画布、画完掰回原比例。

这两件事是「1024x1024 太奇怪了」这个问题的正解：
横图不该被塞进方框，画回来的比例也该跟投稿那张一致。
"""
from __future__ import annotations

import io

import pytest

from meme.draw import (
    LANDSCAPE,
    PORTRAIT,
    SQUARE,
    choose_draw_size,
    fit_to_reference,
    image_size,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def png(width: int, height: int, color=(40, 120, 220)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


# --- 选画布 -----------------------------------------------------------------

def test_no_dimensions_falls_back_to_square():
    assert choose_draw_size(0, 0) == SQUARE
    assert choose_draw_size(None, None) == SQUARE


def test_wide_image_gets_landscape_canvas():
    assert choose_draw_size(1920, 1080) == LANDSCAPE  # 16:9
    assert choose_draw_size(1600, 1000) == LANDSCAPE  # 8:5


def test_tall_image_gets_portrait_canvas():
    assert choose_draw_size(1080, 1920) == PORTRAIT
    assert choose_draw_size(900, 1600) == PORTRAIT


def test_almost_square_stays_square():
    assert choose_draw_size(1000, 1000) == SQUARE
    assert choose_draw_size(1200, 1000) == SQUARE  # 6:5，离方形近，硬塞横幅会拉宽


def test_explicit_setting_wins():
    """配置里写死了尺寸就听它的 —— 有些线路只认某几档。"""
    assert choose_draw_size(1920, 1080, configured="1024x1024") == "1024x1024"


# --- 掰回原比例 -------------------------------------------------------------

def test_close_enough_result_is_returned_byte_for_byte():
    """差得不多就原样返回：不重编码，画质不掉、png 也不会变成 jpg。"""
    drawn = png(1030, 1024)  # 跟 1:1 差 0.6%
    out = fit_to_reference(drawn, 1000, 1000)
    assert out == drawn


def test_wide_result_is_cropped_to_reference_ratio():
    drawn = png(1536, 1024)  # 3:2
    out = fit_to_reference(drawn, 1600, 900)  # 目标 16:9
    width, height = image_size(out)
    assert width == 1600
    assert abs(width / height - 1600 / 900) < 0.01


def test_tall_result_is_cropped_to_reference_ratio():
    drawn = png(1024, 1536)
    out = fit_to_reference(drawn, 900, 1600)
    width, height = image_size(out)
    assert width == 900
    assert abs(width / height - 900 / 1600) < 0.01


def test_unknown_dimensions_leave_data_untouched():
    drawn = png(800, 800)
    assert fit_to_reference(drawn, 0, 0) == drawn
    assert fit_to_reference(drawn, 800, 0) == drawn


def test_garbage_bytes_are_returned_as_is():
    junk = b"not an image at all"
    assert fit_to_reference(junk, 1920, 1080) == junk
