"""插件层：投稿命令、黑名单拦截、卡片按钮动作。

这里的重点是"人"而不是"图"：
* 被拉黑的人再投稿，**一次模型都不该调用**（省钱，也说明拒的是人）；
* `/meme 投稿` 进公共库（别的群刷得到），`/meme 收` 仍按旧配置；
* 管理员永远拉不掉。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import io
import json

import pytest

from meme.chinese import MODE_APPEND
from meme.pipeline import MemePipeline
from meme.review import ReviewPolicy

Image = pytest.importorskip("PIL.Image")


def jpeg_bytes(color=(200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (90, 90), color).save(buffer, "JPEG")
    return buffer.getvalue()


def review_json(**kwargs) -> str:
    base = {
        "is_meme": True,
        "nsfw_level": 0,
        "flags": [],
        "caption_text": "meme",
        "language": "zh",
        "reason": "ok",
        "ban_submitter": False,
        "ban_reason": "",
    }
    base.update(kwargs)
    return json.dumps(base, ensure_ascii=False)


class FakeModerator:
    def __init__(self, payload=None):
        self.payload = payload or review_json()
        self.calls = 0

    async def __call__(self, data_url):
        self.calls += 1
        return self.payload


class FakeTranslator:
    async def __call__(self, prompt):
        return ""


class FakeComponent:
    """冒充 astrbot 的 Image 组件：只要插件能认出它是图、并能拿到字节就行。"""

    type = "Image"

    def __init__(self, data: bytes):
        self.data = data


class FakeEvent:
    def __init__(self, *, text="/meme 投稿", sender="U1", group="G1", admin=False, images=()):
        self.message_str = text
        self.unified_msg_origin = f"qq_official:GroupMessage:{group}"
        self._sender = sender
        self._group = group
        self._admin = admin
        self._images = list(images)
        self.results = []

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        self.results.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return ("chain", chain)


class FakeContext:
    def __init__(self):
        self.sent = []

    def get_provider_by_id(self, provider_id):
        return None

    def get_registered_star(self, name):
        return None

    def get_config(self):
        return {}

    def register_web_api(self, *args, **kwargs):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


@pytest.fixture()
def plugin(plugin_module, tmp_path):
    context = FakeContext()
    instance = plugin_module.MemePlugin(
        context,
        {
            "custom_data_dir": str(tmp_path),
            "cooldown_seconds": 0,
            "send_cooldown_seconds": 0,
            "caption_zh_mode": MODE_APPEND,
            "daily_submit_limit": 10,
            "submission_cooldown_seconds": 0,
        },
    )
    instance.store.init()
    moderator = FakeModerator()
    instance.pipeline = MemePipeline(
        instance.store, ReviewPolicy(), moderator=moderator,
        translator=FakeTranslator(), caption_mode=MODE_APPEND,
    )
    instance._component_bytes = _component_bytes  # 直接用字节，绕开 AstrBot 组件
    return instance, context, moderator


async def _component_bytes(component, timeout: float = 20.0) -> bytes:
    return component.data


def submit(instance, event):
    """完整走一遍命令入口（分发也在测：删掉「收」之后不该再有第二个入口）。"""
    async def run():
        event.message_obj = SimpleNamespace(message=list(event._images))
        return [result async for result in instance.meme_entry(event)]

    return asyncio.run(run())


def text_of(results) -> str:
    return "\n".join(str(payload) for kind, payload in results if kind == "plain")


# --- 投稿去向 ---------------------------------------------------------------

def test_submit_goes_into_the_shared_library(plugin):
    instance, _context, moderator = plugin
    event = FakeEvent(images=[FakeComponent(jpeg_bytes())])
    submit(instance, event)
    assert moderator.calls == 1
    item = instance.store.recent(limit=10)[0]
    assert item.origin == "submitted"
    assert "已入库" in text_of(event.results)


def test_old_shou_command_no_longer_submits(plugin):
    """「/meme 收」这个写法已经删了：图库只有一份，不再有第二个入口。"""
    instance, _context, _moderator = plugin
    event = FakeEvent(text="/meme 收", images=[FakeComponent(jpeg_bytes((1, 2, 3)))])
    submit(instance, event)
    assert instance.store.recent(limit=10) == []


def test_bare_image_without_subcommand_only_hints(plugin):
    """带图但没写子命令：不入库，只回一句怎么写。"""
    instance, _context, _moderator = plugin
    event = FakeEvent(text="", images=[FakeComponent(jpeg_bytes())])
    submit(instance, event)
    assert instance.store.recent(limit=10) == []
    assert "/meme 投稿" in text_of(event.results)


def test_submit_without_image_teaches_the_flow(plugin):
    instance, _context, _moderator = plugin
    event = FakeEvent(images=[])
    submit(instance, event)
    text = text_of(event.results)
    assert "引用" in text and "/meme 投稿" in text


# --- 拉黑 -------------------------------------------------------------------

def test_malicious_submission_bans_the_person(plugin):
    instance, context, moderator = plugin
    moderator.payload = review_json(
        is_meme=False, nsfw_level=4, flags=["sexual"],
        reason="露骨色情", ban_submitter=True, ban_reason="露骨色情",
    )
    event = FakeEvent(images=[FakeComponent(jpeg_bytes())])
    submit(instance, event)

    assert instance.store.is_blacklisted("U1")
    entry = instance.store.blacklist_entry("U1")
    assert entry["reason"] == "露骨色情"
    assert entry["evidence_path"].endswith(".img")
    # 证据图真的落盘了
    import os
    assert os.path.isfile(entry["evidence_path"])
    assert "永久禁止投稿" in text_of(event.results)
    # 群里收到一句通知
    assert context.sent


def test_blacklisted_user_is_rejected_without_calling_the_model(plugin):
    instance, _context, moderator = plugin
    moderator.payload = review_json(
        is_meme=False, flags=["gore"], ban_submitter=True, ban_reason="真实血腥"
    )
    submit(instance, FakeEvent(images=[FakeComponent(jpeg_bytes())]))
    calls_after_ban = moderator.calls

    again = FakeEvent(images=[FakeComponent(jpeg_bytes((9, 9, 9)))])
    submit(instance, again)
    assert moderator.calls == calls_after_ban, "被拉黑的人不该再花一次审核"
    assert "投稿权限已被关闭" in text_of(again.results)


def test_admin_is_never_banned(plugin):
    instance, _context, moderator = plugin
    moderator.payload = review_json(
        is_meme=False, flags=["politics"], ban_submitter=True, ban_reason="时政"
    )
    event = FakeEvent(sender="ADMIN", admin=True, images=[FakeComponent(jpeg_bytes())])
    submit(instance, event)
    assert instance.store.is_blacklisted("ADMIN") is False


def test_ban_can_be_switched_off(plugin):
    instance, _context, moderator = plugin
    instance.config["ban_malicious_submitter"] = False
    moderator.payload = review_json(
        is_meme=False, flags=["politics"], ban_submitter=True, ban_reason="时政"
    )
    event = FakeEvent(images=[FakeComponent(jpeg_bytes())])
    submit(instance, event)
    assert instance.store.is_blacklisted("U1") is False
    assert "没收" in text_of(event.results)


def test_unban_restores_submission(plugin):
    instance, _context, moderator = plugin
    moderator.payload = review_json(
        is_meme=False, flags=["gore"], ban_submitter=True, ban_reason="真实血腥"
    )
    submit(instance, FakeEvent(images=[FakeComponent(jpeg_bytes())]))
    assert instance.store.is_blacklisted("U1")

    instance.store.remove_blacklist("U1")
    instance.config["submission_cooldown_seconds"] = 0  # 投稿冷却默认 30 秒，这里关掉
    moderator.payload = review_json()
    event = FakeEvent(images=[FakeComponent(jpeg_bytes((5, 5, 5)))])
    submit(instance, event)
    assert "已入库" in text_of(event.results)


# --- 卡片按钮动作 -----------------------------------------------------------

class FakeButtonContext:
    origin = "qq_official:GroupMessage:G9"
    member_openid = "U9"
    client = None
    interaction = None


def test_again_button_sends_a_card(plugin):
    instance, _context, _moderator = plugin
    instance.store.add(
        sha256="b" * 64, file_path="/tmp/none.jpg", origin="submitted",
        mime="image/jpeg", caption="绷不住了",
    )
    calls = []

    async def fake_send(origin, item, **kwargs):
        calls.append((origin, item.id))
        return None

    instance._send_item_by_origin = fake_send
    code = asyncio.run(instance._act_again(FakeButtonContext(), {}))
    assert code == 0
    assert calls and calls[0][0] == FakeButtonContext.origin


def test_again_button_respects_cooldown(plugin):
    instance, _context, _moderator = plugin
    instance.config["cooldown_seconds"] = 60
    instance.store.add(
        sha256="c" * 64, file_path="/tmp/none.jpg", origin="submitted",
        mime="image/jpeg",
    )
    called = []

    async def fake_send(*args, **kwargs):
        called.append(1)

    instance._send_item_by_origin = fake_send
    assert asyncio.run(instance._act_again(FakeButtonContext(), {})) == 0
    # 紧接着再点一次：冷却没到，返回 4（无权限/暂时不行），也不该再发
    assert asyncio.run(instance._act_again(FakeButtonContext(), {})) == 4
    assert len(called) == 1


def test_howto_button_returns_the_guide_card(plugin):
    instance, _context, _moderator = plugin
    sent = []

    class Hub:
        async def send_ephemeral_card(self, origin, card, **kwargs):
            sent.append(card)
            return "s"

    instance._hub = Hub()
    instance._get_hub = lambda quiet=False: instance._hub
    code = asyncio.run(instance._act_howto(FakeButtonContext(), {}))
    assert code == 0
    assert "/meme 投稿" in sent[0]["markdown"]
