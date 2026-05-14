from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import time
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .analyzer import build_report, write_report
from .bot.server import serve_bot
from .config import load_settings
from .db import Database
from .digest.service import DailyDigestService
from .feishu_client import FeishuClient
from .feishu_webhook import FeishuWebhookError, FeishuWebhookSender
from .kb.indexer import KnowledgeBaseIndexer
from .kb.retriever import KnowledgeBaseRetriever
from .models import build_match_query, utc_now_iso
from .sources.aihot import sync_aihot_source
from .workflows import FeishuDocSummary, perform_feishu_doc_sync, perform_jike_sync


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync, search, analyze, and deliver your knowledge sources.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Sync Jike collection items into SQLite")
    sync_parser.add_argument("--full", action="store_true", help="Fetch all pages and mark missing items as removed")
    sync_parser.add_argument("--max-pages", type=int, default=None, help="Optional page cap for debugging")
    sync_parser.add_argument(
        "--stale-threshold",
        type=int,
        default=60,
        help="Stop incremental sync after this many consecutive already-known items",
    )

    aihot_parser = subparsers.add_parser("aihot-sync", help="Sync AIHOT public items into SQLite")
    aihot_parser.add_argument("--days", type=int, default=None, help="Recent days window for items API")
    aihot_parser.add_argument("--backfill-days", type=int, default=0, help="Backfill older daily pages")
    aihot_parser.add_argument("--category", default="", help="Optional AIHOT category slug")

    kb_sync_parser = subparsers.add_parser("kb-sync", help="Incrementally sync KB chunks and embeddings")
    kb_sync_parser.add_argument("--source", choices=["all", "jike", "aihot"], default="all")
    kb_sync_parser.add_argument("--limit", type=int, default=None)

    kb_reindex_parser = subparsers.add_parser("kb-reindex", help="Rebuild KB embeddings for active items")
    kb_reindex_parser.add_argument("--source", choices=["all", "jike", "aihot"], default="all")
    kb_reindex_parser.add_argument("--limit", type=int, default=None)

    search_parser = subparsers.add_parser("search", help="Full-text search across synced items")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Maximum result count")
    search_parser.add_argument("--source", choices=["all", "jike", "aihot"], default="all")

    ask_parser = subparsers.add_parser("ask", help="Ask the multi-source knowledge base a question")
    ask_parser.add_argument("question", help="Question text")
    ask_parser.add_argument("--source", choices=["auto", "all", "jike", "aihot"], default="auto")
    ask_parser.add_argument("--limit", type=int, default=5)

    report_parser = subparsers.add_parser("report", help="Generate a Markdown analysis report")
    report_parser.add_argument("--days", type=int, default=30, help="Lookback window")
    report_parser.add_argument("--source", choices=["all", "jike", "aihot"], default="jike")

    digest_parser = subparsers.add_parser("digest", help="Generate the previous-day digest")
    digest_parser.add_argument("--date", help="Digest date in YYYY-MM-DD; defaults to yesterday")
    digest_parser.add_argument("--send", action="store_true", help="Send digest to Feishu webhook")

    auth_parser = subparsers.add_parser("feishu-auth", help="Authorize Feishu user token for doc writing")
    auth_parser.add_argument("--code", help="Optional one-time OAuth code")
    auth_parser.add_argument("--open-browser", action="store_true", help="Open the authorization URL automatically")

    backfill_parser = subparsers.add_parser("feishu-backfill", help="Backfill all unsynced items into a Feishu doc")
    backfill_parser.add_argument("--limit", type=int, default=None, help="Optional item cap for debugging")

    doc_sync_parser = subparsers.add_parser("feishu-sync-doc", help="Sync unsynced items into a Feishu doc")
    doc_sync_parser.add_argument("--limit", type=int, default=None, help="Optional item cap for debugging")

    daily_parser = subparsers.add_parser("run-daily", help="Run source sync, KB sync, digest, and Feishu notification")
    daily_parser.add_argument("--full", action="store_true", help="Use full Jike sync before digesting")
    daily_parser.add_argument("--limit-doc", type=int, default=None, help="Optional Feishu doc sync cap")
    daily_parser.add_argument("--include-doc-sync", action="store_true", help="Also attempt the Feishu doc mirror sync")

    bot_parser = subparsers.add_parser("serve-bot", help="Start the Feishu knowledge-base bot server")
    bot_parser.add_argument("--host", default="0.0.0.0")
    bot_parser.add_argument("--port", type=int, default=8788)

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


def _send_feishu_command_notification(settings, *, title: str, lines: List[str]) -> bool:
    sender = FeishuWebhookSender(settings)
    if not sender.is_enabled():
        return False
    return sender.send_generic_summary(title, lines)


