from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import Settings
from .feishu_client import FeishuApiError, FeishuClient
from .models import clean_body_text, clean_text


def _text_element(content: str) -> List[Dict[str, Any]]:
    return [{"text_run": {"content": content}}]


def paragraph_block(content: str) -> Dict[str, Any]:
    return {"block_type": 2, "text": {"elements": _text_element(content)}}


def heading1_block(content: str) -> Dict[str, Any]:
    return {"block_type": 3, "heading1": {"elements": _text_element(content)}}


def heading2_block(content: str) -> Dict[str, Any]:
    return {"block_type": 4, "heading2": {"elements": _text_element(content)}}


def heading3_block(content: str) -> Dict[str, Any]:
    return {"block_type": 5, "heading3": {"elements": _text_element(content)}}


def divider_block() -> Dict[str, Any]:
    return {"block_type": 22, "divider": {}}


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_block_id(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    return (
        item.get("block_id")
        or item.get("blockId")
        or item.get("block", {}).get("block_id")
        or item.get("children", [{}])[0].get("block_id", "")
    )


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


class FeishuDocSync:
    def __init__(self, settings: Settings, client: FeishuClient):
        self.settings = settings
        self.client = client
        self.state_path = settings.feishu_doc_state_path
        self.state = load_state(self.state_path)

    @property
    def document_id(self) -> str:
        return self.state.get("document_id", "")

    @property
    def document_url(self) -> str:
        return self.state.get("document_url", "")

    def _persist_state(self) -> None:
        save_state(self.state_path, self.state)

    def ensure_document(self) -> Dict[str, Any]:
        if self.document_id and self.state.get("items_root_block_id"):
            return self.state

        document = self.client.create_document(self.settings.feishu_doc_title)
        document_id = document["document_id"]
        document_url = document["url"]

        initial_blocks = [
            heading1_block("概览"),
            paragraph_block("这是一份自动同步的即刻收藏全文知识库，供飞书内直接搜索与回顾。"),
            paragraph_block("总条目数：0 | 已同步到飞书：0"),
            paragraph_block("首次回填时间：未开始 | 最近一次同步时间：未开始"),
            heading1_block("收藏条目"),
        ]
        created = self.client.create_blocks(document_id, document_id, initial_blocks)
        block_ids = [_extract_block_id(item) for item in created]

        if len(block_ids) < 5 or not all(block_ids[:5]):
            raise FeishuApiError("初始化飞书文档失败：未能获取概览与收藏条目块 ID")

        self.state = {
            "document_id": document_id,
            "document_url": document_url,
            "overview_block_id": block_ids[0],
            "purpose_block_id": block_ids[1],
            "stats_block_id": block_ids[2],
            "sync_block_id": block_ids[3],
            "items_root_block_id": block_ids[4],
            "month_sections": {},
        }
        self._persist_state()
        return self.state

    def ensure_month_section(self, month_key: str) -> str:
        self.ensure_document()
        month_sections = self.state.setdefault("month_sections", {})
        if month_key in month_sections:
            return month_sections[month_key]

        created = self.client.create_blocks(
            self.document_id,
            self.state["items_root_block_id"],
            [heading2_block(month_key)],
        )
        block_id = _extract_block_id(created[0]) if created else ""
        if not block_id:
            raise FeishuApiError(f"创建月份区块失败: {month_key}")
        month_sections[month_key] = block_id
        self._persist_state()
        return block_id

    def _update_or_append_block(self, state_key: str, parent_block_id: str, block_payload: Dict[str, Any]) -> str:
        block_id = self.state.get(state_key, "")
        if block_id:
            try:
                self.client.update_block(self.document_id, block_id, block_payload)
                return block_id
            except Exception:
                pass

        created = self.client.create_blocks(self.document_id, parent_block_id, [block_payload])
        new_block_id = _extract_block_id(created[0]) if created else ""
        if not new_block_id:
            raise FeishuApiError(f"无法创建飞书概览块: {state_key}")
        self.state[state_key] = new_block_id
        self._persist_state()
        return new_block_id

    def refresh_overview(self, total_items: int, synced_items: int) -> None:
        self.ensure_document()
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        first_backfill = self.state.get("first_backfill_at", "未开始")
        if first_backfill == "未开始" and synced_items > 0:
            first_backfill = now_text
            self.state["first_backfill_at"] = first_backfill
        self._update_or_append_block(
            "stats_block_id",
            self.state["overview_block_id"],
            paragraph_block(f"总条目数：{total_items} | 已同步到飞书：{synced_items}"),
        )
        self._update_or_append_block(
            "sync_block_id",
            self.state["overview_block_id"],
            paragraph_block(f"首次回填时间：{first_backfill} | 最近一次同步时间：{now_text}"),
        )
        self._persist_state()

    def _metadata_line(self, row) -> str:
        try:
            raw_json = json.loads(row["raw_json"])
        except Exception:
            raw_json = {}

        collect_time = clean_text(raw_json.get("collectTime"))
        created_at = clean_text(row["created_at"])
        author = clean_text(row["author_screen_name"])
        topic = clean_text(row["topic_name"])
        item_type = clean_text(row["item_type"])
        domain = clean_text(row["domain"])
        parts = [
            f"item_id: {row['id']}",
            f"收藏时间: {collect_time or '未知'}",
            f"发布时间: {created_at or '未知'}",
            f"作者: {author or '未知'}",
            f"主题: {topic or '无'}",
            f"类型: {item_type or '未知'}",
            f"域名: {domain or '无'}",
        ]
        return " | ".join(parts)

    def _link_line(self, row) -> str:
        try:
            raw_json = json.loads(row["raw_json"])
        except Exception:
            raw_json = {}
        jike_link = clean_text(raw_json.get("shareUrl")) or clean_text(row["source_url"])
        external_link = clean_text(row["link_url"])
        return f"即刻原帖: {jike_link or '无'} | 外链: {external_link or '无'}"

    def build_item_blocks(self, row) -> List[Dict[str, Any]]:
        display_title = _first_non_empty(clean_text(row["link_title"]), clean_text(row["title"]), "无标题")
        blocks: List[Dict[str, Any]] = [
            heading3_block(display_title),
            paragraph_block(self._metadata_line(row)),
            paragraph_block(self._link_line(row)),
        ]

        content = clean_body_text(row["content"])
        if content:
            for paragraph in content.split("\n\n"):
                paragraph = clean_body_text(paragraph)
                if paragraph:
                    blocks.append(paragraph_block(paragraph))
        else:
            blocks.append(paragraph_block("无正文"))

        blocks.append(divider_block())
        return blocks

    def append_item(self, row) -> str:
        self.ensure_document()
        month_key = clean_text(row["created_at"])[:7] or "未知月份"
        self.ensure_month_section(month_key)
        blocks = self.build_item_blocks(row)
        first_block_id = ""
        for start in range(0, len(blocks), 50):
            chunk = blocks[start : start + 50]
            created = self.client.create_blocks(
                self.document_id,
                self.state["items_root_block_id"],
                chunk,
            )
            if not first_block_id and created:
                first_block_id = _extract_block_id(created[0])
        return first_block_id
