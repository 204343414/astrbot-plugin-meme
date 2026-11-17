"""卡片：拼得对、按钮短、额度会回收、发不出去就降级。

QQ 的硬规矩都钉在这里：Markdown 里的图**必须**带 ``#宽px #高px``；富媒体和
按钮不能同一条消息（所以我们只走卡片）；素材库每机器人 100 张，不回收约
一百次就全线失败，而且报错完全不像额度问题。
"""
from __future__ import annotations

import io

import pytest

from meme.cards import (
    ACTION_AGAIN,
    ACTION_HOWTO,
    MAX_LABEL,
    MAX_SIDE,
    build_howto_card,
    build_meme_card,
    display_size,
    image_markdown,
)
from meme.models import SCOPE_GLOBAL, MemeItem

Image = pytest.importorskip("PIL.Image")


def item(**kwargs) -> MemeItem:
    base = dict(
        id=12,
        sha256="a" * 64,
        origin="submitted",
        scope=SCOPE_GLOBAL,
        file_path="/tmp/x.jpg",
        caption="when the code works",
        caption_zh="当代码跑通的时候",
        used_count=3,
    )
    base.update(kwargs)
    return MemeItem(**base)


def rows_of(card) -> list[dict]:
    return [button for row in card["rows"] for button in row]


# --- Markdown ---------------------------------------------------------------

def test_image_markdown_carries_size():
    """少了 #宽px #高px，QQ 那边就是一张裂图。"""
    md = image_markdown("qqbot-media://image/v1/tok", 1080, 720)
    assert "![梗图 #720px #480px](qqbot-media://image/v1/tok)" == md


def test_display_size_keeps_small_images():
    assert display_size(300, 200) == (300, 200)


def test_display_size_clamps_long_side():
    width, height = display_size(4000, 1000)
    assert max(width, height) == MAX_SIDE
    assert width / height == pytest.approx(4.0, rel=0.01)


def test_display_size_survives_bad_input():
    assert display_size(0, 0) == (MAX_SIDE, MAX_SIDE)
    assert display_size(None, "x") == (MAX_SIDE, MAX_SIDE)


# --- 卡片本身 ---------------------------------------------------------------

def test_card_has_picture_caption_and_two_buttons():
    card = build_meme_card(item(), "qqbot-media://image/v1/tok", width=1080, height=1080)
    assert "![梗图 #720px #720px](qqbot-media://image/v1/tok)" in card["markdown"]
    assert "当代码跑通的时候" in card["markdown"]
    labels = [button["label"] for button in rows_of(card)]
    assert labels == ["再来一张", "我要投稿"]
    assert [button["action_id"] for button in rows_of(card)] == [ACTION_AGAIN, ACTION_HOWTO]


def test_buttons_are_short_and_one_row():
    card = build_meme_card(item(), "x")
    assert len(card["rows"]) == 1
    for button in rows_of(card):
        assert len(button["label"]) <= MAX_LABEL


def test_card_is_clickable_by_everyone():
    """锁成发起者的话，别人点了只会看到"不是你"，等于坏按钮。"""
    assert build_meme_card(item(), "x")["owner_mode"] == "everyone"


def test_card_without_picture_still_has_buttons():
    card = build_meme_card(item(), "")
    assert "![梗图" not in card["markdown"]
    assert [b["action_id"] for b in rows_of(card)] == [ACTION_AGAIN, ACTION_HOWTO]


def test_buttons_can_be_switched_off():
    card = build_meme_card(item(), "x", show_again=False, show_howto=False)
    assert card["rows"] == []


def test_caption_limit_applies():
    card = build_meme_card(item(caption="长" * 500), "x", caption_limit=20)
    line = [row for row in card["markdown"].splitlines() if row.startswith("**#12**")][0]
    assert len(line) <= 40


def test_howto_card_teaches_the_exact_command():
    card = build_howto_card()
    assert "/meme 投稿" in card["markdown"]
    assert "引用" in card["markdown"] and "别的群也能刷到" in card["markdown"]
    assert [b["action_id"] for b in rows_of(card)] == [ACTION_AGAIN]


# --- 素材额度 ---------------------------------------------------------------

