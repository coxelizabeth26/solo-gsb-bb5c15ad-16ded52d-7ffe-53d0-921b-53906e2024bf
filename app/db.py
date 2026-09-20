"""SQLite 持久化层：片段接收、修订裁决、水位线推进与定稿。

并发模型：
- 进程内用一把互斥锁串行化写事务；
- 事务统一以 BEGIN IMMEDIATE 开启，跨进程（多 worker）场景下由
  SQLite 单写者语义 + busy_timeout 保证正确性；
- 「接收片段 + 推进水位线 + 落库定稿」在同一个事务内完成，
  因此不会重复定稿，也不会越过缺口定稿。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel   TEXT PRIMARY KEY,
    watermark INTEGER NOT NULL DEFAULT 0
);

-- 尚未进入连续前缀的暂存片段（可被更高修订号覆盖）
CREATE TABLE IF NOT EXISTS pending_fragments (
    channel     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, seq)
);

-- 定稿字幕：一旦写入不可修改、不可删除
CREATE TABLE IF NOT EXISTS finalized_fragments (
    channel      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    revision     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    finalized_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, seq)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None：自动提交模式，事务由显式 BEGIN/COMMIT 控制
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def submit_fragment(
        self, channel: str, seq: int, revision: int, text: str
    ) -> dict[str, Any]:
        """提交一个字幕片段，并在同一事务内推进水位线。

        返回：
            accepted:  本次提交是否生效（被暂存或触发了定稿）。
                       seq 已定稿、或修订号不高于已暂存版本时为 False。
            watermark: 提交完成后的最新水位线。
            released:  本次提交触发定稿的连续片段（可能为空）。
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(
                    "INSERT INTO channels (channel, watermark) VALUES (?, 0) "
                    "ON CONFLICT(channel) DO NOTHING",
                    (channel,),
                )
                watermark = cur.execute(
                    "SELECT watermark FROM channels WHERE channel = ?", (channel,)
                ).fetchone()["watermark"]

                accepted = False
                if seq > watermark:
                    # 同序号仅接受更高修订号；低于等于现有修订号则本次不生效
                    cur.execute(
                        """
                        INSERT INTO pending_fragments (channel, seq, revision, text)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(channel, seq) DO UPDATE
                            SET revision = excluded.revision,
                                text     = excluded.text
                            WHERE excluded.revision > pending_fragments.revision
                        """,
                        (channel, seq, revision, text),
                    )
                    accepted = cur.rowcount > 0
                # seq <= watermark：该序号已定稿，不可再修改，直接拒绝

                # 从水位线 +1 起取连续前缀，缺口处停止
                rows = cur.execute(
                    "SELECT seq, revision, text FROM pending_fragments "
                    "WHERE channel = ? AND seq > ? ORDER BY seq",
                    (channel, watermark),
                ).fetchall()
                released: list[sqlite3.Row] = []
                next_seq = watermark + 1
                for row in rows:
                    if row["seq"] != next_seq:
                        break
                    released.append(row)
                    next_seq += 1

                if released:
                    cur.executemany(
                        "INSERT INTO finalized_fragments "
                        "(channel, seq, revision, text) VALUES (?, ?, ?, ?)",
                        [
                            (channel, r["seq"], r["revision"], r["text"])
                            for r in released
                        ],
                    )
                    cur.executemany(
                        "DELETE FROM pending_fragments WHERE channel = ? AND seq = ?",
                        [(channel, r["seq"]) for r in released],
                    )
                    watermark = released[-1]["seq"]
                    cur.execute(
                        "UPDATE channels SET watermark = ? WHERE channel = ?",
                        (watermark, channel),
                    )

                cur.execute("COMMIT")
                return {
                    "channel": channel,
                    "seq": seq,
                    "accepted": accepted,
                    "watermark": watermark,
                    "released": [dict(r) for r in released],
                }
            except Exception:
                self._conn.rollback()
                raise

    def get_finalized(
        self, channel: str, from_seq: int = 1, limit: int | None = None
    ) -> dict[str, Any]:
        """查询频道已定稿字幕（从 from_seq 起，按序号升序）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT watermark FROM channels WHERE channel = ?", (channel,)
            ).fetchone()
            watermark = row["watermark"] if row else 0
            sql = (
                "SELECT seq, revision, text, finalized_at FROM finalized_fragments "
                "WHERE channel = ? AND seq >= ? ORDER BY seq"
            )
            params: list[Any] = [channel, from_seq]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            return {
                "channel": channel,
                "watermark": watermark,
                "fragments": [dict(r) for r in rows],
            }

    def get_status(self, channel: str) -> dict[str, Any]:
        """频道状态：水位线、暂存数、定稿数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT watermark FROM channels WHERE channel = ?", (channel,)
            ).fetchone()
            watermark = row["watermark"] if row else 0
            pending = self._conn.execute(
                "SELECT COUNT(*) AS c FROM pending_fragments WHERE channel = ?",
                (channel,),
            ).fetchone()["c"]
            finalized = self._conn.execute(
                "SELECT COUNT(*) AS c FROM finalized_fragments WHERE channel = ?",
                (channel,),
            ).fetchone()["c"]
            return {
                "channel": channel,
                "watermark": watermark,
                "pending": pending,
                "finalized": finalized,
            }
