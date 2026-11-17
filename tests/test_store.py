"""图库：去重、随机挑选、审核缓存、推送状态。"""
from __future__ import annotations

import os

import pytest

from meme.models import MemeItem, Review
from meme.select import pick_meme
from meme.store import MemeStore


@pytest.fixture()
def store(tmp_path):
    instance = MemeStore(
        str(tmp_path / "meme.db"), str(tmp_path / "media"), now=lambda: 1000.0
    )
    instance.init()
    return instance


def _item(meme_id=1, origin="submitted", used_count=0, used_at=0.0):
    return MemeItem(
        id=meme_id,
        origin=origin,
        used_count=used_count,
        used_at=used_at,
    )


# --- 入库 / 去重 ------------------------------------------------------------

def test_add_then_get(store):
    meme_id, created = store.add(sha256="a" * 64, file_path="/tmp/a.jpg", caption="hi")
    assert created is True
    item = store.get(meme_id)
    assert item.sha256 == "a" * 64
    assert item.caption == "hi"
    assert item.status == "active"


def test_same_sha_is_only_stored_once(store):
    first_id, created = store.add(sha256="b" * 64, file_path="/tmp/b.jpg")
    second_id, created_again = store.add(sha256="b" * 64, file_path="/tmp/b2.jpg")
    assert created is True
    assert created_again is False
    assert first_id == second_id
    assert store.stats()["total"] == 1


def test_tags_round_trip(store):
    meme_id, _ = store.add(sha256="c" * 64, file_path="/tmp/c.jpg", tags=("猫", "猫", "狗"))
    assert store.get(meme_id).tags == ("猫", "狗")


def test_delete_removes_row(store):
    meme_id, _ = store.add(sha256="d" * 64, file_path="/tmp/d.jpg")
    removed = store.delete(meme_id)
    assert removed is not None and removed.id == meme_id
    assert store.get(meme_id) is None
    assert store.delete(meme_id) is None


def test_media_path_buckets_by_prefix(store):
    path = store.media_path("abcdef123", ".png")
    assert path.endswith(os.path.join("ab", "abcdef123.png"))


def test_save_media_writes_file(store, tmp_path):
    path = store.save_media("f" * 64, b"hello", ".jpg")
    with open(path, "rb") as fh:
        assert fh.read() == b"hello"
    assert not os.path.exists(path + ".part")


# --- 池子 ----------------------------------------------------------------

def test_pool_is_shared_by_every_group(store):
    """没有「单群库」了：谁问都是同一批图。"""
    store.add(sha256="1" * 64, file_path="/tmp/1.jpg")
    store.add(sha256="2" * 64, file_path="/tmp/2.jpg")
    pool = {item.sha256 for item in store.random_pool()}
    assert pool == {"1" * 64, "2" * 64}


def test_disabled_images_are_not_offered(store):
    meme_id, _ = store.add(sha256="4" * 64, file_path="/tmp/4.jpg")
    store.set_status(meme_id, "disabled")
    assert store.random_pool() == []


def test_stats_counts_active_and_origins(store):
    store.add(sha256="5" * 64, file_path="/a", origin="api")
    store.add(sha256="6" * 64, file_path="/b")
    disabled_id, _ = store.add(sha256="7" * 64, file_path="/c")
    store.set_status(disabled_id, "disabled")
    store.reject("8" * 64, "不像梗图")
    stats = store.stats()
    assert stats["total"] == 3
    assert stats["active"] == 2
    assert stats["by_origin"] == {"api": 1, "submitted": 1}
    assert stats["rejected"] == 1


# --- 随机挑选（纯函数）------------------------------------------------------

def test_pick_prefers_never_used():
    items = [
        _item(1, origin="api", used_count=5, used_at=900),
        _item(2, origin="api", used_count=0, used_at=0),
    ]
    for seed in range(20):
        import random

        chosen = pick_meme(items, {"api": 1}, random.Random(seed))
        assert chosen.id == 2


def test_pick_falls_back_to_least_recently_used():
    items = [
        _item(1, origin="api", used_count=3, used_at=900),
        _item(2, origin="api", used_count=3, used_at=100),
    ]
    import random

    for seed in range(20):
        assert pick_meme(items, {"api": 1}, random.Random(seed)).id == 2


def test_pick_skips_zero_weight_origins():
    items = [_item(1, origin="api"), _item(2, origin="submitted")]
    import random

    for seed in range(20):
        chosen = pick_meme(items, {"api": 0, "submitted": 1}, random.Random(seed))
        assert chosen.origin == "submitted"


def test_pick_returns_none_when_everything_is_zero_weight():
    assert pick_meme([_item(1)], {"submitted": 0}, None) is None


def test_pick_respects_relative_weights():
    """接口 1 : 投稿 9 —— 两千次里接口应该占一成上下，不能一边倒。"""
    import random

    items = [_item(i, origin="api") for i in range(1, 6)] + [
        _item(i, origin="submitted") for i in range(6, 11)
    ]
    rng = random.Random(7)
    picks = [pick_meme(items, {"api": 1, "submitted": 9}, rng) for _ in range(2000)]
    api = sum(1 for item in picks if item.origin == "api")
    assert 120 < api < 280


def test_store_pick_marks_usage(store):
    meme_id, _ = store.add(sha256="9" * 64, file_path="/x")
    store.mark_used(meme_id)
    item = store.get(meme_id)
    assert item.used_count == 1
    assert item.used_at == 1000.0


# --- 审核缓存 / 拒收 --------------------------------------------------------

def test_review_cache_round_trip(store):
    review = Review(is_meme=True, nsfw_level=1, flags=("text_heavy",), caption_text="lol")
    store.cache_review("e" * 64, review)
    cached = store.cached_review("e" * 64)
    assert cached is not None
    assert cached.flags == ("text_heavy",)
    assert cached.caption_text == "lol"


def test_cached_review_overwrites(store):
    store.cache_review("e" * 64, Review(is_meme=True))
    store.cache_review("e" * 64, Review(is_meme=False, parsed=False))
    assert store.cached_review("e" * 64).parsed is False


def test_missing_cache_is_none(store):
    assert store.cached_review("0" * 64) is None


def test_reject_and_lookup(store):
    store.reject("1" * 64, "命中禁发类别：politics", contributor="U1")
    assert store.rejection_reason("1" * 64) == "命中禁发类别：politics"
    assert store.rejected_count() == 1
    store.clear_reject("1" * 64)
    assert store.rejection_reason("1" * 64) is None


# --- 群推送状态 -------------------------------------------------------------

def test_push_counter_resets_on_new_day(store):
    umo = "qq_official:GroupMessage:G1"
    store.touch_group(umo, "G1")
    store.note_push(umo, ok=True, day_key="2026-10-24")
    store.note_push(umo, ok=True, day_key="2026-10-24")
    assert store.group_state(umo)["push_count"] == 2
    # 第二天：计数从 0 重新开始
    store.note_push(umo, ok=False, day_key="2026-10-25")
    assert store.group_state(umo)["push_count"] == 0


def test_blocked_until_only_set_when_asked(store):
    umo = "qq_official:GroupMessage:G2"
    store.touch_group(umo, "G2")
    store.note_push(umo, ok=False, day_key="2026-10-25")
    assert store.group_state(umo)["blocked_until"] == 0
    store.note_push(umo, ok=False, day_key="2026-10-25", blocked_until=2000.0)
    assert store.group_state(umo)["blocked_until"] == 2000.0


def test_meta_round_trip(store):
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    assert store.get_meta("missing", "fallback") == "fallback"