def jpeg_bytes(size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 90, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeHub:
    """只实现 meme 用到的那几个 Hub 方法，行为可以逐项开关。"""

    def __init__(
        self,
        *,
        material_ok=True,
        host_ok=False,
        card_error=None,
    ):
        self.material_ok = material_ok
        self.host_ok = host_ok
        self.card_error = card_error
        self.materials: list[str] = []
        self.deleted: list[str] = []
        self.cards: list[dict] = []
        self.images: list[bytes] = []
        self.actions = type("Actions", (), {"registered": [], "unregister_owner": lambda self, owner: None})()

    async def publish_image_material(self, data, client=None, origin="", file_name=""):
        if not self.material_ok:
            raise RuntimeError("素材库挂了")
        image_id = f"img{len(self.materials) + 1}"
        self.materials.append(image_id)
        return {"image_id": image_id, "url": f"qqbot-media://image/v1/{image_id}", "ttl": 90}

    async def delete_image_material(self, image_id, client=None, origin=""):
        self.deleted.append(image_id)
        return True

    async def publish_image_checked(self, data, slot=""):
        if not self.host_ok:
            raise RuntimeError("图床不可用")
        return "https://img.example.com/x.jpg"

    async def send_ephemeral_card(self, origin, card, **kwargs):
        if self.card_error:
            raise self.card_error
        self.cards.append(card)
        return "session-1"

    async def send_image_message(self, origin, data, text="", **kwargs):
        self.images.append(data)
        return "msg-1"


@pytest.fixture()
def plugin(plugin_module, tmp_path):
    context = FakeContext()
    instance = plugin_module.MemePlugin(
        context, {"custom_data_dir": str(tmp_path), "caption_zh_mode": "append"}
    )
    instance.store.init()
    instance._hub = None
    yield instance, context


class FakeContext:
    def __init__(self):
        self.sent = []

    def get_provider_by_id(self, provider_id):
        return None

    def get_registered_star(self, name):
        return getattr(self, "star", None)

    def get_config(self):
        return {}

    def register_web_api(self, *args, **kwargs):
        return None

    async def send_message(self, umo, chain):
        self.sent.append(umo)


def test_card_send_records_material_and_trims(plugin):
    instance, _context = plugin
    hub = FakeHub()
    instance._hub = hub
    instance.config["card_material_keep"] = 5

    import asyncio

    for _ in range(8):
        asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), jpeg_bytes()))

    assert len(hub.cards) == 8
    # 保留最近 5 张，最旧的 3 张被回收
    assert sorted(hub.deleted) == ["img1", "img2", "img3"]
    assert instance.store.card_material_ids() == ["img4", "img5", "img6", "img7", "img8"]


def test_material_failure_falls_back_to_image_host(plugin):
    instance, _context = plugin
    hub = FakeHub(material_ok=False, host_ok=True)
    instance._hub = hub

    import asyncio

    assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), jpeg_bytes())) is True
    assert "https://img.example.com/x.jpg" in hub.cards[0]["markdown"]
    assert instance.store.card_material_ids() == []


def test_both_sources_down_reports_false(plugin):
    instance, _context = plugin
    instance._hub = FakeHub(material_ok=False, host_ok=False)

    import asyncio

    assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), jpeg_bytes())) is False


def test_rejected_card_releases_material(plugin):
    """QQ 拒了这张卡（比如图转存失败），那张素材要当场退掉，别白占额度。"""
    instance, _context = plugin
    hub = FakeHub(card_error=RuntimeError("40034004"))
    instance._hub = hub

    import asyncio

    assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), jpeg_bytes())) is False
    assert hub.deleted == ["img1"]
    assert instance.store.card_material_ids() == []


# --- 素材自检（面板上的「卡片图能不能显示」） ------------------------------

def test_card_probe_reports_material_ok_and_cleans_up(plugin):
    instance, _context = plugin
    hub = FakeHub()
    instance._hub = hub
    # 素材库上传要先知道用哪个群的 client，所以得有个"说过话的群"
    instance.store.touch_group("qq_official:GroupMessage:G1", "G1")

    import asyncio

    report = asyncio.run(instance.web_card_probe())
    assert report["ok"] is True
    assert report["via"] == "素材库"
    assert report["checks"][0]["name"] == "素材库"
    # 只上传不发送，验证完当场回收，群里不会多出消息
    assert hub.cards == []
    assert hub.deleted == ["img1"]


def test_card_probe_falls_back_to_image_host(plugin):
    instance, _context = plugin
    instance._hub = FakeHub(material_ok=False, host_ok=True)
    instance.store.touch_group("qq_official:GroupMessage:G1", "G1")

    import asyncio

    report = asyncio.run(instance.web_card_probe())
    assert report["ok"] is True
    assert report["via"] == "图床"
    assert [check["name"] for check in report["checks"]] == ["素材库", "图床"]


def test_card_probe_explains_failure(plugin):
    instance, _context = plugin
    instance._hub = FakeHub(material_ok=False, host_ok=False)
    instance.store.touch_group("qq_official:GroupMessage:G1", "G1")

    import asyncio

    report = asyncio.run(instance.web_card_probe())
    assert report["ok"] is False
    assert "降级" in report["message"]


def test_card_probe_without_hub(plugin):
    instance, _context = plugin
    instance._hub = None
    instance._get_hub = lambda quiet=False: None

    import asyncio

    report = asyncio.run(instance.web_card_probe())
    assert report["ok"] is False and "Hub" in report["message"]
