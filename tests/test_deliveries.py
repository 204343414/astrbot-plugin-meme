"""按群投递去重：本群没发过的优先、每群独立记账、记账只在发出去之后。

这一组钉的是用户报的那个 bug：一百多张候选，老群还是那几张熟的。
根因是挑图和记账都是**全局**的 —— 五个群共用一条队列，新图被第一个抽中的
群吃掉，其余群要等整整一轮（实测 14~29 天）才见得到。
"""
from __future__ import annotations

import pytest

from meme.models import MemeItem
from meme.select import pick_meme
from meme.store import MemeStore

WEIGHTS = {"submitted": 3.0, "api": 0.5, "generated": 0.0}


@pytest.fixture()
def store(tmp_path):
    instance = MemeStore(
        str(tmp_path / "meme.db"), str(tmp_path / "media"), now=lambda: 1000.0
    )
    instance.init()
    return instance


def _seed(store, n=5, used=()):
    """塞 n 张图；``used`` 里的下标模拟「全站发过」。"""
    ids = []
    for i in range(n):
        meme_id, _ = store.add(sha256=f"{i:064d}", file_path=f"/tmp/{i}.jpg")
        ids.append(meme_id)
        if i in used:
            store.mark_used(meme_id)
    return ids


# --- 挑选：本群没发过的优先 --------------------------------------------------

def test_group_sees_never_sent_before_repeating(store):
    """A 群发遍了全库之后，B 群仍然应该先拿到「它自己没发过的」。"""
    ids = _seed(store, n=5)
    for meme_id in ids:
        store.mark_used(meme_id, "GroupMessage:A")

    picked = store.pick(WEIGHTS, umo="GroupMessage:B")
    assert picked is not None
    # 全局看这 5 张全发过，但 B 群一张没见过 —— 必须命中而不是走全局 LRU
    assert picked.id in ids


def test_same_group_does_not_repeat_until_pool_exhausted(store):
    """一个群连挑 N 次，应该拿到 N 张不同的（池子够大时）。"""
    ids = set(_seed(store, n=6))
    got = []
    for _ in range(6):
        item = store.pick(WEIGHTS, umo="GroupMessage:A")
        assert item is not None
        got.append(item.id)
        store.mark_used(item.id, "GroupMessage:A")
    assert len(set(got)) == 6, f"六次就重复了：{got}"
    assert set(got) == ids


def test_group_repeats_only_after_full_cycle(store):
    """池子发遍之后才允许重复，且先重复本群最久没发的那张。"""
    ids = _seed(store, n=3)
    for _ in range(3):
        item = store.pick(WEIGHTS, umo="GroupMessage:A")
        store.mark_used(item.id, "GroupMessage:A")

    # 全发过一遍了。把其中一张的「本群最后一次发送时间」改早，它应该最先回来。
    store._write(
        "UPDATE deliveries SET last_at = 1.0 WHERE umo = ? AND meme_id = ?",
        ("GroupMessage:A", ids[1]),
    )
    assert store.pick(WEIGHTS, umo="GroupMessage:A").id == ids[1]


def test_repeat_cycle_actually_rotates(store):
    """池子发遍之后，重复必须是「轮转」而不是死磕同一张。

    回归：最初用 first_at 判断本群冷热，而 first_at 写下就不再更新 ——
    结果池子发遍后每次都选中同一张，222 次推送里同一张图出现了 121 次。
    """
    ids = _seed(store, n=3)
    seq = []
    for step in range(9):  # 三轮
        item = store.pick(WEIGHTS, umo="GroupMessage:A")
        store.mark_used(item.id, "GroupMessage:A")
        seq.append(item.id)
        store._now = (lambda t: (lambda: t))(1000.0 + step)

    # 每一轮都该把三张各发一遍
    for cycle in range(3):
        chunk = seq[cycle * 3 : cycle * 3 + 3]
        assert sorted(chunk) == sorted(ids), f"第 {cycle + 1} 轮没有轮转：{chunk}"


def test_new_meme_reaches_quiet_group_first(store):
    """新图入库后，之前很安静的群应该比活跃群更早拿到它。

    原 bug 的反向验证：全局记账下，活跃群先抽到就把新图「用掉」了，
    安静的群反而是最后一个看到的。
    """
    old = _seed(store, n=8)
    busy, quiet = "GroupMessage:BUSY", "GroupMessage:QUIET"
    # 活跃群把老图全发遍；安静群只发过一张
    for meme_id in old:
        store.mark_used(meme_id, busy)
    store.mark_used(old[0], quiet)

    # 来了一张新图
    new_id, _ = store.add(sha256="f" * 64, file_path="/tmp/new.jpg")

    # 安静群下一次挑选：必须在「它没见过的」里，且新图是第一顺位之一
    picked_quiet = store.pick(WEIGHTS, umo=quiet)
    assert picked_quiet.id != old[0]
    # 新图全站都没发过（used_at=0），在 fresh 里排最前
    assert picked_quiet.id == new_id


def test_pick_without_umo_keeps_old_global_behaviour(store):
    """没传 umo 的调用点（以及老测试）仍按全局记账走，不能炸。"""
    _seed(store, n=4, used={0, 1, 2, 3})
    item = store.pick(WEIGHTS)
    assert item is not None
    assert item.used_count >= 1


