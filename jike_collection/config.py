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
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        data[key] = value
    return data


def _get_env(key: str, env_file: Dict[str, str], default: str = "") -> str:
    return os.environ.get(key, env_file.get(key, default))


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
    timeout: int
    page_size: int
    feishu_timeout: int
    feishu_auth_timeout: int
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
        timeout=int(_get_env("JIKE_TIMEOUT", env_file, "20")),
        page_size=int(_get_env("JIKE_PAGE_SIZE", env_file, "20")),
        feishu_timeout=int(_get_env("FEISHU_TIMEOUT", env_file, "20")),
        feishu_auth_timeout=int(_get_env("FEISHU_AUTH_TIMEOUT", env_file, "300")),
        user_agent="knowledge-auto-update/0.1",
    )
