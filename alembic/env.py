"""Alembic 环境。

- URL 只从环境变量 AGENT_DATABASE_URL 注入（规格 15.2），未配置时 fail closed。
- target_metadata 挂载 Agent 自有模型（12.1 六张表）；只管理 Agent 数据库（12.2 规则 5）。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from administrative_agent.config import settings
from administrative_agent.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = settings.agent_database_url
    if not url:
        raise RuntimeError(
            "AGENT_DATABASE_URL 未配置：Alembic 的数据库 URL 只从环境变量注入，"
            "不读取 alembic.ini 中的占位值"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from administrative_agent.persistence.database import normalize_db_url

    connectable = create_engine(
        normalize_db_url(_database_url()), poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