def test_empty_seen_is_not_none(store):
    """没传 umo（None）和「这个群一张没发过」（空 dict）必须区分开。"""
    ids = _seed(store, n=3, used={0, 1, 2})
    # 空 seen：三张都算「本群没发过」
    item = pick_meme(
        [MemeItem(id=i, origin="submitted", used_count=1, used_at=5.0) for i in ids],
        WEIGHTS,
        seen={},
    )
    assert item is not None
    # None：走全局 LRU，同样能挑出来
    item = pick_meme(
        [MemeItem(id=i, origin="submitted", used_count=1, used_at=5.0) for i in ids],
        WEIGHTS,
        seen=None,
    )
    assert item is not None


# --- 记账：两本账分开 --------------------------------------------------------

def test_mark_used_writes_both_ledgers(store):
    meme_id = _seed(store, n=1)[0]
    store.mark_used(meme_id, "GroupMessage:A")

    global_row = store.get(meme_id)
    assert global_row.used_count == 1
    assert global_row.used_at == 1000.0

    seen = store.seen_by("GroupMessage:A")
    assert seen == {meme_id: 1000.0}


def test_deliveries_are_per_group(store):
    """同一张图发给两个群，两个群各自记账，互不影响。"""
    meme_id = _seed(store, n=1)[0]
    store.mark_used(meme_id, "GroupMessage:A")
    store.mark_used(meme_id, "GroupMessage:B")

    assert meme_id in store.seen_by("GroupMessage:A")
    assert meme_id in store.seen_by("GroupMessage:B")
    assert meme_id not in store.seen_by("GroupMessage:C")
    # 全局热度只加两次，不是每个群一份副本
    assert store.get(meme_id).used_count == 2


def test_first_at_is_kept_across_repeats(store):
    """``first_at`` 记「本群最早那次」，重复发送不能把它推后（否则冷热判断失效）。"""
    meme_id = _seed(store, n=1)[0]
    store.mark_used(meme_id, "GroupMessage:A")
    store._now = lambda: 2000.0
    store.mark_used(meme_id, "GroupMessage:A")

    row = store._rows(
        "SELECT first_at, last_at, times FROM deliveries WHERE umo = ?", ("GroupMessage:A",)
    )[0]
    assert row["first_at"] == 1000.0  # 第一次见到它：不变
    assert row["last_at"] == 2000.0   # 最后一次发它：更新（挑选靠这个）
    assert row["times"] == 2


def test_mark_used_without_umo_only_writes_global(store):
    meme_id = _seed(store, n=1)[0]
    store.mark_used(meme_id)
    assert store.get(meme_id).used_count == 1
    assert store.seen_by("GroupMessage:A") == {}


# --- 回填：升级时不重发已经发过的 --------------------------------------------

def test_backfill_marks_globally_used_as_seen(store):
    """升级后第一次启动，全局发过的图必须算作「群里发过」，否则会重发一遍。"""
    ids = _seed(store, n=5, used={0, 1, 2})  # 0,1,2 全站发过
    store.touch_group("GroupMessage:A")
    store.set_auto_level("GroupMessage:A", "mid")

    done = store.backfill_all_groups()
    assert list(done) == ["GroupMessage:A"]
    assert done["GroupMessage:A"] == 3

    seen = store.seen_by("GroupMessage:A")
    assert set(seen) == {ids[0], ids[1], ids[2]}
    # 没发过的两张不算；它们本来就该优先
    assert ids[3] not in seen and ids[4] not in seen


def test_backfill_preserves_global_cold_order(store):
    """回填用全局 used_at 当本群的 first_at，冷热顺序和升级前一致。"""
    ids = _seed(store, n=4)
    store._write("UPDATE memes SET used_count = 1, used_at = ? WHERE id = ?", (10.0, ids[0]))
    store._write("UPDATE memes SET used_count = 1, used_at = ? WHERE id = ?", (50.0, ids[1]))
    store._write("UPDATE memes SET used_count = 1, used_at = ? WHERE id = ?", (30.0, ids[2]))
    store._write("UPDATE memes SET used_count = 1, used_at = ? WHERE id = ?", (99.0, ids[3]))
    store.touch_group("GroupMessage:A")
    store.set_auto_level("GroupMessage:A", "mid")
    store.backfill_all_groups()

    seen = store.seen_by("GroupMessage:A")
    assert seen[ids[0]] == 10.0  # 最久没发 → 最先回来
    assert seen[ids[3]] == 99.0


def test_backfill_is_idempotent(store):
    _seed(store, n=3, used={0, 1})
    store.touch_group("GroupMessage:A")
    store.set_auto_level("GroupMessage:A", "mid")

    assert store.backfill_all_groups() == {"GroupMessage:A": 2}
    # 第二次启动不该重复写
    assert store.backfill_all_groups() == {}


def test_backfill_skips_groups_without_subscription(store):
    """只给「真在自动推」的群回填；没订阅的群没必要先记一笔。"""
    _seed(store, n=3, used={0, 1})
    store.touch_group("GroupMessage:OFF")  # 有记录但 auto_level 为空
    assert store.backfill_all_groups() == {}
    assert store.seen_by("GroupMessage:OFF") == {}


def test_backfill_does_not_touch_unseen_memes(store):
    """全站都没发过的图不能被回填成「看过」—— 那些正是要优先发的新图。"""
    ids = _seed(store, n=3)  # 一张都没发过
    store.touch_group("GroupMessage:A")
    store.set_auto_level("GroupMessage:A", "mid")
    assert store.backfill_all_groups() == {}
    assert store.seen_by("GroupMessage:A") == {}
    assert store.pick(WEIGHTS, umo="GroupMessage:A").id in ids
