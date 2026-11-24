"""从候选池里挑一张：先按来源权重抽来源，再在该来源里优先发「这个群没发过的」。

「越来越聪明」在这个功能里只有一种落地方式：少发重复的。所以挑选顺序是
本群没发过 → 本群最久没发 → 随机，而不是纯 random.choice。

**为什么要带 umo**：``memes.used_at`` 是**全站**记账，五个群共用一条队列 ——
一张图只要在 A 群发过，B 群眼里它也是「刚发过的」。新图会被第一个抽中的群
吃掉，其余群要等整整一轮（实测 14~29 天）才见得到。所以挑选必须按会话看
「这个群自己发过没有」，那本账在 ``deliveries`` 表里。
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
    *,
    seen: Mapping[int, float] | None = None,
) -> MemeItem | None:
    """两层抽样。权重全为 0 或池子为空时返回 None（调用方据此提示）。

    ``seen`` 是「这个会话发过哪张、最后什么时候发的」：``{meme_id: last_at}``。
    传 None 表示不按群去重（老行为，只有全局记账），留给单测和没有会话上下
    文的调用点。传空 dict 表示「这个群一张都还没发过」—— 这和 None 意思不同，
    所以默认值必须是 None 而不是 {}。
    """
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
    if seen is None:
        return _pick_from_pool(pool, rng)

    # ① 这个群没发过的：一定优先。用户的原话是「本群没发过的优先发是肯定的」。
    fresh = [item for item in pool if item.id not in seen]
    if fresh:
        # 没发过的里面，仍然照顾「全站也没怎么用过的」：先让全新的图露头，
        # 再让老图在这个群里补漏。全站都没发过时这一层退化成纯随机。
        coldest = min(item.used_at for item in fresh)
        lead = [item for item in fresh if item.used_at <= coldest]
        return rng.choice(lead if lead else fresh)

    # ② 这个群全发过了：挑「在本群最久没露面」的那一批，同分随机。
    #    用本群自己的 last_at，不看全局 used_at —— 否则隔壁群刚发完的图
    #    会被误判成「刚发过」，把这个群的队列也一起推后。
    #    （必须是 last_at：first_at 永不更新，用它会让同一张图在池子发遍后
    #    被无限重复选中。）
    oldest = min(seen[item.id] for item in pool)
    coldest = [item for item in pool if seen[item.id] <= oldest]
    return rng.choice(coldest)


def _pick_from_pool(pool: Sequence[MemeItem], rng: random.Random) -> MemeItem:
    """没有会话上下文时的老逻辑：全局未发过优先 → 全局最久没发 → 随机。"""
    fresh = [item for item in pool if item.used_count <= 0]
    if fresh:
        return rng.choice(fresh)
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
