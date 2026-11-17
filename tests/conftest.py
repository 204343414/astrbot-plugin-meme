"""测试用的 AstrBot 桩。

沙箱里没装 AstrBot（也不该为了跑单测去装一个框架），所以这里把
``astrbot.api`` 里插件会用到的那几个名字造出来。业务层 ``meme/`` 本身
不 import astrbot，只有 ``main.py`` 需要；这份桩只服务于 ``test_plugin.py``。

桩要尽量像真的：``filter.command`` 必须把原函数还回来（返回一个万能对象
会让被装饰的 handler 变成另一个对象，测试里调它就静默什么都不做）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _Logger:
    def __getattr__(self, name):
        def _log(*args, **kwargs):
            return None

        return _log


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return

    class BaseMessageComponent:
        type = "Base"

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Image(BaseMessageComponent):
        type = "Image"

        @staticmethod
        def fromFileSystem(path, **kwargs):
            return Image(path=str(path), file=f"file://{path}", **kwargs)

        @staticmethod
        def fromURL(url, **kwargs):
            return Image(url=url, file=url, **kwargs)

    class Plain(BaseMessageComponent):
        type = "Plain"

        def __init__(self, text=""):
            super().__init__()
            self.text = text

    components = types.ModuleType("astrbot.api.message_components")
    components.Image = Image
    components.Plain = Plain
    components.Reply = type("Reply", (BaseMessageComponent,), {"type": "Reply"})

    def command(*args, **kwargs):
        def decorator(func):
            func.__meme_command__ = (args, kwargs)
            return func

        return decorator

    class _FilterNamespace:
        def __getattr__(self, name):
            def _decorator(*args, **kwargs):
                def wrap(func):
                    return func

                return wrap

            return _decorator

    _FilterNamespace.command = staticmethod(command)

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = _FilterNamespace()

    star_module = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    star_module.Star = Star
    star_module.Context = object
    star_module.register = lambda *a, **k: (lambda cls: cls)

    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict
    api.event = event_module
    api.star = star_module
    api.message_components = components

    root = types.ModuleType("astrbot")
    root.api = api
    core = types.ModuleType("astrbot.core")
    message = types.ModuleType("astrbot.core.message")
    result_module = types.ModuleType("astrbot.core.message.message_event_result")

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(("text", text))
            return self

        def base64_image(self, b64):
            self.chain.append(("image", b64))
            return self

    result_module.MessageChain = MessageChain
    message.message_event_result = result_module
    core.message = message
    utils = types.ModuleType("astrbot.core.utils")
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_data_path = lambda: "/tmp/astrbot_data"
    utils.astrbot_path = astrbot_path
    core.utils = utils
    root.core = core

    for name, module in {
        "astrbot": root,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
        "astrbot.api.message_components": components,
        "astrbot.core": core,
        "astrbot.core.message": message,
        "astrbot.core.message.message_event_result": result_module,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": astrbot_path,
    }.items():
        sys.modules[name] = module


_install_astrbot_stub()


@pytest.fixture()
def plugin_module():
    """把整个仓库当成一个包导入，模拟 AstrBot 的 ``data.plugins.<dir>.main``。"""
    import importlib

    name = "meme_plugin_under_test"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(REPO_ROOT)]
        sys.modules[name] = package
    return importlib.import_module(f"{name}.main")
