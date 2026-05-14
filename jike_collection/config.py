from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


def _parse_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'").strip('"')
    return data


def _get_env(key: str, env_file: Dict[str, str], default: str = "") -> str:
    return os.environ.get(key, env_file.get(key, default))


def _get_int_env(key: str, env_file: Dict[str, str], default: int) -> int:
    value = _get_env(key, env_file, str(default))
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class Settings:
    root_dir: Path
    data_dir: Path
    reports_dir: Path
    db_path: Path
    auth_state_path: Path
    feishu_user_token_path: Path
    feishu_doc_state_path: Path
    access_token: str
    refresh_token: str
    feishu_webhook_url: str
    feishu_app_id: str
    feishu_app_secret: str
    feishu_redirect_uri: str
    feishu_doc_title: str
    feishu_scopes: List[str]
    feishu_bot_app_id: str
    feishu_bot_app_secret: str
    feishu_event_verify_token: str
    feishu_event_encrypt_key: str
    feishu_allowed_open_id: str
    bot_public_base_url: str
    llm_base_url: str
    llm_api_key: str
    llm_chat_model: str
    llm_embedding_model: str
    llm_timeout: int
    timeout: int
    page_size: int
    feishu_timeout: int
    feishu_auth_timeout: int
    aihot_enabled: bool
    aihot_user_agent: str
    aihot_sync_days: int
    aihot_backfill_days: int
    user_agent: str


def load_settings(root_dir: Optional[Path] = None) -> Settings:
    root = root_dir or Path.cwd()
    env_file = _parse_env_file(root / ".env")

    data_dir = root / "data"
    reports_dir = root / _get_env("JIKE_REPORTS_DIR", env_file, "reports")
    db_path = root / _get_env("JIKE_DB_PATH", env_file, "data/jike_collection.db")
    auth_state_path = root / _get_env("JIKE_AUTH_STATE_PATH", env_file, "data/jike_auth.json")
    feishu_user_token_path = root / _get_env("FEISHU_USER_TOKEN_FILE", env_file, "data/feishu_user_token.json")
    feishu_doc_state_path = root / _get_env("FEISHU_DOC_STATE_FILE", env_file, "data/feishu_doc_state.json")
    feishu_scopes = _get_env(
        "FEISHU_SCOPES",
        env_file,
        "offline_access docx:document docx:document:readonly",
    ).split()

    aihot_enabled = _get_env("AIHOT_ENABLED", env_file, "1").lower() not in {"0", "false", "no"}
    aihot_user_agent = _get_env(
        "AIHOT_UA",
        env_file,
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )

    feishu_bot_app_id = _get_env("FEISHU_BOT_APP_ID", env_file) or _get_env("FEISHU_APP_ID", env_file)
    feishu_bot_app_secret = _get_env("FEISHU_BOT_APP_SECRET", env_file) or _get_env("FEISHU_APP_SECRET", env_file)

    return Settings(
        root_dir=root,
        data_dir=data_dir,
        reports_dir=reports_dir,
        db_path=db_path,
        auth_state_path=auth_state_path,
        feishu_user_token_path=feishu_user_token_path,
        feishu_doc_state_path=feishu_doc_state_path,
        access_token=_get_env("JIKE_ACCESS_TOKEN", env_file),
        refresh_token=_get_env("JIKE_REFRESH_TOKEN", env_file),
        feishu_webhook_url=_get_env("FEISHU_WEBHOOK_URL", env_file),
        feishu_app_id=_get_env("FEISHU_APP_ID", env_file),
        feishu_app_secret=_get_env("FEISHU_APP_SECRET", env_file),
        feishu_redirect_uri=_get_env("FEISHU_REDIRECT_URI", env_file, "http://127.0.0.1:8787/callback"),
        feishu_doc_title=_get_env("FEISHU_DOC_TITLE", env_file, "即刻收藏知识库"),
        feishu_scopes=feishu_scopes,
        feishu_bot_app_id=feishu_bot_app_id,
        feishu_bot_app_secret=feishu_bot_app_secret,
        feishu_event_verify_token=_get_env("FEISHU_EVENT_VERIFY_TOKEN", env_file),
        feishu_event_encrypt_key=_get_env("FEISHU_EVENT_ENCRYPT_KEY", env_file),
        feishu_allowed_open_id=_get_env("FEISHU_ALLOWED_OPEN_ID", env_file),
        bot_public_base_url=_get_env("BOT_PUBLIC_BASE_URL", env_file),
        llm_base_url=_get_env("LLM_BASE_URL", env_file),
        llm_api_key=_get_env("LLM_API_KEY", env_file),
        llm_chat_model=_get_env("LLM_CHAT_MODEL", env_file),
        llm_embedding_model=_get_env("LLM_EMBEDDING_MODEL", env_file),
        llm_timeout=_get_int_env("LLM_TIMEOUT", env_file, 60),
        timeout=_get_int_env("JIKE_TIMEOUT", env_file, 20),
        page_size=_get_int_env("JIKE_PAGE_SIZE", env_file, 20),
        feishu_timeout=_get_int_env("FEISHU_TIMEOUT", env_file, 20),
        feishu_auth_timeout=_get_int_env("FEISHU_AUTH_TIMEOUT", env_file, 300),
        aihot_enabled=aihot_enabled,
        aihot_user_agent=aihot_user_agent,
        aihot_sync_days=_get_int_env("AIHOT_SYNC_DAYS", env_file, 2),
        aihot_backfill_days=_get_int_env("AIHOT_BACKFILL_DAYS", env_file, 7),
        user_agent="knowledge-auto-update/0.2",
    )
