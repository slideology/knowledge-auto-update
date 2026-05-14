from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Tuple

from ..config import Settings
from ..db import Database
from ..llm.client import ChatMessage, OpenAICompatibleClient
from ..models import LATIN_TOKEN_RE, build_match_query, clean_text, parse_iso_datetime

URL_RE = re.compile(r"https?://\S+")


def _cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_list = list(left)
    right_list = list(right)
    if not left_list or not right_list or len(left_list) != len(right_list):
        return 0.0
    dot = sum(a * b for a, b in zip(left_list, right_list))
    left_norm = math.sqrt(sum(a * a for a in left_list))
    right_norm = math.sqrt(sum(b * b for b in right_list))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass
class RetrievalHit:
    item_id: str
    source_type: str
    title: str
    content: str
    canonical_url: str
    source_author: str
    source_channel: str
    published_at: str
    score: float


@dataclass
class RetrievalResult:
    source_filter: str
    hits: List[RetrievalHit]


@dataclass
class AnswerResult:
    source_filter: str
    answer: str
    hits: List[RetrievalHit]


class KnowledgeBaseRetriever:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.llm = OpenAICompatibleClient(settings)

    def infer_source_filter(self, question: str) -> str:
        lowered = clean_text(question).lower()
        personal_markers = [
            "我收藏",
            "我存过",
            "我看过",
            "我之前",
            "我的收藏",
            "我收藏过",
            "我保存过",
            "即刻收藏",
            "即刻",
            "新增收藏",
            "收藏内容",
        ]
        public_markers = [
            "ai 圈",
            "ai圈",
            "ai 新闻",
            "ai新闻",
            "ai 热点",
            "ai热点",
            "openai 最近",
            "anthropic 最近",
            "google 最近",
            "最近 ai",
            "ai hot",
            "aihot",
        ]
        has_personal = any(marker in lowered for marker in personal_markers)
        has_public = any(marker in lowered for marker in public_markers)
        if "即刻" in lowered:
            has_personal = True
        if has_personal and has_public:
            return "all"
        if has_personal:
            return "jike"
        if has_public:
            return "aihot"
        return "all"

    def _fallback_query(self, question: str) -> str:
        cleaned = clean_text(question)
        for phrase in ["最近", "发了什么", "有什么", "看一下", "看下", "帮我找", "有哪些", "新闻", "热点"]:
            cleaned = cleaned.replace(phrase, " ")
        latin_tokens = [token for token in LATIN_TOKEN_RE.findall(cleaned) if len(token) >= 2]
        if latin_tokens:
            return " ".join(latin_tokens[:6])
        return cleaned.strip()

    def _infer_time_constraint(self, question: str, source_filter: str) -> Tuple[int, str] | None:
        lowered = clean_text(question).lower()
        days: int | None = None
        if any(token in lowered for token in ["过去7天", "最近7天", "过去 7 天", "最近 7 天", "前7天", "前 7 天"]):
            days = 7
        elif any(token in lowered for token in ["过去30天", "最近30天", "过去 30 天", "最近 30 天", "前30天", "前 30 天"]):
            days = 30

        if days is None:
            return None

        time_field = "published_at"
        if "新增" in lowered and ("收藏" in lowered or source_filter == "jike"):
            time_field = "first_seen_at"
        return days, time_field

    def _row_in_time_window(self, row, *, days: int, time_field: str) -> bool:
        value = row[time_field] if time_field in row.keys() else None
        parsed = parse_iso_datetime(value or "")
        if parsed is None:
            return False
        return parsed >= datetime.now(timezone.utc) - timedelta(days=days)

    def _make_hit(self, *, item_id: str, source_type: str, title: str, content: str, canonical_url: str, source_author: str, source_channel: str, published_at: str, score: float) -> RetrievalHit:
        return RetrievalHit(
            item_id=item_id,
            source_type=source_type,
            title=title,
            content=content,
            canonical_url=canonical_url,
            source_author=source_author,
            source_channel=source_channel,
            published_at=published_at,
            score=score,
        )

    def _content_signature(self, hit: RetrievalHit) -> str:
        body = clean_text(hit.content).lower()
        body = URL_RE.sub(" ", body)
        body = re.sub(r"\s+", " ", body).strip()
        title = clean_text(hit.title).lower()
        title = URL_RE.sub(" ", title)
        title = re.sub(r"\s+", " ", title).strip()
        core = body or title
        if len(core) > 240:
            core = core[:240]
        return f"{hit.source_type}|{title[:120]}|{core}"

    def retrieve(self, question: str, *, source_filter: str = "auto", limit: int = 5) -> RetrievalResult:
        effective_source = self.infer_source_filter(question) if source_filter == "auto" else source_filter
        time_constraint = self._infer_time_constraint(question, effective_source)
        match_query = build_match_query(question)
        lexical_rows = self.db.search(match_query, limit=25, source_filter=effective_source)
        if not lexical_rows:
            fallback = self._fallback_query(question)
            if fallback and fallback != clean_text(question):
                lexical_rows = self.db.search(build_match_query(fallback), limit=25, source_filter=effective_source)

        score_map: Dict[str, float] = {}
        candidate_ids: List[str] = []
        for row in lexical_rows:
            candidate_ids.append(row.item_id)
            score_map[row.item_id] = 1.0 / (1.0 + abs(float(row.score)))

        if len(candidate_ids) < 8:
            for row in self.db.fetch_recent_kb_rows(source_filter=effective_source, limit=12):
                item_id = row["item_id"]
                if item_id not in candidate_ids:
                    candidate_ids.append(item_id)

        candidate_rows = self.db.fetch_kb_candidate_rows(candidate_ids)
        if time_constraint:
            days, time_field = time_constraint
            candidate_rows = [row for row in candidate_rows if self._row_in_time_window(row, days=days, time_field=time_field)]
        if not candidate_rows and time_constraint:
            days, time_field = time_constraint
            recent_rows = self.db.fetch_recent_items(days=days, limit=max(limit * 4, 20), source_filter=effective_source, time_field=time_field)
            fallback_hits = [
                self._make_hit(
                    item_id=row["id"],
                    source_type=row["source_type"],
                    title=row["title"],
                    content=row["content"],
                    canonical_url=row["canonical_url"] or row["link_url"],
                    source_author=row["source_author"] or row["author_screen_name"],
                    source_channel=row["source_channel"] or row["topic_name"],
                    published_at=row[time_field] or row["published_at"] or row["created_at"],
                    score=1.0,
                )
                for row in recent_rows[:limit]
            ]
            deduped = []
            seen_signatures = set()
            for hit in fallback_hits:
                signature = self._content_signature(hit)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                deduped.append(hit)
                if len(deduped) >= limit:
                    break
            return RetrievalResult(source_filter=effective_source, hits=deduped)
        if not candidate_rows and lexical_rows:
            fallback_hits = [
                self._make_hit(
                    item_id=row.item_id,
                    source_type=row.source_type,
                    title=row.title,
                    content=row.content,
                    canonical_url=row.link_url,
                    source_author=row.author_screen_name,
                    source_channel=row.source_channel or row.topic_name,
                    published_at=row.published_at or row.created_at,
                    score=1.0 / (1.0 + abs(float(row.score))),
                )
                for row in lexical_rows[:limit]
            ]
            return RetrievalResult(source_filter=effective_source, hits=fallback_hits)
        if not candidate_rows:
            return RetrievalResult(source_filter=effective_source, hits=[])

        vector_scores: Dict[str, float] = {}
        if self.llm.is_configured():
            query_vector = self.llm.embed_texts([question])[0]
            embedding_rows = self.db.fetch_kb_embeddings_for_chunk_ids(
                [row["chunk_id"] for row in candidate_rows],
                self.settings.llm_embedding_model,
            )
            for embedding_row in embedding_rows:
                try:
                    vector = json.loads(embedding_row["embedding_vector"])
                except Exception:
                    vector = []
                vector_scores[embedding_row["chunk_id"]] = _cosine_similarity(query_vector, vector)

        hits: List[RetrievalHit] = []
        for row in candidate_rows:
            lexical_score = score_map.get(row["item_id"], 0.0)
            vector_score = vector_scores.get(row["chunk_id"], 0.0)
            total_score = lexical_score * 0.35 + vector_score * 0.65 if vector_scores else lexical_score
            hits.append(
                RetrievalHit(
                    item_id=row["item_id"],
                    source_type=row["source_type"],
                    title=row["title"],
                    content=row["content"],
                    canonical_url=row["canonical_url"],
                    source_author=row["source_author"] or row["author_screen_name"],
                    source_channel=row["source_channel"] or row["topic_name"],
                    published_at=row["published_at"],
                    score=total_score,
                )
            )

        hits.sort(key=lambda item: (item.score, item.published_at), reverse=True)
        deduped: List[RetrievalHit] = []
        seen = set()
        seen_signatures = set()
        for hit in hits:
            if hit.item_id in seen:
                continue
            signature = self._content_signature(hit)
            if signature in seen_signatures:
                continue
            deduped.append(hit)
            seen.add(hit.item_id)
            seen_signatures.add(signature)
            if len(deduped) >= limit:
                break
        return RetrievalResult(source_filter=effective_source, hits=deduped)

    def answer(self, question: str, *, source_filter: str = "auto", limit: int = 5) -> AnswerResult:
        result = self.retrieve(question, source_filter=source_filter, limit=limit)
        if not result.hits:
            return AnswerResult(
                source_filter=result.source_filter,
                answer="未找到相关内容。你可以换个关键词，或者明确说是查“我的收藏”还是“最近 AI 热点”。",
                hits=[],
            )

        if not self.llm.is_configured():
            lines = ["当前未配置 LLM，先返回命中的候选内容：", ""]
            for hit in result.hits:
                source_label = "即刻收藏" if hit.source_type == "jike" else "AIHOT 精选"
                lines.append(f"- [{source_label}] {hit.title} | {hit.published_at} | {hit.canonical_url}")
            return AnswerResult(source_filter=result.source_filter, answer="\n".join(lines), hits=result.hits)

        context_blocks = []
        for idx, hit in enumerate(result.hits, start=1):
            source_label = "即刻收藏" if hit.source_type == "jike" else "AIHOT 精选"
            context_blocks.append(
                "\n".join(
                    [
                        f"[{idx}] 来源: {source_label}",
                        f"[{idx}] 标题: {hit.title}",
                        f"[{idx}] 作者/来源: {hit.source_author or '未知'}",
                        f"[{idx}] 频道/分类: {hit.source_channel or '无'}",
                        f"[{idx}] 时间: {hit.published_at}",
                        f"[{idx}] 链接: {hit.canonical_url or '无'}",
                        f"[{idx}] 正文: {hit.content or '无正文'}",
                    ]
                )
            )

        system_prompt = (
            "你是一个知识库问答助手。只能基于提供的上下文回答，不能编造未命中的信息。"
            "先给简短结论，再列出命中的内容，且明确标注来源是“即刻收藏”还是“AIHOT 精选”。"
        )
        context_text = "\n\n".join(context_blocks)
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"候选上下文：\n\n{context_text}\n\n"
            "请输出中文回答。要求："
            "1. 先给简短结论；"
            "2. 如命中多条，按相关性列 3-5 条；"
            "3. 每条带标题、来源、时间和链接；"
            "4. 如果信息不足，明确说明。"
        )
        answer_text = self.llm.chat(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        return AnswerResult(source_filter=result.source_filter, answer=answer_text, hits=result.hits)
