"""测试夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "status.db"


@pytest.fixture()
def app(db_path: Path):
    return create_app(f"sqlite:///{db_path}")


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def reopen(app, db_path: Path):
    """模拟服务重启：关闭当前应用并以同一数据库文件新建应用。"""

    def _reopen() -> TestClient:
        return TestClient(create_app(f"sqlite:///{db_path}"))

    return _reopen
