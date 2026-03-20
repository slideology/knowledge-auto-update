from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse


TYPE_LABELS = {
    "ORIGINAL_POST": "原创",
    "REPOST": "转发",
    "QUESTION": "提问",
    "ANSWER": "回答",
    "PERSONAL_UPDATE": "个人更新",
    "MEDIUM": "文章",
}

CJK_BLOCK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
WHITESPACE_RE = re.compile(r"\s+")
LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass
class NormalizedItem:
    item_id: str
    item_type: str
    title: str
    content: str
    link_title: str
    link_url: str
    author_screen_name: str
    author_username: str
    topic_id: str
    topic_name: str
    source_url: str
    created_at: str
    first_seen_at: str
    last_seen_at: str
    has_images: int
    has_video: int
    has_audio: int
    domain: str
    search_blob: str
    raw_json: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = WHITESPACE_RE.sub(" ", value).strip()
    return value


def clean_body_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)

    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in value.split("\n")]

    normalized: List[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if not previous_blank and normalized:
                normalized.append("")
            previous_blank = True
            continue
        normalized.append(WHITESPACE_RE.sub(" ", line))
        previous_blank = False

    while normalized and normalized[-1] == "":
        normalized.pop()

    return "\n".join(normalized)


def truncate_text(text: str, limit: int = 80) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _best_link(item: Dict[str, Any]) -> str:
    link_info = item.get("linkInfo") or {}
    link_url = link_info.get("originalLinkUrl") or link_info.get("linkUrl") or ""
    if link_url:
        return link_url

    item_id = item.get("id", "")
    item_type = item.get("type", "")
    if item_type == "REPOST":
        return f"https://m.okjike.com/reposts/{item_id}"
    if item_type == "MEDIUM":
        return f"https://www.okjike.com/medium/{item_id}"
    if item_type == "QUESTION":
        return f"https://m.okjike.com/questions/{item_id}"
    return f"https://m.okjike.com/originalPosts/{item_id}"


def _extract_domain(link_url: str) -> str:
    if not link_url:
        return ""
    try:
        return urlparse(link_url).netloc.lower()
    except Exception:
        return ""


def _extract_content(item: Dict[str, Any]) -> str:
    candidates: List[str] = []

    if item.get("content"):
        candidates.append(item["content"])
    if item.get("title"):
        candidates.append(item["title"])

    question = item.get("question") or {}
    if question.get("title"):
        candidates.append(question["title"])

    link_info = item.get("linkInfo") or {}
    if link_info.get("title"):
        candidates.append(link_info["title"])

    topic = item.get("topic") or {}
    if topic.get("content"):
        candidates.append(topic["content"])

    target = item.get("target") or {}
    if target.get("content"):
        target_content = clean_body_text(target["content"])
        if target_content:
            candidates.append("转发内容\n" + target_content)
    target_link_info = target.get("linkInfo") or {}
    if target_link_info.get("title"):
        candidates.append(target_link_info["title"])

    unique: List[str] = []
    seen = set()
    for candidate in candidates:
        text = clean_body_text(candidate)
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    return "\n".join(unique)


def _extract_title(item: Dict[str, Any], content: str) -> str:
    link_info = item.get("linkInfo") or {}
    topic = item.get("topic") or {}

    base = (
        clean_text(item.get("content"))
        or clean_text(item.get("title"))
        or clean_text(link_info.get("title"))
        or clean_text(topic.get("content"))
        or content
    )
    label = TYPE_LABELS.get(item.get("type", ""), item.get("type", "动态"))
    short = truncate_text(base or "无标题收藏", 72)
    return f"{label}: {short}"


def _cjk_ngrams(text: str, min_n: int = 2, max_n: int = 3) -> Iterable[str]:
    for match in CJK_BLOCK.finditer(text):
        block = match.group(0)
        for n in range(min_n, max_n + 1):
            if len(block) < n:
                continue
            for idx in range(len(block) - n + 1):
                yield block[idx : idx + n]


def build_search_blob(*parts: str) -> str:
    base = " ".join(part for part in (clean_text(p) for p in parts) if part)
    grams = list(dict.fromkeys(_cjk_ngrams(base)))
    if grams:
        return base + " " + " ".join(grams)
    return base


def build_match_query(query: str) -> str:
    clean_query = clean_text(query)
    latin_tokens = [token.lower() for token in LATIN_TOKEN_RE.findall(clean_query)]
    cjk_tokens = list(dict.fromkeys(_cjk_ngrams(clean_query)))
    tokens = list(dict.fromkeys(latin_tokens + cjk_tokens))

    if not tokens and clean_query:
        tokens = [clean_query]
    if not tokens:
        return '""'

    return " AND ".join(f'"{token}"' for token in tokens[:24])


def normalize_item(item: Dict[str, Any], seen_at: Optional[str] = None) -> NormalizedItem:
    now = seen_at or utc_now_iso()
    content = _extract_content(item)
    link_info = item.get("linkInfo") or {}
    user = item.get("user") or {}
    topic = item.get("topic") or {}
    target = item.get("target") or {}

    link_title = clean_text(link_info.get("title"))
    link_url = _best_link(item)
    author_screen_name = clean_text(user.get("screenName"))
    author_username = clean_text(user.get("username"))
    topic_id = clean_text(topic.get("id"))
    topic_name = clean_text(topic.get("content"))
    created_at = clean_text(item.get("createdAt")) or now
    title = _extract_title(item, content)
    has_images = 1 if item.get("pictures") or target.get("pictures") else 0
    has_video = 1 if item.get("video") or (link_info.get("video") if link_info else None) else 0
    has_audio = 1 if item.get("audio") or (link_info.get("audio") if link_info else None) else 0
    domain = _extract_domain(link_url)

    search_blob = build_search_blob(
        title,
        content,
        link_title,
        link_url,
        author_screen_name,
        author_username,
        topic_name,
        domain,
    )

    return NormalizedItem(
        item_id=str(item["id"]),
        item_type=clean_text(item.get("type")) or "UNKNOWN",
        title=title,
        content=content,
        link_title=link_title,
        link_url=link_url,
        author_screen_name=author_screen_name,
        author_username=author_username,
        topic_id=topic_id,
        topic_name=topic_name,
        source_url=link_url,
        created_at=created_at,
        first_seen_at=now,
        last_seen_at=now,
        has_images=has_images,
        has_video=has_video,
        has_audio=has_audio,
        domain=domain,
        search_blob=search_blob,
        raw_json=json.dumps(item, ensure_ascii=False, sort_keys=True),
    )
