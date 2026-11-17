"""在 Node 里把页面脚本真跑一遍：假 DOM + 假桥接层 + 一份真实形状的 state。

只查"能不能跑通、该出现的字出现了没有"，不查像素。线上那次事故
（调参按钮没反应）就是这种测试能提前拦住的类型。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "pages" / "meme"
HARNESS = Path(__file__).resolve().parent / "page_harness.mjs"

STATE = {
    "cards": [],
    "origins": [],
    "blacklist": [],
    "card": {"enabled": True, "materials": 9, "materialKeep": 60},
    "running": {"captionMode": "append", "moderationProvider": "grok", "dataDir": "/data"},
    "activeTotal": 26,
    "libraryTotal": 26,
    "rejected": 0,
    "hasMore": False,
    "offset": 0,
    "auto": {
        "enabled": True,
        "pcts": {"low": 25, "mid": 45, "high": 87},
        "maxPerDay": 12,
        "quiet": "01:00-06:59",
        "groups": 2,
        "subscribed": [
            {"group": "4E80F5C0", "umu": "a:G:4E80F5C0", "level": "high", "pct": 87,
             "since": 0, "lastPush": 0, "sentToday": 1, "blockedUntil": 0},
            {"group": "ED36228F", "umu": "a:G:ED36228F", "level": "mid", "pct": 45,
             "since": 0, "lastPush": 0, "sentToday": 0, "blockedUntil": 0},
        ],
        "offed": [{"umo": "a:G:DEADBEEF", "reason": "no_permission", "at": 1, "manual": False}],
        "offedCount": 1,
        "bootstrap": True,
    },
}


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_page_script_boots_and_shows_the_group_list(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(STATE, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["node", str(HARNESS), str(PAGE), str(state_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "个群订阅中" in result.stdout
    assert "4E80F5C0" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_page_survives_an_empty_library_and_no_subscriptions(tmp_path):
    """刚装好的机器人：库空、没有任何群订阅 —— 页面也必须照常渲染。"""
    state = json.loads(json.dumps(STATE))
    state["auto"]["subscribed"] = []
    state["auto"]["groups"] = 0
    state["auto"]["offed"] = []
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["node", str(HARNESS), str(PAGE), str(state_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
