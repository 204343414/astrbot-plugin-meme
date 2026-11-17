"""自动搬石：三档概率、每分钟抽签、日上限、免打扰、按钮权限。

这块全部是「概率 + 时间 + 权限」，最容易写完自己都不确定对不对，
所以把随机数和时钟都注入进去测：不靠 mock sleep。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import random

import pytest

from meme.cards import (
    ACTION_AGAIN,
    ACTION_AUTO_HIGH,
    ACTION_AUTO_LOW,
    ACTION_AUTO_MID,
    ACTION_AUTO_OFF,
    ACTION_AUTO_SUB,
    ACTION_HOWTO,
    AUTO_NOTE,
    build_auto_done_card,
    build_auto_pick_card,
    build_meme_card,
)
from meme.models import MemeItem


@pytest.fixture()
def store(tmp_path):
    from meme.store import MemeStore

    instance = MemeStore(str(tmp_path / "meme.db"), str(tmp_path / "media"))
    instance.init()
    return instance


@pytest.fixture()
def plugin(plugin_module, tmp_path):
    """一个真的插件实例：store 落在 tmp 上，发送链路由各用例自己替换。"""
    from meme.pipeline import MemePipeline
    from meme.review import ReviewPolicy

    instance = plugin_module.MemePlugin(
        FakeContext(),
        {
            "custom_data_dir": str(tmp_path),
            "cooldown_seconds": 0,
            "send_cooldown_seconds": 0,
            "submission_cooldown_seconds": 0,
        },
    )
    instance.store.init()
    instance.pipeline = MemePipeline(instance.store, ReviewPolicy(), moderator=None)
    return instance


class FakeContext:
    """只要插件能问「全局管理员是谁」就行。"""

    def __init__(self, admins=()):
        self._admins = list(admins)

    def get_config(self):
        return {"admins_id": self._admins}

    def get_provider_by_id(self, provider_id):
        return None


class FixedRandom:
    """给 _auto_tick 用的假随机源：先决定「第几分钟抽」再决定「中不中」。"""

    def __init__(self, minute=0, roll=1):
        self.minute = minute
        self.roll = roll
        self.calls = []

    def randint(self, a, b):
        """上界 59 的是「抽签分钟」（下界会被抬到当前分钟），上界 100 的是掷骰。"""
        self.calls.append(("randint", a, b))
        return self.minute if b == 59 else self.roll

    def __getattr__(self, name):
        return getattr(random, name)


# --- 卡片 -------------------------------------------------------------------

def test_meme_card_offers_subscribe_when_not_subscribed():
    card = build_meme_card(MemeItem(id=7, caption="hi"), "qqbot-media://x", auto="sub")
    assert [b["action_id"] for b in card["rows"][0]] == [
        ACTION_AGAIN,
        ACTION_HOWTO,
        ACTION_AUTO_SUB,
    ]
    assert card["rows"][0][-1]["label"] == "自动订阅"


def test_meme_card_offers_cancel_when_subscribed():
    card = build_meme_card(MemeItem(id=7), "", auto="off")
    assert card["rows"][0][-1]["action_id"] == ACTION_AUTO_OFF
    assert card["rows"][0][-1]["label"] == "取消自动"


def test_auto_note_shows_on_auto_cards():
    card = build_meme_card(MemeItem(id=7, caption="hi"), "qqbot-media://x", auto_note=AUTO_NOTE)
    assert AUTO_NOTE in card["markdown"]
    assert AUTO_NOTE == "当前为 每日自动推送中..."


def test_pick_card_buttons_follow_the_configured_percentages():
    """面板里把低档改成 5%，群里的按钮就该写「低5%」。"""
    card = build_auto_pick_card({"low": 5, "mid": 25, "high": 50})
    labels = [b["label"] for b in card["rows"][0]]
    assert labels == ["低5%", "中25%", "高50%"]
    assert [b["action_id"] for b in card["rows"][0]] == [
        ACTION_AUTO_LOW,
        ACTION_AUTO_MID,
        ACTION_AUTO_HIGH,
    ]
    assert all(len(label) <= 6 for label in labels)


def test_pick_card_labels_survive_three_digit_percentages():
    card = build_auto_pick_card({"low": 100, "mid": 100, "high": 100})
    assert all(len(b["label"]) <= 6 for b in card["rows"][0])


def test_done_card_says_on_and_off():
    on = build_auto_done_card("mid", 25)
    assert "已开启" in on["markdown"] and "25%" in on["markdown"]
    assert on["rows"][0][-1]["action_id"] == ACTION_AUTO_OFF
    off = build_auto_done_card(off=True)
    assert "已关闭" in off["markdown"]
    assert off["rows"][0][-1]["action_id"] == ACTION_AUTO_SUB


# --- 存储 -------------------------------------------------------------------

def test_auto_level_round_trip(store):
    store.touch_group("umo:A", "G1")
    assert store.group_state("umo:A")["auto_level"] == ""
    store.set_auto_level("umo:A", "high")
    assert store.group_state("umo:A")["auto_level"] == "high"
    assert store.group_state("umo:A")["auto_since"] > 0
    assert [g["umo"] for g in store.auto_groups()] == ["umo:A"]
    store.set_auto_level("umo:A", "")
    assert store.group_state("umo:A")["auto_level"] == ""
    assert store.auto_groups() == []


def test_auto_level_rejects_unknown_value(store):
    with pytest.raises(ValueError):
        store.set_auto_level("umo:A", "超高")


def test_switching_off_clears_today_counter(store):
    store.set_auto_level("umo:A", "low")
    store.note_auto_push("umo:A", day_key="2026-10-25")
    store.note_auto_push("umo:A", day_key="2026-10-25")
    assert store.group_state("umo:A")["auto_sent_count"] == 2
    store.set_auto_level("umo:A", "")
    assert store.group_state("umo:A")["auto_sent_count"] == 0


def test_counter_resets_across_days(store):
    assert store.note_auto_push("umo:A", day_key="2026-10-25") == 1
    assert store.note_auto_push("umo:A", day_key="2026-10-25") == 2
    assert store.note_auto_push("umo:A", day_key="2026-10-26") == 1


def test_failed_push_does_not_consume_the_daily_budget(store):
    store.note_auto_push("umo:A", day_key="2026-10-25")
    assert store.note_auto_push("umo:A", day_key="2026-10-25", ok=False) == 1


def test_old_database_gets_auto_columns(tmp_path):
    """老库（groups 表没这几列）启动时必须自动补列，否则插件直接加载失败。"""
    import sqlite3

    from meme.store import MemeStore

    db = tmp_path / "meme.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE groups (
            umo TEXT PRIMARY KEY,
            group_id TEXT NOT NULL DEFAULT '',
            daily_enabled INTEGER NOT NULL DEFAULT 0,
            daily_slots TEXT NOT NULL DEFAULT '',
            blocked_until REAL NOT NULL DEFAULT 0,
            last_push REAL NOT NULL DEFAULT 0,
            push_count INTEGER NOT NULL DEFAULT 0,
            push_date TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO groups(umo) VALUES ('old');
        """
    )
    conn.commit()
    conn.close()

    instance = MemeStore(str(db))
    instance.init()
    cols = {row["name"] for row in instance._rows("PRAGMA table_info(groups)")}
    assert {"auto_level", "auto_since", "auto_sent_date", "auto_sent_count"} <= cols
    assert instance.group_state("old")["auto_level"] == ""


