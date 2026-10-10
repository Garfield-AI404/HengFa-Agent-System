"""checkpointer 工厂测试（D7/RV-10）：共库默认、fail closed、PostgresStore 关闭。

真实连接池的构造不在单测中触发（FakePool 替换 ConnectionPool）；
真实 PostgreSQL 连通由 scripts/d7_checkpoint_demo.py 提供运行证据。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import administrative_agent
import administrative_agent.agent.checkpoint as ckpt
from administrative_agent.agent.checkpoint import (
    CheckpointUnconfiguredError,
    checkpointer_factory,
)
from administrative_agent.config import Settings


class FakePool:
    """记录 dsn 与 kwargs 的假连接池（PostgresSaver 构造不校验类型）。"""

    def __init__(self, dsn: str, **kwargs) -> None:
        self.dsn = dsn
        self.kwargs = kwargs


@pytest.fixture
def fake_pool(monkeypatch) -> list[FakePool]:
    """monkeypatch ConnectionPool，返回已创建实例列表（供断言 dsn）。"""
    created: list[FakePool] = []

    def _make(dsn: str, **kwargs) -> FakePool:
        pool = FakePool(dsn, **kwargs)
        created.append(pool)
        return pool

    monkeypatch.setattr(ckpt, "ConnectionPool", _make)
    return created


def test_factory_shared_db_fallback(fake_pool: list[FakePool]) -> None:
    """CHECKPOINT_DATABASE_URL 未配置时回退 AGENT_DATABASE_URL（共库默认）。"""
    settings = Settings(
        agent_database_url="postgresql://agent:pw@127.0.0.1:15432/agent",
        checkpoint_database_url="",
    )
    saver = checkpointer_factory(settings)
    assert saver.conn is fake_pool[0]
    assert fake_pool[0].dsn == settings.agent_database_url
    # 建连超时防挂死
    assert fake_pool[0].kwargs["kwargs"]["connect_timeout"] == 5


def test_factory_prefers_checkpoint_url(fake_pool: list[FakePool]) -> None:
    """CHECKPOINT_DATABASE_URL 配置时优先（允许分库部署）。"""
    settings = Settings(
        agent_database_url="postgresql://agent:pw@127.0.0.1:15432/agent",
        checkpoint_database_url="postgresql://agent:pw@127.0.0.1:15433/cp",
    )
    checkpointer_factory(settings)
    assert fake_pool[0].dsn == settings.checkpoint_database_url


def test_factory_fail_closed_when_unconfigured(fake_pool: list[FakePool]) -> None:
    """两个连接串都未配置：fail closed，不降级为内存 checkpoint。"""
    settings = Settings(agent_database_url="", checkpoint_database_url="")
    with pytest.raises(CheckpointUnconfiguredError):
        checkpointer_factory(settings)
    assert fake_pool == []  # 未尝试连接


def test_factory_rejects_bad_url(monkeypatch) -> None:
    """URL 非法：连接池构造失败时抛 CheckpointError（fail closed）。"""

    class _ExplodingPool:
        def __init__(self, *args, **kwargs) -> None:
            raise ValueError("invalid dsn")

    monkeypatch.setattr(ckpt, "ConnectionPool", _ExplodingPool)
    settings = Settings(agent_database_url="not-a-url", checkpoint_database_url="")
    with pytest.raises(ckpt.CheckpointError):
        checkpointer_factory(settings)


def test_postgres_store_not_exported() -> None:
    """8.5 末条：一期不初始化 LangGraph PostgresStore——模块不导出 store 工厂。"""
    assert not hasattr(ckpt, "PostgresStore")
    assert not hasattr(ckpt, "postgres_store_factory")
    store_names = [n for n in dir(ckpt) if "store" in n.lower()]
    assert store_names == []


def test_postgres_store_not_instantiated_in_module_source() -> None:
    """checkpoint.py 源码不 import / 不实例化 PostgresStore。"""
    source = inspect.getsource(ckpt)
    assert "PostgresStore(" not in source
    assert "import PostgresStore" not in source
    assert "postgres_store" not in source


def test_postgres_store_disabled_whole_src() -> None:
    """整个 src 包无任何 PostgresStore 实例化/导入（跨单元防回归）。"""
    root = Path(administrative_agent.__file__).parent
    hits: list[str] = []
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "PostgresStore(" in line or "PostgresStore as " in line:
                hits.append(f"{path.relative_to(root)}:{lineno}")
    assert hits == []
