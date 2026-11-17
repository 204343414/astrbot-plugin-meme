"""从候选池里挑一张：先按来源权重抽来源，再在该来源里优先发没发过的。

「越来越聪明」在这个功能里只有一种落地方式：少发重复的。所以挑选顺序是
未发过 → 最久没发 → 随机，而不是纯 random.choice。
"""
from __future__ import annotations

import random
from typing import Iterable, Mapping, Sequence

from .models import MemeItem


def group_by_origin(items: Iterable[MemeItem]) -> dict[str, list[MemeItem]]:
    buckets: dict[str, list[MemeItem]] = {}
    for item in items:
        buckets.setdefault(item.origin, []).append(item)
    return buckets


def weight_of(origin: str, weights: Mapping[str, float]) -> float:
    try:
        return float(weights.get(origin, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def pick_meme(
    items: Sequence[MemeItem],
    weights: Mapping[str, float],
    rng: random.Random | None = None,
) -> MemeItem | None:
    """两层抽样。权重全为 0 或池子为空时返回 None（调用方据此提示）。"""
    rng = rng or random.Random()
    buckets = group_by_origin(items)
    origins = [origin for origin in sorted(buckets) if weight_of(origin, weights) > 0]
    if not origins:
        return None
    if len(origins) == 1:
        chosen_origin = origins[0]
    else:
        chosen_origin = rng.choices(
            origins, weights=[weight_of(o, weights) for o in origins], k=1
        )[0]

    pool = buckets[chosen_origin]
    fresh = [item for item in pool if item.used_count <= 0]
    if fresh:
        return rng.choice(fresh)
    # 全发过了：优先最久以前发的，同分随机，避免每次都是同样那张
    oldest = min(item.used_at for item in pool)
    coldest = [item for item in pool if item.used_at <= oldest]
    return rng.choice(coldest)


def format_stats(
    total: int,
    by_origin: Mapping[str, int],
    active: int,
    rejected: int,
    pending: int = 0,
) -> str:
    """``/meme 库`` 的回话。数字要能一眼看出「到底有没有图可发」。"""
    lines = [f"库里能发：{active} 张（含下架过的共 {total} 张）"]
    if by_origin:
        detail = "、".join(f"{name} {count}" for name, count in sorted(by_origin.items()))
        lines.append(f"来源：{detail}")
    if rejected:
        lines.append(f"已拒收 {rejected} 张（同一张图不会再重复送审）")
    if pending:
        lines.append(f"待审核 {pending} 张")
    if active == 0:
        lines.append("还没有可发的图：引用一张图、@我，然后打「/meme 投稿」。")
    return "\n".join(lines)
