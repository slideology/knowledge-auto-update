from __future__ import annotations

import argparse
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import time
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .analyzer import build_report, write_report
from .config import load_settings
from .db import Database
from .feishu_client import FeishuApiError, FeishuClient
from .feishu_webhook import FeishuWebhookError, FeishuWebhookSender
from .models import build_match_query, utc_now_iso
from .workflows import perform_feishu_doc_sync, perform_jike_sync


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync, search, analyze, and deliver your Jike collections.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Sync collection items into SQLite")
    sync_parser.add_argument("--full", action="store_true", help="Fetch all pages and mark missing items as removed")
    sync_parser.add_argument("--max-pages", type=int, default=None, help="Optional page cap for debugging")
    sync_parser.add_argument(
        "--stale-threshold",
        type=int,
        default=60,
        help="Stop incremental sync after this many consecutive already-known items",
    )

    search_parser = subparsers.add_parser("search", help="Full-text search across synced items")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Maximum result count")

    report_parser = subparsers.add_parser("report", help="Generate a Markdown analysis report")
    report_parser.add_argument("--days", type=int, default=30, help="Lookback window")

    auth_parser = subparsers.add_parser("feishu-auth", help="Authorize Feishu user token for doc writing")
    auth_parser.add_argument("--code", help="Optional one-time OAuth code")
    auth_parser.add_argument("--open-browser", action="store_true", help="Open the authorization URL automatically")

    backfill_parser = subparsers.add_parser("feishu-backfill", help="Backfill all unsynced items into a Feishu doc")
    backfill_parser.add_argument("--limit", type=int, default=None, help="Optional item cap for debugging")

    doc_sync_parser = subparsers.add_parser("feishu-sync-doc", help="Sync unsynced items into a Feishu doc")
    doc_sync_parser.add_argument("--limit", type=int, default=None, help="Optional item cap for debugging")

    daily_parser = subparsers.add_parser("run-daily", help="Run incremental sync, doc sync, and webhook notification")
    daily_parser.add_argument("--full", action="store_true", help="Use full Jike sync before pushing to Feishu")
    daily_parser.add_argument("--limit-doc", type=int, default=None, help="Optional Feishu doc sync cap")

    subparsers.add_parser("stats", help="Show local database stats")
    return parser


def _print_sync_summary(summary) -> None:
    if summary.success:
        print(
            f"Sync complete: seen={summary.seen_count}, inserted={summary.inserted}, "
            f"updated={summary.updated}, removed={summary.removed}, detail_fallbacks={summary.detail_fallbacks}"
        )
        if summary.note:
            print(f"Note: {summary.note}")
    else:
        print(f"Sync failed: {summary.error}", file=sys.stderr)


def _send_feishu_command_notification(
    settings,
    *,
    title: str,
    lines: List[str],
) -> bool:
    sender = FeishuWebhookSender(settings)
    if not sender.is_enabled():
        return False
    return sender.send_generic_summary(title, lines)


