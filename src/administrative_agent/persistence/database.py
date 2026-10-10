"""SQLAlchemy 2.0 engine/session（规格 2.6 persistence/database.py）。

连接串只从 AGENT_DATABASE_URL 环境变量注入（规格 15.2、12.4），
不硬编码；未配置时 fail closed。LangGraph checkpoint 的连接
（CHECKPOINT_DATABASE_URL）属 D7，不在本模块。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from administrative_agent.config import settings


def normalize_db_url(url: str) -> str:
    """规范化 PostgreSQL 连接串 driver 前缀。

    规格 15.2 的 AGENT_DATABASE_URL 写作 ``postgresql://…``；SQLAlchemy
    对该前缀默认使用 psycopg2，而本项目依赖 psycopg[binary]（psycopg 3）。
    显式绑定 psycopg3 driver（``postgresql+psycopg://``），已带 driver 的
    连接串原样通过（允许部署方覆盖为其他 driver）。
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _build_engine() -> Engine:
    url = settings.agent_database_url
    if not url:
        raise RuntimeError(
            "AGENT_DATABASE_URL 未配置：数据库连接串只从环境变量注入（规格 15.2），"
            "不得硬编码；生产使用密钥管理注入"
        )
    return create_engine(normalize_db_url(url), pool_pre_ping=True)


engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """会话上下文：正常提交、异常回滚、始终关闭。

    业务写路径（D6/D8+）统一经此边界管理 Agent 本地事务。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
