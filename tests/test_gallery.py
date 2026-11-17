"""网页图库的数据形状：筛选、排序、分页。"""
from __future__ import annotations

from meme.gallery import (
    SORT_NEW,
    SORT_STALE,
    SORT_WARM,
    build_card,
    build_gallery,
    build_origin_breakdown,
    filter_items,
    matches_query,
    origin_label,
    sort_items,
)
from meme.models import MemeItem


def item(meme_id, *, origin="submitted", status="active", caption="",
         caption_zh="", tags=(), used_count=0, used_at=0.0):
    return MemeItem(
        id=meme_id, origin=origin, status=status, caption=caption,
        caption_zh=caption_zh, tags=tuple(tags), used_count=used_count,
        used_at=used_at, file_path=f"/tmp/{meme_id}.jpg",
    )


def test_origin_labels():
    assert origin_label("submitted") == "投稿"
    assert origin_label("submitted") == "投稿"
    assert origin_label("api") == "接口"
    assert origin_label("weird") == "weird"


def test_query_matches_caption_and_id_and_contributor():
    entry = item(42, caption="when the code works", caption_zh="代码跑通时")
    assert matches_query(entry, "code")
    assert matches_query(entry, "代码")
    assert matches_query(entry, "42")
    assert not matches_query(entry, "无关")


def test_query_matches_origin_label():
    assert matches_query(item(1, origin="submitted"), "投稿")


def test_empty_query_matches_everything():
    assert matches_query(item(1), "")


def test_filter_by_origin_status_and_query():
    entries = [
        item(1, origin="submitted"),
        item(2, origin="submitted", caption="猫"),
        item(3, origin="submitted", status="disabled", caption="狗"),
    ]
    assert [e.id for e in filter_items(entries, origin="submitted")] == [1, 2, 3]
    assert [e.id for e in filter_items(entries, status="active")] == [1, 2]
    assert [e.id for e in filter_items(entries, query="猫")] == [2]
    assert [e.id for e in filter_items(entries, origin="api")] == []
    assert [e.id for e in filter_items(entries, origin="all", status="all")] == [1, 2, 3]


def test_sort_new_is_newest_first():
    assert [e.id for e in sort_items([item(1), item(3), item(2)], SORT_NEW)] == [3, 2, 1]


def test_sort_stale_puts_never_used_first():
    entries = [item(1, used_at=500), item(2, used_at=0), item(3, used_at=100)]
    assert [e.id for e in sort_items(entries, SORT_STALE)] == [2, 3, 1]


def test_sort_warm_is_most_used_first():
    entries = [item(1, used_count=1), item(2, used_count=9), item(3, used_count=4)]
    assert [e.id for e in sort_items(entries, SORT_WARM)] == [2, 3, 1]


def test_build_card_carries_labels_and_flags():
    card = build_card(item(7, origin="submitted", used_count=3), "data:x")
    assert card["id"] == 7
    assert card["originLabel"] == "投稿"
    assert card["active"] is True
    assert card["thumb"] == "data:x"
    assert card["usedCount"] == 3


def test_build_card_marks_disabled_and_missing_files():
    card = build_card(item(8, status="disabled"), "", has_file=False)
    assert card["active"] is False
    assert card["hasFile"] is False


def test_build_gallery_paginates():
    entries = [item(i) for i in range(1, 11)]
    page = build_gallery(
        entries, total=11, stats={"total": 11, "active": 10, "rejected": 2},
        thumb_of=lambda _e: "", limit=4, offset=0,
    )
    assert [card["id"] for card in page["cards"]] == [1, 2, 3, 4]
    assert page["total"] == 10
    assert page["libraryTotal"] == 11
    assert page["rejected"] == 2
    assert page["hasMore"] is True

    last = build_gallery(
        entries, total=11, stats={"total": 11, "group_total": 11, "rejected": 2},
        thumb_of=lambda _e: "", limit=4, offset=8,
    )
    assert [card["id"] for card in last["cards"]] == [9, 10]
    assert last["hasMore"] is False


def test_build_gallery_clamps_limit():
    entries = [item(i) for i in range(1, 4)]
    page = build_gallery(entries, total=3, stats={}, thumb_of=lambda _e: "", limit=10_000)
    assert page["limit"] == 120


def test_build_origin_breakdown_sorted_by_count():
    entries = [item(1, origin="api"), item(2, origin="submitted"), item(3, origin="submitted")]
    breakdown = build_origin_breakdown(entries)
    assert breakdown[0] == {"origin": "submitted", "label": "投稿", "count": 2}
    assert breakdown[1]["count"] == 1