def _format_source_label(value: str) -> str:
    return {"jike": "即刻收藏", "aihot": "AIHOT 精选"}.get(value, value)


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


def _digest_card_lines(summary, failures: List[str]) -> List[str]:
    excerpt = summary.summary_markdown.strip()
    if len(excerpt) > 2800:
        excerpt = excerpt[:2799].rstrip() + "…"
    lines = [
        f"**日期**: {summary.digest_date}",
        f"**昨日新增收藏数**: {summary.jike_count}",
        f"**昨日 AIHOT 热点数**: {summary.aihot_count}",
    ]
    if failures:
        lines.append(f"**异常阶段**: {' | '.join(failures)}")
    if excerpt:
        lines.append("**摘要**:\n" + excerpt)
    return lines


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


def run_aihot_sync(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        summary = sync_aihot_source(
            db,
            settings,
            days=args.days,
            backfill_days=args.backfill_days,
            category=args.category,
        )
        if summary.success:
            print(
                f"AIHOT sync complete: seen={summary.seen_count}, "
                f"inserted={summary.inserted}, updated={summary.updated}"
            )
            if summary.note:
                print(f"Note: {summary.note}")
            return 0
        print(f"AIHOT sync failed: {summary.error}", file=sys.stderr)
        return 1
    finally:
        db.close()


def run_kb_sync(args: argparse.Namespace, *, full: bool) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        summary = KnowledgeBaseIndexer(db, settings).sync(
            full=full,
            source_filter=args.source,
            limit=args.limit,
        )
        if summary.success:
            print(
                f"KB sync complete: seen={summary.items_seen}, indexed={summary.items_indexed}, "
                f"failed={summary.items_failed}"
            )
            if summary.note:
                print(f"Note: {summary.note}")
            return 0
        print(f"KB sync failed: {summary.error or summary.note}", file=sys.stderr)
        return 1
    finally:
        db.close()


def run_search(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        match_query = build_match_query(args.query)
        rows = db.search(match_query, limit=args.limit, source_filter=args.source)
        if not rows:
            print("No matches found.")
            return 0

        for idx, row in enumerate(rows, start=1):
            snippet = row.content.strip().replace("\n", " ")
            if len(snippet) > 160:
                snippet = snippet[:159].rstrip() + "…"
            print(f"{idx}. [{_format_source_label(row.source_type)}] {row.title}")
            print(f"   time: {row.published_at or row.created_at}")
            print(f"   author/channel: {row.author_screen_name or row.source_channel or '未知'}")
            print(f"   topic: {row.topic_name or row.source_channel or '无'}")
            print(f"   domain: {row.domain or '无'}")
            print(f"   link: {row.link_url}")
            if snippet:
                print(f"   text: {snippet}")
        return 0
    finally:
        db.close()


def run_ask(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        answer = KnowledgeBaseRetriever(db, settings).answer(
            args.question,
            source_filter=args.source,
            limit=args.limit,
        )
        print(answer.answer)
        return 0
    finally:
        db.close()


def run_report(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        rows = db.fetch_recent_items(days=args.days, source_filter=args.source, time_field="published_at")
        report = build_report(rows, days=args.days)
        path = write_report(report, settings.reports_dir, days=args.days)
        print(f"Report written to {path}")
        return 0
    finally:
        db.close()


def run_digest(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        digest_date = date.fromisoformat(args.date) if args.date else None
        summary = DailyDigestService(db, settings).build_digest(digest_date)
        if not summary.success:
            print(f"Digest failed: {summary.error or summary.note}", file=sys.stderr)
            return 1

        print(summary.summary_markdown)
        if args.send:
            webhook_sent = False
            try:
                webhook_sent = _send_feishu_command_notification(
                    settings,
                    title=f"即刻收藏日报 {summary.digest_date}",
                    lines=_digest_card_lines(summary, []),
                )
            except (FeishuWebhookError, OSError) as exc:
                print(f"Digest webhook failed: {exc}", file=sys.stderr)
                return 1
            db.update_digest_webhook_status(summary.digest_date, webhook_sent)
            print(f"\nWebhook sent: {webhook_sent}")
        return 0
    finally:
        db.close()


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
    doc_written = 0
    doc_failed = 0
    note_parts: List[str] = []
    failure_stages: List[str] = []
    try:
        jike_summary = perform_jike_sync(
            db,
            settings,
            full=args.full,
            max_pages=None,
            stale_threshold=60,
        )
        note_parts.append(jike_summary.note or jike_summary.error or "")
        if not jike_summary.success:
            failure_stages.append("即刻同步失败")

        aihot_summary = sync_aihot_source(db, settings, days=settings.aihot_sync_days)
        note_parts.append(aihot_summary.note or aihot_summary.error or "")
        if not aihot_summary.success:
            failure_stages.append("AIHOT 同步失败")

        kb_summary = KnowledgeBaseIndexer(db, settings).sync(source_filter="all")
        note_parts.append(kb_summary.note or kb_summary.error or "")
        if not kb_summary.success:
            failure_stages.append("KB 索引失败")

        digest_summary = DailyDigestService(db, settings).build_digest()
        note_parts.append(digest_summary.note or digest_summary.error or "")
        if not digest_summary.success:
            failure_stages.append("日报生成失败")

        if args.include_doc_sync:
            doc_summary = perform_feishu_doc_sync(db, settings, mode="daily", limit=args.limit_doc)
            note_parts.append(doc_summary.note or doc_summary.error or "")
            doc_written = doc_summary.written
            doc_failed = doc_summary.failed
            if not doc_summary.success:
                failure_stages.append("飞书文档镜像失败")

        if settings.feishu_webhook_url:
            lines = _digest_card_lines(digest_summary, failure_stages)
            try:
                webhook_sent = _send_feishu_command_notification(
                    settings,
                    title=f"即刻收藏日报 {digest_summary.digest_date}",
                    lines=lines,
                )
            except (FeishuWebhookError, OSError) as exc:
                note_parts.append(str(exc))
                failure_stages.append("飞书通知失败")
        if digest_summary.success:
            db.update_digest_webhook_status(digest_summary.digest_date, webhook_sent)

        overall_success = not failure_stages
        db.finish_delivery_run(
            delivery_run_id,
            finished_at=utc_now_iso(),
            status="success" if overall_success else "partial_failure",
            jike_inserted=jike_summary.inserted,
            jike_updated=jike_summary.updated,
            doc_written=doc_written,
            doc_failed=doc_failed,
            webhook_sent=webhook_sent,
            note=" | ".join(part for part in note_parts if part),
        )

        _print_sync_summary(jike_summary)
        print(
            f"AIHOT sync: seen={aihot_summary.seen_count}, inserted={aihot_summary.inserted}, "
            f"updated={aihot_summary.updated}"
        )
        print(
            f"KB sync: seen={kb_summary.items_seen}, indexed={kb_summary.items_indexed}, "
            f"failed={kb_summary.items_failed}"
        )
        print(
            f"Digest: date={digest_summary.digest_date}, jike={digest_summary.jike_count}, "
            f"aihot={digest_summary.aihot_count}, webhook_sent={webhook_sent}"
        )
        if failure_stages:
            print(f"Failures: {' | '.join(failure_stages)}", file=sys.stderr)
        return 0 if overall_success else 1
    finally:
        db.close()


def run_serve_bot(args: argparse.Namespace) -> int:
    settings = load_settings()
    serve_bot(settings, host=args.host, port=args.port)
    return 0


def run_stats() -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    try:
        stats = db.fetch_stats(source_filter="all")
        print(f"total_items: {stats['total_items']}")
        print(f"active_items: {stats['active_items']}")
        print(f"image_items: {stats['image_items']}")
        print(f"video_items: {stats['video_items']}")
        print(f"audio_items: {stats['audio_items']}")
        print(f"oldest_item_at: {stats['oldest_item_at']}")
        print(f"newest_item_at: {stats['newest_item_at']}")
        print(f"feishu_synced_items: {db.count_synced_doc_items('jike')}")
        print(f"kb_chunks: {db.count_kb_chunks('all')}")
        for row in db.fetch_source_counts():
            print(f"source_{row['source_type']}: {row['count']}")
        return 0
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command == "sync":
        return run_sync(args)
    if args.command == "aihot-sync":
        return run_aihot_sync(args)
    if args.command == "kb-sync":
        return run_kb_sync(args, full=False)
    if args.command == "kb-reindex":
        return run_kb_sync(args, full=True)
    if args.command == "search":
        return run_search(args)
    if args.command == "ask":
        return run_ask(args)
    if args.command == "report":
        return run_report(args)
    if args.command == "digest":
        return run_digest(args)
    if args.command == "feishu-auth":
        return run_feishu_auth(args)
    if args.command == "feishu-backfill":
        return run_feishu_backfill(args)
    if args.command == "feishu-sync-doc":
        return run_feishu_sync_doc(args)
    if args.command == "run-daily":
        return run_daily(args)
    if args.command == "serve-bot":
        return run_serve_bot(args)
    if args.command == "stats":
        return run_stats()
    parser.error(f"Unknown command: {args.command}")
    return 2
