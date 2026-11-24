"""SQLite 存储：图库、审核缓存、拒收记录、群推送状态。

用 SQLite 而不是 json，是因为这里就三件事：去重（sha256 唯一约束）、
随机取样（随时可能上万行）、以及并发写（群里几个人同时投稿）。json 文件
在这三件事上每一件都要自己加锁重写整个文件。

连接每次操作现开现关：AstrBot 的插件回调跑在事件循环里，长连接跨线程
容易炸（sqlite3 的 check_same_thread），而这种量级下开连接的开销可以忽略。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Callable

from .models import (
    STATUS_ACTIVE,
    MemeItem,
    Review,
)
from .media import ext_for_mime
from .select import pick_meme

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256      TEXT NOT NULL UNIQUE,
    origin      TEXT NOT NULL DEFAULT 'submitted',
    -- scope / group_id 是老「单群图」留下的列。功能已经砍掉（图库只有一份、所有群共用），
    -- 列保留只是为了不动老库的表结构：SQLite 删列的代价远大于留两个被忽略的列。
    scope       TEXT NOT NULL DEFAULT 'group',
    group_id    TEXT NOT NULL DEFAULT '',
    file_path   TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT 'image/jpeg',
    caption     TEXT NOT NULL DEFAULT '',
    caption_zh  TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    nsfw_level  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active',
    contributor TEXT NOT NULL DEFAULT '',
    source_url  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    used_at     REAL NOT NULL DEFAULT 0,
    used_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memes_scope ON memes(scope, group_id, status);
CREATE INDEX IF NOT EXISTS idx_memes_origin ON memes(origin);

CREATE TABLE IF NOT EXISTS review_cache (
    sha256     TEXT PRIMARY KEY,
    review     TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rejects (
    sha256      TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT '',
    contributor TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rejects_created ON rejects(created_at);

CREATE TABLE IF NOT EXISTS groups (
    umo           TEXT PRIMARY KEY,
    group_id      TEXT NOT NULL DEFAULT '',
    -- daily_enabled / daily_slots 是老「/meme 每日」留下的列。那个功能已经删了
    -- （和自动搬石是同一个能力的两套实现），列留着不动老表结构，代码不读。
    daily_enabled INTEGER NOT NULL DEFAULT 0,
    daily_slots   TEXT NOT NULL DEFAULT '',
    blocked_until REAL NOT NULL DEFAULT 0,
    last_push     REAL NOT NULL DEFAULT 0,
    push_count    INTEGER NOT NULL DEFAULT 0,
    push_date     TEXT NOT NULL DEFAULT '',
    -- 每小时自动推（自动搬石）：''=没开，low/mid/high=档位；
    -- 概率本身不落库，每次从配置读，面板改了立刻生效。
    auto_level      TEXT NOT NULL DEFAULT '',
    auto_since      REAL NOT NULL DEFAULT 0,
    auto_sent_date  TEXT NOT NULL DEFAULT '',
    auto_sent_count INTEGER NOT NULL DEFAULT 0,
    -- 「试过了但不行」的墓碑：群没开主动消息 / 机器人不在群里时自动关订阅，
    -- 记下原因和时间，之后不再自动重开（否则每次重启都白试一遍）。
    auto_off_reason TEXT NOT NULL DEFAULT '',
    auto_off_at     REAL NOT NULL DEFAULT 0,
    -- 1 = 是用户/管理员明确点「取消自动」关的，批量铺开时不许覆盖
    auto_opt_out    INTEGER NOT NULL DEFAULT 0,
    -- 最后一次在这个群活动的时间（批量铺开只挑最近活跃的群）
    last_seen       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);

-- 投递记录：哪个会话、什么时候、把哪张图发出去过。
--
-- 为什么要有这张表：memes 表里只有一个全局的 used_at/used_count，那是「全站
-- 发过几次」的热度，不是「这个群看过没有」。只靠全局字段时，五个群共用一条
-- 队列 —— 新图被第一个抽中的群吃掉，其余群要等整整一轮（实测 14~29 天）才
-- 见得到，体感就是「一百多张候选，老群还是那几张熟的」。
--
-- 主键 (umo, meme_id)：一个群对一张图只记一行（只关心「发过没」和「多早发的」，
-- 发过几次不影响挑选 —— 挑选只按最早那次算冷热）。
CREATE TABLE IF NOT EXISTS deliveries (
    umo       TEXT NOT NULL,
    meme_id   INTEGER NOT NULL,
    first_at  REAL NOT NULL DEFAULT 0,
    last_at   REAL NOT NULL DEFAULT 0,
    times     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (umo, meme_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_meme ON deliveries(meme_id);

-- 恶意投稿黑名单：按「人」拉黑，跨群生效。reason/evidence 都留着，事后能复盘。
CREATE TABLE IF NOT EXISTS blacklist (
    sender_id     TEXT PRIMARY KEY,
    origin        TEXT NOT NULL DEFAULT '',
    group_id      TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    evidence_sha  TEXT NOT NULL DEFAULT '',
    evidence_path TEXT NOT NULL DEFAULT '',
    hits          INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL DEFAULT 0
);

-- QQ 素材库额度记账：每发一张带图卡片就占一格（每机器人 100 张），
-- 超出的旧素材要主动删，否则一百次之后就全线发不出图。
CREATE TABLE IF NOT EXISTS card_materials (
    image_id   TEXT PRIMARY KEY,
    meme_id    INTEGER NOT NULL DEFAULT 0,
    origin     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    sha256     TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    last_used  REAL NOT NULL DEFAULT 0
);

-- 注意：card_materials 的 sha256 索引不能写在这里。老库的 card_materials 表已存在，
-- CREATE TABLE IF NOT EXISTS 会跳过建表、只留下缺列的老表，此时建索引会直接报
-- "no such column: sha256" 把整个插件加载打挂。索引统一放到 init() 里补完列之后建。
"""


