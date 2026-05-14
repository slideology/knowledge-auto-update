from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import List, Optional

from .config import Settings
from .db import Database
from .feishu_client import FeishuApiError, FeishuClient
from .feishu_doc import FeishuDocSync
from .jike_api import JikeApiError, JikeClient
from .models import normalize_item, utc_now_iso


@dataclass
class SyncSummary:
    success: bool
    mode: str
    seen_count: int
    inserted: int
    updated: int
    removed: int
    detail_fallbacks: int
    note: str
    error: str = ""


@dataclass
class FeishuDocSummary:
    success: bool
    mode: str
    attempted: int
    written: int
    failed: int
    document_url: str
    note: str
    error: str = ""


def perform_jike_sync(
    db: Database,
    settings: Settings,
    *,
    full: bool = False,
    max_pages: Optional[int] = None,
    stale_threshold: int = 60,
) -> SyncSummary:
    client = JikeClient(settings)
    started_at = utc_now_iso()
    run_mode = "full" if full else "incremental"
    run_id = db.start_sync_run(started_at, run_mode, source_type="jike")

    inserted = 0
    updated = 0
    seen_count = 0
    consecutive_existing = 0
    seen_ids: List[str] = []
    detail_fallbacks = 0
    detail_backoff_enabled = False

    try:
        for page in client.iter_collection_pages(limit=settings.page_size, max_pages=max_pages):
            if not page.items:
                break

            for raw_item in page.items:
                item_id = str(raw_item.get("id") or "")
                item_type = str(raw_item.get("type") or "")
                if detail_backoff_enabled or not db.should_fetch_detail(item_id, item_type):
                    detailed_item = raw_item
                else:
                    try:
                        detailed_item = client.fetch_item_detail(raw_item)
                        sleep(0.05)
                    except JikeApiError as exc:
                        detail_fallbacks += 1
                        detailed_item = raw_item
                        if "HTTP 429" in str(exc):
                            detail_backoff_enabled = True

                normalized = normalize_item(detailed_item)
                status = db.upsert_item(normalized)
                seen_ids.append(normalized.item_id)
                seen_count += 1

                if status == "inserted":
                    inserted += 1
                    consecutive_existing = 0
                else:
                    if status == "updated":
                        updated += 1
                    consecutive_existing += 1

                if not full and consecutive_existing >= stale_threshold:
                    break

            if not full and consecutive_existing >= stale_threshold:
                break

        removed = 0
        note = ""
        if full:
            removed = db.mark_missing_items_as_removed(seen_ids, source_type="jike")
            note = "full sync completed"
        elif consecutive_existing >= stale_threshold:
            note = f"stopped early after {consecutive_existing} consecutive existing items"

        if detail_fallbacks:
            suffix = f"{detail_fallbacks} detail requests fell back to list payload"
            note = f"{note}; {suffix}" if note else suffix

        summary = SyncSummary(
            success=True,
            mode=run_mode,
            seen_count=seen_count,
            inserted=inserted,
            updated=updated,
            removed=removed,
            detail_fallbacks=detail_fallbacks,
            note=note,
        )
        db.finish_sync_run(
            run_id,
            finished_at=utc_now_iso(),
            status="success",
            items_seen=seen_count,
            items_inserted=inserted,
            items_updated=updated,
            items_marked_removed=removed,
            note=note,
        )
        return summary
    except JikeApiError as exc:
        error_message = str(exc)
        db.finish_sync_run(
            run_id,
            finished_at=utc_now_iso(),
            status="error",
            items_seen=seen_count,
            items_inserted=inserted,
            items_updated=updated,
            note=error_message,
        )
        return SyncSummary(
            success=False,
            mode=run_mode,
            seen_count=seen_count,
            inserted=inserted,
            updated=updated,
            removed=0,
            detail_fallbacks=detail_fallbacks,
            note="sync failed",
            error=error_message,
        )


def perform_feishu_doc_sync(
    db: Database,
    settings: Settings,
    *,
    mode: str,
    limit: Optional[int] = None,
) -> FeishuDocSummary:
    rows = db.fetch_unsynced_items(limit=limit, source_filter="jike")
    attempted = len(rows)
    if attempted == 0:
        return FeishuDocSummary(
            success=True,
            mode=mode,
            attempted=0,
            written=0,
            failed=0,
            document_url="",
            note="no unsynced items",
        )

    client = FeishuClient(settings)
    doc_sync = FeishuDocSync(settings, client)
    written = 0
    failed = 0
    now = utc_now_iso()

    try:
        doc_sync.ensure_document()
        for row in rows:
            try:
                block_id = doc_sync.append_item(row)
                db.mark_item_doc_synced(row["id"], now, doc_block_id=block_id)
                written += 1
                sleep(0.15)
            except Exception as exc:
                failed += 1
                db.mark_item_doc_failed(row["id"], now, str(exc))
                if "HTTP 429" in str(exc):
                    sleep(5)

        overview_note = ""
        try:
            doc_sync.refresh_overview(
                total_items=int(db.fetch_stats()["active_items"]),
                synced_items=db.count_synced_doc_items(),
            )
        except Exception as exc:
            overview_note = f"; overview refresh skipped: {exc}"
        return FeishuDocSummary(
            success=failed == 0,
            mode=mode,
            attempted=attempted,
            written=written,
            failed=failed,
            document_url=doc_sync.document_url,
            note=("doc sync completed" if failed == 0 else "doc sync completed with failures") + overview_note,
        )
    except FeishuApiError as exc:
        return FeishuDocSummary(
            success=False,
            mode=mode,
            attempted=attempted,
            written=written,
            failed=attempted - written,
            document_url=doc_sync.document_url,
            note="doc sync failed",
            error=str(exc),
        )
