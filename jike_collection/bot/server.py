from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from ..config import Settings
from ..db import Database
from ..feishu_client import FeishuApiError, FeishuClient
from ..kb.retriever import KnowledgeBaseRetriever


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return str(parsed.get("text") or parsed.get("title") or "").strip()
        except Exception:
            return content.strip()
    if isinstance(content, dict):
        return str(content.get("text") or content.get("title") or "").strip()
    return ""


def _strip_mentions(text: str) -> str:
    cleaned = text
    for token in ["@_user_1", "@机器人", "@bot"]:
        cleaned = cleaned.replace(token, "")
    return cleaned.strip()


class FeishuBotService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.client = FeishuClient(settings)
        self.retriever = KnowledgeBaseRetriever(self.db, settings)

    def close(self) -> None:
        self.db.close()

    def handle_event(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if payload.get("challenge"):
            if self.settings.feishu_event_verify_token:
                token = payload.get("token") or payload.get("header", {}).get("token") or ""
                if token and token != self.settings.feishu_event_verify_token:
                    return 403, {"error": "invalid token"}
            return 200, {"challenge": payload["challenge"]}

        if payload.get("encrypt"):
            return 400, {"error": "encrypted Feishu events are not supported; disable event encryption first"}

        token = payload.get("token") or payload.get("header", {}).get("token") or ""
        if self.settings.feishu_event_verify_token and token and token != self.settings.feishu_event_verify_token:
            return 403, {"error": "invalid token"}

        header = payload.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return 200, {"ok": True}

        event = payload.get("event") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        open_id = sender_id.get("open_id") or sender_id.get("user_id") or ""
        if self.settings.feishu_allowed_open_id and open_id != self.settings.feishu_allowed_open_id:
            return 200, {"ok": True}

        message = event.get("message") or {}
        chat_type = message.get("chat_type") or ""
        mentions = message.get("mentions") or []
        if chat_type != "p2p" and not mentions:
            return 200, {"ok": True}

        text = _strip_mentions(_extract_message_text(message.get("content")))
        if not text:
            return 200, {"ok": True}

        answer = self.retriever.answer(text)
        receive_id_type = "open_id"
        receive_id = open_id
        if chat_type != "p2p":
            receive_id_type = "chat_id"
            receive_id = message.get("chat_id") or open_id

        try:
            self.client.send_message(
                receive_id_type=receive_id_type,
                receive_id=receive_id,
                msg_type="text",
                content={"text": answer.answer[:3800]},
            )
        except FeishuApiError as exc:
            return 500, {"error": str(exc)}
        return 200, {"ok": True}


def serve_bot(settings: Settings, host: str = "0.0.0.0", port: int = 8788) -> None:
    service = FeishuBotService(settings)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # type: ignore[override]
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._write_json(200, {"ok": True})
                return
            self._write_json(404, {"error": "not found"})

        def do_POST(self):  # type: ignore[override]
            parsed = urlparse(self.path)
            if parsed.path != "/feishu/events":
                self._write_json(404, {"error": "not found"})
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self._write_json(400, {"error": "invalid json"})
                return
            status, body = service.handle_event(payload)
            self._write_json(status, body)

        def log_message(self, format, *args):  # type: ignore[override]
            return

        def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # A single-process bot is enough for our low-volume Feishu callbacks, and
    # keeping requests on one thread avoids sqlite thread-affinity errors.
    httpd = HTTPServer((host, port), Handler)
    try:
        httpd.serve_forever()
    finally:
        service.close()
