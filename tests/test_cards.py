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
from meme.models import MemeItem

Image = pytest.importorskip("PIL.Image")


def item(**kwargs) -> MemeItem:
    base = dict(
        id=12,
        sha256="a" * 64,
        origin="submitted",
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
    assert "引用" in card["markdown"] and "所有群共用同一份" in card["markdown"]
    assert [b["action_id"] for b in rows_of(card)] == [ACTION_AGAIN]


# --- 素材额度 ---------------------------------------------------------------

def jpeg_bytes(color=(30, 90, 200), size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
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


def test_many_different_images_get_trimmed(plugin):
    """8 张不同的图 → 8 个素材格，只保留最近 5 个，最旧的 3 个被回收。"""
    instance, _context = plugin
    hub = FakeHub()
    instance._hub = hub
    instance.config["card_material_keep"] = 5

    import asyncio

    for index in range(8):
        data = jpeg_bytes(color=(index * 20, 90, 200))
        assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), data)) is True

    assert len(hub.cards) == 8
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


# --- 素材复用：一张图只占一格额度 ---------------------------------------

def test_same_image_reuses_its_material(plugin):
    """同一张梗图再发一次，不该再上传一张（否则 100 格额度按「发送次数」被吃掉）。

    QQ 素材库是「一个引用用很多次」，不是「一条消息一格」。
    """
    instance, _context = plugin
    hub = FakeHub()
    instance._hub = hub
    data = jpeg_bytes()

    import asyncio

    for _ in range(5):
        assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), data)) is True

    assert hub.materials == ["img1"], "第二次以后应当复用引用，不该再上传"
    assert len(hub.cards) == 5
    assert all("qqbot-media://image/v1/img1" in card["markdown"] for card in hub.cards)


def test_two_different_images_take_two_slots(plugin):
    instance, _context = plugin
    hub = FakeHub()
    instance._hub = hub

    import asyncio

    asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), jpeg_bytes(color=(5, 120, 30))))
    asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), jpeg_bytes(color=(200, 9, 9))))
    assert hub.materials == ["img1", "img2"]


def test_expired_reference_is_reuploaded(plugin):
    """平台 90 天后会把没人用的引用回收（40035009）：那张卡发失败时要自动重传。

    这条是"不静默"的关键 —— 不然一个失效引用会被反复用下去，每次都是裂图。
    """
    import asyncio

    instance, _context = plugin
    data = jpeg_bytes()

    first = FakeHub()
    instance._hub = first
    assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), data)) is True
    assert first.materials == ["img1"]

    class ExpiringHub(FakeHub):
        """第一次发送时抛 40035009（模拟平台把这张引用回收了），之后正常。

        只针对「复用旧引用」的那一次：重传之后（哪怕 id 又排到 img1）必须放行，
        否则测的就不是"重传"，而是"这频道永远发不出去"。
        """

        expired_once = False

        async def send_ephemeral_card(self, origin, card, **kwargs):
            if not self.expired_once:
                self.expired_once = True
                raise RuntimeError("40035009 图片已过期")
            self.cards.append(card)
            return "s"

    second = ExpiringHub()
    instance._hub = second
    assert asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), data)) is True
    assert len(second.materials) == 1, "旧引用失效后应当重新上传，而不是放弃这张卡"
    assert second.cards, "重传之后这张卡必须发出去"


def test_stale_cache_is_ignored_without_asking_qq(plugin):
    """记账里超过复用天数（默认 80 天，留 10 天余量）的引用直接不用。"""
    instance, _context = plugin
    hub = FakeHub()
    instance._hub = hub
    data = jpeg_bytes()

    import asyncio

    asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), data))
    from meme.media import sha256_of

    sha = sha256_of(data)
    entry = instance.store.material_for_sha(sha)
    old = entry["last_used"] - 90 * 86400
    instance.store._write("UPDATE card_materials SET last_used = ? WHERE image_id = ?",
                          (old, entry["image_id"]))
    hub.materials.clear()
    hub.materials.append("img2")
    asyncio.run(instance._send_card_item("qq:GroupMessage:G1", item(), data))
    assert instance.store.material_for_sha(sha)
    assert "img2" in hub.cards[-1]["markdown"]

