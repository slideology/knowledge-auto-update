from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import Settings


API_BASE = "https://api.ruguoapp.com"
COLLECTIONS_LIST_PATH = "/1.0/collections/list"
REFRESH_TOKEN_PATH = "/app_auth_tokens.refresh"
ORIGINAL_POST_GET_PATH = "/1.0/originalPosts/get"
REPOST_GET_PATH = "/1.0/reposts/get"


class JikeApiError(RuntimeError):
    pass


@dataclass
class PageResult:
    items: List[Dict[str, Any]]
    load_more_key: Optional[str]


class JikeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._auth_state = self._load_auth_state()

    def _load_auth_state(self) -> Dict[str, str]:
        path = self.settings.auth_state_path
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    @property
    def access_token(self) -> str:
        return self._auth_state.get("access_token") or self.settings.access_token

    @property
    def refresh_token(self) -> str:
        return self._auth_state.get("refresh_token") or self.settings.refresh_token

    def _save_auth_state(self, access_token: str, refresh_token: str) -> None:
        self.settings.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }
        self.settings.auth_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._auth_state = payload

    def _request(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        method: str = "POST",
        allow_refresh: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.settings.user_agent,
        }
        if self.access_token:
            headers["x-jike-access-token"] = self.access_token
        if extra_headers:
            headers.update(extra_headers)

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = Request(API_BASE + path, data=data, headers=headers, method=method)

        try:
            with urlopen(request, timeout=self.settings.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and allow_refresh and self.refresh_token:
                self.refresh_access_token()
                return self._request(path, payload, method=method, allow_refresh=False, extra_headers=extra_headers)

            try:
                error_payload = json.loads(body)
                message = error_payload.get("error") or error_payload.get("message") or body
            except Exception:
                message = body
            raise JikeApiError(f"HTTP {exc.code} for {path}: {message}") from exc

    def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise JikeApiError("缺少 JIKE_REFRESH_TOKEN，无法刷新 access token")

        response = self._request(
            REFRESH_TOKEN_PATH,
            {},
            extra_headers={"x-jike-refresh-token": self.refresh_token},
            allow_refresh=False,
        )
        new_access = response.get("x-jike-access-token") or ""
        new_refresh = response.get("x-jike-refresh-token") or self.refresh_token
        if not new_access:
            raise JikeApiError("刷新 token 失败，接口没有返回新的 access token")
        self._save_auth_state(new_access, new_refresh)

    def _request_item_detail(self, path: str, item_id: str) -> Dict[str, Any]:
        response = self._request(f"{path}?id={item_id}", payload=None, method="GET")
        data = response.get("data")
        if not isinstance(data, dict):
            raise JikeApiError(f"{path} 返回格式异常: {response}")
        return data

    def fetch_item_detail(self, item: Dict[str, Any]) -> Dict[str, Any]:
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if not item_id:
            return item

        if item_type == "ORIGINAL_POST":
            detail = self._request_item_detail(ORIGINAL_POST_GET_PATH, item_id)
        elif item_type == "REPOST":
            detail = self._request_item_detail(REPOST_GET_PATH, item_id)
        else:
            return item

        merged = dict(item)
        merged.update(detail)
        for key in ("collectTime", "collected"):
            if key in item:
                merged[key] = item[key]
        return merged

    def fetch_collection_page(self, load_more_key: Optional[str] = None, limit: Optional[int] = None) -> PageResult:
        if not self.access_token:
            raise JikeApiError("缺少 JIKE_ACCESS_TOKEN，请先在 .env 中配置")

        payload: Dict[str, Any] = {"limit": limit or self.settings.page_size}
        if load_more_key:
            payload["loadMoreKey"] = load_more_key

        response = self._request(COLLECTIONS_LIST_PATH, payload)
        items = response.get("data") or []
        if not isinstance(items, list):
            raise JikeApiError(f"collections/list 返回格式异常: {response}")
        return PageResult(items=items, load_more_key=response.get("loadMoreKey"))

    def iter_collection_pages(self, *, limit: Optional[int] = None, max_pages: Optional[int] = None) -> Generator[PageResult, None, None]:
        load_more_key: Optional[str] = None
        page_number = 0

        while True:
            page = self.fetch_collection_page(load_more_key=load_more_key, limit=limit)
            yield page
            page_number += 1

            if not page.load_more_key:
                break
            if max_pages is not None and page_number >= max_pages:
                break

            load_more_key = page.load_more_key
