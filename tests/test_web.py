"""页面接口跑在真的 quart 上：注册、读列表、拖图上传、调参、删除。

不用 mock 请求对象 —— 桥接层最后发出来的就是普通 HTTP 请求，这里用 quart 的
测试客户端真发一遍，才能发现「multipart 字段名不对」「返回体没被 jsonify」
这类只在真实请求里才暴露的问题。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

pytest.importorskip("quart")
from quart import Quart  # noqa: E402

from meme.chinese import MODE_APPEND  # noqa: E402
from meme.pipeline import MemePipeline  # noqa: E402
from meme.review import ReviewPolicy  # noqa: E402
from meme.thumbs import GALLERY_THUMB  # noqa: E402

Image = pytest.importorskip("PIL.Image")

GOOD_REVIEW = {
    "is_meme": True,
    "nsfw_level": 0,
    "flags": [],
    "caption_text": "when the code works",
    "language": "en",
    "reason": "ok",
}


def jpeg_bytes(color=(120, 200, 90), size=(600, 600)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


class FakeModerator:
    def __init__(self, payload=None):
        self.payload = json.dumps(payload or GOOD_REVIEW, ensure_ascii=False)
        self.calls = 0

    async def __call__(self, data_url):
        self.calls += 1
        return self.payload


class FakeTranslator:
    def __init__(self):
        self.calls = 0

    async def __call__(self, prompt):
        self.calls += 1
        return "当代码跑通的时候"


class FakeContext:
    """只实现插件和 web 层真正用到的那几个方法。"""

    def __init__(self):
        self.providers = {}
        self.global_config = {}
        self.route_table = []

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    def get_registered_star(self, name):
        return None

    def get_config(self):
        return self.global_config

    def register_web_api(self, route, handler, methods, desc):
        self.route_table.append((route, handler, methods, desc))

    async def send_message(self, umo, chain):
        return None


@pytest.fixture()
def env(plugin_module, tmp_path):
    context = FakeContext()
    plugin = plugin_module.MemePlugin(
        context,
        {
            "custom_data_dir": str(tmp_path),
            "cooldown_seconds": 0,
            "send_cooldown_seconds": 0,
            "caption_zh_mode": MODE_APPEND,
        },
    )
    plugin.store.init()
    moderator = FakeModerator()
    plugin.pipeline = MemePipeline(
        plugin.store,
        ReviewPolicy(),
        moderator=moderator,
        translator=FakeTranslator(),
        caption_mode=MODE_APPEND,
    )

    from meme_plugin_under_test.web import MemeWebUI

    web = MemeWebUI(context, plugin)
    web.register()

    app = Quart(__name__)
    for route, handler, methods, _desc in context.route_table:
        app.add_url_rule(route, endpoint=route, view_func=handler, methods=methods)
    return {"plugin": plugin, "app": app, "web": web, "context": context, "moderator": moderator}


def request(env, method, path, *, json_body=None, files_data=None):
    """真发一次请求，返回 ``(状态码, JSON)``。

    multipart 必须给 werkzeug 的 FileStorage（新版 quart 不认元组）；
    JSON 统一手动 dumps，免得不同版本对 ``json=`` 的处理不一致。
    """
    from werkzeug.datastructures import FileStorage

    kwargs = {}
    if json_body is not None:
        kwargs["data"] = json.dumps(json_body)
        kwargs["headers"] = {"Content-Type": "application/json"}
    if files_data is not None:
        kwargs["files"] = {
            name: FileStorage(
                stream=io.BytesIO(content),
                filename=filename,
                content_type=content_type or "application/octet-stream",
            )
            for name, (content, filename, content_type) in files_data.items()
        }

    async def run():
        client = env["app"].test_client()
        response = await getattr(client, method)(path, **kwargs)
        return response.status_code, await response.get_json()

    return asyncio.run(run())


def payload_of(result):
    _status, payload = result
    assert "status" in payload, payload
    return payload


def data_of(result):
    payload = payload_of(result)
    assert payload["status"] == "ok", payload
    return payload["data"]


def upload(env, content, filename="cat.jpg", mime="image/jpeg"):
    return data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/upload",
                files_data={"file": (content, filename, mime)})
    )


def state(env, query=""):
    return data_of(request(env, "get", f"/astrbot_plugin_meme/ui/state{query}"))


# --- 注册 ---------------------------------------------------------------

def test_routes_registered(env):
    routes = [route for route, *_ in env["context"].route_table]
    for expected in (
        "/astrbot_plugin_meme/ui/state",
        "/astrbot_plugin_meme/ui/detail",
        "/astrbot_plugin_meme/ui/upload",
        "/astrbot_plugin_meme/ui/options",
        "/astrbot_plugin_meme/ui/config",
        "/astrbot_plugin_meme/ui/toggle",
        "/astrbot_plugin_meme/ui/recheck",
        "/astrbot_plugin_meme/ui/delete",
    ):
        assert expected in routes


def test_config_route_accepts_both_methods(env):
    route, _handler, methods, _desc = next(
        entry for entry in env["context"].route_table
        if entry[0].endswith("/ui/config")
    )
    assert set(methods) == {"GET", "POST"}


# --- 读列表 -------------------------------------------------------------

def test_state_is_empty_but_valid(env):
    data = state(env)
    assert data["cards"] == []
    assert data["total"] == 0
    assert data["running"]["captionMode"] == MODE_APPEND
    assert data["running"]["dataDir"]


def test_state_no_longer_lists_groups(env):
    """页面上的「按群筛选」跟着单群图库一起删了：图库只有一份，没有群视角。"""
    env["plugin"].store.touch_group("qq_official:GroupMessage:G1", "G1")
    assert "groups" not in state(env)


# --- 上传 ---------------------------------------------------------------

def test_upload_without_review_is_instant(env):
    data = upload(env, jpeg_bytes())
    assert data["ok"] is True
    assert data["skippedReview"] is True
    assert data["meme"]["id"] == 1
    assert env["moderator"].calls == 0  # 免审就是真的没调模型

    listed = state(env)
    assert listed["total"] == 1
    card = listed["cards"][0]
    assert card["thumb"].startswith("data:image/jpeg;base64,")
    assert card["hasFile"] is True


def test_upload_rejects_non_image(env):
    data = upload(env, b"hello", filename="note.txt", mime="text/plain")
    assert data["ok"] is False
    assert "不是图片" in data["message"]


def test_duplicate_upload_is_reported(env):
    image = jpeg_bytes()
    first = upload(env, image, filename="a.jpg")
    second = upload(env, image, filename="a-copy.jpg")
    assert second["duplicate"] is True
    assert second["meme"]["id"] == first["meme"]["id"]


def test_upload_options_switch_to_reviewed_upload(env):
    options = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/options",
                json_body={"skip_review": False})
    )
    assert options == {"ok": True, "skipReview": False}

    data = upload(env, jpeg_bytes((10, 10, 200)), filename="dog.jpg")
    assert data["ok"] is True
    assert data["skippedReview"] is False
    assert env["moderator"].calls == 1  # 这次真的送审了

    item = env["plugin"].store.get(data["meme"]["id"])
    assert item.caption_zh == "当代码跑通的时候"


def test_options_survive_between_requests(env):
    request(env, "post", "/astrbot_plugin_meme/ui/options",
            json_body={"skip_review": True})
    again = data_of(request(env, "get", "/astrbot_plugin_meme/ui/options"))
    assert again["skipReview"] is True
    assert "scope" not in again  # 「可见范围」跟着单群图库一起删了


def test_state_filters_by_origin_and_query(env):
    upload(env, jpeg_bytes((1, 1, 1)), filename="1.jpg")
    request(env, "post", "/astrbot_plugin_meme/ui/options",
            json_body={"skip_review": False})
    upload(env, jpeg_bytes((2, 2, 2)), filename="2.jpg")

    only_reviewed = state(env, "?origin=submitted&q=%E4%BB%A3%E7%A0%81")
    assert len(only_reviewed["cards"]) == 1
    assert only_reviewed["cards"][0]["captionZh"] == "当代码跑通的时候"

    assert state(env)["total"] == 2


# --- 单张操作 -----------------------------------------------------------

def test_toggle_and_delete(env):
    meme_id = upload(env, jpeg_bytes())["meme"]["id"]

    toggled = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/toggle",
                json_body={"id": meme_id, "active": False})
    )
    assert toggled["status"] == "disabled"
    assert state(env)["cards"][0]["active"] is False  # 停用的仍然看得见

    removed = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/delete", json_body={"id": meme_id})
    )
    assert removed["id"] == meme_id
    assert state(env)["cards"] == []

    again = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/delete", json_body={"id": meme_id})
    )
    assert again["status"] == "error"


def test_recheck_reports_verdict(env):
    meme_id = upload(env, jpeg_bytes())["meme"]["id"]
    data = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/recheck", json_body={"id": meme_id})
    )
    assert data["accepted"] is True
    assert data["language"] == "en"
    assert data["caption"] == "when the code works"


def test_recheck_disables_a_bad_image(env):
    meme_id = upload(env, jpeg_bytes())["meme"]["id"]
    env["plugin"].pipeline = MemePipeline(
        env["plugin"].store,
        ReviewPolicy(),
        moderator=FakeModerator(dict(GOOD_REVIEW, nsfw_level=4)),
        caption_mode=MODE_APPEND,
    )
    data = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/recheck", json_body={"id": meme_id})
    )
    assert data["accepted"] is False
    assert env["plugin"].store.get(meme_id).status == "disabled"


def test_detail_returns_preview_bigger_than_thumbnail(env):
    meme_id = upload(env, jpeg_bytes())["meme"]["id"]
    data = data_of(request(env, "get", f"/astrbot_plugin_meme/ui/detail?id={meme_id}"))
    assert data["image"].startswith("data:image/jpeg;base64,")
    assert data["id"] == meme_id
    raw = base64.b64decode(data["image"].split(",", 1)[-1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) >= GALLERY_THUMB


def test_detail_missing_id_is_error(env):
    assert payload_of(request(env, "get", "/astrbot_plugin_meme/ui/detail?id=999"))["status"] == "error"


# --- 调参 ---------------------------------------------------------------

def test_config_get_lists_fields_and_readonly_values(env):
    config = data_of(request(env, "get", "/astrbot_plugin_meme/ui/config"))
    keys = {field["key"] for field in config["fields"]}
    assert "nsfw_max_level" in keys and "reject_flags" in keys
    assert "moderation_provider_id" in config["readonly"]


def test_config_save_applies_and_rebuilds_pipeline(env):
    data = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/config",
                json_body={"values": {"nsfw_max_level": 3, "caption_zh_mode": "redraw"}})
    )
    assert data["applied"]["nsfw_max_level"] == 3
    assert env["plugin"].config["nsfw_max_level"] == 3
    # 管线必须重建，否则新阈值要等重启才生效
    assert env["plugin"].pipeline.policy.nsfw_max_level == 3


def test_config_save_rejects_bad_values(env):
    payload = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/config",
                json_body={"values": {"nsfw_max_level": 99}})
    )
    assert payload["status"] == "error"
    assert "0" in payload["message"] and "4" in payload["message"]


def test_config_save_partial_success_is_ok(env):
    payload = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/config",
                json_body={"values": {"cooldown_seconds": 10, "nsfw_max_level": 99}})
    )
    assert payload["status"] == "ok"
    assert env["plugin"].config["cooldown_seconds"] == 10
    assert payload["data"]["errors"]


# --- 拖进来的是网址（服务端代下） ---------------------------------------

def _fake_fetch(monkeypatch, payload, calls=None):
    """把 adapters.fetch_bytes 换成假下载器，顺便记下真被调用过。"""
    import meme_plugin_under_test.meme.adapters as adapters

    async def fake(url, timeout=30.0, headers=None, proxy=""):
        if calls is not None:
            calls.append({"url": url, "proxy": proxy})
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(adapters, "fetch_bytes", fake)


def test_fetch_url_ingests_downloaded_bytes(env, monkeypatch):
    calls = []
    _fake_fetch(monkeypatch, jpeg_bytes(), calls)
    data = data_of(
        request(env, "post", "/astrbot_plugin_meme/ui/fetch_url",
                json_body={"url": "https://example.com/memes/quote.jpg"})
    )
    assert data["ok"] is True
    assert data["sourceUrl"].endswith("quote.jpg")
    assert "example.com" in data["message"]
    assert calls and calls[0]["url"] == "https://example.com/memes/quote.jpg"
    assert state(env)["libraryTotal"] == 1


def test_fetch_url_rejects_non_http(env, monkeypatch):
    _fake_fetch(monkeypatch, jpeg_bytes())
    payload = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/fetch_url",
                json_body={"url": "file:///etc/passwd"})
    )
    assert payload["status"] == "error"
    assert "http" in payload["message"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6185/api/v1/plugins",
        "http://localhost/x.png",
        "http://192.168.1.10/a.png",
        "http://[::1]/a.png",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_fetch_url_blocks_intranet(env, monkeypatch, url):
    calls = []
    _fake_fetch(monkeypatch, jpeg_bytes(), calls)
    payload = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/fetch_url", json_body={"url": url})
    )
    assert payload["status"] == "error"
    assert "内网" in payload["message"] or "本机" in payload["message"]
    assert not calls, "拦在本机/内网之前，不该真的发出请求"


def test_fetch_url_reports_download_failure(env, monkeypatch):
    import httpx

    _fake_fetch(monkeypatch, httpx.ConnectTimeout("连不上"))
    payload = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/fetch_url",
                json_body={"url": "https://example.com/a.jpg"})
    )
    assert payload["status"] == "error"
    assert "下载失败" in payload["message"]


def test_fetch_url_rejects_non_image(env, monkeypatch):
    _fake_fetch(monkeypatch, b"<html>not an image</html>")
    payload = payload_of(
        request(env, "post", "/astrbot_plugin_meme/ui/fetch_url",
                json_body={"url": "https://example.com/index.html"})
    )
    assert payload["status"] == "error"
    assert "不是图片" in payload["message"]


# --- 拉黑名单（页面） ---------------------------------------------------

def test_state_exposes_blacklist_and_card_info(env):
    env["plugin"].store.add_blacklist("U9", origin="g", reason="露骨色情")
    data = state(env)
    assert [entry["sender_id"] for entry in data["blacklist"]] == ["U9"]
    assert data["card"]["enabled"] is True
    assert data["card"]["materialKeep"] >= 5


def test_unban_route_restores_submission(env):
    env["plugin"].store.add_blacklist("U9", reason="露骨色情")
    data = data_of(request(env, "post", "/astrbot_plugin_meme/ui/unban", json_body={"id": "U9"}))
    assert data["ok"] is True
    assert env["plugin"].store.is_blacklisted("U9") is False


def test_unban_unknown_user_is_an_error(env):
    payload = payload_of(request(env, "post", "/astrbot_plugin_meme/ui/unban", json_body={"id": "nobody"}))
    assert payload["status"] == "error"
    assert "不在黑名单" in payload["message"]


def test_card_probe_route_registered(env):
    routes = [route for route, *_ in env["context"].route_table]
    assert "/astrbot_plugin_meme/ui/card_probe" in routes
    assert "/astrbot_plugin_meme/ui/fetch_url" in routes
