from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from ..config import Settings
from ..db import Database
from ..llm.client import LLMConfigurationError, OpenAICompatibleClient
from ..models import build_dedupe_key, content_hash, parse_iso_datetime, utc_now_iso


@dataclass
class KBIndexSummary:
    success: bool
    mode: str
    items_seen: int
    items_indexed: int
    items_failed: int
    note: str
    error: str = ""


class KnowledgeBaseIndexer:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.llm = OpenAICompatibleClient(settings)

    def build_chunk_text(self, row) -> str:
        lines: List[str] = [
            f"来源: {'即刻收藏' if row['source_type'] == 'jike' else 'AIHOT 精选'}",
            f"标题: {row['title']}",
        ]
        if row["source_author"]:
            lines.append(f"作者/来源: {row['source_author']}")
        if row["source_channel"]:
            lines.append(f"频道/分类: {row['source_channel']}")
        if row["topic_name"] and row["topic_name"] != row["source_channel"]:
            lines.append(f"主题: {row['topic_name']}")
        if row["link_title"]:
            lines.append(f"链接标题: {row['link_title']}")
        if row["canonical_url"]:
            lines.append(f"链接: {row['canonical_url']}")
        if row["published_at"]:
            lines.append(f"发布时间: {row['published_at']}")
        if row["content"]:
            lines.append("")
            lines.append(row["content"])
        return "\n".join(lines).strip()

    def _is_current_row_newer(self, current_row, existing_chunk_row) -> bool:
        current_first_seen = parse_iso_datetime(current_row["first_seen_at"] or "") or parse_iso_datetime(current_row["published_at"] or "") or parse_iso_datetime(current_row["created_at"] or "")
        existing_first_seen = parse_iso_datetime(existing_chunk_row["first_seen_at"] or "") or parse_iso_datetime(existing_chunk_row["published_at"] or "") or parse_iso_datetime(existing_chunk_row["created_at"] or "")
        if current_first_seen is None:
            return False
        if existing_first_seen is None:
            return True
        return current_first_seen >= existing_first_seen

    def sync(self, *, full: bool = False, source_filter: str = "all", limit: Optional[int] = None) -> KBIndexSummary:
        if not self.llm.is_configured():
            return KBIndexSummary(
                success=False,
                mode="full" if full else "incremental",
                items_seen=0,
                items_indexed=0,
                items_failed=0,
                note="kb sync failed",
                error="未配置 LLM_BASE_URL / LLM_API_KEY / LLM_CHAT_MODEL / LLM_EMBEDDING_MODEL",
            )

        rows = self.db.fetch_all_active_items(source_filter=source_filter)
        if limit is not None:
            rows = rows[:limit]

        mode = "full" if full else "incremental"
        run_id = self.db.start_kb_index_run(utc_now_iso(), mode)
        seen = 0
        indexed = 0
        failed = 0

        try:
            if full:
                self.db.cleanup_duplicate_kb_chunks(source_filter=source_filter)
            for row in rows:
                seen += 1
                chunk_id = f"{row['id']}:0"
                chunk_text = self.build_chunk_text(row)
                chunk_hash = content_hash(row["source_type"], row["title"], row["content"], row["canonical_url"], chunk_text)
                dedupe_key = build_dedupe_key(row["title"], row["content"])
                existing_duplicate = self.db.find_kb_chunk_by_dedupe_key(row["source_type"], dedupe_key)
                if existing_duplicate is not None and existing_duplicate["item_id"] != row["id"]:
                    if self._is_current_row_newer(row, existing_duplicate):
                        self.db.delete_kb_chunk(existing_duplicate["chunk_id"])
                    else:
                        stale_current = self.db.get_kb_chunk(row["id"], 0)
                        if stale_current is not None:
                            self.db.delete_kb_chunk(stale_current["chunk_id"])
                        continue
                chunk_status = self.db.upsert_kb_chunk(
                    chunk_id=chunk_id,
                    item_id=row["id"],
                    source_type=row["source_type"],
                    chunk_index=0,
                    chunk_text=chunk_text,
                    content_hash=chunk_hash,
                    dedupe_key=dedupe_key,
                    created_at=row["published_at"] or row["created_at"],
                    updated_at=utc_now_iso(),
                )

                embedding_row = self.db.get_kb_embedding(chunk_id, self.settings.llm_embedding_model)
                needs_embedding = full or chunk_status != "unchanged" or embedding_row is None
                if embedding_row is not None and embedding_row["content_hash"] != chunk_hash:
                    needs_embedding = True

                if not needs_embedding:
                    continue

                try:
                    vector = self.llm.embed_texts([chunk_text])[0]
                    self.db.upsert_kb_embedding(
                        chunk_id=chunk_id,
                        embedding_model=self.settings.llm_embedding_model,
                        embedding_vector=json.dumps(vector),
                        content_hash=chunk_hash,
                        embedded_at=utc_now_iso(),
                    )
                    indexed += 1
                except Exception:
                    failed += 1

            status = "success" if failed == 0 else "partial_failure"
            note = "kb sync complete" if failed == 0 else "kb sync completed with failures"
            self.db.finish_kb_index_run(
                run_id,
                finished_at=utc_now_iso(),
                status=status,
                items_seen=seen,
                items_indexed=indexed,
                items_failed=failed,
                note=note,
            )
            return KBIndexSummary(
                success=failed == 0,
                mode=mode,
                items_seen=seen,
                items_indexed=indexed,
                items_failed=failed,
                note=note,
            )
        except Exception as exc:
            self.db.finish_kb_index_run(
                run_id,
                finished_at=utc_now_iso(),
                status="error",
                items_seen=seen,
                items_indexed=indexed,
                items_failed=failed + 1,
                note=str(exc),
            )
            return KBIndexSummary(
                success=False,
                mode=mode,
                items_seen=seen,
                items_indexed=indexed,
                items_failed=failed + 1,
                note="kb sync failed",
                error=str(exc),
            )