def test_old_db_without_sha_column_still_loads(tmp_path):
    """老库的 card_materials 表没有 sha256 列时，init() 必须先补列再建索引。

    回归用：把索引写在建表脚本里、而补列在脚本之后，会报 no such column: sha256，
    直接导致整个插件加载失败。
    """
    import sqlite3

    from meme.store import MemeStore

    db = tmp_path / "meme.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE card_materials (
            image_id   TEXT PRIMARY KEY,
            meme_id    INTEGER NOT NULL DEFAULT 0,
            origin     TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO card_materials(image_id, created_at) VALUES ('old1', 1.0);
        """
    )
    conn.commit()
    conn.close()

    store = MemeStore(str(db))
    store.init()
    cols = {row["name"] for row in store._rows("PRAGMA table_info(card_materials)")}
    assert {"sha256", "url", "last_used"} <= cols
    assert store.card_material_ids() == ["old1"]


def test_every_card_stays_clickable_for_a_day():
    """Hub 默认只认 1 小时，聊天记录里的旧卡点上去只会说「卡片已过期」，
    群友会以为按钮坏了（线上真的发生过）。卡片自己声明 24 小时。"""
    from meme_plugin_under_test.meme.cards import (
        ACTION_AUTO_OFF,
        CARD_TTL_SECONDS,
        build_auto_done_card,
        build_auto_pick_card,
        build_howto_card,
        build_meme_card,
    )

    assert CARD_TTL_SECONDS == 86400
    item = MemeItem(id=1, file_path="/tmp/a.jpg", mime="image/jpeg")
    for card in (
        build_meme_card(item),
        build_howto_card(),
        build_auto_pick_card({"low": 10, "mid": 25, "high": 50}),
        build_auto_done_card("high", 50),
    ):
        assert card["ttl_seconds"] == CARD_TTL_SECONDS
    card = build_auto_done_card("high", 50)
    assert [b["action_id"] for b in card["rows"][0]][-1] == ACTION_AUTO_OFF


def test_auto_push_card_is_image_and_buttons_only():
    """用户点名要求：自动推送的卡片不要正文、不要编号，只要图和按钮，
    看起来跟群友自己发的一张图一样。"""
    from meme_plugin_under_test.meme.cards import build_meme_card

    item = MemeItem(id=41, file_path="/tmp/a.jpg", mime="image/jpeg",
                    caption="免疫系统", used_count=3)
    card = build_meme_card(
        item,
        "qqbot-media://image/v1/abc",
        auto_note="当前为 每日自动推送中...",
        bare=True,
    )
    text = card["markdown"]
    assert "![梗图" in text                      # 图还在
    assert "#41" not in text                     # 编号不再露出来
    assert "免疫系统" not in text                # 图注也不带
    assert "发过 3 次" not in text
    assert "每日自动推送中" not in text          # 那行灰字一起去掉
    assert [b["label"] for b in card["rows"][0]][:2] == ["再来一张", "我要投稿"]


def test_manual_card_still_shows_the_id():
    """手动 /meme 那种卡片照旧带编号和图注（群里报编号删图要用）。"""
    from meme_plugin_under_test.meme.cards import build_meme_card

    item = MemeItem(id=41, file_path="/tmp/a.jpg", mime="image/jpeg", caption="免疫系统")
    text = build_meme_card(item, "qqbot-media://image/v1/abc")["markdown"]
    assert "#41" in text and "免疫系统" in text


def test_bare_card_without_image_is_never_an_empty_markdown():
    """无图兜底时正文不能是空字符串（QQ 会当成空卡片拒发）。"""
    from meme_plugin_under_test.meme.cards import build_meme_card

    item = MemeItem(id=7, file_path="/tmp/a.jpg", mime="image/jpeg")
    text = build_meme_card(item, "", bare=True)["markdown"]
    assert text.strip() != ""