# --- 时间与概率 -------------------------------------------------------------

def test_quiet_range_parses_overnight_window(plugin):
    plugin.config["auto_push_quiet"] = "23:00-08:00"
    assert plugin._auto_quiet_range() == (23 * 60, 8 * 60)
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 23, 30)) is True
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 7, 59)) is True
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 8, 0)) is False
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 12, 0)) is False


def test_quiet_range_can_be_disabled(plugin):
    plugin.config["auto_push_quiet"] = ""
    assert plugin._auto_quiet_range() == (0, 0)
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 3, 0)) is False


def test_quiet_range_handles_same_day_window(plugin):
    plugin.config["auto_push_quiet"] = "12:00-14:00"
    assert plugin._auto_quiet_range() == (720, 840)
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 13, 0)) is True
    assert plugin._in_auto_quiet(dt.datetime(2026, 10, 25, 23, 0)) is False


def test_percentages_come_from_config(plugin):
    plugin.config.update(
        {"auto_push_low_pct": 5, "auto_push_mid_pct": 30, "auto_push_high_pct": 60}
    )
    assert plugin._auto_pcts() == {"low": 5, "mid": 30, "high": 60}
    assert plugin._auto_pct("mid") == 30


def test_percentages_are_clamped(plugin):
    plugin.config.update(
        {"auto_push_low_pct": -5, "auto_push_mid_pct": 999, "auto_push_high_pct": 50}
    )
    assert plugin._auto_pcts() == {"low": 0, "mid": 100, "high": 50}


def test_each_group_gets_a_random_minute_per_hour(plugin):
    plugin._rng = FixedRandom(minute=17)
    moment = dt.datetime(2026, 10, 25, 10, 17)
    assert plugin._auto_now_minute_matches("umo:A", moment) is True
    # 同一小时内再问一次：还是同一个分钟（不会每分钟重新抽）
    assert plugin._auto_now_minute_matches("umo:A", dt.datetime(2026, 10, 25, 10, 30)) is False
    # 下一个小时：重新抽签
    plugin._rng.minute = 42
    assert plugin._auto_now_minute_matches("umo:A", dt.datetime(2026, 10, 25, 11, 42)) is True


