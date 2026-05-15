from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import Database
from ..llm.client import ChatMessage, OpenAICompatibleClient
from ..models import utc_now_iso


@dataclass
class DigestSummary:
    success: bool
    digest_date: str
    jike_count: int
    aihot_count: int
    summary_markdown: str
    note: str
    error: str = ""


class DailyDigestService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.llm = OpenAICompatibleClient(settings)
        self.local_tz = ZoneInfo("Asia/Shanghai")

    def _date_window(self, digest_date: date) -> tuple[str, str]:
        local_start = datetime.combine(digest_date, time.min, tzinfo=self.local_tz)
        local_end = local_start + timedelta(days=1)
        utc_start = local_start.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        utc_end = local_end.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        return utc_start, utc_end

    def _format_item_lines(self, rows, source_label: str, limit: int = 8) -> str:
        lines = []
        for row in rows[:limit]:
            lines.append(
                "\n".join(
                    [
                        f"- 标题: {row['title']}",
                        f"  来源: {source_label}",
                        f"  作者/来源名: {row['source_author'] or row['author_screen_name'] or '未知'}",
                        f"  分类/主题: {row['source_channel'] or row['topic_name'] or '无'}",
                        f"  时间: {row['published_at'] if source_label == 'AIHOT 精选' else row['first_seen_at']}",
                        f"  链接: {row['canonical_url'] or row['link_url'] or row['source_url']}",
                        f"  内容: {(row['content'] or '')[:240]}",
                    ]
                )
            )
        return "\n".join(lines)

    def _fallback_summary(self, digest_date: str, jike_rows, aihot_rows) -> str:
        lines = [
            f"# {digest_date} 收藏摘要",
            "",
            f"- 你的新增收藏：{len(jike_rows)} 条",
            f"- AIHOT 热点：{len(aihot_rows)} 条",
            "",
        ]
        if jike_rows:
            lines.append("## 你的新增收藏")
            lines.append("")
            for row in jike_rows[:5]:
                lines.append(f"- {row['title']} | {row['canonical_url'] or row['link_url'] or row['source_url']}")
            lines.append("")
        else:
            lines.extend(["## 你的新增收藏", "", "- 昨日没有新增收藏", ""])
        if aihot_rows:
            lines.append("## AIHOT 热点")
            lines.append("")
            for row in aihot_rows[:5]:
                lines.append(f"- {row['title']} | {row['canonical_url'] or row['link_url'] or row['source_url']}")
            lines.append("")
        else:
            lines.extend(["## AIHOT 热点", "", "- 昨日没有命中 AIHOT 热点", ""])
        return "\n".join(lines)

    def build_digest(self, digest_date: date | None = None) -> DigestSummary:
        target_date = digest_date or (datetime.now(self.local_tz).date() - timedelta(days=1))
        digest_date_str = target_date.isoformat()
        run_id = self.db.start_digest_run(digest_date_str, utc_now_iso())
        start_iso, end_iso = self._date_window(target_date)
        jike_rows = self.db.fetch_items_in_window("jike", "first_seen_at", start_iso, end_iso)
        aihot_rows = self.db.fetch_items_in_window("aihot", "published_at", start_iso, end_iso)

        try:
            llm_error = ""
            if self.llm.is_configured():
                prompt = (
                    f"请基于下面两组材料，为 {digest_date_str} 生成一份中文 Markdown 日报。\n\n"
                    "输出结构固定为：\n"
                    "1. 今日概览\n"
                    "2. 你的新增收藏\n"
                    "3. AIHOT 热点对照\n"
                    "4. 值得回看的内容\n"
                    "5. 可能漏看的热点\n\n"
                    "要求：\n"
                    "- 不要编造未提供的信息\n"
                    "- 显式区分“你的新增收藏”和“AIHOT 热点”\n"
                    "- 写出两者的重合点与差异点\n"
                    "- 如果任一侧为空，要明确说明\n\n"
                    f"你的新增收藏（{len(jike_rows)} 条）：\n{self._format_item_lines(jike_rows, '即刻收藏')}\n\n"
                    f"AIHOT 热点（{len(aihot_rows)} 条）：\n{self._format_item_lines(aihot_rows, 'AIHOT 精选')}\n"
                )
                try:
                    summary_markdown = self.llm.chat(
                        [
                            ChatMessage(
                                role="system",
                                content="你是一个内容分析助手，负责把私人收藏与外部 AI 热点做对照总结。",
                            ),
                            ChatMessage(role="user", content=prompt),
                        ],
                        temperature=0.2,
                        max_tokens=1600,
                    )
                except Exception as exc:
                    llm_error = str(exc)
                    summary_markdown = self._fallback_summary(digest_date_str, jike_rows, aihot_rows)
                    summary_markdown += (
                        "\n\n> 备注：LLM 摘要服务临时不可用，本次使用基础摘要模板。"
                    )
            else:
                summary_markdown = self._fallback_summary(digest_date_str, jike_rows, aihot_rows)

            self.db.finish_digest_run(
                run_id,
                finished_at=utc_now_iso(),
                status="success",
                jike_item_count=len(jike_rows),
                aihot_item_count=len(aihot_rows),
                summary_markdown=summary_markdown,
                webhook_sent=False,
                error="",
            )
            return DigestSummary(
                success=True,
                digest_date=digest_date_str,
                jike_count=len(jike_rows),
                aihot_count=len(aihot_rows),
                summary_markdown=summary_markdown,
                note="digest generated with fallback" if llm_error else "digest generated",
            )
        except Exception as exc:
            error_message = str(exc)
            self.db.finish_digest_run(
                run_id,
                finished_at=utc_now_iso(),
                status="error",
                jike_item_count=len(jike_rows),
                aihot_item_count=len(aihot_rows),
                summary_markdown="",
                webhook_sent=False,
                error=error_message,
            )
            return DigestSummary(
                success=False,
                digest_date=digest_date_str,
                jike_count=len(jike_rows),
                aihot_count=len(aihot_rows),
                summary_markdown="",
                note="digest failed",
                error=error_message,
            )