def run_sync(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        summary = perform_jike_sync(
            db,
            settings,
            full=args.full,
            max_pages=args.max_pages,
            stale_threshold=args.stale_threshold,
        )
        _print_sync_summary(summary)
        return 0 if summary.success else 1
    finally:
        db.close()


def run_search(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        match_query = build_match_query(args.query)
        rows = db.search(match_query, limit=args.limit)
        if not rows:
            print("No matches found.")
            return 0

        for idx, row in enumerate(rows, start=1):
            snippet = row.content.strip().replace("\n", " ")
            if len(snippet) > 160:
                snippet = snippet[:159].rstrip() + "…"
            print(f"{idx}. {row.title}")
            print(f"   time: {row.created_at}")
            print(f"   author: {row.author_screen_name or '未知'}")
            print(f"   topic: {row.topic_name or '无'}")
            print(f"   domain: {row.domain or '无'}")
            print(f"   link: {row.link_url}")
            if snippet:
                print(f"   text: {snippet}")
        return 0
    finally:
        db.close()


def run_report(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        rows = db.fetch_recent_items(days=args.days)
        report = build_report(rows, days=args.days)
        path = write_report(report, settings.reports_dir, days=args.days)
        print(f"Report written to {path}")
        return 0
    finally:
        db.close()


def _wait_for_feishu_callback(redirect_uri: str, timeout_seconds: int) -> Dict[str, str]:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    callback_path = parsed.path or "/"
    result: Dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # type: ignore[override]
            request_url = urlparse(self.path)
            if request_url.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(request_url.query)
            result["code"] = params.get("code", [""])[0]
            result["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("飞书授权完成，你可以回到终端了。".encode("utf-8"))

        def log_message(self, format, *args):  # type: ignore[override]
            return

    httpd = HTTPServer((host, port), CallbackHandler)
    httpd.timeout = 1
    deadline = time() + timeout_seconds
    try:
        while "code" not in result and time() < deadline:
            httpd.handle_request()
        return result
    finally:
        httpd.server_close()


def run_feishu_auth(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = FeishuClient(settings)
    if args.code:
        token = client.exchange_code_for_user_token(args.code)
        print(f"Feishu auth complete. Token expires in {token.get('expires_in', 'unknown')} seconds.")
        return 0

    auth_url, state = client.get_authorization_url()
    print("Open this URL in your browser to authorize Feishu access:")
    print(auth_url)
    if args.open_browser:
        webbrowser.open(auth_url)

    try:
        result = _wait_for_feishu_callback(settings.feishu_redirect_uri, settings.feishu_auth_timeout)
    except OSError as exc:
        print(f"Failed to bind callback server: {exc}", file=sys.stderr)
        print("You can rerun with --code <oauth_code> instead.", file=sys.stderr)
        return 1

    code = result.get("code", "")
    received_state = result.get("state", "")
    if not code:
        print("Did not receive OAuth code. Retry with --code or check redirect URI.", file=sys.stderr)
        return 1
    if received_state and received_state != state:
        print("State mismatch during Feishu OAuth callback.", file=sys.stderr)
        return 1

    token = client.exchange_code_for_user_token(code)
    print(f"Feishu auth complete. Token expires in {token.get('expires_in', 'unknown')} seconds.")
    return 0


def run_feishu_backfill(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    started_at = utc_now_iso()
    delivery_run_id = db.start_delivery_run(started_at, "feishu-backfill")
    webhook_sent = False
    try:
        summary = perform_feishu_doc_sync(db, settings, mode="backfill", limit=args.limit)
        lines = [
            f"**执行模式**: 历史回填",
            f"**飞书文档写入**: {'成功' if summary.success else '部分失败/失败'}",
            f"**尝试写入条数**: {summary.attempted}",
            f"**成功写入条数**: {summary.written}",
            f"**失败条数**: {summary.failed}",
        ]
        if summary.document_url:
            lines.append(f"**飞书文档**: [打开知识库]({summary.document_url})")
        if summary.error:
            lines.append(f"**错误**: {summary.error[:200]}")
        elif summary.note:
            lines.append(f"**备注**: {summary.note[:200]}")

        note_parts = [summary.note or "", summary.error or ""]
        if settings.feishu_webhook_url:
            try:
                webhook_sent = _send_feishu_command_notification(
                    settings,
                    title="即刻收藏飞书回填结果",
                    lines=lines,
                )
            except (FeishuWebhookError, OSError) as exc:
                note_parts.append(str(exc))

        db.finish_delivery_run(
            delivery_run_id,
            finished_at=utc_now_iso(),
            status="success" if summary.success else "partial_failure",
            jike_inserted=0,
            jike_updated=0,
            doc_written=summary.written,
            doc_failed=summary.failed,
            webhook_sent=webhook_sent,
            note=" | ".join(part for part in note_parts if part),
        )
        if summary.success:
            print(
                f"Feishu backfill complete: attempted={summary.attempted}, "
                f"written={summary.written}, failed={summary.failed}"
            )
            if summary.note:
                print(f"Note: {summary.note}")
            if summary.document_url:
                print(f"Document: {summary.document_url}")
            return 0
        print(f"Feishu backfill failed: {summary.error or summary.note}", file=sys.stderr)
        return 1
    finally:
        db.close()


def run_feishu_sync_doc(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    started_at = utc_now_iso()
    delivery_run_id = db.start_delivery_run(started_at, "feishu-sync-doc")
    webhook_sent = False
    try:
        summary = perform_feishu_doc_sync(db, settings, mode="incremental", limit=args.limit)
        lines = [
            f"**执行模式**: 增量同步文档",
            f"**飞书文档写入**: {'成功' if summary.success else '部分失败/失败'}",
            f"**尝试写入条数**: {summary.attempted}",
            f"**成功写入条数**: {summary.written}",
            f"**失败条数**: {summary.failed}",
        ]
        if summary.attempted == 0:
            lines.append("**本次结果**: 没有待同步的新收藏")
        if summary.document_url:
            lines.append(f"**飞书文档**: [打开知识库]({summary.document_url})")
        if summary.error:
            lines.append(f"**错误**: {summary.error[:200]}")
        elif summary.note:
            lines.append(f"**备注**: {summary.note[:200]}")

        note_parts = [summary.note or "", summary.error or ""]
        if settings.feishu_webhook_url:
            try:
                webhook_sent = _send_feishu_command_notification(
                    settings,
                    title="即刻收藏飞书增量同步结果",
                    lines=lines,
                )
            except (FeishuWebhookError, OSError) as exc:
                note_parts.append(str(exc))

        db.finish_delivery_run(
            delivery_run_id,
            finished_at=utc_now_iso(),
            status="success" if summary.success else "partial_failure",
            jike_inserted=0,
            jike_updated=0,
            doc_written=summary.written,
            doc_failed=summary.failed,
            webhook_sent=webhook_sent,
            note=" | ".join(part for part in note_parts if part),
        )
        if summary.success:
            print(
                f"Feishu doc sync complete: attempted={summary.attempted}, "
                f"written={summary.written}, failed={summary.failed}"
            )
            if summary.note:
                print(f"Note: {summary.note}")
            if summary.document_url:
                print(f"Document: {summary.document_url}")
            return 0
        print(f"Feishu doc sync failed: {summary.error or summary.note}", file=sys.stderr)
        return 1
    finally:
        db.close()


def run_daily(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    started_at = utc_now_iso()
    delivery_run_id = db.start_delivery_run(started_at, "run-daily")
    webhook_sent = False
    note_parts: List[str] = []
    try:
        sync_summary = perform_jike_sync(
            db,
            settings,
            full=args.full,
            max_pages=None,
            stale_threshold=60,
        )
        note_parts.append(sync_summary.note or sync_summary.error or "")

        doc_summary = None
        if sync_summary.success:
            doc_summary = perform_feishu_doc_sync(
                db,
                settings,
                mode="daily",
                limit=args.limit_doc,
            )
            note_parts.append(doc_summary.note or doc_summary.error or "")
        else:
            from .workflows import FeishuDocSummary

            doc_summary = FeishuDocSummary(
                success=False,
                mode="daily",
                attempted=0,
                written=0,
                failed=0,
                document_url="",
                note="skipped because Jike sync failed",
            )

        sender = FeishuWebhookSender(settings)
        if sender.is_enabled():
            try:
                webhook_sent = sender.send_daily_summary(sync_summary, doc_summary)
            except (FeishuWebhookError, OSError) as exc:
                note_parts.append(str(exc))

        overall_success = sync_summary.success and doc_summary.success
        db.finish_delivery_run(
            delivery_run_id,
            finished_at=utc_now_iso(),
            status="success" if overall_success else "partial_failure",
            jike_inserted=sync_summary.inserted,
            jike_updated=sync_summary.updated,
            doc_written=doc_summary.written,
            doc_failed=doc_summary.failed,
            webhook_sent=webhook_sent,
            note=" | ".join(part for part in note_parts if part),
        )

        _print_sync_summary(sync_summary)
        print(
            f"Feishu daily summary: attempted={doc_summary.attempted}, "
            f"written={doc_summary.written}, failed={doc_summary.failed}, webhook_sent={webhook_sent}"
        )
        if doc_summary.error or doc_summary.note:
            print(f"Feishu note: {doc_summary.error or doc_summary.note}")
        if doc_summary.document_url:
            print(f"Document: {doc_summary.document_url}")
        return 0 if overall_success else 1
    finally:
        db.close()


def run_stats() -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        stats = db.fetch_stats()
        print(f"total_items: {stats['total_items']}")
        print(f"active_items: {stats['active_items']}")
        print(f"image_items: {stats['image_items']}")
        print(f"video_items: {stats['video_items']}")
        print(f"audio_items: {stats['audio_items']}")
        print(f"oldest_item_at: {stats['oldest_item_at']}")
        print(f"newest_item_at: {stats['newest_item_at']}")
        print(f"feishu_synced_items: {db.count_synced_doc_items()}")
        return 0
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command == "sync":
        return run_sync(args)
    if args.command == "search":
        return run_search(args)
    if args.command == "report":
        return run_report(args)
    if args.command == "feishu-auth":
        return run_feishu_auth(args)
    if args.command == "feishu-backfill":
        return run_feishu_backfill(args)
    if args.command == "feishu-sync-doc":
        return run_feishu_sync_doc(args)
    if args.command == "run-daily":
        return run_daily(args)
    if args.command == "stats":
        return run_stats()
    parser.error(f"Unknown command: {args.command}")
    return 2