def test_late_start_never_draws_a_minute_already_past(plugin):
    """插件在 12:40 才启动：这一小时的抽签分钟不能抽到 12:10，否则白等一小时。"""
    plugin._rng = FixedRandom(minute=10)  # 假随机源「想给」10，但它没机会被采纳
    moment = dt.datetime(2026, 10, 25, 12, 40)
    plugin._auto_now_minute_matches("umo:A", moment)
    # 关键：下界被抬到 40，真实的 randint(40, 59) 不可能返回已经过去的分钟
    assert plugin._rng.calls[-1] == ("randint", 40, 59)


# --- 整条抽签 ---------------------------------------------------------------

def _subscribe(plugin, umo, level="high"):
    plugin.store.touch_group(umo, "G1")
    plugin.store.set_auto_level(umo, level)


def _stub_hub(plugin):
    """让代码以为 Hub 在（真正的发送被各用例替换掉）。"""

    class FakeHub:
        pass

    plugin._get_hub = lambda quiet=False: FakeHub()  # type: ignore[assignment]


def _stub_send(plugin):
    sent = []

    async def fake_send(origin, item, *, auto=False, **kwargs):
        sent.append((origin, item.id, auto))
        return True

    plugin._send_item_by_origin = fake_send
    return sent


def _add_meme(plugin, tag="x"):
    return plugin.store.add(sha256=tag * 64, file_path=f"/tmp/{tag}.jpg", caption=tag)[0]


def _run_tick(plugin, moment):
    """把 _auto_tick 固定到某个时刻跑一遍。"""

    class FakeDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    import sys

    class Shim:
        datetime = FakeDateTime

    # 插件是当成 `meme_plugin_under_test.main` 导入的；换掉那个模块里的 dt 即可。
    # 存的是**模块里原来那个 dt**（datetime 模块），不是 dt.datetime —— 前者存错
    # 会把模块的 dt 换成类，后面所有用例一起炸。
    module = sys.modules[type(plugin).__module__]
    original = module.dt
    module.dt = Shim
    try:
        asyncio.run(plugin._auto_tick())
    finally:
        module.dt = original


def test_low_roll_sends_when_dice_hits(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "high")
    sent = _stub_send(plugin)
    plugin.config["auto_push_quiet"] = ""
    plugin._rng = FixedRandom(minute=5, roll=1)  # 抽中
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 5))
    assert sent and sent[0][0] == "umo:A" and sent[0][2] is True
    assert plugin.store.group_state("umo:A")["auto_sent_count"] == 1


def test_roll_misses_most_of_the_time(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "low")
    sent = _stub_send(plugin)
    plugin.config["auto_push_quiet"] = ""
    plugin._rng = FixedRandom(minute=5, roll=99)  # 99 > 10，没抽中
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 5))
    assert sent == []
    assert plugin.store.group_state("umo:A")["auto_sent_count"] == 0


def test_wrong_minute_does_nothing(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "high")
    sent = _stub_send(plugin)
    plugin.config["auto_push_quiet"] = ""
    plugin._rng = FixedRandom(minute=30, roll=1)
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 7))  # 本小时抽的是 30 分
    assert sent == []


def test_daily_cap_stops_the_roll(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "high")
    sent = _stub_send(plugin)
    plugin.config.update({"auto_push_quiet": "", "auto_push_max_per_day": 2})
    plugin.store.note_auto_push("umo:A", day_key="2026-10-25")
    plugin.store.note_auto_push("umo:A", day_key="2026-10-25")
    plugin._rng = FixedRandom(minute=5, roll=1)
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 5))
    assert sent == []


def test_quiet_hours_suppress_the_roll(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "high")
    sent = _stub_send(plugin)
    plugin.config["auto_push_quiet"] = "23:00-08:00"
    plugin._rng = FixedRandom(minute=5, roll=1)
    _run_tick(plugin, dt.datetime(2026, 10, 25, 2, 5))
    assert sent == []


def test_master_switch_stops_everything(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "high")
    sent = _stub_send(plugin)
    plugin.config.update({"auto_push_quiet": "", "auto_push_enabled": False})
    plugin._rng = FixedRandom(minute=5, roll=1)
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 5))
    assert sent == []


def test_unsubscribed_group_is_never_rolled(plugin):
    _add_meme(plugin, "a")
    plugin.store.touch_group("umo:B", "G2")  # 只是见过面，没订阅
    sent = _stub_send(plugin)
    plugin.config["auto_push_quiet"] = ""
    plugin._rng = FixedRandom(minute=5, roll=1)
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 5))
    assert sent == []


