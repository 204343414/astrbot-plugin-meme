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

    class Book:
        def __init__(self, role):
            self.role = role

        async def is_group_manager(self, origin, openid):
            return openid == "MANAGER" and self.role in ("owner", "admin")

        async def role_of(self, origin, openid):
            return self.role if openid == "MANAGER" else ""

    class FakeHub:
        """照 Hub 的真实形状：角色账本叫 identities（复数）。"""

        def __init__(self, role):
            self.identities = Book(role)

    async def capture(origin, card, **kwargs):
        return None

    plugin._send_hub_card = capture
    hub = FakeHub("owner")
    plugin._get_hub = lambda quiet=False: hub  # type: ignore[assignment]
    assert asyncio.run(plugin._act_auto_off(FakeActionContext("MANAGER"), {})) == 0
    assert plugin.store.group_state(FakeActionContext.origin)["auto_level"] == ""


def test_cancel_refused_when_the_recorded_role_is_a_plain_member(plugin):
    plugin.store.touch_group(FakeActionContext.origin, "G1")
    plugin.store.set_auto_level(FakeActionContext.origin, "high")

    class Book:
        async def is_group_manager(self, origin, openid):
            return False

        async def role_of(self, origin, openid):
            return "member"

    class FakeHub:
        def __init__(self):
            self.identities = Book()

    plugin._get_hub = lambda quiet=False: FakeHub()  # type: ignore[assignment]
    assert asyncio.run(plugin._act_auto_off(FakeActionContext("MANAGER"), {})) == 4
    assert plugin.store.group_state(FakeActionContext.origin)["auto_level"] == "high"


def test_cancel_uses_the_hub_gate_when_it_is_available(plugin):
    """Hub 自己的 _clicker_is_manager 还认 operator / AstrBot 管理员，
    插件跟它用同一套判断，才不会出现「Hub 说能点、插件说不能」。"""
    plugin.store.touch_group(FakeActionContext.origin, "G1")
    plugin.store.set_auto_level(FakeActionContext.origin, "high")
    seen = []

    class FakeHub:
        async def _clicker_is_manager(self, origin, openid):
            seen.append(openid)
            return True

        # 故意不放 identities：先走 Hub 的闸门就该直接放行
    async def capture(origin, card, **kwargs):
        return None

    plugin._send_hub_card = capture
    plugin._get_hub = lambda quiet=False: FakeHub()  # type: ignore[assignment]
    assert asyncio.run(plugin._act_auto_off(FakeActionContext("ANYONE"), {})) == 0
    assert seen == ["ANYONE"]


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


# --- 失败即关订阅（用户要求：别占着一个发不出去的档位）-----------------------

class _BoomHub:
    def __init__(self, message):
        self.message = message

    async def send_ephemeral_card(self, *a, **k):
        raise RuntimeError(self.message)

    async def send_image_message(self, *a, **k):
        raise RuntimeError(self.message)


def _make_push_fail(plugin, message, umo="umo:A"):
    _add_meme(plugin, "a")
    _subscribe(plugin, umo, "low")
    plugin._get_hub = lambda quiet=False: _BoomHub(message)  # type: ignore[assignment]
    plugin._read_file = lambda path: b"\xff\xd8\xff\xe0" + b"0" * 32  # 让它走到发送


def test_no_permission_closes_the_subscription(plugin):
    """群没开「机器人主动发言」→ 不能再留着每天白试。"""
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    _make_push_fail(plugin, "40034105 主动消息失败, 无权限")
    asyncio.run(plugin._auto_push_one("umo:A", day_key="2026-10-25"))

    state = plugin.store.group_state("umo:A")
    assert state["auto_level"] == ""
    assert state["auto_off_reason"] == "no_permission"
    assert state["auto_off_at"] > 0
    assert state["auto_opt_out"] == 0  # 不是人关的，页面上要能区分


