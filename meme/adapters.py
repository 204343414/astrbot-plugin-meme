"""把外部世界包成本管线认的那几个函数。

这里只依赖 httpx，不依赖 AstrBot：``provider`` 是从外面传进来的对象，
只要它有 ``text_chat``（AstrBot 的 Provider 契约）就能用。这样测试可以塞
一个三行的假 provider，不必把 AstrBot 装起来。
"""
from __future__ import annotations

import base64
import inspect
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .draw import choose_draw_size, fit_to_reference, image_size

logger = logging.getLogger(__name__)

#: httpx 0.26 起 ``proxy`` 单数，之前叫 ``proxies``。容器里两种都可能。
_PROXY_KW = "proxy" if "proxy" in inspect.signature(
    httpx.AsyncClient.__init__
).parameters else "proxies"


def client_kwargs(timeout: float, proxy: str = "") -> dict[str, Any]:
    """给 httpx 的构造参数。

    代理必须显式传：容器里的出站流量基本都靠 AstrBot 的 ``http_proxy``
    （``172.18.0.1:7890`` 这类地址）转发，而 httpx 默认读环境变量、环境变量
    又常常是空的。不传就等于直连 —— 表现是 ``Network is unreachable``，
    看起来像接口挂了，其实是根本没出门。
    """
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    proxy = (proxy or "").strip()
    if proxy:
        kwargs[_PROXY_KW] = proxy
    return kwargs


def _build_client(timeout: float, proxy: str = "") -> httpx.AsyncClient:
    kwargs = client_kwargs(timeout, proxy)
    try:
        return httpx.AsyncClient(**kwargs)
    except TypeError:  # 版本不认这个参数名
        kwargs.pop(_PROXY_KW, None)
        if proxy:
            kwargs["proxies"] = proxy
        return httpx.AsyncClient(**kwargs)


# --- 多模态：审核 / 翻译 ----------------------------------------------------