class MemeStore:
    def __init__(
        self,
        db_path: str,
        media_dir: str = "",
        now: Callable[[], float] | None = None,
    ) -> None:
        self.db_path = db_path
        self.media_dir = media_dir or os.path.join(os.path.dirname(db_path), "media")
        self._now = now or time.time
        self._lock = threading.RLock()

    # --- 基础设施 ---------------------------------------------------------

    def init(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        os.makedirs(self.media_dir, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 老库补列：素材复用（按 sha256 认回已经传过的那张）是后加的
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(card_materials)")}
            for name, ddl in (
                ("sha256", "TEXT NOT NULL DEFAULT ''"),
                ("url", "TEXT NOT NULL DEFAULT ''"),
                ("last_used", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE card_materials ADD COLUMN {name} {ddl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_card_materials_sha ON card_materials(sha256)"
            )
            # 老库补列：后台「拒收」页面（0.6.4 加的）。没有这些列也能跑，
            # 只是页面上的「来源 / 大小 / 格式」是空的。
            reject_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rejects)")}
            for name, ddl in (
                ("origin", "TEXT NOT NULL DEFAULT ''"),
                ("source_url", "TEXT NOT NULL DEFAULT ''"),
                ("mime", "TEXT NOT NULL DEFAULT ''"),
                ("size", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in reject_columns:
                    conn.execute(f"ALTER TABLE rejects ADD COLUMN {name} {ddl}")
            # 老库补列：每小时自动推（0.5.0 加的）
            group_columns = {row["name"] for row in conn.execute("PRAGMA table_info(groups)")}
            for name, ddl in (
                ("auto_level", "TEXT NOT NULL DEFAULT ''"),
                ("auto_since", "REAL NOT NULL DEFAULT 0"),
                ("auto_sent_date", "TEXT NOT NULL DEFAULT ''"),
                ("auto_sent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_off_reason", "TEXT NOT NULL DEFAULT ''"),
                ("auto_off_at", "REAL NOT NULL DEFAULT 0"),
                ("auto_opt_out", "INTEGER NOT NULL DEFAULT 0"),
                ("last_seen", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in group_columns:
                    conn.execute(f"ALTER TABLE groups ADD COLUMN {name} {ddl}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _write(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock, self._connect() as conn:
            return conn.execute(sql, tuple(params))

    def _rows(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return list(conn.execute(sql, tuple(params)).fetchall())

    def now(self) -> float:
        return float(self._now())

    # --- 图库 -------------------------------------------------------------

    def media_path(self, sha256: str, ext: str = ".jpg") -> str:
        """按前两位分目录，单个目录不会堆到几万文件。"""
        bucket = os.path.join(self.media_dir, sha256[:2] or "00")
        return os.path.join(bucket, sha256 + ext)

    def save_media(self, sha256: str, data: bytes, ext: str = ".jpg") -> str:
        path = self.media_path(sha256, ext)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    def get(self, meme_id: int) -> MemeItem | None:
        rows = self._rows("SELECT * FROM memes WHERE id = ?", (int(meme_id),))
        return MemeItem.from_row(rows[0]) if rows else None

    def by_sha(self, sha256: str) -> MemeItem | None:
        rows = self._rows("SELECT * FROM memes WHERE sha256 = ?", (sha256,))
        return MemeItem.from_row(rows[0]) if rows else None

    def add(
        self,
        *,
        sha256: str,
        file_path: str,
        origin: str = "submitted",
        mime: str = "image/jpeg",
        caption: str = "",
        caption_zh: str = "",
        tags: Iterable[str] = (),
        nsfw_level: int = 0,
        contributor: str = "",
        source_url: str = "",
    ) -> tuple[int, bool]:
        """入库。``(id, 是不是新建)``；同一 sha256 只留一行。"""
        tag_text = ",".join(dict.fromkeys(t.strip() for t in tags if t and t.strip()))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memes (sha256, origin, file_path, mime,
                                   caption, caption_zh, tags, nsfw_level, status,
                                   contributor, source_url, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (
                    sha256,
                    origin,
                    file_path,
                    mime,
                    caption,
                    caption_zh,
                    tag_text,
                    int(nsfw_level),
                    STATUS_ACTIVE,
                    contributor,
                    source_url,
                    self.now(),
                ),
            )
            created = cur.rowcount > 0
            row = conn.execute(
                "SELECT id FROM memes WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return (int(row["id"]) if row else 0, created)

    def delete(self, meme_id: int) -> MemeItem | None:
        item = self.get(meme_id)
        if item is None:
            return None
        self._write("DELETE FROM memes WHERE id = ?", (int(meme_id),))
        return item

    def set_status(self, meme_id: int, status: str) -> bool:
        cur = self._write(
            "UPDATE memes SET status = ? WHERE id = ?", (status, int(meme_id))
        )
        return cur.rowcount > 0

    def update_caption(self, meme_id: int, caption: str = None, caption_zh: str = None) -> None:
        sets, params = [], []
        if caption is not None:
            sets.append("caption = ?")
            params.append(caption)
        if caption_zh is not None:
            sets.append("caption_zh = ?")
            params.append(caption_zh)
        if not sets:
            return
        params.append(int(meme_id))
        self._write(f"UPDATE memes SET {', '.join(sets)} WHERE id = ?", params)

    def random_pool(self) -> list[MemeItem]:
        """能发的所有图。图库只有一份，所有群看到的是同一批 ——
        但「谁发过」是按群算的，见 ``deliveries`` 表和 ``seen_by``。"""
        rows = self._rows(
            "SELECT * FROM memes WHERE status = 'active'"
        )
        return [MemeItem.from_row(row) for row in rows]

    def seen_by(self, umo: str) -> dict[int, float]:
        """这个会话发过哪几张、各自**最后一次**是啥时候：``{meme_id: last_at}``。

        用 ``last_at`` 而不是 ``first_at``：``first_at`` 一旦写下就再也不变，
        拿它判断冷热的话，池子发遍之后永远是同一张「最早那回发的」被选中 ——
        实测一条 222 次的推送里同一张图被推了 121 次。``first_at`` 仍然保留，
        它的用途是「这个群第一次看到它是什么时候」。

        ``umo`` 为空表示「没有会话上下文」，返回空 dict（等同于谁都没发过）。
        """
        if not umo:
            return {}
        rows = self._rows(
            "SELECT meme_id, last_at FROM deliveries WHERE umo = ?", (str(umo),)
        )
        return {int(row["meme_id"]): float(row["last_at"] or 0) for row in rows}

    def pick(self, weights: dict, rng=None, umo: str = "") -> MemeItem | None:
        """给一个会话挑一张。传了 ``umo`` 就按本群自己的投递记录去重。"""
        return pick_meme(
            self.random_pool(),
            weights,
            rng,
            seen=self.seen_by(umo) if umo else None,
        )

    def mark_used(self, meme_id: int, umo: str = "") -> None:
        """记账。

        两本账分开记，不能混：
          * ``memes.used_at/used_count`` = **全站**热度（页面统计、卡片上的
            「发过 N 次」都读它）。跨群共享，用来让同一张图别在五个群同时刷屏。
          * ``deliveries`` = **本群**发过没有。挑选只认这本，新图才能保证在
            每个群都先露面一次。

        ``umo`` 为空时只记全局账（没有会话上下文的老调用点）。
        """
        now = self.now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE memes SET used_at = ?, used_count = used_count + 1 WHERE id = ?",
                (now, int(meme_id)),
            )
            if umo:
                conn.execute(
                    """INSERT INTO deliveries (umo, meme_id, first_at, last_at, times)
                       VALUES (?,?,?,?,1)
                       ON CONFLICT(umo, meme_id) DO UPDATE SET
                           last_at = excluded.last_at,
                           times = deliveries.times + 1""",
                    (str(umo), int(meme_id), now, now),
                )

    def recent(self, limit: int = 10) -> list[MemeItem]:
        rows = self._rows(
            "SELECT * FROM memes ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        return [MemeItem.from_row(row) for row in rows]

    def stats(self) -> dict:
        total = self._rows("SELECT COUNT(*) AS n FROM memes")[0]["n"]
        active = self._rows(
            "SELECT COUNT(*) AS n FROM memes WHERE status = 'active'"
        )[0]["n"]
        by_origin_rows = self._rows(
            "SELECT origin, COUNT(*) AS n FROM memes WHERE status = 'active' GROUP BY origin"
        )
        rejected = self._rows("SELECT COUNT(*) AS n FROM rejects")[0]["n"]
        return {
            "total": int(total),
            "active": int(active),
            "by_origin": {row["origin"]: int(row["n"]) for row in by_origin_rows},
            "rejected": int(rejected),
        }

    # --- 审核缓存 ---------------------------------------------------------

    def cached_review(self, sha256: str) -> Review | None:
        rows = self._rows(
            "SELECT review FROM review_cache WHERE sha256 = ?", (sha256,)
        )
        if not rows:
            return None
        return Review.from_json(rows[0]["review"])

    def cache_review(self, sha256: str, review: Review) -> None:
        self._write(
            """INSERT INTO review_cache (sha256, review, created_at) VALUES (?,?,?)
               ON CONFLICT(sha256) DO UPDATE SET review = excluded.review,
                                                 created_at = excluded.created_at""",
            (sha256, review.to_json(), self.now()),
        )

    # --- 拒收记录 ---------------------------------------------------------

    def reject(
        self,
        sha256: str,
        reason: str,
        contributor: str = "",
        *,
        origin: str = "",
        source_url: str = "",
        mime: str = "",
        size: int = 0,
    ) -> None:
        """记一条拒收。

        ``origin`` / ``source_url`` / ``mime`` / ``size`` 是 0.6.4 加的：后台的
        「拒收」页面要靠它们区分「哪个群投的、多大、什么格式」，也方便判断
        一条老记录为什么没有图（0.6.4 之前拒收的图不入盘，只有 sha256）。
        """
        self._write(
            """INSERT INTO rejects
                   (sha256, reason, contributor, created_at, origin, source_url, mime, size)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(sha256) DO UPDATE SET reason = excluded.reason,
                                                 created_at = excluded.created_at,
                                                 origin = excluded.origin,
                                                 source_url = excluded.source_url,
                                                 mime = excluded.mime,
                                                 size = excluded.size""",
            (sha256, reason, contributor, self.now(),
             str(origin or ""), str(source_url or ""), str(mime or ""), int(size or 0)),
        )

    def rejection_reason(self, sha256: str) -> str | None:
        rows = self._rows("SELECT reason FROM rejects WHERE sha256 = ?", (sha256,))
        return rows[0]["reason"] if rows else None

    def clear_reject(self, sha256: str) -> None:
        self._write("DELETE FROM rejects WHERE sha256 = ?", (sha256,))

    def rejected_count(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM rejects")[0]["n"])

    def rejected_page(self, limit: int = 24, offset: int = 0) -> list[dict]:
        """拒收列表，最新的在前。``has_file`` 表示这张的图还在不在盘上。

        0.6.4 之前拒收的图**从来没写进磁盘**（审核判死当场就丢了字节），
        所以那些行 ``has_file`` 恒为 False，页面据此显示「当时没存图」。
        """
        rows = self._rows(
            "SELECT * FROM rejects ORDER BY created_at DESC, sha256 LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )
        return [self._reject_row(row) for row in rows]

    def _reject_row(self, row) -> dict:
        data = dict(row)
        sha = str(data.get("sha256") or "")
        path = self.rejected_media_path(sha, ext_for_mime(str(data.get("mime") or ""))) if sha else ""
        data["has_file"] = bool(path) and os.path.isfile(path)
        data["file_path"] = path if data["has_file"] else ""
        return data

    def rejected_media_path(self, sha256: str, ext: str = ".jpg") -> str:
        """拒收图的落盘路径：``media/rejected/ab/<sha>.<ext>``。

        和正式库的 ``media/ab/`` 分开放，是怕 `clean` 之类的清理逻辑把
        正式图当垃圾扫走，也怕人工翻目录时把没审核过的图当成库里的图。
        """
        bucket = os.path.join(self.media_dir, "rejected", sha256[:2] or "00")
        return os.path.join(bucket, sha256 + (ext or ".jpg"))

    def save_rejected(self, sha256: str, data: bytes, ext: str = ".jpg") -> str:
        """把被拒的图存下来（后台「拒收」页面要能看图判断）。

        存盘失败不算业务失败：拒收记录本身比那张图重要，调用方拿到空字符串
        照常往下走，页面只会显示「图没存下来」。
        """
        path = self.rejected_media_path(sha256, ext)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            return ""
        return path

    def rejected_bytes(self, sha256: str, mime: str = "") -> bytes | None:
        """读回一张拒收图的原始字节（「接收」时要入库）。"""
        path = self.rejected_media_path(sha256, ext_for_mime(mime))
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def drop_rejected_file(self, sha256: str, mime: str = "") -> None:
        path = self.rejected_media_path(sha256, ext_for_mime(mime))
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    # --- 群推送状态 -------------------------------------------------------

    def touch_group(self, umo: str, group_id: str = "") -> None:
        """登记一个见过面的会话。

        ``group_id`` 只在非空时覆盖：从按钮回调这类拿不到群号的地方调过来时，
        如果让它把已有的群号写成空串，主动推送就会拿着空群去挑图。
        """
        if not umo:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO groups (umo, group_id, last_seen) VALUES (?,?,?)
                   ON CONFLICT(umo) DO UPDATE SET
                       group_id = CASE WHEN excluded.group_id <> ''
                                       THEN excluded.group_id ELSE groups.group_id END,
                       last_seen = excluded.last_seen""",
                (umo, group_id or "", self.now()),
            )

    #: 回填时最多认几个群当「发过」。群太多的话回填量会炸，而且那种规模的库
    #: 本来也不需要回填（第一次发就按群记账了）。
    BACKFILL_GROUP_LIMIT = 12

    def backfill_deliveries(self, umo: str, only_if_empty: bool = True) -> int:
        """把**全局**记账回填成**本群**记账，返回写入的行数。

        干什么用：``deliveries`` 是 0.6.3 才加的表，之前所有发送只记了全局
        ``used_at``。如果不管，升级后每个群的 ``seen_by`` 都是空的 —— 于是
        「本群没发过的优先」会把手里那 100 多张图**每个群从头重发一遍**，用户
        看到的就是一晚上全是熟图，比不改还糟。

        所以升级时按「这个群最近发出去过什么」猜一次：取 ``groups.auto_level``
        非空的群（真在自动推的），把全局发过的每一张都记到它头上，时间用
        ``memes.used_at``（全局最久没发的那批，回填后也是本群最久没发的，
        冷热顺序和升级前一致）。没发过的图（``used_at = 0``）不记 —— 那些
        本来就是「谁都没见过」，每个群都该优先发。

        ``only_if_empty=True`` 时，这个群已经有记录就什么都不做（重复升级安全）。
        """
        if not umo:
            return 0
        with self._lock, self._connect() as conn:
            if only_if_empty:
                have = conn.execute(
                    "SELECT 1 FROM deliveries WHERE umo = ? LIMIT 1", (str(umo),)
                ).fetchone()
                if have:
                    return 0
            cur = conn.execute(
                """INSERT OR IGNORE INTO deliveries
                       (umo, meme_id, first_at, last_at, times)
                   SELECT ?, id, used_at, used_at, used_count
                   FROM memes
                   WHERE status = 'active' AND used_count > 0 AND used_at > 0""",
                (str(umo),),
            )
            return int(cur.rowcount or 0)

    def backfill_all_groups(self) -> dict[str, int]:
        """给所有「真在自动推」的群各回填一次。返回 ``{umo: 写了几行}``。"""
        rows = self._rows(
            "SELECT umo FROM groups WHERE umo != '' AND auto_level != '' "
            "ORDER BY last_seen DESC LIMIT ?",
            (int(self.BACKFILL_GROUP_LIMIT),),
        )
        done: dict[str, int] = {}
        for row in rows:
            umo = str(row["umo"] or "")
            if not umo:
                continue
            n = self.backfill_deliveries(umo)
            if n:
                done[umo] = n
        return done

    def recent_group_umos(self, limit: int = 5) -> list[str]:
        """最近说过话的群（有 umo 的那些）。卡片自检需要一个真群来拿 client。"""
        rows = self._rows(
            "SELECT umo FROM groups WHERE umo != '' ORDER BY last_push DESC, umo LIMIT ?",
            (int(limit),),
        )
        return [row["umo"] for row in rows]

    # --- 每小时自动推（自动搬石） ----------------------------------------

    def set_auto_level(
        self,
        umo: str,
        level: str,
        *,
        opt_out: bool = True,
        reason: str = "",
    ) -> None:
        """开/关/换档。``level`` 空串 = 关掉。

        关掉时有两种，必须分清楚，否则「批量铺开试一遍」会变成死循环：

        * ``opt_out=True``（默认，用户点「取消自动」/ 管理员命令）—— 打上
          ``auto_opt_out`` 标记，之后批量铺开**不许**再把它打开；
        * ``opt_out=False, reason="no_permission"`` —— 是自动关闭：记下墓碑
          （原因 + 时间），之后同样不再重开，但页面上要能看出「不是人关的」。

        顺便把「今天已发几条」清零：下次重新订阅应该拿到完整的一天额度。
        """
        if not umo:
            return
        self.touch_group(umo)
        level = str(level or "").strip().lower()
        if level and level not in ("low", "mid", "high"):
            raise ValueError(f"不认识的档位：{level}")
        if level:
            self._write(
                """UPDATE groups SET auto_level = ?, auto_since = ?,
                       auto_off_reason = '', auto_off_at = 0, auto_opt_out = 0
                   WHERE umo = ?""",
                (level, self.now(), umo),
            )
            return
        if opt_out:
            self._write(
                """UPDATE groups SET auto_level = '', auto_since = 0,
                       auto_sent_date = '', auto_sent_count = 0,
                       auto_opt_out = 1, auto_off_at = ?, auto_off_reason = ?
                   WHERE umo = ?""",
                (self.now(), str(reason or "manual"), umo),
            )
        else:
            self._write(
                """UPDATE groups SET auto_level = '', auto_since = 0,
                       auto_sent_date = '', auto_sent_count = 0,
                       auto_opt_out = 0, auto_off_at = ?, auto_off_reason = ?
                   WHERE umo = ?""",
                (self.now(), str(reason or "unknown"), umo),
            )

    def auto_groups(self) -> list[dict]:
        """所有开了自动推送的群。"""
        rows = self._rows("SELECT * FROM groups WHERE auto_level != ''")
        return [dict(row) for row in rows]

    def auto_offed_groups(self, limit: int = 50) -> list[dict]:
        """被自动关掉的群（页面上要看得到，否则「怎么自己不推了」无从查起）。"""
        rows = self._rows(
            """SELECT * FROM groups
               WHERE auto_off_reason != '' AND auto_opt_out = 0
               ORDER BY auto_off_at DESC LIMIT ?""",
            (int(limit),),
        )
        return [dict(row) for row in rows]

    def bootstrap_auto(self, level: str = "low", days: int = 30) -> list[str]:
        """批量给「还没订阅过」的群先开一档（用户要的「先测试这个功能」）。

        三条硬条件，任意一条不满足就跳过：
          1. ``auto_level`` 为空 —— 已经订阅的群不动；
          2. ``auto_opt_out = 0`` —— 人明确关过的不许偷偷重开；
          3. ``auto_off_reason = ''`` —— 自动关过（没权限/不在群）的也不再试。

        再按「最近出现过」过滤：平台没有「查这个群开没开主动消息」的接口，我们
        只能靠「抽中之后真发一次、失败再关」来淘汰 —— 所以至少别去碰那些三个月
        没动静的死群。``days=0`` 表示不限。
        返回真正开起来的 umo 列表。
        """
        level = str(level or "low")
        if level not in ("low", "mid", "high"):
            raise ValueError(f"不认识的档位：{level}")
        cutoff = self.now() - int(days) * 86400 if days and days > 0 else 0.0
        rows = self._rows(
            """SELECT umo, last_seen, last_push FROM groups
               WHERE umo != '' AND auto_level = ''
                 AND auto_opt_out = 0 AND auto_off_reason = ''""",
        )
        opened: list[str] = []
        for row in rows:
            seen = max(float(row["last_seen"] or 0), float(row["last_push"] or 0))
            if cutoff and seen < cutoff:
                continue
            self.set_auto_level(row["umo"], level)
            opened.append(row["umo"])
        return opened

    def note_auto_push(self, umo: str, *, day_key: str, ok: bool = True) -> int:
        """记一次自动推送尝试，返回「今天已发几条」。

        只统计**真的发出去了**的次数（``ok=False`` 时数字不动）：发失败的那次不该
        吃掉当天的额度，否则群关了主动消息之后，第二天限额会莫名其妙地少几条。
        """
        if not umo:
            return 0
        self.touch_group(umo)  # 没登记过的会话也要能计数（否则 UPDATE 命中 0 行，静默丢数）
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT auto_sent_count, auto_sent_date FROM groups WHERE umo = ?", (umo,)
            ).fetchone()
            same_day = bool(row) and row["auto_sent_date"] == day_key
            count = int(row["auto_sent_count"] or 0) if same_day else 0
            if ok:
                count += 1
            conn.execute(
                "UPDATE groups SET auto_sent_count = ?, auto_sent_date = ? WHERE umo = ?",
                (count, day_key, umo),
            )
            return count

    def group_state(self, umo: str) -> dict | None:
        rows = self._rows("SELECT * FROM groups WHERE umo = ?", (umo,))
        return dict(rows[0]) if rows else None

    def note_push(
        self,
        umo: str,
        *,
        ok: bool,
        day_key: str = "",
        blocked_until: float = 0.0,
    ) -> None:
        """记一次推送结果。

        ``push_count`` 是「今天已经推了几条」，跨天自动归零；``blocked_until``
        只在收到 QQ 的「无主动消息权限」时置位，之后 24 小时内不再骚扰那个群。
        """
        if not umo:
            return
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT push_count, push_date FROM groups WHERE umo = ?", (umo,)
            ).fetchone()
            same_day = bool(row) and row["push_date"] == day_key
            count = int(row["push_count"] or 0) if same_day else 0
            if ok:
                count += 1
            conn.execute(
                """UPDATE groups
                   SET last_push = ?, push_count = ?, push_date = ?,
                       blocked_until = CASE WHEN ? > 0 THEN ? ELSE blocked_until END
                   WHERE umo = ?""",
                (
                    self.now() if ok else 0.0,
                    count,
                    day_key,
                    float(blocked_until),
                    float(blocked_until),
                    umo,
                ),
            )

    # --- 恶意投稿黑名单 ---------------------------------------------------

    def add_blacklist(
        self,
        sender_id: str,
        *,
        origin: str = "",
        group_id: str = "",
        reason: str = "",
        evidence_sha: str = "",
        evidence_path: str = "",
    ) -> bool:
        """把一个人拉进「不许投稿」名单。已经在了就累计次数、刷新理由。"""
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            return False
        now = self.now()
        self._write(
            """INSERT INTO blacklist
                   (sender_id, origin, group_id, reason, evidence_sha, evidence_path, hits, created_at)
               VALUES (?,?,?,?,?,?,1,?)
               ON CONFLICT(sender_id) DO UPDATE SET
                   reason = excluded.reason,
                   evidence_sha = CASE WHEN excluded.evidence_sha != ''
                                       THEN excluded.evidence_sha ELSE blacklist.evidence_sha END,
                   evidence_path = CASE WHEN excluded.evidence_path != ''
                                        THEN excluded.evidence_path ELSE blacklist.evidence_path END,
                   hits = blacklist.hits + 1""",
            (sender_id, origin, group_id, reason, evidence_sha, evidence_path, now),
        )
        return True

    def blacklist_entry(self, sender_id: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM blacklist WHERE sender_id = ?", (str(sender_id or ""),)
        )
        return dict(rows[0]) if rows else None

    def is_blacklisted(self, sender_id: str) -> bool:
        return self.blacklist_entry(sender_id) is not None

    def remove_blacklist(self, sender_id: str) -> bool:
        cursor = self._write(
            "DELETE FROM blacklist WHERE sender_id = ?", (str(sender_id or ""),)
        )
        return bool(cursor.rowcount)

    def list_blacklist(self, limit: int = 50) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM blacklist ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
        return [dict(row) for row in rows]

    # --- 卡片素材额度 -----------------------------------------------------

    def add_card_material(
        self,
        image_id: str,
        *,
        meme_id: int = 0,
        origin: str = "",
        sha256: str = "",
        url: str = "",
    ) -> None:
        image_id = str(image_id or "").strip()
        if not image_id:
            return
        self._write(
            """INSERT INTO card_materials
                   (image_id, meme_id, origin, created_at, sha256, url, last_used)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(image_id) DO UPDATE SET
                   created_at = excluded.created_at,
                   sha256 = excluded.sha256,
                   url = excluded.url,
                   last_used = excluded.last_used""",
            (
                image_id,
                int(meme_id),
                origin,
                self.now(),
                sha256,
                url,
                self.now(),
            ),
        )

    def material_for_sha(self, sha256: str) -> dict | None:
        """同一张图传过一次就认回来，别再占一格额度。

        QQ 素材库是「一张引用用很多次」，不是「一条消息一格」。所以按内容
        sha256 复用，是这套额度管理里最省的一招：库里 30 张图，最多只占 30 格。
        """
        if not sha256:
            return None
        rows = self._rows(
            """SELECT * FROM card_materials WHERE sha256 = ?
               ORDER BY last_used DESC LIMIT 1""",
            (sha256,),
        )
        return dict(rows[0]) if rows else None

    def touch_material(self, image_id: str) -> None:
        self._write(
            "UPDATE card_materials SET last_used = ? WHERE image_id = ?",
            (self.now(), str(image_id)),
        )

    def drop_material_by_sha(self, sha256: str) -> None:
        self._write("DELETE FROM card_materials WHERE sha256 = ?", (str(sha256),))

    def card_material_ids(self) -> list[str]:
        rows = self._rows(
            "SELECT image_id FROM card_materials ORDER BY COALESCE(last_used, created_at) ASC"
        )
        return [row["image_id"] for row in rows]

    def drop_card_material(self, image_id: str) -> None:
        self._write("DELETE FROM card_materials WHERE image_id = ?", (str(image_id),))

    # --- 杂项 -------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        rows = self._rows("SELECT v FROM meta WHERE k = ?", (key,))
        return rows[0]["v"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._write(
            """INSERT INTO meta (k, v) VALUES (?,?)
               ON CONFLICT(k) DO UPDATE SET v = excluded.v""",
            (key, str(value)),
        )
