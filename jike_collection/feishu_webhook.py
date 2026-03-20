from __future__ import annotations

import json
from typing import Optional
from urllib.request import Request, urlopen

from .config import Settings
from .workflows import FeishuDocSummary, SyncSummary


class FeishuWebhookError(RuntimeError):
    pass


class FeishuWebhookSender:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.webhook_url = settings.feishu_webhook_url

    def is_enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_daily_summary(
        self,
        sync_summary: SyncSummary,
        doc_summary: Optional[FeishuDocSummary] = None,
    ) -> bool:
        if not self.is_enabled():
            return False

        inserted = sync_summary.inserted
        doc_summary = doc_summary or FeishuDocSummary(
            success=False,
            mode="none",
            attempted=0,
            written=0,
            failed=0,
            document_url="",
            note="文档同步未执行",
        )

        lines = [
            f"**即刻同步**: {'成功' if sync_summary.success else '失败'}",
            f"**今日新增收藏**: {inserted}",
            f"**即刻更新条数**: inserted={sync_summary.inserted}, updated={sync_summary.updated}",
            f"**飞书文档写入**: {'成功' if doc_summary.success else '部分失败/未执行'}",
            f"**飞书追加条数**: {doc_summary.written}",
            f"**飞书失败条数**: {doc_summary.failed}",
        ]

        if inserted == 0:
            lines.append("**今日结论**: 今日无新增收藏，文档未追加内容")
        else:
            lines.append("**今日结论**: 今日有新增收藏，已尝试同步到飞书文档")

        if doc_summary.document_url:
            lines.append(f"**飞书文档**: [打开知识库]({doc_summary.document_url})")

        if sync_summary.error:
            lines.append(f"**即刻错误**: {sync_summary.error[:200]}")
        elif sync_summary.note:
            lines.append(f"**即刻备注**: {sync_summary.note[:200]}")

        if doc_summary.error:
            lines.append(f"**飞书错误**: {doc_summary.error[:200]}")
        elif doc_summary.note:
            lines.append(f"**飞书备注**: {doc_summary.note[:200]}")

        payload = {
            "msg_type": "interactive",
            "card": self._build_card("即刻收藏每日同步", lines),
        }
        return self._send_payload(payload)

    def send_generic_summary(self, title: str, lines: list[str]) -> bool:
        if not self.is_enabled():
            return False
        payload = {
            "msg_type": "interactive",
            "card": self._build_card(title, lines),
        }
        return self._send_payload(payload)

    def _build_card(self, title: str, lines: list[str]) -> dict:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": "\n".join(lines),
                }
            ],
        }

    def _send_payload(self, payload: dict) -> bool:
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.settings.feishu_timeout) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            if data.get("StatusCode") == 0 or data.get("code") == 0:
                return True
            raise FeishuWebhookError(f"飞书 webhook 发送失败: {data}")
