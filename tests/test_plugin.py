"""接进插件这一层：命令分词、发图、投稿、权限、冷却、定时。

业务逻辑在 test_pipeline / test_store / test_review 里钉死了；这里只管
「点得动、分得对、发不出去时说得清」。
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os

import pytest

from meme.chinese import MODE_APPEND
from meme.pipeline import MemePipeline
from meme.review import ReviewPolicy

JPEG_HEAD = b"\xff\xd8\xff\xe0"
JPEG = JPEG_HEAD + b"fake-jpeg-body"
GOOD_REVIEW = {
    "is_meme": True,
    "nsfw_level": 0,
    "flags": [],
    "caption_text": "when the code works",
    "language": "en",
    "reason": "ok",
}


class FakeMessage:
    """对应 AstrBot 的 AstrBotMessage：消息段在 ``.message`` 里。"""

    def __init__(self, chain):
        self.message = chain


class FakeComponent:
    """假的 AstrBot 消息段。用 type 字符串区分，和真的 ComponentType 行为一致。"""

    def __init__(self, type, **kwargs):
        self.type = type
        self.__dict__.update(kwargs)


class FakeEvent:
    def __init__(
        self,
        message_str="/meme",
        group_id="G1",
        sender_id="U1",
        umo="qq_official:GroupMessage:G1",
        components=(),
    ):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._sender_id = sender_id
        self.message_obj = FakeMessage(list(components))
        self.results = []

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        self.results.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return ("chain", chain)


class FakeContext:
    def __init__(self, providers=None, global_config=None, hub_star=None):
        self.providers = providers or {}
        self.global_config = global_config or {}
        self.hub_star = hub_star
        self.sent = []

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    def get_registered_star(self, name):
        return self.hub_star

    def get_config(self):
        return self.global_config

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class FakeModerator:
    def __init__(self, payload=None):
        self.payload = json.dumps(payload or GOOD_REVIEW, ensure_ascii=False)
        self.calls = 0

    async def __call__(self, data_url):
        self.calls += 1
        return self.payload


@pytest.fixture()
def plugin(plugin_module, tmp_path):
    context = FakeContext()
    instance = plugin_module.MemePlugin(
        context,
        {
            "custom_data_dir": str(tmp_path),
            "cooldown_seconds": 0,
            "send_cooldown_seconds": 0,
            "submission_cooldown_seconds": 0,
            "caption_zh_mode": MODE_APPEND,
        },
    )
    instance.store.init()
    instance.pipeline = MemePipeline(
        instance.store,
        ReviewPolicy(),
        moderator=FakeModerator(),
        caption_mode=MODE_APPEND,
    )
    return instance


def add_meme(plugin, tag="one", origin="submitted"):
    data = JPEG_HEAD + tag.encode() + b"\x00" * 32
    sha = __import__("hashlib").sha256(data).hexdigest()
    path = plugin.store.save_media(sha, data, ".jpg")
    meme_id, _created = plugin.store.add(
        sha256=sha,
        file_path=path,
        origin=origin,
        caption="hi",
    )
    return plugin.store.get(meme_id)


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [chunk async for chunk in agen]


# --- 分词 ------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("/meme", ""),
        ("/meme 库", "库"),
        ("梗图 收", "收"),
        ("来张梗图 每日 9:00,21:00", "每日 9:00,21:00"),
        ("随便说点什么", "随便说点什么"),
        ("", ""),
    ],
)
def test_command_argument_parsing(plugin, text, expected):
    assert plugin._args(FakeEvent(message_str=text)) == expected


# --- 随机发图 ---------------------------------------------------------------

def test_random_send_returns_image_with_caption(plugin):
    add_meme(plugin, "one")
    event = FakeEvent()
    result = run(plugin._do_random(event))
    kind, payload = result
    assert kind == "chain"
    assert len(payload) == 2  # 图 + 图注
    assert payload[1].text == "hi"


def test_random_send_degrades_to_text_when_pick_fails(plugin):
    event = FakeEvent()
    kind, payload = run(plugin._do_random(event))
    assert kind == "plain"
    assert "投稿" in payload


def test_random_send_is_cooldown_limited(plugin):
    plugin.config["cooldown_seconds"] = 60
    add_meme(plugin, "one")
    run(plugin._do_random(FakeEvent()))
    kind, payload = run(plugin._do_random(FakeEvent()))
    assert kind == "plain"
    assert "秒后再来" in payload


def test_group_cooldown_applies_across_users(plugin):
    plugin.config["send_cooldown_seconds"] = 30
    add_meme(plugin, "one")
    add_meme(plugin, "two")
    run(plugin._do_random(FakeEvent(sender_id="U1")))
    kind, payload = run(plugin._do_random(FakeEvent(sender_id="U2")))
    assert kind == "plain"
    assert "这个群" in payload


def test_send_marks_usage(plugin):
    item = add_meme(plugin, "one")
    run(plugin._do_random(FakeEvent()))
    assert plugin.store.get(item.id).used_count == 1


def test_missing_file_falls_back_to_text(plugin):
    item = add_meme(plugin, "gone")
    os.remove(item.file_path)
    kind, payload = run(plugin._do_random(FakeEvent()))
    assert kind == "plain"
    assert "发不出来" in payload


def test_hub_path_is_preferred_when_available(plugin):
    sent = {}

    class FakeHub:
        async def send_image_message(self, origin, data, text=""):
            sent["origin"] = origin
            sent["text"] = text
            return "msg-1"

    plugin._hub = FakeHub()
    add_meme(plugin, "one")
    assert run(plugin._do_random(FakeEvent())) is None
    assert sent["text"] == "hi"


def test_hub_failure_falls_back_to_native(plugin):
    class BrokenHub:
        async def send_image_message(self, origin, data, text=""):
            raise RuntimeError("上传失败")

    plugin._hub = BrokenHub()
    add_meme(plugin, "one")
    kind, _payload = run(plugin._do_random(FakeEvent()))
    assert kind == "chain"


# --- 投稿 ------------------------------------------------------------------

def test_submission_stores_the_image(plugin, monkeypatch):
    async def fake_bytes(component):
        return JPEG_HEAD + b"submitted-body"

    monkeypatch.setattr(plugin, "_component_bytes", fake_bytes)
    event = FakeEvent(
        message_str="/meme 收", components=[FakeComponent("Image", url="http://x/1.jpg")]
    )
    chunks = run(collect(plugin._do_submit(event, plugin._event_images(event))))
    assert "已入库" in chunks[0][1]
    assert plugin.store.stats()["total"] == 1


def test_submission_without_image_explains_how(plugin):
    event = FakeEvent(message_str="/meme 收")
    chunks = run(collect(plugin._do_submit(event, [])))
    assert "投稿方式" in chunks[0][1]


def test_submission_denied_by_moderation_is_reported(plugin):
    bad = dict(GOOD_REVIEW, flags=["politics"])
    plugin.pipeline = MemePipeline(
        plugin.store,
        ReviewPolicy(),
        moderator=FakeModerator(bad),
        caption_mode=MODE_APPEND,
    )

    async def fake_bytes(component):
        return JPEG_HEAD + b"bad-body"

    plugin._component_bytes = fake_bytes
    event = FakeEvent(
        message_str="/meme 收", components=[FakeComponent("Image", url="http://x/2.jpg")]
    )
    chunks = run(collect(plugin._do_submit(event, plugin._event_images(event))))
    assert "没收" in chunks[0][1]
    assert "politics" in chunks[0][1]


def test_submission_respects_daily_limit(plugin):
    plugin.config["daily_submit_limit"] = 1
    plugin.store.set_meta(plugin._submit_day_key("qq_official:GroupMessage:G1", "U1"), "1")

    async def fake_bytes(component):
        return JPEG_HEAD + b"another"

    plugin._component_bytes = fake_bytes
    event = FakeEvent(
        message_str="/meme 收", components=[FakeComponent("Image", url="http://x/3.jpg")]
    )
    chunks = run(collect(plugin._do_submit(event, plugin._event_images(event))))
    assert "今天已经投了" in chunks[0][1]


def test_image_in_quoted_message_counts_as_submission(plugin):
    quoted = FakeComponent(
        "Reply", id="1", chain=[FakeComponent("Image", url="http://x/q.jpg")]
    )
    event = FakeEvent(message_str="/meme 收", components=[quoted])
    images = plugin._event_images(event)
    assert len(images) == 1 and images[0].url == "http://x/q.jpg"


def test_text_only_message_has_no_images(plugin):
    event = FakeEvent(message_str="/meme", components=[FakeComponent("Plain", text="x")])
    assert plugin._event_images(event) == []


# --- 库 / 权限 --------------------------------------------------------------

def test_stats_command_reports_empty_library(plugin):
    chunks = run(collect(plugin._do_stats(FakeEvent())))
    assert "还没有可发的图" in chunks[0][1]


def test_stats_command_counts_the_shared_library(plugin):
    add_meme(plugin, "one")
    add_meme(plugin, "two")
    chunks = run(collect(plugin._do_stats(FakeEvent())))
    # 图库只有一份：两个「群」问到的都是同一批
    assert "库里能发：2 张" in chunks[0][1]


def test_delete_requires_admin(plugin):
    item = add_meme(plugin, "one")
    kind, payload = run(plugin._do_delete(FakeEvent(sender_id="U1"), str(item.id)))
    assert kind == "plain"
    assert "管理员" in payload


def test_admin_from_extra_admins_can_delete(plugin):
    plugin.config["extra_admins"] = ["U1"]
    item = add_meme(plugin, "one")
    _kind, payload = run(plugin._do_delete(FakeEvent(sender_id="U1"), str(item.id)))
    assert "已下架" in payload
    assert plugin.store.get(item.id) is None


def test_admin_from_global_config_can_delete(plugin):
    plugin.context.global_config = {"admins_id": ["U9"]}
    item = add_meme(plugin, "one")
    _kind, payload = run(plugin._do_delete(FakeEvent(sender_id="U9"), str(item.id)))
    assert "已下架" in payload


def test_review_command_flags_a_bad_image(plugin):
    plugin.config["extra_admins"] = ["U1"]
    item = add_meme(plugin, "one")
    bad = dict(GOOD_REVIEW, nsfw_level=4)
    plugin.pipeline = MemePipeline(
        plugin.store,
        ReviewPolicy(),
        moderator=FakeModerator(bad),
        caption_mode=MODE_APPEND,
    )
    _kind, payload = run(plugin._do_review(FakeEvent(sender_id="U1"), str(item.id)))
    assert "不通过" in payload
    assert plugin.store.get(item.id).status == "disabled"


# --- 组件字节 ---------------------------------------------------------------

def test_component_bytes_from_base64(plugin):
    component = FakeComponent(
        "Image", file="base64://" + base64.b64encode(JPEG).decode()
    )
    assert run(plugin._component_bytes(component)) == JPEG


def test_component_bytes_from_local_file(plugin, tmp_path):
    path = tmp_path / "x.jpg"
    path.write_bytes(JPEG)
    component = FakeComponent("Image", file=f"file://{path}")
    assert run(plugin._component_bytes(component)) == JPEG


def test_component_bytes_without_source_raises(plugin):
    with pytest.raises(RuntimeError):
        run(plugin._component_bytes(FakeComponent("Image")))


# --- 时间 / 免打扰 ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9:00,21:00", ["09:00", "21:00"]),
        ("09:00 21:30", ["09:00", "21:30"]),
        ("9.30", ["09:30"]),
        ("25:00,08:00", ["08:00"]),
        ("胡说八道", []),
        ("", []),
    ],
)
def test_parse_slots(plugin_module, raw, expected):
    assert plugin_module._parse_slots(raw) == expected


def test_quiet_hours_within_same_day(plugin):
    plugin.config["daily_push_quiet_start"] = "13:00"
    plugin.config["daily_push_quiet_end"] = "14:00"
    assert plugin._in_quiet_hours(dt.datetime(2026, 10, 25, 13, 30)) is True
    assert plugin._in_quiet_hours(dt.datetime(2026, 10, 25, 15, 0)) is False


def test_quiet_hours_across_midnight(plugin):
    plugin.config["daily_push_quiet_start"] = "23:00"
    plugin.config["daily_push_quiet_end"] = "08:00"
    assert plugin._in_quiet_hours(dt.datetime(2026, 10, 25, 23, 30)) is True
    assert plugin._in_quiet_hours(dt.datetime(2026, 10, 25, 3, 0)) is True
    assert plugin._in_quiet_hours(dt.datetime(2026, 10, 25, 12, 0)) is False


def test_quiet_hours_disabled_when_unset(plugin):
    assert plugin._in_quiet_hours(dt.datetime(2026, 10, 25, 3, 0)) is False


# --- 每日推送 ---------------------------------------------------------------

def test_daily_command_shows_state_and_requires_admin(plugin):
    chunks = run(collect(plugin._do_daily(FakeEvent(), "")))
    assert "关着" in chunks[0][1]
    chunks = run(collect(plugin._do_daily(FakeEvent(sender_id="U1"), "9:00")))
    assert "管理员" in chunks[0][1]


def test_daily_command_enables_slots(plugin):
    plugin.config["extra_admins"] = ["U1"]
    chunks = run(collect(plugin._do_daily(FakeEvent(), "9:00,21:00")))
    assert "已开" in chunks[0][1]
    state = plugin.store.group_state("qq_official:GroupMessage:G1")
    assert state["daily_enabled"] == 1
    assert state["daily_slots"] == "09:00,21:00"


def test_daily_command_can_be_turned_off(plugin):
    plugin.config["extra_admins"] = ["U1"]
    run(collect(plugin._do_daily(FakeEvent(), "9:00")))
    chunks = run(collect(plugin._do_daily(FakeEvent(), "关")))
    assert "已关" in chunks[0][1]
    assert plugin.store.group_state("qq_official:GroupMessage:G1")["daily_enabled"] == 0


def test_daily_push_marks_usage_and_counter(plugin, monkeypatch):
    add_meme(plugin, "one")
    umo = "qq_official:GroupMessage:G1"
    plugin.store.touch_group(umo, "G1")
    plugin.store.set_daily(umo, True, "09:00")

    sent = {}

    class FakeHub:
        async def send_media_message(self, origin, data, file_type, text=""):
            sent["file_type"] = file_type
            sent["text"] = text
            return "msg-1"

    plugin._hub = FakeHub()
    run(plugin._daily_push(umo))
    assert sent["file_type"] == 1  # QQ 的「图片」编号
    assert plugin.store.group_state(umo)["push_count"] == 1


def test_daily_push_respects_per_day_cap(plugin):
    add_meme(plugin, "one")
    umo = "qq_official:GroupMessage:G1"
    plugin.store.touch_group(umo, "G1")
    plugin.store.set_daily(umo, True, "09:00")
    plugin.config["daily_push_max_per_day"] = 0

    class FakeHub:
        async def send_media_message(self, *a, **k):
            raise AssertionError("不该发")

    plugin._hub = FakeHub()
    run(plugin._daily_push(umo))


def test_daily_push_skips_blocked_groups(plugin):
    umo = "qq_official:GroupMessage:G1"
    plugin.store.set_daily(umo, True, "09:00")
    plugin.store.note_push(umo, ok=False, day_key="2026-10-25", blocked_until=9e9)

    class FakeHub:
        async def send_media_message(self, *a, **k):
            raise AssertionError("不该发")

    plugin._hub = FakeHub()
    run(plugin._daily_push(umo))


def test_permission_denied_blocks_group_for_a_day(plugin):
    add_meme(plugin, "one")
    umo = "qq_official:GroupMessage:G1"
    plugin.store.touch_group(umo, "G1")
    plugin.store.set_daily(umo, True, "09:00")

    class NoPermissionHub:
        async def send_media_message(self, *a, **k):
            raise RuntimeError("40034105 主动消息失败, 无权限")

    plugin._hub = NoPermissionHub()
    plugin.context.send_message = None  # 让退路也失败
    run(plugin._daily_push(umo))
    state = plugin.store.group_state(umo)
    assert state["blocked_until"] > 0


# --- 自检（必须自己带图）---------------------------------------------------

def test_selftest_requires_admin(plugin):
    chunks = run(collect(plugin._do_selftest(FakeEvent(sender_id="U1"), [])))
    assert "管理员" in chunks[0][1]


def test_selftest_needs_an_image_now(plugin):
    """内置图已经全删了：不带图自检只会让你先引用一张，不会偷偷拿别的图顶。"""
    plugin.config["extra_admins"] = ["U1"]

    class FakeImage:
        type = "Image"

        def __init__(self, data):
            self.data = data

    chunks = run(collect(plugin._do_selftest(FakeEvent(sender_id="U1"), [])))
    assert "/meme 测试" in chunks[0][1]

    async def fake_bytes(component):
        return component.data

    plugin._component_bytes = fake_bytes
    chunks = run(collect(plugin._do_selftest(FakeEvent(sender_id="U1"), [FakeImage(JPEG_HEAD + b"probe")])))
    assert "自检结果" in chunks[0][1]


# --- 惰性解析 Provider ------------------------------------------------------

def test_pipeline_waits_for_providers_that_load_after_the_plugin(plugin_module, tmp_path):
    """AstrBot 先 plugin_manager.reload()，再 provider_manager.initialize()。

    所以 initialize() 那一刻 get_provider_by_id 一定是 None；管线必须等到
    第一次真正用的时候才解析，否则配置明明填了也会一直显示「未配」。
    """
    context = FakeContext(providers={})

    class FakeProvider:
        provider_config = {"api_base": "https://x/v1", "key": "k", "model": "m"}

        async def text_chat(self, prompt=None, image_urls=None, **kwargs):
            return type("R", (), {"completion_text": "{}"})()

    instance = plugin_module.MemePlugin(
        context,
        {
            "custom_data_dir": str(tmp_path),
            "moderation_provider_id": "late/provider",
            "caption_zh_mode": MODE_APPEND,
        },
    )
    instance.store.init()
    # Provider 还没加载：这时不能把「没有审核」这件事钉死
    assert instance.pipeline.moderator is None

    context.providers["late/provider"] = FakeProvider()
    assert instance.pipeline.moderator is not None
