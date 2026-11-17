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
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    STATUS_ACTIVE,
    MemeItem,
    Review,
)
from .select import pick_meme

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256      TEXT NOT NULL UNIQUE,
    origin      TEXT NOT NULL DEFAULT 'submitted',
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

CREATE TABLE IF NOT EXISTS groups (
    umo           TEXT PRIMARY KEY,
    group_id      TEXT NOT NULL DEFAULT '',
    daily_enabled INTEGER NOT NULL DEFAULT 0,
    daily_slots   TEXT NOT NULL DEFAULT '',
    blocked_until REAL NOT NULL DEFAULT 0,
    last_push     REAL NOT NULL DEFAULT 0,
    push_count    INTEGER NOT NULL DEFAULT 0,
    push_date     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);
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
        scope: str = SCOPE_GROUP,
        group_id: str = "",
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
                INSERT INTO memes (sha256, origin, scope, group_id, file_path, mime,
                                   caption, caption_zh, tags, nsfw_level, status,
                                   contributor, source_url, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (
                    sha256,
                    origin,
                    scope,
                    group_id,
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

    def random_pool(self, group_id: str = "") -> list[MemeItem]:
        """本群能发的所有图：全局图 + 本群自己的图。"""
        rows = self._rows(
            """
            SELECT * FROM memes
            WHERE status = 'active'
              AND (scope = ? OR (scope = ? AND group_id = ?))
            """,
            (SCOPE_GLOBAL, SCOPE_GROUP, group_id),
        )
        return [MemeItem.from_row(row) for row in rows]

    def pick(self, group_id: str, weights: dict, rng=None) -> MemeItem | None:
        return pick_meme(self.random_pool(group_id), weights, rng)

    def mark_used(self, meme_id: int) -> None:
        self._write(
            "UPDATE memes SET used_at = ?, used_count = used_count + 1 WHERE id = ?",
            (self.now(), int(meme_id)),
        )

    def recent(self, limit: int = 10, group_id: str = "") -> list[MemeItem]:
        if group_id:
            rows = self._rows(
                """SELECT * FROM memes
                   WHERE scope = ? OR (scope = ? AND group_id = ?)
                   ORDER BY id DESC LIMIT ?""",
                (SCOPE_GLOBAL, SCOPE_GROUP, group_id, int(limit)),
            )
        else:
            rows = self._rows(
                "SELECT * FROM memes ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        return [MemeItem.from_row(row) for row in rows]

    def stats(self, group_id: str = "") -> dict:
        total = self._rows("SELECT COUNT(*) AS n FROM memes")[0]["n"]
        group_total = self._rows(
            """SELECT COUNT(*) AS n FROM memes
               WHERE status = 'active'
                 AND (scope = ? OR (scope = ? AND group_id = ?))""",
            (SCOPE_GLOBAL, SCOPE_GROUP, group_id),
        )[0]["n"]
        by_origin_rows = self._rows(
            "SELECT origin, COUNT(*) AS n FROM memes GROUP BY origin"
        )
        rejected = self._rows("SELECT COUNT(*) AS n FROM rejects")[0]["n"]
        return {
            "total": int(total),
            "group_total": int(group_total),
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

    def reject(self, sha256: str, reason: str, contributor: str = "") -> None:
        self._write(
            """INSERT INTO rejects (sha256, reason, contributor, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(sha256) DO UPDATE SET reason = excluded.reason,
                                                 created_at = excluded.created_at""",
            (sha256, reason, contributor, self.now()),
        )

    def rejection_reason(self, sha256: str) -> str | None:
        rows = self._rows("SELECT reason FROM rejects WHERE sha256 = ?", (sha256,))
        return rows[0]["reason"] if rows else None

    def clear_reject(self, sha256: str) -> None:
        self._write("DELETE FROM rejects WHERE sha256 = ?", (sha256,))

    def rejected_count(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM rejects")[0]["n"])

    # --- 群推送状态 -------------------------------------------------------

    def touch_group(self, umo: str, group_id: str = "") -> None:
        """登记一个见过面的会话。

        ``group_id`` 只在非空时覆盖：``set_daily`` 这类从别处调过来的地方
        拿不到群号，如果让它把已有的群号写成空串，定时推送就会去一个空群的
        池子里挑图，然后一张也挑不到——静默地什么都不发。
        """
        if not umo:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO groups (umo, group_id) VALUES (?,?)
                   ON CONFLICT(umo) DO UPDATE SET group_id =
                       CASE WHEN excluded.group_id <> '' THEN excluded.group_id
                            ELSE groups.group_id END""",
                (umo, group_id or ""),
            )

    def set_daily(self, umo: str, enabled: bool, slots: str = "") -> None:
        self.touch_group(umo)
        self._write(
            "UPDATE groups SET daily_enabled = ?, daily_slots = ? WHERE umo = ?",
            (1 if enabled else 0, slots or "", umo),
        )

    def daily_groups(self) -> list[dict]:
        rows = self._rows("SELECT * FROM groups WHERE daily_enabled = 1")
        return [dict(row) for row in rows]

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
