"""应用配置。"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATABASE_URL = "sqlite:///./data/student_status.db"


def database_url_from_env() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def ensure_sqlite_directory(database_url: str) -> None:
    """SQLite 文件库的父目录不存在时自动创建。"""
    path = sqlite_file_path(database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def sqlite_file_path(database_url: str) -> Path | None:
    """返回 SQLite 文件路径（内存库或非 SQLite 返回 None）。"""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw_path = database_url[len(prefix) :].split("?", 1)[0]
    if not raw_path or raw_path == ":memory:":
        return None
    return Path(raw_path).expanduser().resolve()
