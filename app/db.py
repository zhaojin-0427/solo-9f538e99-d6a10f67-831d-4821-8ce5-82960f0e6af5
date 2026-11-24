"""数据库连接与会话管理。"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import ensure_sqlite_directory


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class Database:
    """按数据库 URL 创建引擎与会话工厂。

    使用实例而不是模块级全局变量，便于测试隔离与验证“重启后继续读取同一
    SQLite 文件”的场景。
    """

    def __init__(self, url: str) -> None:
        self.url = url
        is_sqlite = url.startswith("sqlite:")
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        engine_kwargs = {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            # 内存库在连接关闭后即消失，必须复用同一个连接。
            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
            **engine_kwargs,
        )
        if is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )

    def create_all(self) -> None:
        # 先导入模型，确保元数据已完成注册。
        from . import models  # noqa: F401

        ensure_sqlite_directory(self.url)
        Base.metadata.create_all(self.engine)

    def session(self):
        return self.session_factory()

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        # WAL 让查询与提交互不阻塞；内存库会返回 memory，无副作用。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


