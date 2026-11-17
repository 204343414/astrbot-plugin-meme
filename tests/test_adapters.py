"""适配层：地址归一化、响应解析、provider 里读密钥。"""
from __future__ import annotations

import asyncio
import base64

import pytest

from meme.adapters import (
    Credentials,
    OpenAIImageGenerator,
    ProviderTranslator,
    ProviderVision,
    _dig,
    normalize_openai_base,
    parse_image_response,
    resolve_provider_credentials,
)


# --- base URL ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://api.openai.com", "https://api.openai.com/v1"),
        ("https://api.openai.com/", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://globalai.vip/v1/", "https://globalai.vip/v1"),
        ("https://x.com/compatible-mode/v1", "https://x.com/compatible-mode/v1"),
        ("", ""),
    ],
)
def test_normalizes_base_url_to_v1(raw, expected):
    assert normalize_openai_base(raw) == expected


def test_does_not_double_append_version():
    assert normalize_openai_base("https://x.com/v2") == "https://x.com/v2"


# --- 响应解析 ---------------------------------------------------------------

def test_parses_b64_json():
    payload = {"data": [{"b64_json": base64.b64encode(b"PNGDATA").decode()}]}
    assert parse_image_response(payload) == b"PNGDATA"


def test_parses_data_url_payload():
    payload = {"data": [{"url": "data:image/png;base64," + base64.b64encode(b"X").decode()}]}
    assert parse_image_response(payload) == b"X"


def test_url_only_payload_yields_nothing():
    """只有 http url 时不下载：下载交给上层，解析层保持无副作用。"""
    assert parse_image_response({"data": [{"url": "https://x/y.png"}]}) == b""


def test_passes_raw_bytes_through():
    assert parse_image_response(b"raw") == b"raw"


def test_garbage_payload_is_empty():
    assert parse_image_response({}) == b""
    assert parse_image_response({"data": []}) == b""
    assert parse_image_response(None) == b""


# --- provider 凭证 ----------------------------------------------------------

class FakeProvider:
    def __init__(self, provider_config=None, api_keys=None):
        self.provider_config = provider_config or {}
        self.api_keys = api_keys or []


def test_reads_credentials_from_provider_config():
    provider = FakeProvider(
        {
            "api_base": "https://globalai.vip",
            "key": ["sk-first", "sk-second"],
            "model": "gpt-image-2.6-sunburst",
        }
    )
    creds = resolve_provider_credentials(provider)
    assert creds.api_base == "https://globalai.vip"
    assert creds.api_key == "sk-first"
    assert creds.model == "gpt-image-2.6-sunburst"


def test_model_override_wins():
    provider = FakeProvider({"api_base": "https://x/v1", "key": "k", "model": "old"})
    assert resolve_provider_credentials(provider, "new-model").model == "new-model"


def test_falls_back_to_instance_attributes():
    provider = FakeProvider(api_keys=["sk-attr"])
    provider.api_base = "https://y/v1"
    provider.model_name = "m"
    creds = resolve_provider_credentials(provider)
    assert creds.api_base == "https://y/v1"
    assert creds.api_key == "sk-attr"
    assert creds.model == "m"


def test_missing_everything_is_empty_not_crash():
    assert resolve_provider_credentials(FakeProvider()) == Credentials()


# --- 多模态 ----------------------------------------------------------------

class FakeLLMResponse:
    def __init__(self, text):
        self.completion_text = text


class FakeChatProvider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def text_chat(self, prompt=None, image_urls=None, **kwargs):
        self.calls.append((prompt, image_urls))
        return self.reply


def test_vision_sends_the_image_as_data_url():
    provider = FakeChatProvider(FakeLLMResponse('{"is_meme": true}'))
    vision = ProviderVision(provider, "看图")
    result = asyncio.run(vision("data:image/jpeg;base64,AAA"))
    assert result == '{"is_meme": true}'
    prompt, image_urls = provider.calls[0]
    assert prompt == "看图"
    assert image_urls == ["data:image/jpeg;base64,AAA"]


def test_vision_accepts_plain_string_reply():
    provider = FakeChatProvider('{"is_meme": false}')
    assert asyncio.run(ProviderVision(provider, "p")("data:,")) == '{"is_meme": false}'


def test_translator_does_not_send_an_image():
    provider = FakeChatProvider(FakeLLMResponse("中文"))
    assert asyncio.run(ProviderTranslator(provider)("翻一下")) == "中文"
    assert provider.calls[0] == ("翻一下", None)


def test_provider_without_text_chat_raises_clearly():
    with pytest.raises(RuntimeError):
        asyncio.run(ProviderVision(object(), "p")("data:,"))


# --- 画图器（只测不联网的分支）----------------------------------------------

def test_generator_refuses_without_credentials():
    generator = OpenAIImageGenerator(Credentials())
    with pytest.raises(RuntimeError):
        asyncio.run(generator(b"img", "prompt"))


def test_generator_refuses_without_model():
    generator = OpenAIImageGenerator(
        Credentials(api_base="https://x/v1", api_key="k", model="")
    )
    with pytest.raises(RuntimeError):
        asyncio.run(generator(b"img", "prompt"))


# --- JSON 路径 -------------------------------------------------------------

def test_dig_handles_nested_and_list_paths():
    payload = {"data": [{"url": "https://img"}, {"url": "https://img2"}]}
    assert _dig(payload, "data.1.url") == "https://img2"
    assert _dig(payload, "data.9.url") is None
    assert _dig(payload, "nope.url") is None
    assert _dig(payload, "") is payload


# --- 代理 -------------------------------------------------------------------

def test_client_kwargs_includes_proxy_when_given():
    from meme.adapters import client_kwargs

    kwargs = client_kwargs(30.0, "http://172.18.0.1:7890")
    assert "http://172.18.0.1:7890" in kwargs.values()
    assert kwargs["timeout"] == 30.0
    assert kwargs["follow_redirects"] is True


def test_client_kwargs_is_bare_without_proxy():
    from meme.adapters import client_kwargs

    kwargs = client_kwargs(5.0, "")
    assert kwargs["timeout"] == 5.0
    # 没有代理就不该出现代理参数（空串会覆盖掉环境变量里的代理）
    assert not any(k in kwargs for k in ("proxy", "proxies"))


def test_generator_keeps_the_proxy_for_its_requests():
    generator = OpenAIImageGenerator(
        Credentials(api_base="https://x/v1", api_key="k", model="m"),
        proxy="http://127.0.0.1:7890",
    )
    assert generator.proxy == "http://127.0.0.1:7890"


def test_json_source_keeps_the_proxy():
    from meme.adapters import JsonApiImageSource

    source = JsonApiImageSource("https://x/api", "data.url", proxy="http://p:1")
    assert source.proxy == "http://p:1"
