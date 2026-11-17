"""网页图库的数据形状：一屏要什么、怎么筛、怎么排。

放在业务层而不是网页层，是因为「这个群现在到底有哪些图能发」这件事
本来就该能在群命令里回答（`/meme 库`），网页只是把它铺成网格而已。
"""
from __future__ import annotations

from typing import Callable, Iterable

from .models import MemeItem, STATUS_ACTIVE

ORIGIN_LABELS = {
    "submitted": "投稿",
    "api": "接口",
    "generated": "AI 现画",
}
SORT_NEW = "new"
SORT_STALE = "stale"
SORT_WARM = "warm"
SORTS = (SORT_NEW, SORT_STALE, SORT_WARM)


def origin_label(origin: str) -> str:
    return ORIGIN_LABELS.get(origin, origin or "未知")


def matches_query(item: MemeItem, query: str) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        [
            str(item.id),
            item.caption or "",
            item.caption_zh or "",
            " ".join(item.tags),
            item.contributor or "",
            origin_label(item.origin),
        ]
    ).lower()
    return needle in haystack


def filter_items(
    items: Iterable[MemeItem],
    *,
    origin: str = "",
    query: str = "",
    status: str = "",
) -> list[MemeItem]:
    result = []
    for item in items:
        if origin and origin != "all" and item.origin != origin:
            continue
        if status and status != "all" and item.status != status:
            continue
        if not matches_query(item, query):
            continue
        result.append(item)
    return result


def sort_items(items: list[MemeItem], mode: str = SORT_NEW) -> list[MemeItem]:
    """三种排法，对应三种真实问题：
    新入库的（刚拖进来的在哪）、最久没发的（该轮到的）、发得最多的（最受欢迎的）。
    """
    if mode == SORT_STALE:
        return sorted(items, key=lambda item: (item.used_at or 0, item.id))
    if mode == SORT_WARM:
        return sorted(items, key=lambda item: (item.used_count, item.id), reverse=True)
    return sorted(items, key=lambda item: item.id, reverse=True)


def build_card(item: MemeItem, thumb_url: str = "", has_file: bool = True) -> dict:
    return {
        "id": item.id,
        "thumb": thumb_url,
        "origin": item.origin,
        "originLabel": origin_label(item.origin),
        "status": item.status,
        "active": item.status == STATUS_ACTIVE,
        "caption": item.caption,
        "captionZh": item.caption_zh,
        "tags": list(item.tags),
        "nsfwLevel": item.nsfw_level,
        "usedCount": item.used_count,
        "contributor": item.contributor,
        "sourceUrl": item.source_url,
        "hasFile": has_file,
    }


def build_gallery(
    items: list[MemeItem],
    *,
    total: int,
    stats: dict,
    thumb_of: Callable[[MemeItem], str],
    file_exists: Callable[[str], bool] = lambda _path: True,
    limit: int = 24,
    offset: int = 0,
) -> dict:
    """把筛选后的列表切成一页，并把统计一并带上（网页顶部那几个数字）。"""
    limit = max(1, min(int(limit or 24), 120))
    offset = max(0, int(offset or 0))
    window = items[offset : offset + limit]
    return {
        "cards": [
            build_card(item, thumb_of(item), file_exists(item.file_path))
            for item in window
        ],
        "total": len(items),
        "libraryTotal": int(stats.get("total", 0)),
        "activeTotal": int(stats.get("active", 0)),
        "rejected": int(stats.get("rejected", 0)),
        "offset": offset,
        "limit": limit,
        "hasMore": offset + len(window) < len(items),
    }


def build_origin_breakdown(items: Iterable[MemeItem]) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.origin] = counts.get(item.origin, 0) + 1
    return [
        {"origin": origin, "label": origin_label(origin), "count": count}
        for origin, count in sorted(counts.items(), key=lambda pair: -pair[1])
    ]
