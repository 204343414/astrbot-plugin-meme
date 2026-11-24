"""后台「拒收」页面：列出来、看图、人工接收、丢弃。

这一组钉的是用户提的那条需求：被审核模型判死的图，要在后台能看见、能人工
捞回来。原来 `rejects` 表只有 sha256 和原因，**图片字节在判死的瞬间就丢了**，
所以那个页面根本无从看图。
"""
from __future__ import annotations

import asyncio
import io
import json
import os

import pytest

pytest.importorskip("quart")
pytest.importorskip("PIL.Image")

Image = pytest.importorskip("PIL.Image")


def jpeg_bytes(color=(200, 90, 90), size=(240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


BAD_REVIEW = {
    "is_meme": False,
    "nsfw_level": 0,
    "flags": [],
    "caption_text": "",
    "language": "zh",
    "reason": "看着不像梗图（像是截图/照片/公告）",
}


@pytest.fixture()
def env(tmp_path, plugin_module):
    """一个搭好 web 路由的环境；审核模型恒定判死，方便造拒收数据。"""
    from quart import Quart

    from meme.chinese import MODE_APPEND
    from meme.pipeline import MemePipeline
    from meme.review import ReviewPolicy
    from meme_plugin_under_test.web import MemeWebUI

    class Ctx:
        def __init__(self):
            self.route_table = []
            self.global_config = {}

        def get_provider_by_id(self, _pid):
            return None

        def get_registered_star(self, _name):
            return None

        def get_config(self):
            return self.global_config

        def register_web_api(self, route, handler, methods, desc):
            self.route_table.append((route, handler, methods, desc))

    class BadModerator:
        def __init__(self):
            self.calls = 0

        async def __call__(self, _data_url):
            self.calls += 1
            return json.dumps(BAD_REVIEW, ensure_ascii=False)

    context = Ctx()
    plugin = plugin_module.MemePlugin(
        context,
        {"custom_data_dir": str(tmp_path), "cooldown_seconds": 0, "caption_zh_mode": MODE_APPEND},
    )
    plugin.store.init()
    moderator = BadModerator()
    plugin.pipeline = MemePipeline(plugin.store, ReviewPolicy(), moderator=moderator)

    web = MemeWebUI(context, plugin)
    web.register()
    app = Quart(__name__)
    for route, handler, methods, _desc in context.route_table:
        app.add_url_rule(route, endpoint=route, view_func=handler, methods=methods)
    return {"plugin": plugin, "app": app, "web": web, "moderator": moderator}


def call(env, method, path, *, json_body=None):
    kwargs = {}
    if json_body is not None:
        kwargs["data"] = json.dumps(json_body)
        kwargs["headers"] = {"Content-Type": "application/json"}

    async def run():
        client = env["app"].test_client()
        response = await getattr(client, method)(path, **kwargs)
        return response.status_code, await response.get_json()

    return asyncio.run(run())


def ok_data(result):
    _status, payload = result
    assert payload.get("status") == "ok", payload
    return payload["data"]


def err_message(result):
    _status, payload = result
    assert payload.get("status") == "error", payload
    return payload.get("message", "")


def make_reject(env, data=None, reason="看着不像梗图", contributor="U1", **extra):
    """造一条真实的拒收：走管线，让 pipeline 自己落盘 + 记账。"""
    from meme.media import sha256_of

    data = data if data is not None else jpeg_bytes()
    sha = sha256_of(data)
    env["plugin"].store.save_rejected(sha, data, ".jpg")
    env["plugin"].store.reject(sha, reason, contributor, mime="image/jpeg", size=len(data), **extra)
    return sha, data


# --- 拒收时必须要存图（这次改动的核心）--------------------------------------

def test_rejected_image_is_saved_to_disk(env, tmp_path):
    """审核判死之后，图必须留在盘上，否则后台页面根本没法看。"""
    from meme.media import sha256_of

    data = jpeg_bytes()
    sha = sha256_of(data)

    async def run():
        return await env["plugin"].pipeline.ingest(
            data, origin="submitted", contributor="U1"
        )

    result = asyncio.run(run())
    assert result.ok is False
    assert "不像梗图" in result.reason

    # 拒收记录在
    assert env["plugin"].store.rejection_reason(sha)
    # 图也在盘上，而且在 rejected/ 子目录（不跟正式库混）
    path = env["plugin"].store.rejected_media_path(sha, ".jpg")
    assert os.path.isfile(path), f"拒收的图没落盘：{path}"
    assert f"{os.sep}rejected{os.sep}" in path
    with open(path, "rb") as fh:
        assert fh.read() == data


def test_rejected_record_keeps_metadata(env):
    from meme.media import sha256_of

    data = jpeg_bytes(color=(10, 20, 30))
    sha = sha256_of(data)

    async def run():
        return await env["plugin"].pipeline.ingest(
            data, origin="submitted", contributor="U9", source_url="https://x/y.jpg"
        )

    asyncio.run(run())
    rows = env["plugin"].store.rejected_page(10, 0)
    assert len(rows) == 1
    row = rows[0]
    assert row["sha256"] == sha
    assert row["contributor"] == "U9"
    assert row["mime"] == "image/jpeg"
    assert row["size"] == len(data)
    assert row["has_file"] is True


# --- 列表接口 ---------------------------------------------------------------

def test_rejected_list_is_newest_first(env):
    env["plugin"].store._now = lambda: 1000.0
    first, _ = make_reject(env, jpeg_bytes((1, 2, 3)), reason="第一条")
    env["plugin"].store._now = lambda: 2000.0
    second, _ = make_reject(env, jpeg_bytes((4, 5, 6)), reason="第二条")

    payload = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/rejected"))
    assert payload["total"] == 2
    assert [c["sha256"] for c in payload["cards"]] == [second, first]
    assert payload["cards"][0]["reason"] == "第二条"
    assert payload["cards"][0]["hasFile"] is True
    assert payload["cards"][0]["thumb"].startswith("data:")


def test_rejected_list_flags_rows_without_file(env):
    """0.6.4 之前留下的旧记录：没有图，列表要明确标出来。"""
    env["plugin"].store.reject("a" * 64, "老记录", "U1")  # 只写表，不存图
    payload = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/rejected"))
    assert payload["total"] == 1
    card = payload["cards"][0]
    assert card["hasFile"] is False
    assert card["thumb"] == ""
    assert payload["noFileCount"] == 1


def test_rejected_list_paginates(env):
    for i in range(5):
        env["plugin"].store._now = (lambda t: (lambda: t))(1000.0 + i)
        make_reject(env, jpeg_bytes((i * 20, 40, 60)), reason=f"第{i}条")

    page1 = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/rejected?limit=2&offset=0"))
    assert len(page1["cards"]) == 2
    assert page1["hasMore"] is True
    page3 = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/rejected?limit=2&offset=4"))
    assert len(page3["cards"]) == 1
    assert page3["hasMore"] is False


def test_rejected_list_caps_page_size(env):
    """一次要 500 张也不给 —— 一页塞几十张大图 data URL 会把 iframe 卡死。"""
    payload = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/rejected?limit=500"))
    assert payload["limit"] <= 60


# --- 大图接口 ---------------------------------------------------------------

def test_rejected_detail_returns_full_image(env):
    sha, data = make_reject(env, reason="广告")
    payload = ok_data(call(env, "get", f"/astrbot_plugin_meme/ui/rejected_detail?sha={sha}"))
    assert payload["reason"] == "广告"
    assert payload["hasFile"] is True
    assert payload["image"].startswith("data:image/jpeg;base64,")
    assert payload["alreadyInLibrary"] is False


def test_rejected_detail_unknown_sha(env):
    message = err_message(call(env, "get", "/astrbot_plugin_meme/ui/rejected_detail?sha=" + "0" * 64))
    assert "没有" in message


# --- 人工接收（不再走 AI 审核）----------------------------------------------

def test_accept_puts_it_in_library_without_reviewing(env):
    """点「接收」= 人肉审核通过 → 直接入库，**不再花一次模型调用**。"""
    sha, data = make_reject(env, reason="误判")

    payload = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": sha}))
    assert payload["ok"] is True
    assert payload["id"] > 0
    assert env["moderator"].calls == 0, "人工接收不该再调审核模型"

    # 进了正式库，字节原样
    item = env["plugin"].store.by_sha(sha)
    assert item is not None
    assert item.status == "active"
    with open(item.file_path, "rb") as fh:
        assert fh.read() == data
    # 拒收记录清掉了（不清的话同一张再投稿会被直接拒）
    assert env["plugin"].store.rejection_reason(sha) is None
    # 盘上那份「拒收副本」也清掉，不留双份
    assert not os.path.isfile(env["plugin"].store.rejected_media_path(sha, ".jpg"))


def test_accept_then_the_meme_can_be_picked(env):
    """接收之后这张图要能真的被挑选发出去（不能只是躺在库里）。"""
    from meme.select import pick_meme

    sha, _ = make_reject(env)
    ok_data(call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": sha}))
    item = pick_meme(env["plugin"].store.random_pool(), {"submitted": 3.0})
    assert item is not None
    assert item.sha256 == sha


def test_accept_refuses_when_no_image_was_kept(env):
    """旧记录没图 —— 不能假装成功，要说清为什么。"""
    env["plugin"].store.reject("b" * 64, "老记录", "U1")
    message = err_message(
        call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": "b" * 64})
    )
    assert "没存下来" in message
    # 记录本身不该被误删：用户还没决定怎么处理它
    assert env["plugin"].store.rejection_reason("b" * 64)


def test_accept_is_idempotent_when_already_in_library(env):
    """同一张已经在库里了：只说一声并清记录，不能重复入库。"""
    sha, _ = make_reject(env)
    first = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": sha}))
    # 重新造一条同 sha 的拒收记录，再点一次
    env["plugin"].store.reject(sha, "又拒了一次", "U1")
    second = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": sha}))
    assert second["id"] == first["id"]
    assert second["alreadyThere"] is True
    assert env["plugin"].store.rejection_reason(sha) is None


def test_accept_rejects_tampered_file(env):
    """盘上的文件跟 sha256 对不上就不入库 —— 否则以后去重全乱。"""
    sha, data = make_reject(env)
    path = env["plugin"].store.rejected_media_path(sha, ".jpg")
    with open(path, "wb") as fh:
        fh.write(jpeg_bytes((9, 9, 9)))  # 换成另一张图
    message = err_message(
        call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": sha})
    )
    assert "指纹" in message or "对不上" in message
    assert env["plugin"].store.by_sha(sha) is None


def test_accept_unknown_sha(env):
    message = err_message(
        call(env, "post", "/astrbot_plugin_meme/ui/rejected_accept", json_body={"sha": "c" * 64})
    )
    assert "没有" in message


# --- 丢弃 -------------------------------------------------------------------

def test_drop_removes_record_and_file(env):
    sha, _ = make_reject(env)
    path = env["plugin"].store.rejected_media_path(sha, ".jpg")
    assert os.path.isfile(path)

    payload = ok_data(call(env, "post", "/astrbot_plugin_meme/ui/rejected_drop", json_body={"sha": sha}))
    assert payload["ok"] is True
    assert env["plugin"].store.rejection_reason(sha) is None
    assert not os.path.isfile(path)
    # 丢弃 = 「这张不该收」，但以后可以重新投稿（记录没了就没人拦它）
    assert env["plugin"].store.rejected_count() == 0


def test_dropped_meme_can_be_submitted_again(env):
    """丢弃之后同一张再投稿要能重新进管线（不能被旧记录挡住）。"""
    from meme.media import sha256_of

    data = jpeg_bytes((33, 44, 55))
    sha = sha256_of(data)
    make_reject(env, data)
    ok_data(call(env, "post", "/astrbot_plugin_meme/ui/rejected_drop", json_body={"sha": sha}))
    assert env["plugin"].store.rejection_reason(sha) is None


# --- 统计数字 ---------------------------------------------------------------

def test_state_reports_rejected_count(env):
    make_reject(env)
    make_reject(env, jpeg_bytes((77, 88, 99)))
    payload = ok_data(call(env, "get", "/astrbot_plugin_meme/ui/state"))
    assert payload["rejected"] == 2


def test_old_database_gets_new_columns(tmp_path):
    """老库（没有 origin/size 列的 rejects 表）升级后要能正常读写。"""
    import sqlite3

    from meme.store import MemeStore

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE rejects (
               sha256 TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',
               contributor TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL DEFAULT 0)"""
    )
    conn.execute("INSERT INTO rejects VALUES (?,?,?,?)", ("d" * 64, "旧原因", "U1", 1000.0))
    conn.commit()
    conn.close()

    store = MemeStore(path, str(tmp_path / "media"))
    store.init()  # 补列

    rows = store.rejected_page(10, 0)
    assert len(rows) == 1
    assert rows[0]["reason"] == "旧原因"
    assert rows[0]["has_file"] is False
    assert rows[0]["origin"] == ""

    # 新写入也要能带上新列
    store.save_rejected("e" * 64, jpeg_bytes(), ".jpg")
    store.reject("e" * 64, "新原因", "U2", origin="submitted", mime="image/jpeg", size=123)
    rows = store.rejected_page(10, 0)
    assert rows[0]["sha256"] == "e" * 64
    assert rows[0]["size"] == 123
