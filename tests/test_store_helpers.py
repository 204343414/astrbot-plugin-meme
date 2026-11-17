"""测试用的小工具：造一个临时 MemeStore。"""
from __future__ import annotations

import os


def build_store(tmp_path):
    from meme.store import MemeStore

    store = MemeStore(
        os.path.join(str(tmp_path), "meme.db"), os.path.join(str(tmp_path), "media")
    )
    store.init()
    return store
