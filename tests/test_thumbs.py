"""缩略图：会缩小、会缓存、坏图不炸。"""
from __future__ import annotations

import io
import os

import pytest

from meme import thumbs
from meme.thumbs import GALLERY_THUMB, data_url, make_thumb, read_thumb, thumb_cache_path

Image = pytest.importorskip("PIL.Image")


def jpeg(width=800, height=600, color=(200, 90, 30)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_thumb_shrinks_and_stays_jpeg():
    original = jpeg()
    thumb = make_thumb(original, size=GALLERY_THUMB)
    assert thumb.startswith(b"\xff\xd8")
    assert max(size_of(thumb)) == GALLERY_THUMB
    assert len(thumb) < len(original)


def test_square_source_keeps_aspect():
    thumb = make_thumb(jpeg(1000, 1000), size=200)
    assert size_of(thumb) == (200, 200)


def test_small_image_is_not_upscaled():
    thumb = make_thumb(jpeg(120, 90), size=300)
    assert size_of(thumb) == (120, 90)


def test_garbage_input_is_returned_as_is():
    assert make_thumb(b"not an image") == b"not an image"
    assert make_thumb(b"") == b""


def test_cache_path_is_bucketed():
    path = thumb_cache_path("/cache", "abcdef1234", 300)
    assert path.endswith(os.path.join("ab", "abcdef1234_300.jpg"))


def test_read_thumb_writes_cache_then_reuses_it(tmp_path, monkeypatch):
    source = tmp_path / "src.jpg"
    source.write_bytes(jpeg())
    cache = tmp_path / "cache"

    calls = {"n": 0}
    real = thumbs.make_thumb

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(thumbs, "make_thumb", counting)

    first = read_thumb(str(source), "a" * 64, str(cache), GALLERY_THUMB)
    second = read_thumb(str(source), "a" * 64, str(cache), GALLERY_THUMB)

    assert first == second
    assert calls["n"] == 1  # 第二次直接读缓存
    assert os.path.isfile(thumb_cache_path(str(cache), "a" * 64, GALLERY_THUMB))


def test_read_thumb_without_pil_returns_original(tmp_path, monkeypatch):
    source = tmp_path / "src.jpg"
    original = jpeg()
    source.write_bytes(original)
    monkeypatch.setattr(thumbs, "Image", None)
    assert read_thumb(str(source), "b" * 64, str(tmp_path / "c"), 300) == original


def test_read_thumb_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        read_thumb(str(tmp_path / "nope.jpg"), "c" * 64, str(tmp_path / "c"))


def test_data_url_prefix():
    assert data_url(b"abc").startswith("data:image/jpeg;base64,")
