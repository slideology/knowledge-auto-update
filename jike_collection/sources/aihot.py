from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import Settings
from ..db import Database
from ..models import (
    NormalizedItem,
    build_search_blob,
    clean_body_text,
    clean_text,
    make_item_id,
    serialize_json,
    utc_now_iso,
)


AIHOT_BASE = "https://aihot.virxact.com"

CATEGORY_LABELS = {
    "ai-models": "模型发布/更新",
    "ai-products": "产品发布/更新",
    "industry": "行业动态",
    "paper": "论文研究",
    "tip": "技巧与观点",
}


class AIHOTApiError(RuntimeError):
    pass


@dataclass
class AIHOTSyncSummary:
    success: bool
    mode: str
    seen_count: int
    inserted: int
    updated: int
    note: str
    error: str = ""


class AIHOTClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request_json(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            AIHOT_BASE + path + query,
            headers={
                "User-Agent": self.settings.aihot_user_agent,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.settings.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AIHOTApiError(f"HTTP {exc.code} for {path}: {body}") from exc

    def fetch_items_page(
        self,
        *,
        mode: str = "selected",
        category: str = "",
        since: str = "",
        take: int = 100,
        cursor: str = "",
        query: str = "",
    ) -> Dict[str, Any]:
        params: Dict[str, str] = {"mode": mode, "take": str(take)}
        if category:
            params["category"] = category
        if since:
            params["since"] = since
        if cursor:
            params["cursor"] = cursor
        if query:
            params["q"] = query
        return self._request_json("/api/public/items", params)

    def iter_items(
        self,
        *,
        mode: str = "selected",
        category: str = "",
        since: str = "",
        take: int = 100,
        query: str = "",
    ) -> Generator[Dict[str, Any], None, None]:
        cursor = ""
        while True:
            payload = self.fetch_items_page(
                mode=mode,
                category=category,
                since=since,
                take=take,
                cursor=cursor,
                query=query,
            )
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise AIHOTApiError(f"/api/public/items 返回格式异常: {payload}")
            for item in items:
                yield item

            if not payload.get("hasNext") or not payload.get("nextCursor"):
                break
            cursor = str(payload["nextCursor"])

    def fetch_daily(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        path = "/api/public/daily" if not date_str else f"/api/public/daily/{date_str}"
        return self._request_json(path)

    def iter_daily_backfill_items(self, days: int) -> Generator[Dict[str, Any], None, None]:
        today = datetime.now(timezone.utc).date()
        for offset in range(1, max(days, 0) + 1):
            target_date = today - timedelta(days=offset)
            payload = self.fetch_daily(target_date.isoformat())
            sections = payload.get("sections") or []
            for section in sections:
                label = clean_text(section.get("label"))
                for item in section.get("items") or []:
                    enriched = dict(item)
                    enriched["_daily_date"] = payload.get("date", target_date.isoformat())
                    enriched["_daily_label"] = label
                    yield enriched
            for item in payload.get("flashes") or []:
                enriched = dict(item)
                enriched["_daily_date"] = payload.get("date", target_date.isoformat())
                enriched["_daily_label"] = "快讯"
                yield enriched


def _category_label(value: str) -> str:
    return CATEGORY_LABELS.get(value, value or "")


def _safe_id(*parts: str) -> str:
    base = "|".join(part for part in parts if part)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def normalize_aihot_item(item: Dict[str, Any], seen_at: Optional[str] = None) -> NormalizedItem:
    now = seen_at or utc_now_iso()
    source_item_id = clean_text(item.get("id"))
    category = clean_text(item.get("category")) or clean_text(item.get("_daily_label"))
    title = clean_text(item.get("title")) or "AIHOT 条目"
    summary = clean_body_text(item.get("summary"))
    title_en = clean_text(item.get("title_en") or item.get("titleEn"))
    url = clean_text(item.get("url") or item.get("sourceUrl"))
    source_name = clean_text(item.get("source") or item.get("sourceName"))
    published_at = clean_text(item.get("publishedAt"))
    if not published_at:
        daily_date = clean_text(item.get("_daily_date"))
        published_at = f"{daily_date}T00:00:00+00:00" if daily_date else now

    if not source_item_id:
        source_item_id = f"daily:{clean_text(item.get('_daily_date'))}:{_safe_id(title, url, source_name)}"

    content = summary or title_en or title
    link_title = title_en or source_name
    category_label = _category_label(category)
    tags = [value for value in [category, category_label, "aihot"] if value]
    metadata = {
        "source": source_name,
        "category": category,
        "daily_date": clean_text(item.get("_daily_date")),
        "daily_label": clean_text(item.get("_daily_label")),
    }
    search_blob = build_search_blob(
        title,
        content,
        title_en,
        source_name,
        category_label,
        url,
        "AIHOT",
        "AI 新闻",
        "AI 热点",
    )

    return NormalizedItem(
        item_id=make_item_id("aihot", source_item_id),
        source_type="aihot",
        source_item_id=source_item_id,
        item_type="AIHOT_ITEM",
        title=title,
        content=content,
        link_title=link_title,
        link_url=url,
        author_screen_name=source_name,
        author_username="",
        source_author=source_name,
        source_channel=category_label,
        topic_id=category,
        topic_name=category_label,
        source_url=url,
        canonical_url=url,
        created_at=published_at,
        published_at=published_at,
        collected_at=now,
        first_seen_at=now,
        last_seen_at=now,
        has_images=0,
        has_video=0,
        has_audio=0,
        domain="aihot.virxact.com" if not url else clean_text(url.split("/")[2] if "://" in url else ""),
        tags_json=serialize_json(tags, default="[]"),
        metadata_json=serialize_json(metadata, default="{}"),
        search_blob=search_blob,
        raw_json=json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def sync_aihot_source(
    db: Database,
    settings: Settings,
    *,
    mode: str = "selected",
    days: Optional[int] = None,
    backfill_days: int = 0,
    category: str = "",
) -> AIHOTSyncSummary:
    if not settings.aihot_enabled:
        return AIHOTSyncSummary(
            success=True,
            mode="disabled",
            seen_count=0,
            inserted=0,
            updated=0,
            note="AIHOT disabled by configuration",
        )

    from ..models import utc_days_ago_iso

    client = AIHOTClient(settings)
    seen_count = 0
    inserted = 0
    updated = 0
    source_mode = "backfill" if backfill_days else mode
    now = utc_now_iso()
    run_id = db.start_sync_run(now, source_mode, source_type="aihot")
    note_parts: List[str] = []

    try:
        if backfill_days > 0:
            iterator = client.iter_daily_backfill_items(backfill_days)
        else:
            since = utc_days_ago_iso(days or settings.aihot_sync_days)
            iterator = client.iter_items(mode=mode, category=category, since=since, take=100)

        for raw_item in iterator:
            normalized = normalize_aihot_item(raw_item, seen_at=now)
            status = db.upsert_item(normalized)
            seen_count += 1
            if status == "inserted":
                inserted += 1
            elif status == "updated":
                updated += 1

        if backfill_days > 0:
            note_parts.append(f"backfilled {backfill_days} daily pages")
        else:
            note_parts.append(f"synced AIHOT {mode} items")
        if category:
            note_parts.append(f"category={category}")

        db.finish_sync_run(
            run_id,
            finished_at=utc_now_iso(),
            status="success",
            items_seen=seen_count,
            items_inserted=inserted,
            items_updated=updated,
            items_marked_removed=0,
            note="; ".join(note_parts),
        )
        return AIHOTSyncSummary(
            success=True,
            mode=source_mode,
            seen_count=seen_count,
            inserted=inserted,
            updated=updated,
            note="; ".join(note_parts),
        )
    except AIHOTApiError as exc:
        error_message = str(exc)
        db.finish_sync_run(
            run_id,
            finished_at=utc_now_iso(),
            status="error",
            items_seen=seen_count,
            items_inserted=inserted,
            items_updated=updated,
            items_marked_removed=0,
            note=error_message,
        )
        return AIHOTSyncSummary(
            success=False,
            mode=source_mode,
            seen_count=seen_count,
            inserted=inserted,
            updated=updated,
            note="sync failed",
            error=error_message,
        )
