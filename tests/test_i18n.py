"""插件自带的 zh-CN 词条得能被面板认出来。

面板侧栏的页签名字是按 `pages.<页面目录名>.title` 取的（dashboard/src/utils/
pluginI18n.js 里的 getPluginPageI18nBase），页内标题 `pageTitle` 才用
`views.<名>.title`。两个都要有，否则侧栏会直接显示目录名 "meme"。
"""
from __future__ import annotations

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
I18N_PATH = PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json"
PAGES_DIR = PLUGIN_ROOT / "pages"


def _locale() -> dict:
    return json.loads(I18N_PATH.read_text(encoding="utf-8"))


def test_page_directories_have_titles():
    locale = _locale()
    pages = sorted(path.name for path in PAGES_DIR.iterdir() if (path / "index.html").is_file())
    assert pages, "pages/ 下没有页面，面板里就没有入口"
    for name in pages:
        title = locale.get("pages", {}).get(name, {}).get("title")
        assert title, f"缺 pages.{name}.title，侧栏会显示目录名"
        assert title == locale.get("views", {}).get(name, {}).get("title")


def test_page_title_is_chinese():
    locale = _locale()
    title = locale["pages"]["meme"]["title"]
    assert any("\u4e00" <= char <= "\u9fff" for char in title)
