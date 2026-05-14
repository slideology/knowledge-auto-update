from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .models import NormalizedItem, build_search_blob


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'jike',
    source_item_id TEXT NOT NULL DEFAULT '',
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    link_title TEXT NOT NULL,
    link_url TEXT NOT NULL,
    author_screen_name TEXT NOT NULL,
    author_username TEXT NOT NULL,
    source_author TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL DEFAULT '',
    topic_id TEXT NOT NULL,
    topic_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    has_images INTEGER NOT NULL DEFAULT 0,
    has_video INTEGER NOT NULL DEFAULT 0,
    has_audio INTEGER NOT NULL DEFAULT 0,
    domain TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    is_collected INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_type TEXT NOT NULL DEFAULT 'jike',
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

CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS kb_embeddings (
    chunk_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_vector TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedded_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, embedding_model),
    FOREIGN KEY(chunk_id) REFERENCES kb_chunks(chunk_id)
);

CREATE TABLE IF NOT EXISTS kb_index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_indexed INTEGER NOT NULL DEFAULT 0,
    items_failed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daily_digest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    jike_item_count INTEGER NOT NULL DEFAULT 0,
    aihot_item_count INTEGER NOT NULL DEFAULT 0,
    summary_markdown TEXT NOT NULL DEFAULT '',
    webhook_sent INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5 (
    item_id UNINDEXED,
    title,
    content,
    link_title,
    link_url,
    author,
    topic,
    source_type,
    search_blob,
    tokenize='unicode61'
);
"""


ITEM_COLUMNS = {
    "source_type": "TEXT NOT NULL DEFAULT 'jike'",
    "source_item_id": "TEXT NOT NULL DEFAULT ''",
    "source_author": "TEXT NOT NULL DEFAULT ''",
    "source_channel": "TEXT NOT NULL DEFAULT ''",
    "canonical_url": "TEXT NOT NULL DEFAULT ''",
    "published_at": "TEXT NOT NULL DEFAULT ''",
    "collected_at": "TEXT NOT NULL DEFAULT ''",
    "tags_json": "TEXT NOT NULL DEFAULT '[]'",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}


@dataclass
class SearchRow:
    item_id: str
    score: float
    source_type: str
    title: str
    content: str
    link_url: str
    author_screen_name: str
    topic_name: str
    source_channel: str
    created_at: str
    published_at: str
    domain: str


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _table_columns(self, table: str) -> Dict[str, sqlite3.Row]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"]: row for row in rows}

    def _index_exists(self, index_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        return row is not None

    def _migrate_schema(self) -> None:
        item_columns = self._table_columns("items")
        for name, definition in ITEM_COLUMNS.items():
            if name not in item_columns:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")

        sync_columns = self._table_columns("sync_runs")
        if "source_type" not in sync_columns:
            self.conn.execute("ALTER TABLE sync_runs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'jike'")

        self.conn.execute("UPDATE items SET source_type = 'jike' WHERE source_type = ''")
        self.conn.execute("UPDATE items SET source_item_id = id WHERE source_item_id = ''")
        self.conn.execute(
            """
            UPDATE items
            SET source_author = CASE
                WHEN source_author != '' THEN source_author
                WHEN author_screen_name != '' THEN author_screen_name
                ELSE author_username
            END
            WHERE source_author = ''
            """
        )
        self.conn.execute("UPDATE items SET source_channel = topic_name WHERE source_channel = ''")
        self.conn.execute("UPDATE items SET published_at = created_at WHERE published_at = ''")
        self.conn.execute("UPDATE items SET collected_at = first_seen_at WHERE collected_at = ''")
        self.conn.execute(
            """
            UPDATE items
            SET canonical_url = CASE
                WHEN link_url != '' THEN link_url
                ELSE source_url
            END
            WHERE canonical_url = ''
            """
        )
        self.conn.execute("UPDATE items SET tags_json = '[]' WHERE tags_json = ''")
        self.conn.execute("UPDATE items SET metadata_json = '{}' WHERE metadata_json = ''")
        self.conn.execute("UPDATE sync_runs SET source_type = 'jike' WHERE source_type = ''")

        if not self._index_exists("idx_items_created_at"):
            self.conn.execute("CREATE INDEX idx_items_created_at ON items(created_at DESC)")
        if not self._index_exists("idx_items_published_at"):
            self.conn.execute("CREATE INDEX idx_items_published_at ON items(published_at DESC)")
        if not self._index_exists("idx_items_last_seen_at"):
            self.conn.execute("CREATE INDEX idx_items_last_seen_at ON items(last_seen_at DESC)")
        if not self._index_exists("idx_items_is_collected"):
            self.conn.execute("CREATE INDEX idx_items_is_collected ON items(is_collected, created_at DESC)")
        if not self._index_exists("idx_items_domain"):
            self.conn.execute("CREATE INDEX idx_items_domain ON items(domain)")
        if not self._index_exists("idx_items_source_type"):
            self.conn.execute("CREATE INDEX idx_items_source_type ON items(source_type, published_at DESC)")
        if not self._index_exists("idx_items_source_unique"):
            self.conn.execute("CREATE UNIQUE INDEX idx_items_source_unique ON items(source_type, source_item_id)")
        if not self._index_exists("idx_feishu_doc_sync_status"):
            self.conn.execute("CREATE INDEX idx_feishu_doc_sync_status ON feishu_doc_sync(status, synced_at DESC)")
        if not self._index_exists("idx_kb_chunks_item"):
            self.conn.execute("CREATE UNIQUE INDEX idx_kb_chunks_item ON kb_chunks(item_id, chunk_index)")
        if not self._index_exists("idx_kb_chunks_source"):
            self.conn.execute("CREATE INDEX idx_kb_chunks_source ON kb_chunks(source_type, updated_at DESC)")
        kb_chunk_columns = self._table_columns("kb_chunks")
        if "dedupe_key" not in kb_chunk_columns:
            self.conn.execute("ALTER TABLE kb_chunks ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''")
        if not self._index_exists("idx_kb_chunks_dedupe"):
            self.conn.execute("CREATE INDEX idx_kb_chunks_dedupe ON kb_chunks(source_type, dedupe_key)")
        if not self._index_exists("idx_kb_embeddings_model"):
            self.conn.execute("CREATE INDEX idx_kb_embeddings_model ON kb_embeddings(embedding_model, embedded_at DESC)")

        fts_columns = self._table_columns("items_fts")
        if "source_type" not in fts_columns:
            self._rebuild_items_fts()

    def _rebuild_items_fts(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS items_fts")
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE items_fts USING fts5 (
                item_id UNINDEXED,
                title,
                content,
                link_title,
                link_url,
                author,
                topic,
                source_type,
                search_blob,
                tokenize='unicode61'
            )
            """
        )
        rows = self.conn.execute(
            """
            SELECT
                id, title, content, link_title, link_url, author_screen_name, author_username,
                source_author, topic_name, source_channel, source_type
            FROM items
            """
        ).fetchall()
        for row in rows:
            author = " ".join(
                part
                for part in [row["author_screen_name"], row["author_username"], row["source_author"]]
                if part
            )
            topic = " ".join(part for part in [row["topic_name"], row["source_channel"]] if part)
            search_blob = build_search_blob(
                row["title"],
                row["content"],
                row["link_title"],
                row["link_url"],
                author,
                topic,
                row["source_type"],
            )
            self.conn.execute(
                """
                INSERT INTO items_fts (
                    item_id, title, content, link_title, link_url, author, topic, source_type, search_blob
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["title"],
                    row["content"],
                    row["link_title"],
                    row["link_url"],
                    author,
                    topic,
                    row["source_type"],
                    search_blob,
                ),
            )

    def start_sync_run(self, started_at: str, mode: str, source_type: str = "jike") -> int:
        cursor = self.conn.execute(
            "INSERT INTO sync_runs (started_at, source_type, mode, status) VALUES (?, ?, ?, ?)",
            (started_at, source_type, mode, "running"),
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
                id, source_type, source_item_id, item_type, title, content, link_title, link_url,
                author_screen_name, author_username, source_author, source_channel, topic_id, topic_name,
                source_url, canonical_url, created_at, published_at, collected_at, first_seen_at,
                last_seen_at, has_images, has_video, has_audio, domain, tags_json, metadata_json,
                is_collected, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_type = excluded.source_type,
                source_item_id = excluded.source_item_id,
                item_type = excluded.item_type,
                title = excluded.title,
                content = excluded.content,
                link_title = excluded.link_title,
                link_url = excluded.link_url,
                author_screen_name = excluded.author_screen_name,
                author_username = excluded.author_username,
                source_author = excluded.source_author,
                source_channel = excluded.source_channel,
                topic_id = excluded.topic_id,
                topic_name = excluded.topic_name,
                source_url = excluded.source_url,
                canonical_url = excluded.canonical_url,
                created_at = excluded.created_at,
                published_at = excluded.published_at,
                collected_at = excluded.collected_at,
                first_seen_at = items.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                has_images = excluded.has_images,
                has_video = excluded.has_video,
                has_audio = excluded.has_audio,
                domain = excluded.domain,
                tags_json = excluded.tags_json,
                metadata_json = excluded.metadata_json,
                is_collected = 1,
                raw_json = excluded.raw_json
            """,
            (
                item.item_id,
                item.source_type,
                item.source_item_id,
                item.item_type,
                item.title,
                item.content,
                item.link_title,
                item.link_url,
                item.author_screen_name,
                item.author_username,
                item.source_author,
                item.source_channel,
                item.topic_id,
                item.topic_name,
                item.source_url,
                item.canonical_url,
                item.created_at,
                item.published_at,
                item.collected_at,
                first_seen_at,
                item.last_seen_at,
                item.has_images,
                item.has_video,
                item.has_audio,
                item.domain,
                item.tags_json,
                item.metadata_json,
                item.raw_json,
            ),
        )

        self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item.item_id,))
        self.conn.execute(
            """
            INSERT INTO items_fts (
                item_id, title, content, link_title, link_url, author, topic, source_type, search_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.item_id,
                item.title,
                item.content,
                item.link_title,
                item.link_url,
                " ".join(
                    part
                    for part in [
                        item.author_screen_name,
                        item.author_username,
                        item.source_author,
                    ]
                    if part
                ),
                " ".join(part for part in [item.topic_name, item.source_channel] if part),
                item.source_type,
                item.search_blob,
            ),
        )
        self.conn.commit()
        return status

    def mark_missing_items_as_removed(self, seen_ids: Sequence[str], source_type: str = "jike") -> int:
        if not seen_ids:
            return 0

        placeholders = ",".join("?" for _ in seen_ids)
        cursor = self.conn.execute(
            f"""
            UPDATE items
            SET is_collected = 0
            WHERE is_collected = 1
              AND source_type = ?
              AND id NOT IN ({placeholders})
            """,
            [source_type, *seen_ids],
        )
        self.conn.commit()
        return cursor.rowcount

    def search(self, match_query: str, limit: int = 10, source_filter: str = "all") -> List[SearchRow]:
        rows = self.conn.execute(
            """
            SELECT
                items_fts.item_id AS item_id,
                bm25(items_fts) AS score,
                items.source_type AS source_type,
                items.title AS title,
                items.content AS content,
                items.link_url AS link_url,
                items.author_screen_name AS author_screen_name,
                items.topic_name AS topic_name,
                items.source_channel AS source_channel,
                items.created_at AS created_at,
                items.published_at AS published_at,
                items.domain AS domain
            FROM items_fts
            JOIN items ON items.id = items_fts.item_id
            WHERE items_fts MATCH ?
              AND items.is_collected = 1
              AND (? = 'all' OR items.source_type = ?)
            ORDER BY score
            LIMIT ?
            """,
            (match_query, source_filter, source_filter, limit),
        ).fetchall()
        return [SearchRow(**dict(row)) for row in rows]

    def fetch_recent_items(
        self,
        days: int = 30,
        limit: Optional[int] = None,
        source_filter: str = "jike",
        time_field: str = "published_at",
    ) -> List[sqlite3.Row]:
        if time_field not in {"published_at", "created_at", "first_seen_at", "collected_at"}:
            time_field = "published_at"

        sql = f"""
            SELECT *
            FROM items
            WHERE is_collected = 1
              AND (? = 'all' OR source_type = ?)
              AND datetime({time_field}) >= datetime('now', ?)
            ORDER BY datetime({time_field}) DESC
        """
        params: List[object] = [source_filter, source_filter, f"-{days} days"]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def fetch_all_active_items(self, source_filter: str = "all") -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM items
            WHERE is_collected = 1
              AND (? = 'all' OR source_type = ?)
            ORDER BY datetime(published_at) DESC, id ASC
            """,
            (source_filter, source_filter),
        ).fetchall()

    def fetch_items_in_window(
        self,
        source_type: str,
        time_field: str,
        start_iso: str,
        end_iso: str,
    ) -> List[sqlite3.Row]:
        if time_field not in {"published_at", "created_at", "first_seen_at", "collected_at"}:
            raise ValueError(f"Unsupported time field: {time_field}")
        return self.conn.execute(
            f"""
            SELECT *
            FROM items
            WHERE is_collected = 1
              AND source_type = ?
              AND datetime({time_field}) >= datetime(?)
              AND datetime({time_field}) < datetime(?)
            ORDER BY datetime({time_field}) DESC, id ASC
            """,
            (source_type, start_iso, end_iso),
        ).fetchall()

    def fetch_stats(self, source_filter: str = "all") -> sqlite3.Row:
        return self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_items,
                COALESCE(SUM(CASE WHEN is_collected = 1 THEN 1 ELSE 0 END), 0) AS active_items,
                COALESCE(SUM(CASE WHEN has_images = 1 THEN 1 ELSE 0 END), 0) AS image_items,
                COALESCE(SUM(CASE WHEN has_video = 1 THEN 1 ELSE 0 END), 0) AS video_items,
                COALESCE(SUM(CASE WHEN has_audio = 1 THEN 1 ELSE 0 END), 0) AS audio_items,
                MIN(published_at) AS oldest_item_at,
                MAX(published_at) AS newest_item_at
            FROM items
            WHERE (? = 'all' OR source_type = ?)
            """,
            (source_filter, source_filter),
        ).fetchone()

    def fetch_source_counts(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT source_type, COUNT(*) AS count
            FROM items
            WHERE is_collected = 1
            GROUP BY source_type
            ORDER BY source_type
            """
        ).fetchall()

    def count_kb_chunks(self, source_filter: str = "all") -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM kb_chunks
            JOIN items ON items.id = kb_chunks.item_id
            WHERE (? = 'all' OR items.source_type = ?)
            """,
            (source_filter, source_filter),
        ).fetchone()
        return int(row["count"])

    def fetch_unsynced_items(self, limit: Optional[int] = None, source_filter: str = "jike") -> List[sqlite3.Row]:
        sql = """
            SELECT items.*
            FROM items
            LEFT JOIN feishu_doc_sync ON feishu_doc_sync.item_id = items.id
            WHERE items.is_collected = 1
              AND (? = 'all' OR items.source_type = ?)
              AND (
                feishu_doc_sync.item_id IS NULL
                OR feishu_doc_sync.status != 'synced'
              )
            ORDER BY datetime(items.published_at) ASC, items.id ASC
        """
        params: List[object] = [source_filter, source_filter]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def get_item(self, item_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def get_items(self, item_ids: Sequence[str]) -> List[sqlite3.Row]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        return self.conn.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders}) ORDER BY datetime(published_at) DESC",
            list(item_ids),
        ).fetchall()

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

    def count_synced_doc_items(self, source_filter: str = "jike") -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM feishu_doc_sync
            JOIN items ON items.id = feishu_doc_sync.item_id
            WHERE feishu_doc_sync.status = 'synced'
              AND (? = 'all' OR items.source_type = ?)
            """,
            (source_filter, source_filter),
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

    def start_kb_index_run(self, started_at: str, mode: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO kb_index_runs (started_at, mode, status) VALUES (?, ?, ?)",
            (started_at, mode, "running"),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_kb_index_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        items_seen: int,
        items_indexed: int,
        items_failed: int,
        note: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE kb_index_runs
            SET finished_at = ?, status = ?, items_seen = ?, items_indexed = ?, items_failed = ?, note = ?
            WHERE id = ?
            """,
            (finished_at, status, items_seen, items_indexed, items_failed, note, run_id),
        )
        self.conn.commit()

    def get_kb_chunk(self, item_id: str, chunk_index: int = 0) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM kb_chunks WHERE item_id = ? AND chunk_index = ? LIMIT 1",
            (item_id, chunk_index),
        ).fetchone()

    def upsert_kb_chunk(
        self,
        *,
        chunk_id: str,
        item_id: str,
        source_type: str,
        chunk_index: int,
        chunk_text: str,
        content_hash: str,
        dedupe_key: str,
        created_at: str,
        updated_at: str,
    ) -> str:
        existing = self.conn.execute(
            "SELECT content_hash, chunk_text, dedupe_key FROM kb_chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if existing is None:
            status = "inserted"
        elif existing["content_hash"] != content_hash or existing["chunk_text"] != chunk_text or existing["dedupe_key"] != dedupe_key:
            status = "updated"
        else:
            status = "unchanged"

        self.conn.execute(
            """
            INSERT INTO kb_chunks (
                chunk_id, item_id, source_type, chunk_index, chunk_text, content_hash, dedupe_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                item_id = excluded.item_id,
                source_type = excluded.source_type,
                chunk_index = excluded.chunk_index,
                chunk_text = excluded.chunk_text,
                content_hash = excluded.content_hash,
                dedupe_key = excluded.dedupe_key,
                updated_at = excluded.updated_at
            """,
            (chunk_id, item_id, source_type, chunk_index, chunk_text, content_hash, dedupe_key, created_at, updated_at),
        )
        self.conn.commit()
        return status

    def find_kb_chunk_by_dedupe_key(self, source_type: str, dedupe_key: str) -> Optional[sqlite3.Row]:
        if not dedupe_key:
            return None
        return self.conn.execute(
            """
            SELECT
                kb_chunks.*,
                items.first_seen_at,
                items.published_at,
                items.created_at
            FROM kb_chunks
            JOIN items ON items.id = kb_chunks.item_id
            WHERE kb_chunks.source_type = ?
              AND kb_chunks.dedupe_key = ?
            ORDER BY datetime(items.first_seen_at) DESC, datetime(items.published_at) DESC, kb_chunks.updated_at DESC
            LIMIT 1
            """,
            (source_type, dedupe_key),
        ).fetchone()

    def delete_kb_chunk(self, chunk_id: str) -> None:
        self.conn.execute("DELETE FROM kb_embeddings WHERE chunk_id = ?", (chunk_id,))
        self.conn.execute("DELETE FROM kb_chunks WHERE chunk_id = ?", (chunk_id,))
        self.conn.commit()

    def cleanup_duplicate_kb_chunks(self, source_filter: str = "all") -> int:
        rows = self.conn.execute(
            """
            SELECT
                kb_chunks.chunk_id,
                kb_chunks.source_type,
                kb_chunks.dedupe_key,
                kb_chunks.item_id,
                items.first_seen_at,
                items.published_at,
                items.created_at
            FROM kb_chunks
            JOIN items ON items.id = kb_chunks.item_id
            WHERE kb_chunks.dedupe_key != ''
              AND (? = 'all' OR kb_chunks.source_type = ?)
            ORDER BY kb_chunks.source_type ASC, kb_chunks.dedupe_key ASC,
                     datetime(items.first_seen_at) DESC, datetime(items.published_at) DESC, kb_chunks.updated_at DESC
            """,
            (source_filter, source_filter),
        ).fetchall()
        seen: Dict[tuple[str, str], str] = {}
        removed = 0
        for row in rows:
            key = (row["source_type"], row["dedupe_key"])
            if key not in seen:
                seen[key] = row["chunk_id"]
                continue
            self.delete_kb_chunk(row["chunk_id"])
            removed += 1
        return removed

    def get_kb_embedding(self, chunk_id: str, embedding_model: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM kb_embeddings
            WHERE chunk_id = ? AND embedding_model = ?
            LIMIT 1
            """,
            (chunk_id, embedding_model),
        ).fetchone()

    def upsert_kb_embedding(
        self,
        *,
        chunk_id: str,
        embedding_model: str,
        embedding_vector: str,
        content_hash: str,
        embedded_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO kb_embeddings (chunk_id, embedding_model, embedding_vector, content_hash, embedded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id, embedding_model) DO UPDATE SET
                embedding_vector = excluded.embedding_vector,
                content_hash = excluded.content_hash,
                embedded_at = excluded.embedded_at
            """,
            (chunk_id, embedding_model, embedding_vector, content_hash, embedded_at),
        )
        self.conn.commit()

    def fetch_kb_candidate_rows(self, item_ids: Sequence[str]) -> List[sqlite3.Row]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        return self.conn.execute(
            f"""
            SELECT
                kb_chunks.*,
                items.title,
                items.content,
                items.link_url,
                items.link_title,
                items.author_screen_name,
                items.source_author,
                items.topic_name,
                items.source_channel,
                items.source_type,
                items.canonical_url,
                items.published_at,
                items.collected_at,
                items.first_seen_at,
                items.created_at
            FROM kb_chunks
            JOIN items ON items.id = kb_chunks.item_id
            WHERE kb_chunks.item_id IN ({placeholders})
            ORDER BY datetime(items.published_at) DESC
            """,
            list(item_ids),
        ).fetchall()

    def fetch_recent_kb_rows(self, source_filter: str = "all", limit: int = 50) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                kb_chunks.*,
                items.title,
                items.content,
                items.link_url,
                items.link_title,
                items.author_screen_name,
                items.source_author,
                items.topic_name,
                items.source_channel,
                items.source_type,
                items.canonical_url,
                items.published_at,
                items.collected_at,
                items.first_seen_at,
                items.created_at
            FROM kb_chunks
            JOIN items ON items.id = kb_chunks.item_id
            WHERE (? = 'all' OR items.source_type = ?)
              AND items.is_collected = 1
            ORDER BY datetime(items.published_at) DESC
            LIMIT ?
            """,
            (source_filter, source_filter, limit),
        ).fetchall()

    def fetch_kb_embeddings_for_chunk_ids(self, chunk_ids: Sequence[str], embedding_model: str) -> List[sqlite3.Row]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        return self.conn.execute(
            f"""
            SELECT *
            FROM kb_embeddings
            WHERE chunk_id IN ({placeholders})
              AND embedding_model = ?
            """,
            [*chunk_ids, embedding_model],
        ).fetchall()

    def start_digest_run(self, digest_date: str, started_at: str) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO daily_digest_runs (digest_date, started_at, status)
            VALUES (?, ?, 'running')
            ON CONFLICT(digest_date) DO UPDATE SET
                started_at = excluded.started_at,
                finished_at = NULL,
                status = 'running',
                error = '',
                webhook_sent = 0
            """,
            (digest_date, started_at),
        )
        self.conn.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        row = self.conn.execute(
            "SELECT id FROM daily_digest_runs WHERE digest_date = ? LIMIT 1",
            (digest_date,),
        ).fetchone()
        return int(row["id"])

    def finish_digest_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        jike_item_count: int,
        aihot_item_count: int,
        summary_markdown: str,
        webhook_sent: bool,
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE daily_digest_runs
            SET finished_at = ?, status = ?, jike_item_count = ?, aihot_item_count = ?,
                summary_markdown = ?, webhook_sent = ?, error = ?
            WHERE id = ?
            """,
            (
                finished_at,
                status,
                jike_item_count,
                aihot_item_count,
                summary_markdown,
                1 if webhook_sent else 0,
                error,
                run_id,
            ),
        )
        self.conn.commit()

    def update_digest_webhook_status(self, digest_date: str, webhook_sent: bool) -> None:
        self.conn.execute(
            "UPDATE daily_digest_runs SET webhook_sent = ? WHERE digest_date = ?",
            (1 if webhook_sent else 0, digest_date),
        )
        self.conn.commit()
