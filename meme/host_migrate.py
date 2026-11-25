"""把库里已有的图搬到自建图床 —— 可以脱离 AstrBot 跑的批处理。

为什么要有这个东西，而不是只留面板上那个按钮：

1. **面板按钮走的是 dashboard 的 JWT 鉴权**，人在浏览器里点没问题，但
   「一次性把 109 张搬完」这种运维活儿不该必须有人守在浏览器前面。
2. 面板那一批请求是一次 HTTP 往返，跑几十秒没有任何中间输出。批量搬运要能
   在服务器上 `docker exec` 跑、边跑边看进度、断了随时接着跑（已经搬过的
   在 `hosted` 表里，重跑会自动跳过）。

用法（在容器里）::

    python -m meme.host_migrate \\
        --db /AstrBot/data/plugin_data/astrbot_plugin_meme/meme.db \\
        --images /AstrBot/data/plugin_data/astrbot_plugin_qqofficial_hub/images \\
        --base-url https://img.8700k.top

只写 `hosted` 表、只往图床目录追加文件，**不删任何原图** —— 所以随时可以
回退（把插件配置里的 `prefer_image_host` 关掉就退回素材库那条路）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许被当作脚本直接跑（`python meme/host_migrate.py`）而不是模块
if __package__ in (None, ""):  # pragma: no cover - 只在直接执行时走到
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meme.store import MemeStore  # noqa: E402


def _load_image_host(hub_plugin_dir: str, images_dir: str, base_url: str):
    """把 Hub 的图床类拿过来用。

    直接 import 同一份实现，而不是在这里重写一遍 sha256 去重和落盘逻辑 ——
    两份实现早晚会漂移，而漂移出来的那一份会悄悄写出打不开的 URL。
    """
    hub = Path(hub_plugin_dir).resolve()
    if not (hub / "qqofficial_hub" / "image_host.py").is_file():
        raise SystemExit(f"找不到图床实现：{hub}/qqofficial_hub/image_host.py")
    sys.path.insert(0, str(hub))
    from qqofficial_hub.image_host import ImageHost  # noqa: PLC0415

    # data_dir 的上一层：ImageHost 内部会拼上 /images
    return ImageHost(Path(images_dir).resolve().parent, base_url=base_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把梗图库里的图搬到自建图床，写入 hosted 表",
    )
    parser.add_argument("--db", required=True, help="meme.db 路径")
    parser.add_argument("--images", required=True, help="图床的 images 目录（含已发过的图）")
    parser.add_argument("--base-url", required=True, help="图床公网地址，如 https://img.8700k.top")
    parser.add_argument(
        "--hub",
        default="/AstrBot/data/plugins/astrbot_plugin_qqofficial_hub",
        help="Hub 插件目录（借它的 image_host 实现）",
    )
    parser.add_argument("--limit", type=int, default=0, help="只搬前 N 张，0 = 全搬")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计要搬多少张，不写盘",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")

    store = MemeStore(str(db_path))
    store.init()

    pending = []
    for item in store.all_for_hosting():
        if not store.hosted_url(item.sha256):
            pending.append(item)
    total_planned = len(pending)
    if args.limit > 0:
        pending = pending[: args.limit]

    stats = store.hosted_stats()
    print(
        f"[前置] 全库能发 {stats['active']} 张，已上图床 {stats['covered']} 张，"
        f"本次待搬 {total_planned} 张"
        + (f"（限 {args.limit} 张）" if args.limit else "")
    )
    if args.dry_run:
        print("[完成] dry-run，什么都没写")
        return 0

    host = _load_image_host(args.hub, args.images, args.base_url)
    host.directory.mkdir(parents=True, exist_ok=True)

    moved = reused = 0
    failed: list[str] = []
    for index, item in enumerate(pending, 1):
        path = Path(item.file_path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            failed.append(f"#{item.id} 读不到 {path.name}：{exc}")
            print(f"  [{index}/{len(pending)}] #{item.id} 读不到：{exc}")
            continue
        try:
            # 用离线版：批量导入不需要图床已经在跑，只需要文件落到目录里。
            # 线上那个 publish_if_absent 会检查 running，导入时端口往往还没起。
            publish = getattr(host, "offline_publish_if_absent", None) or host.publish_if_absent
            url, created = publish(data)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"#{item.id} 上传失败：{type(exc).__name__}: {exc}")
            print(f"  [{index}/{len(pending)}] #{item.id} 上传失败：{exc}")
            continue
        store.set_hosted_url(item.sha256, url)
        if created:
            moved += 1
        else:
            reused += 1
        if index % 10 == 0 or index == len(pending):
            print(
                f"  [{index}/{len(pending)}] 新传 {moved} · 复用 {reused} · "
                f"失败 {len(failed)} → 最新 {url.rsplit('/', 1)[-1]}"
            )

    after = store.hosted_stats()
    print(
        f"[完成] 新传 {moved} 张、复用 {reused} 张、失败 {len(failed)} 张；"
        f"当前覆盖 {after['covered']}/{after['active']} 张"
    )
    if failed:
        print("[失败明细]")
        for line in failed[:20]:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
