from __future__ import annotations

from ..config import Settings
from ..db import Database
from ..workflows import SyncSummary, perform_jike_sync


def sync_jike_source(
    db: Database,
    settings: Settings,
    *,
    full: bool = False,
    max_pages: int | None = None,
    stale_threshold: int = 60,
) -> SyncSummary:
    return perform_jike_sync(
        db,
        settings,
        full=full,
        max_pages=max_pages,
        stale_threshold=stale_threshold,
    )
