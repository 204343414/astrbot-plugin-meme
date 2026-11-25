"""批处理搬图（``meme/host_migrate.py``）的测试。

为什么单独测这个脚本：它是唯一一条**绕过面板**改数据库的路径，跑一次会写
100 多行 `hosted`。写错了不会当场炸，而是让之后每一张卡片的图都指向一个
不存在的地址 —— 这种错要等群里有人抱怨才发现。
"""

from __future__ import annotations

import hashlib
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meme import host_migrate  # noqa: E402
from meme.store import MemeStore  # noqa: E402


def png(colour=(200, 30, 30)) -> bytes:
    """造一张最小的合法 PNG。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(colour) * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def seed(store: MemeStore, count: int = 3) -> list[str]:
    shas = []
    for index in range(count):
        data = png((index * 30 + 10, 20, 30))
        sha = hashlib.sha256(data).hexdigest()
        path = store.save_media(sha, data, ".png")
        store.add(sha256=sha, origin="upload", file_path=path, mime="image/png")
        shas.append(sha)
    return shas


class FakeImageHost:
    """够用的假图床：真实现里那份 sha256 去重逻辑没必要在这里重测。"""

    def __init__(self, url_base: str, images_dir: Path) -> None:
        self.url_base = url_base.rstrip("/")
        self.directory = images_dir
        self.directory.mkdir(parents=True, exist_ok=True)
        self._by_digest: dict[str, str] = {}

    def offline_publish_if_absent(self, data: bytes):
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._by_digest:
            return self._by_digest[digest], False
        name = f"{digest[:24]}.png"
        (self.directory / name).write_bytes(data)
        url = f"{self.url_base}/i/{name}"
        self._by_digest[digest] = url
        return url, True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "meme.db"
    store = MemeStore(str(db), str(tmp_path / "media"))
    store.init()
    shas = seed(store)
    images = tmp_path / "hub" / "images"

    def fake_load(hub_plugin_dir, images_dir, base_url):
        assert Path(images_dir) == images
        return FakeImageHost(base_url, Path(images_dir))

    monkeypatch.setattr(host_migrate, "_load_image_host", fake_load)
    return store, db, images, shas


def test_dry_run_writes_nothing(env):
    store, db, images, _ = env
    code = host_migrate.main(
        ["--db", str(db), "--images", str(images), "--base-url", "https://x.test", "--dry-run"]
    )
    assert code == 0
    assert store.hosted_stats()["covered"] == 0
    assert not images.exists() or not list(images.glob("*"))


def test_migrates_everything_and_records_urls(env):
    store, db, images, shas = env
    code = host_migrate.main(
        ["--db", str(db), "--images", str(images), "--base-url", "https://x.test"]
    )
    assert code == 0
    stats = store.hosted_stats()
    assert stats["covered"] == 3
    for sha in shas:
        url = store.hosted_url(sha)
        assert url and url.startswith("https://x.test/i/")
        # 落盘的文件必须真的在，否则 URL 是空的承诺
        assert (images / url.rsplit("/", 1)[-1]).is_file()


def test_second_run_is_a_no_op(env):
    store, db, images, _ = env
    args = ["--db", str(db), "--images", str(images), "--base-url", "https://x.test"]
    assert host_migrate.main(args) == 0
    before = {path.name for path in images.glob("*")}
    assert host_migrate.main(args) == 0
    after = {path.name for path in images.glob("*")}
    assert before == after, "重跑不该再写出新文件"
    assert store.hosted_stats()["covered"] == 3


def test_limit_takes_a_batch_and_leaves_the_rest(env):
    store, db, images, _ = env
    code = host_migrate.main(
        ["--db", str(db), "--images", str(images), "--base-url", "https://x.test", "--limit", "2"]
    )
    assert code == 0
    assert store.hosted_stats()["covered"] == 2
    # 剩下那张还得能被下一批搬走
    assert host_migrate.main(
        ["--db", str(db), "--images", str(images), "--base-url", "https://x.test"]
    ) == 0
    assert store.hosted_stats()["covered"] == 3


def test_missing_file_is_reported_not_fatal(env, tmp_path):
    """一张图读不到，不能让整批停在那儿 —— 剩下的 108 张还在等着。"""
    store, db, images, _ = env
    victim = store.all_for_hosting()[0]
    Path(victim.file_path).unlink()

    code = host_migrate.main(
        ["--db", str(db), "--images", str(images), "--base-url", "https://x.test"]
    )
    assert code == 1, "有失败时退出码要非 0，运维脚本靠它判断"
    assert store.hosted_stats()["covered"] == 2, "其他两张照样搬完"


def test_refuses_a_missing_database(tmp_path, capsys):
    with pytest.raises(SystemExit):
        host_migrate.main(
            [
                "--db", str(tmp_path / "nope.db"),
                "--images", str(tmp_path / "images"),
                "--base-url", "https://x.test",
            ]
        )


def test_load_image_host_fails_loudly_when_the_hub_is_missing(tmp_path):
    """Hub 目录不对时必须大声失败。

    悄悄降级成一个「差不多能用」的实现是最坏的选择：它会写出一批 URL，
    而真正的图床服务端根本不认识那些文件名 —— 109 张全变裂图，且不报错。
    """
    with pytest.raises(SystemExit):
        host_migrate._load_image_host(
            str(tmp_path / "nowhere"), str(tmp_path / "images"), "https://x.test"
        )
