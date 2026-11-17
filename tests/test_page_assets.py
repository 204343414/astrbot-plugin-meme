"""页面静态检查：app.js 引用的元素 id 必须真的在 index.html 里。

为什么值得单写一个测试
----------------------
线上真出过：`app.js` 里给一个早就删掉的「可见范围」下拉框挂事件（`optScope`），
HTML 里没有这个元素 → `bind()` 抛 TypeError → **它后面注册的所有按钮都没挂上**，
`loadState()` 也没跑。表现出来是「调参点了没反应」「统计永远是 —」，
控制台只有一句 TypeError，很难猜到是前面某个元素没了。

这个测试把「id 对不上」变成红灯，前端改坏了在提交这一关就能看见。
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "meme"


def test_every_element_id_used_by_js_exists_in_html():
    js = (PAGE / "app.js").read_text(encoding="utf-8")
    html = (PAGE / "index.html").read_text(encoding="utf-8")
    used = {name for name in re.findall(r'el\("([^"]+)"\)', js)}
    have = set(re.findall(r'id="([^"]+)"', html))
    missing = sorted(used - have)
    assert not missing, f"app.js 用了页面上不存在的元素：{missing}"


def test_bind_only_registers_handlers_through_the_guard():
    """bind() 里不允许再出现 `el("x").addEventListener` —— 那正是连坐的写法。"""
    js = (PAGE / "app.js").read_text(encoding="utf-8")
    start = js.index("function bind()")
    end = js.index("function showError")
    body = js[start:end]
    offenders = re.findall(r'el\("([^"]+)"\)\.addEventListener', body)
    assert not offenders, f"这些地方要改成 on(...)：{offenders}"


def test_page_declares_what_it_reads_for_the_auto_panel():
    """自动推送那一块要能在页面上直接看到，而不是只藏在 title 悬停里。"""
    js = (PAGE / "app.js").read_text(encoding="utf-8")
    html = (PAGE / "index.html").read_text(encoding="utf-8")
    for marker in ("autoPanel", "autoGroups", "autoSummary", "autoOffed"):
        assert f'id="{marker}"' in html, marker
    assert "renderAuto(" in js
    assert "个群订阅中" in js
