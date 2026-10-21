from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """所有 ORM 表的基类."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def state_backend() -> str:
    """memory(本地)或 sql(MySQL/SQLite 持久化)."""
    return os.getenv("AGENT_STATE_BACKEND", "memory").strip().lower() or "memory"


def database_url() -> str:
    """生产使用 MySQL URL,本地测试可用 SQLite."""
    return os.getenv("AGENT_DATABASE_URL", "sqlite:///./agent_state.db").strip()


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = database_url()
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=_connect_args(url),
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个事务一个 Session;异常回滚,正常提交."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """创建表;生产建议换成 Alembic 迁移."""
    from db import models  # noqa: F401

    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """测试用:释放连接并清空缓存."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
