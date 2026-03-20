from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .models import NormalizedItem


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    link_title TEXT NOT NULL,
    link_url TEXT NOT NULL,
    author_screen_name TEXT NOT NULL,
    author_username TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    topic_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    has_images INTEGER NOT NULL DEFAULT 0,
    has_video INTEGER NOT NULL DEFAULT 0,
    has_audio INTEGER NOT NULL DEFAULT 0,
    domain TEXT NOT NULL DEFAULT '',
    is_collected INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_last_seen_at ON items(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_is_collected ON items(is_collected, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_domain ON items(domain);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_inserted INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    items_marked_removed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS feishu_doc_sync (
    item_id TEXT PRIMARY KEY,
    doc_block_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_feishu_doc_sync_status ON feishu_doc_sync(status, synced_at DESC);

CREATE TABLE IF NOT EXISTS feishu_delivery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    jike_inserted INTEGER NOT NULL DEFAULT 0,
    jike_updated INTEGER NOT NULL DEFAULT 0,
    doc_written INTEGER NOT NULL DEFAULT 0,
    doc_failed INTEGER NOT NULL DEFAULT 0,
    webhook_sent INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5 (
    item_id UNINDEXED,
    title,
    content,
    link_title,
    link_url,
    author,
    topic,
    search_blob,
    tokenize='unicode61'
);
"""


@dataclass
class SearchRow:
    item_id: str
    score: float
    title: str
    content: str
    link_url: str
    author_screen_name: str
    topic_name: str
    created_at: str
    domain: str


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_sync_run(self, started_at: str, mode: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO sync_runs (started_at, mode, status) VALUES (?, ?, ?)",
            (started_at, mode, "running"),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        items_seen: int,
        items_inserted: int,
        items_updated: int,
        items_marked_removed: int = 0,
        note: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs
            SET finished_at = ?, status = ?, items_seen = ?, items_inserted = ?,
                items_updated = ?, items_marked_removed = ?, note = ?
            WHERE id = ?
            """,
            (
                finished_at,
                status,
                items_seen,
                items_inserted,
                items_updated,
                items_marked_removed,
                note,
                run_id,
            ),
        )
        self.conn.commit()

    def item_exists(self, item_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM items WHERE id = ? LIMIT 1", (item_id,)).fetchone()
        return row is not None

    def should_fetch_detail(self, item_id: str, item_type: str) -> bool:
        detail_markers = {
            "ORIGINAL_POST": '"shouldShowCommentTip":',
            "REPOST": '"liked":',
        }
        marker = detail_markers.get(item_type)
        if not marker:
            return False

        row = self.conn.execute("SELECT raw_json FROM items WHERE id = ? LIMIT 1", (item_id,)).fetchone()
        if row is None:
            return True
        raw_json = row["raw_json"] or ""
        return marker not in raw_json

    def upsert_item(self, item: NormalizedItem) -> str:
        existing = self.conn.execute(
            "SELECT id, first_seen_at, raw_json FROM items WHERE id = ?",
            (item.item_id,),
        ).fetchone()

        if existing is None:
            first_seen_at = item.first_seen_at
            status = "inserted"
        else:
            first_seen_at = existing["first_seen_at"]
            status = "updated" if existing["raw_json"] != item.raw_json else "unchanged"

        self.conn.execute(
            """
            INSERT INTO items (
                id, item_type, title, content, link_title, link_url, author_screen_name,
                author_username, topic_id, topic_name, source_url, created_at, first_seen_at,
                last_seen_at, has_images, has_video, has_audio, domain, is_collected, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                item_type = excluded.item_type,
                title = excluded.title,
                content = excluded.content,
                link_title = excluded.link_title,
                link_url = excluded.link_url,
                author_screen_name = excluded.author_screen_name,
                author_username = excluded.author_username,
                topic_id = excluded.topic_id,
                topic_name = excluded.topic_name,
                source_url = excluded.source_url,
                created_at = excluded.created_at,
                first_seen_at = items.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                has_images = excluded.has_images,
                has_video = excluded.has_video,
                has_audio = excluded.has_audio,
                domain = excluded.domain,
                is_collected = 1,
                raw_json = excluded.raw_json
            """,
            (
                item.item_id,
                item.item_type,
                item.title,
                item.content,
                item.link_title,
                item.link_url,
                item.author_screen_name,
                item.author_username,
                item.topic_id,
                item.topic_name,
                item.source_url,
                item.created_at,
                first_seen_at,
                item.last_seen_at,
                item.has_images,
                item.has_video,
                item.has_audio,
                item.domain,
                item.raw_json,
            ),
        )

        self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item.item_id,))
        self.conn.execute(
            """
            INSERT INTO items_fts (item_id, title, content, link_title, link_url, author, topic, search_blob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.item_id,
                item.title,
                item.content,
                item.link_title,
                item.link_url,
                " ".join(part for part in [item.author_screen_name, item.author_username] if part),
                item.topic_name,
                item.search_blob,
            ),
        )
        self.conn.commit()
        return status

    def mark_missing_items_as_removed(self, seen_ids: Sequence[str]) -> int:
        if not seen_ids:
            return 0

        placeholders = ",".join("?" for _ in seen_ids)
        cursor = self.conn.execute(
            f"""
            UPDATE items
            SET is_collected = 0
            WHERE is_collected = 1 AND id NOT IN ({placeholders})
            """,
            list(seen_ids),
        )
        self.conn.commit()
        return cursor.rowcount

    def search(self, match_query: str, limit: int = 10) -> List[SearchRow]:
        rows = self.conn.execute(
            """
            SELECT
                items_fts.item_id AS item_id,
                bm25(items_fts) AS score,
                items.title AS title,
                items.content AS content,
                items.link_url AS link_url,
                items.author_screen_name AS author_screen_name,
                items.topic_name AS topic_name,
                items.created_at AS created_at,
                items.domain AS domain
            FROM items_fts
            JOIN items ON items.id = items_fts.item_id
            WHERE items_fts MATCH ? AND items.is_collected = 1
            ORDER BY score
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        return [SearchRow(**dict(row)) for row in rows]

    def fetch_recent_items(self, days: int = 30, limit: Optional[int] = None) -> List[sqlite3.Row]:
        sql = """
            SELECT *
            FROM items
            WHERE is_collected = 1
              AND datetime(created_at) >= datetime('now', ?)
            ORDER BY datetime(created_at) DESC
        """
        params: List[object] = [f"-{days} days"]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def fetch_all_active_items(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE is_collected = 1 ORDER BY datetime(created_at) DESC"
        ).fetchall()

    def fetch_stats(self) -> sqlite3.Row:
        return self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_items,
                COALESCE(SUM(CASE WHEN is_collected = 1 THEN 1 ELSE 0 END), 0) AS active_items,
                COALESCE(SUM(CASE WHEN has_images = 1 THEN 1 ELSE 0 END), 0) AS image_items,
                COALESCE(SUM(CASE WHEN has_video = 1 THEN 1 ELSE 0 END), 0) AS video_items,
                COALESCE(SUM(CASE WHEN has_audio = 1 THEN 1 ELSE 0 END), 0) AS audio_items,
                MIN(created_at) AS oldest_item_at,
                MAX(created_at) AS newest_item_at
            FROM items
            """
        ).fetchone()

    def fetch_unsynced_items(self, limit: Optional[int] = None) -> List[sqlite3.Row]:
        sql = """
            SELECT items.*
            FROM items
            LEFT JOIN feishu_doc_sync ON feishu_doc_sync.item_id = items.id
            WHERE items.is_collected = 1
              AND (
                feishu_doc_sync.item_id IS NULL
                OR feishu_doc_sync.status != 'synced'
              )
            ORDER BY datetime(items.created_at) ASC, items.id ASC
        """
        params: List[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def mark_item_doc_synced(self, item_id: str, synced_at: str, doc_block_id: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO feishu_doc_sync (item_id, doc_block_id, status, synced_at, last_error)
            VALUES (?, ?, 'synced', ?, '')
            ON CONFLICT(item_id) DO UPDATE SET
                doc_block_id = excluded.doc_block_id,
                status = 'synced',
                synced_at = excluded.synced_at,
                last_error = ''
            """,
            (item_id, doc_block_id, synced_at),
        )
        self.conn.commit()

    def mark_item_doc_failed(self, item_id: str, synced_at: str, error_message: str) -> None:
        self.conn.execute(
            """
            INSERT INTO feishu_doc_sync (item_id, doc_block_id, status, synced_at, last_error)
            VALUES (?, '', 'failed', ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                status = 'failed',
                synced_at = excluded.synced_at,
                last_error = excluded.last_error
            """,
            (item_id, synced_at, error_message),
        )
        self.conn.commit()

    def count_synced_doc_items(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM feishu_doc_sync WHERE status = 'synced'"
        ).fetchone()
        return int(row["count"])

    def start_delivery_run(self, started_at: str, mode: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO feishu_delivery_runs (started_at, mode, status) VALUES (?, ?, ?)",
            (started_at, mode, "running"),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_delivery_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        jike_inserted: int,
        jike_updated: int,
        doc_written: int,
        doc_failed: int,
        webhook_sent: bool,
        note: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE feishu_delivery_runs
            SET finished_at = ?, status = ?, jike_inserted = ?, jike_updated = ?,
                doc_written = ?, doc_failed = ?, webhook_sent = ?, note = ?
            WHERE id = ?
            """,
            (
                finished_at,
                status,
                jike_inserted,
                jike_updated,
                doc_written,
                doc_failed,
                1 if webhook_sent else 0,
                note,
                run_id,
            ),
        )
        self.conn.commit()