def test_blocked_group_is_skipped(plugin):
    _add_meme(plugin, "a")
    _subscribe(plugin, "umo:A", "high")
    sent = _stub_send(plugin)
    plugin.config["auto_push_quiet"] = ""
    plugin.store.note_push("umo:A", ok=False, day_key="2026-10-25", blocked_until=9e9)
    plugin._rng = FixedRandom(minute=5, roll=1)
    _run_tick(plugin, dt.datetime(2026, 10, 25, 12, 5))
    assert sent == []


# --- 按钮权限 ---------------------------------------------------------------

class FakeActionContext:
    origin = "qq_official:GroupMessage:G1"

    def __init__(self, openid="U1"):
        self.member_openid = openid
        self.client = None
        self.interaction = None


def test_subscribe_button_opens_the_pick_card(plugin):
    plugin.config["auto_push_quiet"] = ""
    sent = []

    async def capture(origin, card, **kwargs):
        sent.append(card)

    plugin._send_hub_card = capture
    _stub_hub(plugin)
    code = asyncio.run(plugin._act_auto_sub(FakeActionContext(), {}))
    assert code == 0
    assert "自动搬石" in sent[0]["markdown"]


def test_choosing_a_level_writes_it_and_replies(plugin):
    sent = []

    async def capture(origin, card, **kwargs):
        sent.append(card)

    plugin._send_hub_card = capture
    _stub_hub(plugin)
    callback = plugin._act_auto_set("mid")
    code = asyncio.run(callback(FakeActionContext(), {}))
    assert code == 0
    assert plugin.store.group_state(FakeActionContext.origin)["auto_level"] == "mid"
    assert "已开启" in sent[0]["markdown"]


def test_cancel_requires_a_manager(plugin):
    plugin.store.touch_group(FakeActionContext.origin, "G1")
    plugin.store.set_auto_level(FakeActionContext.origin, "high")
    code = asyncio.run(plugin._act_auto_off(FakeActionContext("stranger"), {}))
    assert code == 4  # 无权限
    assert plugin.store.group_state(FakeActionContext.origin)["auto_level"] == "high"


def test_cancel_works_for_configured_admin(plugin):
    plugin.store.touch_group(FakeActionContext.origin, "G1")
    plugin.store.set_auto_level(FakeActionContext.origin, "high")
    plugin.config["extra_admins"] = ["BOSS"]

    async def capture(origin, card, **kwargs):
        return None

    plugin._send_hub_card = capture
    code = asyncio.run(plugin._act_auto_off(FakeActionContext("BOSS"), {}))
    assert code == 0
    assert plugin.store.group_state(FakeActionContext.origin)["auto_level"] == ""


def test_cancel_works_when_hub_knows_the_group_role(plugin):
    plugin.store.touch_group(FakeActionContext.origin, "G1")
    plugin.store.set_auto_level(FakeActionContext.origin, "high")

    class FakeHub:
        async def is_group_manager(self, origin, openid):
            return openid == "MANAGER"

    async def capture(origin, card, **kwargs):
        return None

    plugin._send_hub_card = capture
    hub = FakeHub()
    plugin._get_hub = lambda quiet=False: hub  # type: ignore[assignment]
    assert asyncio.run(plugin._act_auto_off(FakeActionContext("MANAGER"), {})) == 0
    assert plugin.store.group_state(FakeActionContext.origin)["auto_level"] == ""


# --- 配置校验 ---------------------------------------------------------------

def test_settings_reject_reversed_probabilities():
    from meme.settings import auto_prob_problem

    assert auto_prob_problem({"auto_push_low_pct": 10, "auto_push_mid_pct": 25,
                              "auto_push_high_pct": 50}) == ""
    problem = auto_prob_problem({"auto_push_low_pct": 60, "auto_push_mid_pct": 25,
                                 "auto_push_high_pct": 10})
    assert "低 ≤ 中 ≤ 高" in problem


def test_saving_reversed_probabilities_is_refused(plugin):
    result = asyncio.run(
        plugin.web_save_settings(
            {"auto_push_low_pct": 80, "auto_push_mid_pct": 25, "auto_push_high_pct": 10}
        )
    )
    assert result["ok"] is False
    # 一个都没写进去：整批拒绝，避免三档颠倒
    assert plugin.config.get("auto_push_low_pct") != 80


def test_new_fields_are_visible_on_the_settings_page():
    from meme.settings import FIELD_BY_KEY

    for key in (
        "auto_push_enabled",
        "auto_push_low_pct",
        "auto_push_mid_pct",
        "auto_push_high_pct",
        "auto_push_max_per_day",
        "auto_push_quiet",
        "card_show_auto",
    ):
        assert key in FIELD_BY_KEY, key
