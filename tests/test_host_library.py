"""把梗图库搬到自建图床。

为什么要有这条路：QQ 素材库每机器人只有 **100 张**、90 天过期、还要靠平台
去抓 URL 再转存。塞满之后所有带图卡片一起发不出去，报错只有一句
``markdown image quota exceeded``。自建图床是我们自己的磁盘 + 自己的域名，
**没有张数上限**，搬过去之后发卡片就直接用那个公网 URL。
"""
from __future__ import annotations

import asyncio
import io
import json
import os

import pytest

pytest.importorskip("quart")
Image = pytest.importorskip("PIL.Image")


def jpeg(color=(120, 80, 200), size=(240, 240)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture()
def env(tmp_path, plugin_module):
    from quart import Quart

    from meme.chinese import MODE_APPEND
    from meme.pipeline import MemePipeline
    from meme.review import ReviewPolicy
    from meme_plugin_under_test.web import MemeWebUI

    class Ctx:
        def __init__(self):
            self.route_table = []
            self.global_config = {}

        def get_provider_by_id(self, _p):
            return None

        def get_registered_star(self, _n):
            return None

        def get_config(self):
            return self.global_config

        def register_web_api(self, route, handler, methods, desc):
            self.route_table.append((route, handler, methods, desc))

    context = Ctx()
    plugin = plugin_module.MemePlugin(
        context, {"custom_data_dir": str(tmp_path), "cooldown_seconds": 0,
                  "caption_zh_mode": MODE_APPEND}
    )
    plugin.store.init()
    plugin.pipeline = MemePipeline(plugin.store, ReviewPolicy(), moderator=None)

    web = MemeWebUI(context, plugin)
    web.register()
    app = Quart(__name__)
    for route, handler, methods, _d in context.route_table:
        app.add_url_rule(route, endpoint=route, view_func=handler, methods=methods)
    return {"plugin": plugin, "app": app, "web": web}


class FakeHost:
    """模拟 Hub 图床：按内容去重，记住上传过什么。"""

    def __init__(self, reachable=True):
        self.uploads: list[bytes] = []
        self.reachable = reachable

    def publish_if_absent(self, data, slot=""):
        if data in self.uploads:
            return f"https://img.example/i/existing-{len(self.uploads)}.jpg", False
        self.uploads.append(data)
        return f"https://img.example/i/new-{len(self.uploads)}.jpg", True

    def publish(self, data, slot=""):
        self.uploads.append(data)
        return f"https://img.example/i/legacy-{len(self.uploads)}.jpg"


class FakeHub:
    def __init__(self, host=None, reachable=True):
        self.image_host = host or FakeHost()
        self._reachable = reachable

    async def image_host_reachable(self):
        return self._reachable

    async def image_host_problem(self):
        return "" if self._reachable else "图床未开启：配置里打开 image_host_enabled"

    def image_host_ready(self):
        return self._reachable


def call(env, method, path, *, body=None):
    kwargs = {}
    if body is not None:
        kwargs["data"] = json.dumps(body)
        kwargs["headers"] = {"Content-Type": "application/json"}

    async def run():
        client = env["app"].test_client()
        resp = await getattr(client, method)(path, **kwargs)
        return await resp.get_json()

    return asyncio.run(run())


def ok_data(payload):
    assert payload.get("status") == "ok", payload
    return payload["data"]


def seed(env, n=3):
    """往库里塞 n 张互不相同的图。

    sha256 必须用**真实内容哈希**：发送那一侧会拿图上字节现算一遍去查图床
    地址，用假值的话两边对不上，"已经搬过的图优先走图床"就永远命中不了。
    """
    from meme.media import sha256_of

    ids = []
    for i in range(n):
        data = jpeg((i * 40 % 256, 90, 160))
        sha = sha256_of(data)
        path = env["plugin"].store.save_media(sha, data, ".jpg")
        meme_id, _ = env["plugin"].store.add(
            sha256=sha, file_path=path, origin="submitted"
        )
        ids.append(meme_id)
    return ids


# --- 存储层 ----------------------------------------------------------------

def test_hosted_url_roundtrip(env):
    store = env["plugin"].store
    assert store.hosted_url("a" * 64) == ""
    store.set_hosted_url("a" * 64, "https://img.example/i/x.jpg")
    assert store.hosted_url("a" * 64) == "https://img.example/i/x.jpg"
    # 覆盖更新
    store.set_hosted_url("a" * 64, "https://img.example/i/y.jpg")
    assert store.hosted_url("a" * 64) == "https://img.example/i/y.jpg"


def test_hosted_stats_counts_coverage(env):
    ids = seed(env, 4)
    store = env["plugin"].store
    for i in ids[:2]:
        item = store.get(i)
        store.set_hosted_url(item.sha256, f"https://img.example/i/{i}.jpg")
    stats = store.hosted_stats()
    assert stats["active"] == 4
    assert stats["covered"] == 2
    assert stats["hosted"] == 2


# --- 搬迁接口 --------------------------------------------------------------

def test_host_library_uploads_everything(env):
    seed(env, 5)
    env["plugin"]._hub = FakeHub()

    data = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={}))
    assert data["ok"] is True
    assert data["moved"] == 5
    assert data["skipped"] == 0
    assert data["covered"] == 5
    assert data["remaining"] == 0
    assert len(env["plugin"]._hub.image_host.uploads) == 5


