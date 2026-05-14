from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuApiError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._app_access_token: Optional[str] = None
        self._tenant_access_token: Optional[str] = None
        self._tenant_access_token_expires_at: float = 0

    def _token_path(self) -> Path:
        return self.settings.feishu_user_token_path

    def get_authorization_url(self, state: Optional[str] = None) -> Tuple[str, str]:
        actual_state = state or secrets.token_urlsafe(24)
        params = {
            "app_id": self.settings.feishu_app_id,
            "redirect_uri": self.settings.feishu_redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.settings.feishu_scopes),
            "state": actual_state,
        }
        return (
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(params),
            actual_state,
        )

    def _load_user_token(self) -> Optional[Dict[str, Any]]:
        path = self._token_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_user_token(self, payload: Dict[str, Any]) -> None:
        token_data = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(payload.get("expires_in", 0)),
            "refresh_expires_at": int(time.time()) + int(payload.get("refresh_expires_in", 0)),
            "token_type": payload.get("token_type", "Bearer"),
        }
        path = self._token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_refresh: bool = False,
        retries: int = 3,
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else FEISHU_API_BASE + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": self.settings.user_agent,
        }
        if headers:
            request_headers.update(headers)

        request = Request(url, data=data, headers=request_headers, method=method)

        for attempt in range(retries):
            try:
                with urlopen(request, timeout=self.settings.feishu_timeout) as response:
                    raw = response.read().decode("utf-8")
                    data = json.loads(raw) if raw else {}
                    if isinstance(data, dict) and data.get("code") not in (None, 0):
                        raise FeishuApiError(f"Feishu API error for {path}: {data}")
                    return data
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 401 and allow_refresh:
                    self.refresh_user_access_token()
                    request_headers["Authorization"] = f"Bearer {self.get_user_access_token()}"
                    request = Request(url, data=data, headers=request_headers, method=method)
                    continue
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise FeishuApiError(f"HTTP {exc.code} for {path}: {body}") from exc

    def get_app_access_token(self) -> str:
        if self._app_access_token:
            return self._app_access_token
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            raise FeishuApiError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")

        data = self._request_json(
            "POST",
            "/auth/v3/app_access_token/internal/",
            payload={"app_id": self.settings.feishu_app_id, "app_secret": self.settings.feishu_app_secret},
            retries=1,
        )
        token = data.get("app_access_token")
        if not token:
            raise FeishuApiError(f"获取飞书 app_access_token 失败: {data}")
        self._app_access_token = token
        return token

    def get_tenant_access_token(self) -> str:
        if self._tenant_access_token and self._tenant_access_token_expires_at - 60 > time.time():
            return self._tenant_access_token
        if not self.settings.feishu_bot_app_id or not self.settings.feishu_bot_app_secret:
            raise FeishuApiError("缺少 FEISHU_BOT_APP_ID 或 FEISHU_BOT_APP_SECRET")

        data = self._request_json(
            "POST",
            "/auth/v3/tenant_access_token/internal/",
            payload={
                "app_id": self.settings.feishu_bot_app_id,
                "app_secret": self.settings.feishu_bot_app_secret,
            },
            retries=1,
        )
        token = data.get("tenant_access_token")
        if not token:
            raise FeishuApiError(f"获取飞书 tenant_access_token 失败: {data}")
        self._tenant_access_token = token
        self._tenant_access_token_expires_at = time.time() + int(data.get("expire", 7200))
        return token

    def exchange_code_for_user_token(self, code: str) -> Dict[str, Any]:
        app_access_token = self.get_app_access_token()
        data = self._request_json(
            "POST",
            "/authen/v1/access_token",
            headers={"Authorization": f"Bearer {app_access_token}"},
            payload={
                "grant_type": "authorization_code",
                "code": code,
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
            retries=1,
        )
        token_payload = data.get("data", {})
        if not token_payload.get("access_token"):
            raise FeishuApiError(f"获取飞书 user_access_token 失败: {data}")
        self._save_user_token(token_payload)
        return token_payload

    def refresh_user_access_token(self) -> Dict[str, Any]:
        token_data = self._load_user_token()
        if not token_data or not token_data.get("refresh_token"):
            raise FeishuApiError("未找到可刷新的飞书用户 token，请先执行 feishu-auth")

        app_access_token = self.get_app_access_token()
        data = self._request_json(
            "POST",
            "/authen/v1/refresh_access_token",
            headers={"Authorization": f"Bearer {app_access_token}"},
            payload={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
            retries=1,
        )
        token_payload = data.get("data", {})
        if not token_payload.get("access_token"):
            raise FeishuApiError(f"刷新飞书 user_access_token 失败: {data}")
        self._save_user_token(token_payload)
        return token_payload

    def get_user_access_token(self) -> str:
        token_data = self._load_user_token()
        if not token_data:
            raise FeishuApiError("未找到飞书用户授权，请先执行 feishu-auth")
        if token_data.get("expires_at", 0) - 60 > time.time():
            return token_data["access_token"]
        refreshed = self.refresh_user_access_token()
        return refreshed["access_token"]

    def _user_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_user_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _tenant_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def create_document(self, title: str) -> Dict[str, str]:
        data = self._request_json(
            "POST",
            "/docx/v1/documents",
            payload={"title": title},
            headers=self._user_headers(),
            allow_refresh=True,
        )
        document = data.get("data", {}).get("document") or data.get("data", {})
        document_id = document.get("document_id") or document.get("documentId") or document.get("obj_token") or ""
        if not document_id:
            raise FeishuApiError(f"创建飞书文档失败，返回缺少 document_id: {data}")
        document_url = document.get("url") or f"https://feishu.cn/docx/{document_id}"
        return {"document_id": document_id, "url": document_url}

    def create_blocks(self, document_id: str, parent_block_id: str, children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        data = self._request_json(
            "POST",
            f"/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
            payload={"children": children, "index": -1},
            headers=self._user_headers(),
            allow_refresh=True,
        )
        body = data.get("data", {})
        result_children = body.get("children") or body.get("items") or []
        if isinstance(result_children, list):
            return result_children
        return []

    def update_block(self, document_id: str, block_id: str, block_payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self._request_json(
            "PATCH",
            f"/docx/v1/documents/{document_id}/blocks/{block_id}",
            payload=block_payload,
            headers=self._user_headers(),
            allow_refresh=True,
        )
        return data.get("data", {}) or {}

    def send_message(
        self,
        *,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: Dict[str, Any],
    ) -> Dict[str, Any]:
        if receive_id_type not in {"open_id", "chat_id"}:
            raise FeishuApiError(f"Unsupported receive_id_type: {receive_id_type}")
        data = self._request_json(
            "POST",
            f"/im/v1/messages?receive_id_type={receive_id_type}",
            payload={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            headers=self._tenant_headers(),
            retries=2,
        )
        return data.get("data", {}) or {}