def test_not_in_group_closes_the_subscription(plugin):
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    _make_push_fail(plugin, "机器人不在群 群不存在")
    asyncio.run(plugin._auto_push_one("umo:A", day_key="2026-10-25"))
    assert plugin.store.group_state("umo:A")["auto_off_reason"] == "gone"


def test_network_blip_does_not_close_the_subscription(plugin):
    """网络抽风、素材库故障不该把订阅关掉 —— 关掉会让人以为功能坏了。"""
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    _make_push_fail(plugin, "ConnectTimeout: 连接超时")
    asyncio.run(plugin._auto_push_one("umo:A", day_key="2026-10-25"))
    state = plugin.store.group_state("umo:A")
    assert state["auto_level"] == "low"
    assert state["auto_off_reason"] == ""


def test_failure_classifier_table():
    from meme_plugin_under_test.main import MemePlugin as P

    assert P._classify_auto_failure("40034105 无权限") == "no_permission"
    assert P._classify_auto_failure("主动消息失败") == "no_permission"
    assert P._classify_auto_failure("群不存在") == "gone"
    assert P._classify_auto_failure("invalid group openid") == "gone"
    assert P._classify_auto_failure("连接超时") == ""
    assert P._classify_auto_failure("") == ""


def test_dead_session_message_closes_the_subscription():
    """「用户/群已注销」也是「这个会话没了」，必须关订阅。

    实测一个已注销的私聊会话每天都被抽中、每次必失败，一周白吃 19 张图的
    配额；而当时认不出这句措辞，订阅一直没被关掉。
    """
    from meme_plugin_under_test.main import MemePlugin as P

    assert P._classify_auto_failure("请求的资源不存在(用户/群已注销)") == "gone"
    assert P._classify_auto_failure("ServerError: 用户已注销") == "gone"


# --- 记账必须在发出去之后（否则发失败也吃配额、还踹到队尾）------------------

def test_failed_push_does_not_mark_the_meme_used(plugin):
    """发失败了，这张图不能算「这个群看过了」。

    原来记账写在发送之前：一个已注销的私聊会话每天都被抽中、每次都发不出去，
    但那 19 张图全被记成「刚发过」，used_at 被推到当前时刻踹到队尾 ——
    群里没人看见，配额还白白吃掉。
    """
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    _make_push_fail(plugin, "ConnectTimeout: 连接超时")

    before = [m.id for m in plugin.store.random_pool()]
    asyncio.run(plugin._auto_push_one("umo:A", day_key="2026-10-25"))

    # 一张图都没被记成「发过」：全局 used_count 还是 0，本群也没有投递记录
    assert all(m.used_count == 0 for m in plugin.store.random_pool())
    assert plugin.store.seen_by("umo:A") == {}
    assert before  # 池子本身没被动过


def test_successful_push_marks_meme_used_for_that_group(plugin):
    """发成功了才记账，而且记到**这个群**头上。"""
    _add_meme(plugin, "a")
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")

    sent = {}

    async def fake_send(origin, item, **kwargs):
        sent["origin"] = origin
        sent["id"] = item.id
        return True

    plugin._send_item_by_origin = fake_send  # type: ignore[assignment]
    asyncio.run(plugin._auto_push_one("umo:A", day_key="2026-10-25"))

    assert sent["origin"] == "umo:A"
    seen = plugin.store.seen_by("umo:A")
    assert list(seen) == [sent["id"]], "只该记这一张，而且记在本群头上"
    assert seen[sent["id"]] > 0, "first_at 要有时间戳，靠它判断本群的冷热"
    # 别的群不受影响
    assert plugin.store.seen_by("umo:B") == {}


# --- 批量铺开低档 -----------------------------------------------------------

def test_bootstrap_opens_low_for_seen_groups(plugin):
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.touch_group("umo:B", "G2")
    opened = asyncio.run(plugin._bootstrap_auto(force=True))
    assert sorted(opened) == ["umo:A", "umo:B"]
    assert plugin.store.group_state("umo:A")["auto_level"] == "low"


