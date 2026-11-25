"""发送失败时**不许记账** —— 这条曾经让图库「看起来只有几张」。

用户的原话：「明明有一百多张，来来回回还是这几张」。

查下来 `deliveries` 表本身是干净的（每个群 109 次 = 109 张不同图），
但代码里埋着一个静默沉底的坑：`/meme` 命令在 `_send_item` 之后**无条件**
`mark_used`。而 `_send_item` 发不出去时返回的是一句「这张图现在发不出来
（#123）」的提示对象 —— 不是 None，所以调用方分不清「这是提示」还是
「这就是结果」。

一次发送失败（素材库满了、图读了半截、Hub 抽风）就把那张图标成「本群已看过」
推到队尾。群里谁也没看到它，而且它再也不会被挑中。失败次数一多，池子里
能露面的就只剩那几张从没失败过的 —— 这正是「来来回回还是这几张」。
"""
from __future__ import annotations

import asyncio
import io

import pytest

Image = pytest.importorskip("PIL.Image")


def jpeg(color=(120, 80, 200), size=(64, 64)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture()
def plugin(plugin_module, tmp_path):
    class Ctx:
        def __init__(self):
            self.global_config = {}

        def get_provider_by_id(self, _p):
            return None

        def get_registered_star(self, _n):
            return None

        def get_config(self):
            return self.global_config

        def register_web_api(self, *a, **k):
            pass

    instance = plugin_module.MemePlugin(
        Ctx(),
        {"custom_data_dir": str(tmp_path), "cooldown_seconds": 0, "card_materials": False},
    )
    instance.store.init()
    return instance


def seed_one(plugin) -> object:
    data = jpeg()
    import hashlib

    from meme.media import sha256_of

    sha = sha256_of(data)
    assert sha == hashlib.sha256(data).hexdigest()
    path = plugin.store.save_media(sha, data, ".jpg")
    plugin.store.add(sha256=sha, origin="submitted", file_path=path, mime="image/jpeg")
    return plugin.store.all_for_hosting()[0]


def test_failed_send_does_not_mark_the_image_as_seen(plugin, plugin_module):
    """发送失败 → 这张图**不能**被标成本群已看过，否则它永远沉底。"""
    item = seed_one(plugin)

    # _read_file 是同步函数（被 asyncio.to_thread 包着调），所以桩也必须是同步的
    def boom(*args, **kwargs):
        raise OSError("图片读不出来")

    plugin._read_file = boom

    event = _FakeEvent()
    out = asyncio.run(_drain(plugin.meme_entry(event)))

    assert out, "失败时仍要说一句话给用户"
    assert plugin.store.seen_by(event.unified_msg_origin) == {}, (
        "发失败的图不该出现在「本群已看过」里"
    )
    assert item.id not in plugin.store.seen_by(event.unified_msg_origin)


def test_successful_send_still_marks_the_image(plugin):
    """正常发出去还是要记账 —— 别把修复做成「永远不记账」。"""
    item = seed_one(plugin)

    async def fake_send(event, item_):
        return None

    plugin._send_item = fake_send
    event = _FakeEvent()
    out = asyncio.run(_drain(plugin.meme_entry(event)))

    assert out == [None] or out == []
    assert item.id in plugin.store.seen_by(event.unified_msg_origin)


def test_send_failed_marker_is_falsy_so_it_cannot_be_mistaken_for_success(
    plugin_module,
):
    """`SendFailed` 必须是假值。

    这条看着琐碎，实际是核心：`_send_item_by_origin` 成功返回 `None`、
    失败返回这个包装。只要它是假值，任何 `if result:` 式的判断都不会把
    失败当成成功；反过来漏判了，就会退回「图没发出去但记账照做」。
    """
    marker = plugin_module.SendFailed("这里有话说")
    assert not marker
    assert bool(marker) is False
    assert marker.result == "这里有话说"


async def _drain(agen):
    """把命令 handler 的异步生成器跑完，收集它 yield 出来的东西。"""
    return [item async for item in agen]


class _FakeEvent:
    """只够 `meme_entry` 用。"""

    def __init__(self):
        self.unified_msg_origin = "umo:group:G1"
        self.message_str = "/meme"
        self.message_obj = None

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return ("chain", chain)

    def get_sender_id(self):
        return "u1"

    def get_sender_name(self):
        return "tester"

    def is_admin(self):
        return False

    def stop_event(self):
        pass

    def set_extra(self, *a, **k):
        pass

    def get_extra(self, *a, **k):
        return None

    def get_messages(self):
        return []

    def get_group_id(self):
        return "G1"

    def get_platform_name(self):
        return "qq_official"

    def is_private_chat(self):
        return False
