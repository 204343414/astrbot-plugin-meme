"""插件后台页面的 Web API。

这一层只做三件事：读请求、把参数交给插件、把结果说成人话。真正干活的是
``main.py`` 里那几个 ``web_*`` 方法 —— 页面和群命令走的是同一套入库/审核逻辑，
不会出现「网页能塞进去、群里抽出来是坏图」这种事。

请求/响应用的是 **`astrbot.api.web`**（AstrBot 4.29 起的官方接口，底下是
FastAPI）。老版本用的是 quart，这里留了一层兼容：拿不到新接口就退回 quart。
踩过的坑：在两套接口之间猜错的症状很安静 —— 页面能打开、列表是空的、
上传一直「没收到文件」，因为请求上下文根本没绑上。
"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_meme"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/ui"

# --- 请求/响应兼容层 ---------------------------------------------------------

_NEW_API = True
try:  # AstrBot >= 4.29
    from astrbot.api.web import error_response as _error_response  # type: ignore
    from astrbot.api.web import json_response as _json_response  # type: ignore
    from astrbot.api.web import request as _request  # type: ignore
except Exception:  # noqa: BLE001 - 老版本只有 quart
    _NEW_API = False
    try:
        from quart import jsonify as _quart_jsonify  # type: ignore
        from quart import request as _quart_request  # type: ignore
    except Exception:  # noqa: BLE001
        _quart_jsonify = None
        _quart_request = None
    # 兼容模式下请求对象换成 quart 的那个 —— 这两行写反过一次，
    # 症状是页面能开、列表空白、上传永远「没收到文件」。
    _request = _quart_request
    _json_response = None
    _error_response = None


async def _read_json() -> dict:
    if _NEW_API:
        payload = await _request.json(default={})
    elif _request is not None:
        payload = await _request.get_json(force=True, silent=True)
    else:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _read_query() -> dict:
    if _NEW_API:
        query = _request.query
        return {key: query.get(key) for key in getattr(query, "keys", lambda: [])()}
    if _request is not None:
        return dict(_request.args)
    return {}


async def _read_upload() -> tuple[bytes, str]:
    """拖进来的文件 → ``(字节, 文件名)``。字段名固定 ``file``（前端桥接层写的）。"""
    if _request is None:
        return b"", ""
    try:
        attribute = _request.files
        # astrbot.api.web 里 files 是方法；quart 里是可等待的属性
        files = attribute() if callable(attribute) else attribute
        files = await files
    except Exception:  # noqa: BLE001
        return b"", ""
    for key in ("file", "files", "upload"):
        handle = files.get(key) if hasattr(files, "get") else None
        if handle is None:
            continue
        name = str(getattr(handle, "filename", "") or "")
        data = handle.read()
        if not isinstance(data, (bytes, bytearray)):
            data = await data
        return bytes(data), name
    return b"", ""


def _ok(data: Any) -> Any:
    if _NEW_API:
        return _json_response({"status": "ok", "data": data})
    if _quart_jsonify is not None:
        return _quart_jsonify({"status": "ok", "data": data})
    return {"status": "ok", "data": data}


def _error(message: str, extra: dict | None = None) -> Any:
    if _NEW_API:
        return _error_response(message, data=extra)
    body: dict[str, Any] = {"status": "error", "message": message}
    if extra:
        body["data"] = extra
    if _quart_jsonify is not None:
        return _quart_jsonify(body)
    return body


async def _current_method() -> str:
    if _NEW_API:
        return str(_request.method or "GET").upper()
    if _request is not None:
        return str(getattr(_request, "method", "GET") or "GET").upper()
    return "GET"


class MemeWebUI:
    def __init__(self, context, plugin) -> None:
        self.context = context
        self.plugin = plugin
        self.routes: list[str] = []

    # --- 注册 -------------------------------------------------------------

    def register(self) -> None:
        """注册页面用的接口。

        路径统一挂在 ``/<插件名>/ui/`` 下面，前端用桥接层调 ``ui/state`` 这种
        相对名。同名路由 + 同样的方法会被替换，所以这里一次一个路径；
        ``config`` 和 ``options`` 用 ``["GET", "POST"]`` 一次注册完。
        """
        table = [
            ("state", self.state, ["GET"], "梗图库概览与候选列表"),
            ("detail", self.detail, ["GET"], "单张图的大图与审核信息"),
            ("upload", self.upload, ["POST"], "拖拽上传（可免审）"),
            ("fetch_url", self.fetch_url, ["POST"], "拖进来的是图片网址时，服务端下载后入库"),
            ("delete", self.delete, ["POST"], "下架一张图"),
            ("toggle", self.toggle, ["POST"], "启用/停用一张图"),
            ("recheck", self.recheck, ["POST"], "重新审核一张图"),
            ("options", self.options_router, ["GET", "POST"], "上传选项（免审/可见范围）"),
            ("config", self.config_router, ["GET", "POST"], "读取/保存可调参数"),
            ("card_probe", self.card_probe, ["POST"], "自检：卡片图能不能进 QQ 素材库"),
            ("unban", self.unban, ["POST"], "恢复某人的投稿权限"),
            ("rejected", self.rejected, ["GET"], "被拒收的图（列表）"),
            ("rejected_detail", self.rejected_detail, ["GET"], "一张被拒收的图（大图）"),
            ("rejected_accept", self.rejected_accept, ["POST"], "人工接收：直接入库"),
            ("rejected_drop", self.rejected_drop, ["POST"], "丢弃这条拒收记录"),
        ]
        for name, handler, methods, desc in table:
            route = f"{ROUTE_PREFIX}/{name}"
            try:
                self.context.register_web_api(route, handler, methods, desc)
                self.routes.append(f"{','.join(methods)} {route}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Meme] 注册 Web API 失败 %s：%s", route, exc)
        logger.info(
            "[Meme] 已注册 %s 个页面接口（%s）",
            len(self.routes),
            "astrbot.api.web" if _NEW_API else "quart 兼容模式",
        )

    # --- 各接口 -----------------------------------------------------------

    async def state(self) -> Any:
        args = await _read_query()
        try:
            payload = await self.plugin.web_state(
                origin=str(args.get("origin") or ""),
                query=str(args.get("q") or ""),
                sort=str(args.get("sort") or ""),
                status=str(args.get("status") or ""),
                limit=int(args.get("limit") or 24),
                offset=int(args.get("offset") or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 读取图库失败", exc_info=True)
            return _error(f"读取图库失败：{type(exc).__name__}: {exc}")
        return _ok(payload)

    async def detail(self) -> Any:
        args = await _read_query()
        try:
            payload = await self.plugin.web_detail(str(args.get("id") or ""))
        except Exception as exc:  # noqa: BLE001
            return _error(f"读取失败：{exc}")
        if payload is None:
            return _error("没有这张图")
        return _ok(payload)

    async def upload(self) -> Any:
        data, filename = await _read_upload()
        if not data:
            # 退路：前端也可以用 JSON 传 base64（大图走这条路时桥接层不转发字节）
            payload = await _read_json()
            raw = str(payload.get("data", "") or "")
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            if raw:
                import base64

                try:
                    data = base64.b64decode(raw)
                except Exception:  # noqa: BLE001
                    data = b""
                filename = str(payload.get("name", "") or "")
        if not data:
            return _error("没收到文件")
        payload = await _read_json()
        try:
            result = await self.plugin.web_upload(
                data,
                filename,
                skip_review=payload.get("skip_review"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 网页上传失败", exc_info=True)
            return _error(f"入库失败：{type(exc).__name__}: {exc}")
        return _ok(result)

    async def fetch_url(self) -> Any:
        """拖进来的是网址（网页上的图、聊天里的图）→ 服务端去下。

        页面那边拿不到跨域字节，只能把 URL 递给服务端；服务端下完走的还是
        :meth:`upload` 同一条入库管线（包括免审标记）。
        """
        payload = await _read_json()
        try:
            result = await self.plugin.web_fetch_url(
                str(payload.get("url", "") or ""),
                skip_review=payload.get("skip_review"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 按网址入库失败", exc_info=True)
            return _error(f"入库失败：{type(exc).__name__}: {exc}")
        if result.get("ok"):
            return _ok(result)
        return _error(str(result.get("message", "入库失败")), extra=result)

    async def delete(self) -> Any:
        payload = await _read_json()
        result = await self.plugin.web_delete(str(payload.get("id", "") or ""))
        if result.get("ok"):
            return _ok(result)
        return _error(result.get("message", "删除失败"))

    async def toggle(self) -> Any:
        payload = await _read_json()
        result = await self.plugin.web_toggle(
            str(payload.get("id", "") or ""), bool(payload.get("active", True))
        )
        if result.get("ok"):
            return _ok(result)
        return _error(result.get("message", "操作失败"))

    async def recheck(self) -> Any:
        payload = await _read_json()
        result = await self.plugin.web_recheck(str(payload.get("id", "") or ""))
        if result.get("ok"):
            return _ok(result)
        return _error(result.get("message", "重审失败"))

    async def options_router(self) -> Any:
        if await _current_method() == "POST":
            payload = await _read_json()
            result = await self.plugin.web_set_upload_options(
                skip_review=payload.get("skip_review"),
            )
            return _ok(result)
        return _ok(self.plugin.web_upload_options())

    async def unban(self) -> Any:
        payload = await _read_json()
        result = await self.plugin.web_unban(str(payload.get("id", "") or ""))
        if result.get("ok"):
            return _ok(result)
        return _error(str(result.get("message", "解禁失败")))

    async def card_probe(self) -> Any:
        """自检：上传一张现做的测试图到 QQ 素材库，然后立刻删掉。

        卡片能不能显示图，取决于这条链路（素材库 / 图床）。它挂了的表现是
        「卡片发出去了但图是裂的」，非常难查 —— 所以给面板一个能一键验的入口。
        只上传不发送，验证完当场回收额度，群里不会多出任何消息。
        """
        try:
            result = await self.plugin.web_card_probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 卡片图自检失败", exc_info=True)
            return _error(f"自检失败：{type(exc).__name__}: {exc}")
        return _ok(result)

    async def rejected(self) -> Any:
        args = await _read_query()
        try:
            payload = await self.plugin.web_rejected(
                limit=int(args.get("limit") or 24),
                offset=int(args.get("offset") or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 读取拒收列表失败", exc_info=True)
            return _error(f"读取拒收列表失败：{type(exc).__name__}: {exc}")
        return _ok(payload)

    async def rejected_detail(self) -> Any:
        args = await _read_query()
        try:
            payload = await self.plugin.web_rejected_detail(str(args.get("sha") or ""))
        except Exception as exc:  # noqa: BLE001
            return _error(f"读取失败：{exc}")
        if payload is None:
            return _error("没有这张图")
        return _ok(payload)

    async def rejected_accept(self) -> Any:
        payload = await _read_json()
        result = await self.plugin.web_rejected_accept(str(payload.get("sha") or ""))
        if result.get("ok"):
            return _ok(result)
        return _error(str(result.get("message") or "入库失败"), extra=result)

    async def rejected_drop(self) -> Any:
        payload = await _read_json()
        result = await self.plugin.web_rejected_drop(str(payload.get("sha") or ""))
        if result.get("ok"):
            return _ok(result)
        return _error(str(result.get("message") or "丢弃失败"))

    async def config_router(self) -> Any:
        if await _current_method() == "POST":
            payload = await _read_json()
            values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
            result = await self.plugin.web_save_settings(values if isinstance(values, dict) else {})
            if not result.get("ok"):
                return _error(result.get("message", "保存失败"), extra=result)
            return _ok(result)
        return _ok(self.plugin.web_settings())