def test_bootstrap_skips_already_subscribed_groups(plugin):
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "high")
    assert asyncio.run(plugin._bootstrap_auto(force=True)) == []
    assert plugin.store.group_state("umo:A")["auto_level"] == "high"


def test_bootstrap_never_reopens_a_group_a_human_closed(plugin):
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    plugin.store.set_auto_level("umo:A", "", opt_out=True, reason="manual")
    assert asyncio.run(plugin._bootstrap_auto(force=True)) == []
    assert plugin.store.group_state("umo:A")["auto_level"] == ""


def test_bootstrap_never_retries_a_group_that_has_no_permission(plugin):
    """自动关过的群（墓碑）不该每次重启都再试一遍。"""
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    plugin.store.set_auto_level("umo:A", "", opt_out=False, reason="no_permission")
    assert asyncio.run(plugin._bootstrap_auto(force=True)) == []
    state = plugin.store.group_state("umo:A")
    assert state["auto_level"] == "" and state["auto_off_reason"] == "no_permission"


def test_bootstrap_ignores_stale_groups(plugin):
    plugin.config["auto_push_bootstrap_days"] = 30
    plugin.store.touch_group("umo:OLD", "G9")
    # 把 last_seen 改成 100 天前
    plugin.store._write(
        "UPDATE groups SET last_seen = ? WHERE umo = ?", (1000.0 - 100 * 86400, "umo:OLD")
    )
    plugin.store.touch_group("umo:NEW", "G1")
    assert asyncio.run(plugin._bootstrap_auto(force=True)) == ["umo:NEW"]


def test_bootstrap_obeys_the_switch(plugin):
    plugin.config["auto_push_bootstrap"] = False
    plugin.store.touch_group("umo:A", "G1")
    assert asyncio.run(plugin._bootstrap_auto()) == []
    assert plugin.store.group_state("umo:A")["auto_level"] == ""


def test_reopening_clears_the_tombstone(plugin):
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "", opt_out=False, reason="no_permission")
    plugin.store.set_auto_level("umo:A", "mid")
    state = plugin.store.group_state("umo:A")
    assert state["auto_level"] == "mid"
    assert state["auto_off_reason"] == "" and state["auto_opt_out"] == 0


def test_auto_offed_groups_are_listed_for_the_panel(plugin):
    plugin.store.touch_group("umo:A", "G1")
    plugin.store.set_auto_level("umo:A", "low")
    plugin.store.set_auto_level("umo:A", "", opt_out=False, reason="gone")
    rows = plugin.store.auto_offed_groups()
    assert [r["umo"] for r in rows] == ["umo:A"]
    assert rows[0]["auto_off_reason"] == "gone"


# --- 每日额度按群独立（用户的明确要求）--------------------------------------

def test_daily_quota_is_counted_per_group(plugin):
    """一个群发满 8 条，不能影响别的群今天还能发几条。"""
    _add_meme(plugin, "a")
    a, b = "umo:A", "umo:B"
    for umo in (a, b):
        plugin.store.touch_group(umo, "G")
        plugin.store.set_auto_level(umo, "high")

    # A 群发满今天的额度
    for _ in range(8):
        assert plugin.store.note_auto_push(a, day_key="2026-10-25") > 0

    assert plugin.store.group_state(a)["auto_sent_count"] == 8
    # B 群还是 0：额度是按群算的，不是全局共享
    assert plugin.store.group_state(b)["auto_sent_count"] == 0
    assert plugin.store.note_auto_push(b, day_key="2026-10-25") == 1


def test_daily_quota_resets_on_a_new_day(plugin):
    plugin.store.touch_group("umo:A", "G")
    plugin.store.set_auto_level("umo:A", "high")
    for _ in range(5):
        plugin.store.note_auto_push("umo:A", day_key="2026-10-25")
    assert plugin.store.group_state("umo:A")["auto_sent_count"] == 5
    # 第二天从 1 重新数
    assert plugin.store.note_auto_push("umo:A", day_key="2026-10-26") == 1
