from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..config import Settings


class LLMConfigurationError(RuntimeError):
    pass


class LLMApiError(RuntimeError):
    pass


@dataclass
class ChatMessage:
    role: str
    content: str


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_api_key and self.settings.llm_chat_model)

    def _require(self, *, embeddings: bool = False) -> None:
        if not self.settings.llm_base_url or not self.settings.llm_api_key:
            raise LLMConfigurationError("缺少 LLM_BASE_URL 或 LLM_API_KEY")
        if embeddings and not self.settings.llm_embedding_model:
            raise LLMConfigurationError("缺少 LLM_EMBEDDING_MODEL")
        if not embeddings and not self.settings.llm_chat_model:
            raise LLMConfigurationError("缺少 LLM_CHAT_MODEL")

    def _endpoint(self, path: str) -> str:
        return self.settings.llm_base_url.rstrip("/") + path

    def _request_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = Request(
            self._endpoint(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "User-Agent": self.settings.user_agent,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.llm_timeout) as response:
                raw = response.read().decode("utf-8")
                try:
                    return json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise LLMApiError(f"{path} 返回了非 JSON 响应: {raw[:200]}") from exc
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LLMApiError(f"HTTP {exc.code} for {path}: {body}") from exc

    def embed_texts(self, texts: Iterable[str]) -> List[List[float]]:
        self._require(embeddings=True)
        items = list(texts)
        if not items:
            return []
        payload = {
            "model": self.settings.llm_embedding_model,
            "input": items,
        }
        data = self._request_json("/embeddings", payload)
        vectors = []
        for row in data.get("data", []):
            embedding = row.get("embedding")
            if isinstance(embedding, list):
                vectors.append([float(value) for value in embedding])
        if len(vectors) != len(items):
            raise LLMApiError(f"embeddings 返回数量异常: expected={len(items)} actual={len(vectors)}")
        return vectors

    def chat(
        self,
        messages: List[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> str:
        self._require()
        payload = {
            "model": self.settings.llm_chat_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = self._request_json("/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise LLMApiError(f"chat/completions 返回为空: {data}")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            return "".join(parts).strip()
        raise LLMApiError(f"无法解析 chat/completions 内容: {data}")