def _completion_text(response: Any) -> str:
    """从 LLMResponse 里抠出文本，兼容几种常见形状。"""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    for attr in ("completion_text", "text", "result"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value:
            return value
    result = getattr(response, "result", None)
    if result is not None:
        for attr in ("completion_text", "text"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value:
                return value
    return ""


class ProviderVision:
    """多模态审核：把图塞给 provider，要它回一段 JSON。"""

    def __init__(self, provider: Any, prompt: str) -> None:
        self.provider = provider
        self.prompt = prompt

    async def __call__(self, data_url: str) -> str:
        text_chat = getattr(self.provider, "text_chat", None)
        if text_chat is None:
            raise RuntimeError("该 Provider 不支持文本对话，无法用来审核")
        response = await text_chat(prompt=self.prompt, image_urls=[data_url])
        return _completion_text(response)


class ProviderTranslator:
    """纯文本翻译。默认复用审核 provider：它已经能看图，翻文字自然也行。"""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def __call__(self, prompt: str) -> str:
        text_chat = getattr(self.provider, "text_chat", None)
        if text_chat is None:
            raise RuntimeError("该 Provider 不支持文本对话，无法用来翻译")
        response = await text_chat(prompt=prompt)
        return _completion_text(response)


# --- 画图 -------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    api_base: str = ""
    api_key: str = ""
    model: str = ""


def resolve_provider_credentials(provider: Any, model_override: str = "") -> Credentials:
    """从一个已配置的 Provider 里读出 base_url / key / model。

    AstrBot 在实例化 provider 前会把 ``provider_*`` 配置和它引用的
    ``provider_sources`` 合并成 ``provider_config``，所以这三样都在里面 ——
    这也是「在提供商里一键选中画图模型」能成立的全部原理：选的不是一张
    图片模型列表（AstrBot 没有这个类型），而是一个 OpenAI 兼容的提供商，
    插件拿它的地址和密钥去调 ``/images/edits``。
    """
    config = dict(getattr(provider, "provider_config", None) or {})
    base = str(config.get("api_base") or config.get("base_url") or "").strip()
    raw_key = config.get("key") or config.get("api_key") or ""
    if isinstance(raw_key, (list, tuple)):
        key = str(raw_key[0]).strip() if raw_key else ""
    else:
        key = str(raw_key).strip()
    model = model_override.strip() or str(config.get("model") or "").strip()
    if not base:
        base = str(getattr(provider, "api_base", "") or "").strip()
    if not key:
        keys = getattr(provider, "api_keys", None) or []
        key = str(keys[0]).strip() if keys else ""
    if not model:
        model = str(getattr(provider, "model_name", "") or "").strip()
    return Credentials(api_base=base, api_key=key, model=model)


def normalize_openai_base(base: str) -> str:
    """补成 ``.../v1``。

    OpenAI SDK 会自动拼 ``/images/edits``，所以 base 必须以版本号结尾；
    用户填 ``https://x.com`` 时直接拼会变成 ``https://x.com/images/edits``
    而 404 —— 这个坑 art 插件也踩过，注释里都写着。
    """
    value = (base or "").strip().rstrip("/")
    if not value:
        return ""
    if value.endswith("/v1") or "/v1/" in value + "/":
        return value
    # 已带版本号的（/v1beta、/api/v3 之类）就别再追加
    tail = value.rsplit("/", 1)[-1]
    if tail[:1] == "v" and tail[1:2].isdigit():
        return value
    return value + "/v1"


def parse_image_response(payload: Any) -> bytes:
    """``/images/*`` 的响应 → 图片字节。b64_json 优先，其次 url。"""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if not isinstance(payload, dict):
        return b""
    items = payload.get("data") or []
    if isinstance(items, dict):
        items = [items]
    for item in items:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json") or item.get("image") or ""
        if isinstance(b64, str) and b64:
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[-1]
            try:
                return base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                continue
        url = item.get("url") or ""
        if isinstance(url, str) and url.startswith("data:"):
            try:
                return base64.b64decode(url.split(",", 1)[-1])
            except Exception:  # noqa: BLE001
                continue
    return b""


class OpenAIImageGenerator:
    """重画中文版。

    先走 ``/images/edits``（给原图 + 提示词，画图模型才知道「改这一张」），
    失败再退 ``/images/generations``（纯提示词，画面会变，但至少有中文版），
    两条都挂就抛异常，由管线退回字幕模式。

    尺寸**不写死**：``size="auto"`` 时每次拿原图的宽高比去挑画布（横图用
    1536x1024、竖图用 1024x1536、接近方形才用 1024x1024），画完再裁回原图
    比例。写死方形会让横幅梗图被模型重新构图，中文化变成「重画一张」。
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        size: str = "auto",
        quality: str = "",
        timeout: float = 120.0,
        use_generations_fallback: bool = True,
        proxy: str = "",
    ) -> None:
        self.credentials = credentials
        self.size = size
        self.quality = quality
        self.timeout = timeout
        self.use_generations_fallback = use_generations_fallback
        self.proxy = proxy

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.api_key}"}

    def _url(self, endpoint: str) -> str:
        return f"{normalize_openai_base(self.credentials.api_base)}/{endpoint}"

    async def edit(self, data: bytes, prompt: str, size: str = "") -> bytes:
        form = {"model": self.credentials.model, "prompt": prompt, "size": size or self.size}
        if self.quality:
            form["quality"] = self.quality
        files = {"image": ("meme.png", data, "image/png")}
        async with _build_client(self.timeout, self.proxy) as client:
            response = await client.post(
                self._url("images/edits"), headers=self._headers(), data=form, files=files
            )
        response.raise_for_status()
        return parse_image_response(response.json())

    async def generate(self, prompt: str, size: str = "") -> bytes:
        body = {
            "model": self.credentials.model,
            "prompt": prompt,
            "size": size or self.size,
            "n": 1,
        }
        if self.quality:
            body["quality"] = self.quality
        async with _build_client(self.timeout, self.proxy) as client:
            response = await client.post(
                self._url("images/generations"), headers=self._headers(), json=body
            )
        response.raise_for_status()
        return parse_image_response(response.json())

    async def __call__(self, data: bytes, prompt: str) -> bytes:
        if not self.credentials.api_base or not self.credentials.api_key:
            raise RuntimeError("画图 Provider 没配好（缺 API 地址或密钥）")
        if not self.credentials.model:
            raise RuntimeError("没填重绘模型名")
        width, height = image_size(data)
        size = choose_draw_size(width, height, self.size)
        logger.info(
            "[Meme] 重画尺寸：原图 %sx%s → %s", width or "?", height or "?", size
        )
        image = b""
        try:
            image = await self.edit(data, prompt, size=size)
            if not image:
                logger.info("[Meme] images/edits 没返回图，改试 generations")
        except Exception as exc:  # noqa: BLE001
            logger.info("[Meme] images/edits 不可用（%s），改试 generations", exc)
            if not self.use_generations_fallback:
                raise
        if not image:
            image = await self.generate(prompt, size=size)
        if not image:
            raise RuntimeError("画图接口没有返回图片")
        # 模型不太可能正好画成原图比例，这里掰回来
        return fit_to_reference(image, width, height)


# --- HTTP 抓取 --------------------------------------------------------------


async def fetch_bytes(
    url: str,
    timeout: float = 30.0,
    headers: dict | None = None,
    proxy: str = "",
) -> bytes:
    async with _build_client(timeout, proxy) as client:
        response = await client.get(url, headers=headers or {})
    response.raise_for_status()
    return response.content


class JsonApiImageSource:
    """第三方梗图 API：GET 一个 JSON，按点号路径取图片 URL。

    ``json_path`` 留空表示这个接口直接返回图片本体（不少「随机图」API 就是
    302 到一张图）。
    """

    def __init__(
        self,
        url: str,
        json_path: str = "",
        timeout: float = 30.0,
        proxy: str = "",
    ) -> None:
        self.url = url
        self.json_path = json_path
        self.timeout = timeout
        self.proxy = proxy

    async def fetch(self) -> tuple[bytes, str]:
        """``(图片字节, 来源 URL)``。"""
        if not self.url:
            raise RuntimeError("没有配置第三方接口地址")
        async with _build_client(self.timeout, self.proxy) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not self.json_path or content_type.startswith("image/"):
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"接口返回的不是图片（{content_type or '未知类型'}）")
                return response.content, str(response.url)
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                raise RuntimeError("接口没有返回合法 JSON")
            image_url = _dig(payload, self.json_path)
            if not image_url:
                raise RuntimeError(f"JSON 里找不到 {self.json_path}")
            image_response = await client.get(str(image_url))
            image_response.raise_for_status()
            return image_response.content, str(image_url)


def _dig(payload: Any, path: str) -> Any:
    """``data.0.url`` 这种路径取值，支持列表下标。"""
    current = payload
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current
