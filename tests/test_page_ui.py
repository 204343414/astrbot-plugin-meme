"""页面本身的回归测试：不吃 JS 运行时，只钉住几条踩过的坑。

面板给插件页面的 iframe 是 ``sandbox="allow-scripts allow-forms
allow-downloads"`` —— **没有 allow-modals**。所以 ``window.confirm`` 会静默
返回 false、``alert`` 什么也不弹。第一版页面用 ``window.confirm`` 做删除确认，
表现就是「点删除没用」，而测试全绿（因为测试只测后端）。这里把这条钉住。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PAGE = PLUGIN_ROOT / "pages" / "meme"

JS = (PAGE / "app.js").read_text(encoding="utf-8")
HTML = (PAGE / "index.html").read_text(encoding="utf-8")
SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def test_no_native_modals_in_page():
    """沙盒 iframe 里 window.confirm / alert / prompt 全失效，一个都不许用。"""
    code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)  # 去掉注释，注释里提到不算
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for banned in ("window.confirm", "window.alert", "window.prompt"):
        assert banned not in code, f"页面里不能用 {banned}（会被面板沙盒静默吃掉）"
    for bare in ("confirm", "alert", "prompt"):
        assert not re.search(rf"(?<![\w.$]){bare}\s*\(", code), f"别用裸的 {bare}()"


def test_page_has_its_own_confirm_dialog():
    assert 'id="confirmText"' in HTML and 'id="confirmOk"' in HTML
    assert "function ask(" in JS and "function closeConfirm(" in JS
    assert 'ask(`确定下架' in JS or "ask(`确定下架" in JS


def test_drop_handles_items_and_urls():
    """拖拽要能从 files / items / URL 三种来源里取东西。"""
    assert "dataTransfer.items" in JS
    assert "getAsFile" in JS
    assert "text/uri-list" in JS
    assert "ui/fetch_url" in JS


def test_folder_import_wired():
    assert "webkitdirectory" in HTML
    assert 'id="pickFolder"' in HTML and "folderInput" in JS


def test_schema_descriptions_have_no_plain_talk_prefix():
    """按用户要求：配置项说明里不要「说白了」这种引子。"""
    offenders = [
        key for key, item in SCHEMA.items()
        if isinstance(item, dict) and "白话" in str(item.get("description", ""))
    ]
    assert not offenders, offenders


def test_schema_descriptions_still_explain_something():
    """去掉引子之后不能变成空说明。"""
    for key, item in SCHEMA.items():
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "") or "").strip()
        assert len(desc) >= 8, f"{key} 的说明太短了：{desc!r}"


# --- 拒收页签（0.6.4）------------------------------------------------------

def test_rejected_tab_exists_and_is_wired():
    """顶栏要有「拒收」入口，HTML 的 id 和 JS 绑定的 id 必须对上。

    踩过的坑：HTML 里删了一个元素、JS 还在 `el("xxx").addEventListener`，
    抛 TypeError 之后**后面所有按钮全挂不上**。所以这对 id 用测试钉住。
    """
    assert 'id="toggleRejected"' in HTML
    assert 'id="rejectedCount"' in HTML
    assert 'on("toggleRejected", "click"' in JS
    assert "ui/rejected" in JS
    assert "ui/rejected_accept" in JS
    assert "ui/rejected_drop" in JS
    assert "ui/rejected_detail" in JS


def test_rejected_actions_use_in_page_confirm():
    """接收/丢弃都要走页面内的确认框，不能指望 window.confirm。"""
    for action in ("acceptRejected", "dropRejected"):
        start = JS.index(f"async function {action}")
        body = JS[start : start + 700]
        assert "await ask(" in body, f"{action} 没有二次确认"


def test_rejected_list_is_hidden_by_default():
    """默认不展开：拒收是低频操作，不该占着首屏。"""
    assert re.search(r'id="rejected"[^>]*class="rejected hidden"', HTML)
    assert 'id="rejectedGrid"' in HTML
    assert 'id="rejectedMore"' in HTML


def test_rejected_page_flags_missing_files():
    """0.6.4 之前拒收的图没存盘，页面必须明确说「没存下来」而不是空着。"""
    assert "rejected-noimg" in JS
    assert "没存下来" in JS
    assert "hasFile" in JS