def test_host_library_is_idempotent(env):
    """第二次点「搬图」不该重复上传。"""
    seed(env, 3)
    env["plugin"]._hub = FakeHub()
    ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={}))

    again = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={}))
    assert again["moved"] == 0
    assert again["remaining"] == 0
    assert len(env["plugin"]._hub.image_host.uploads) == 3


def test_host_library_respects_limit_for_batches(env):
    """可以分批搬，避免一次请求跑太久。"""
    seed(env, 6)
    env["plugin"]._hub = FakeHub()
    first = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library",
                         body={"limit": 2}))
    assert first["moved"] == 2
    assert first["remaining"] == 4
    second = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library",
                          body={"limit": 2}))
    assert second["moved"] == 2
    assert second["covered"] == 4


def test_host_library_reports_unreachable_host(env):
    """图床不通时要说清原因，不能装作搬成功了。"""
    seed(env, 2)
    env["plugin"]._hub = FakeHub(reachable=False)
    payload = call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={})
    assert payload.get("status") == "error"
    assert "图床" in payload.get("message", "")
    assert env["plugin"].store.hosted_stats()["covered"] == 0


def test_host_library_without_hub_host(env):
    seed(env, 1)

    class NoHost:
        image_host = None

    env["plugin"]._hub = NoHost()
    payload = call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={})
    assert payload.get("status") == "error"


def test_host_library_skips_missing_files(env):
    """文件没了的图不能卡住整批，记进 failed 继续往下搬。"""
    ids = seed(env, 3)
    missing = env["plugin"].store.get(ids[0])
    os.remove(missing.file_path)
    env["plugin"]._hub = FakeHub()

    data = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={}))
    assert data["moved"] == 2
    assert data["covered"] == 2
    assert data["failed"], "读不到的文件应该报出来"


def test_host_stats_endpoint(env):
    seed(env, 3)
    env["plugin"]._hub = FakeHub()
    ok_data(call(env, "post", "/astrbot_plugin_meme/ui/host_library", body={}))
    data = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/host_stats"))
    assert data["covered"] == 3
    assert data["active"] == 3


# --- 发送时优先用图床 ------------------------------------------------------

def _card_sender(env, item, *, material_url="qqbot-media://image/v1/xyz"):
    """造一个记录「卡片里写了什么图」的假 Hub。"""
    sent: list[str] = []
    uploads: list[int] = []

    class Hub(FakeHub):
        async def send_ephemeral_card(self, origin, card, client=None,
                                      session_id="", event_id=None, msg_id=None,
                                      initiator_openid="", clicker_header="",
                                      verify_image=False):
            sent.append(card["markdown"])
            return True

        async def publish_image_material(self, *a, **k):
            uploads.append(1)
            return {"image_id": "mid-1", "url": material_url, "ttl": 0}

        async def delete_image_material(self, image_id, client=None, origin=""):
            return True

    env["plugin"]._hub = Hub()
    return sent, uploads


def test_send_uses_the_material_store_by_default(env):
    """默认走素材库，即使这张图早就搬上图床了。

    这是被实机推翻过一次的优先级，别随手改回去：

      a) 平台发 markdown 时会**强制转存**图片，用公网 URL 一样占素材库，
         而且占的是我们没有 image_id、事后也删不掉的格子（实机 100 张里
         有 50 张就是这么来的孤儿）；
      b) Cloudflare 的 Browser Integrity Check（error 1010）会拦腾讯的抓图器，
         转存直接失败 —— 用户看到的就是「图片转存失败，请重试」。

    素材库这条路我们能拿到 image_id，能记账、能按 card_material_keep 回收。
    """
    ids = seed(env, 1)
    store = env["plugin"].store
    item = store.get(ids[0])
    # 这张图「已上图床」的同时，也必须默认走素材库
    store.set_hosted_url(item.sha256, "https://img.8700k.top/i/abc.jpg")
    sent, uploads = _card_sender(env, item)

    data = open(item.file_path, "rb").read()
    ok = asyncio.run(env["plugin"]._send_card_item("umo:G", item, data))

    assert ok is True
    assert uploads, "默认应该传素材库"
    assert "qqbot-media://" in sent[0]
    assert "img.8700k.top" not in sent[0]


def test_send_can_be_told_to_prefer_the_hosted_url(env):
    """显式打开 prefer_image_host 时，搬过的图直接用图床 URL。"""
    env["plugin"].cfg["prefer_image_host"] = True
    ids = seed(env, 1)
    store = env["plugin"].store
    item = store.get(ids[0])
    store.set_hosted_url(item.sha256, "https://img.8700k.top/i/abc.jpg")
    sent, uploads = _card_sender(env, item)

    data = open(item.file_path, "rb").read()
    ok = asyncio.run(env["plugin"]._send_card_item("umo:G", item, data))

    assert ok is True
    assert not uploads, "开了开关就不该再占素材库"
    assert "https://img.8700k.top/i/abc.jpg" in sent[0]
