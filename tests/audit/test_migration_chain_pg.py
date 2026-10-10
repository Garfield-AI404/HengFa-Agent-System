"""迁移链全新库集成测试（D25，16.7 上报处理决策，方案 A）。

背景：D3（2cbb7f06dcfa）已创建 trg_audit_events_append_only（函数
f_audit_events_append_only），D8（92d64bb42eca）原实现再创建同名触发器 →
任何全新库 upgrade head 都因 DuplicateObject 失败（12.2-3 全新环境部署
演练实测暴露）。项目负责人 2026-08-23 批准方案 A：D8 upgrade 先 DROP IF
EXISTS D3 对象再创建自身对象，downgrade 对称恢复 D3 对象（变更面仅触发器
对象、行为等价；供 RV-04/RV-11 复核）。

本测试在独立数据库（每运行 uuid 后缀）上完整复现 12.2-3 全新环境口径：
从零 upgrade head 成功 → 终态触发器为 D8 函数 → downgrade 恢复 D3 对象
→ 再 upgrade head 循环可用 → 清理。默认跳过（未设置 TEST_AGENT_DATABASE_URL
或容器未启动）；在 D7 演示容器（hengfa-pg-15432）或等效 PG 上执行
（POSTGRES_USER 需具备 CREATE/DROP DATABASE 权限，官方镜像 agent 用户即
超级用户）。

⚠ 对 TEST_URL 的库名是**安全的**：``_with_db()`` / ``_chain_db_name()`` 会把
库名替换为一次性的 ``agent_migchain_<uuid>`` 库，**不会作用于 TEST_URL 指向
的库本身**（终审实跑复查：零残留、agent 库未被触碰）。示例 DSN 用隔离库口径：

    TEST_AGENT_DATABASE_URL=postgresql://agent:agent_demo_pw@127.0.0.1:15432/agent_migchain_demo \
        uv run pytest tests/audit/test_migration_chain_pg.py -q
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# uv run 会把项目根加入 sys.path，而仓库根 alembic/ 目录（迁移脚本目录，
# 含 D2-D5 占位 __init__.py）会遮蔽 site-packages 的 alembic 包，导致
# `from alembic import command` 失败（d12-d21 demo 的 upgrade 分支从未
# 真正执行过的原因之一）。先移除该项目根项；src 包由 uv run 的
# PYTHONPATH 提供（src/ 仍在 sys.path），不受影响。
sys.path = [
    p for p in sys.path
    if p and os.path.normcase(os.path.abspath(p)) != os.path.normcase(str(_REPO_ROOT))
]

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from administrative_agent.persistence.database import normalize_db_url  # noqa: E402

TEST_URL = os.getenv("TEST_AGENT_DATABASE_URL")
if TEST_URL is None:
    pytest.skip(
        "TEST_AGENT_DATABASE_URL 未设置：真实 PG 集成测试跳过",
        allow_module_level=True,
    )

_HEAD_REVISION = "a7d1e4f9c2b3"  # 2026-09-19 新增 chat_stream_events 迁移后前移
_D3_REVISION = "2cbb7f06dcfa"
_TRIGGER = "trg_audit_events_append_only"
_D8_FUNCTION = "audit_events_append_only_protect"
_D3_FUNCTION = "f_audit_events_append_only"


def _chain_db_name() -> str:
    return f"agent_migchain_{uuid.uuid4().hex[:8]}"


def _admin_engine() -> Engine:
    """管理连接（postgres 库）——建库/删库用。

    注意：不用 SQLAlchemy URL 对象（str(URL) 默认把密码渲染为 ***），
    与 normalize_db_url 一致用纯字符串替换。
    """
    return create_engine(normalize_db_url(_with_db("postgres")), pool_pre_ping=True)


def _with_db(db: str) -> str:
    """把 TEST_URL 的库名替换为 db（纯字符串，保留密码原文）。"""
    return TEST_URL.rsplit("/", 1)[0] + "/" + db


def _run_alembic(
    url: str,
    revision: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    downgrade: bool = False,
) -> None:
    # pytest 在收集阶段之后会把 rootdir（仓库根）重新放入 sys.path[0]，
    # 遮蔽 site-packages 的 alembic 包（仓库根 alembic/ 是迁移脚本目录，
    # 占位 __init__.py 无 command 属性）——仅在导入 alembic 的窗口内临时
    # 移除该项目根项，导入成功后恢复（sys.modules 缓存 site-packages 版本，
    # 后续调用不受影响）。
    _orig_path = sys.path[:]
    sys.path = [
        p for p in _orig_path
        if p and os.path.normcase(os.path.abspath(p)) != os.path.normcase(str(_REPO_ROOT))
    ]
    try:
        from alembic import command
        from alembic.config import Config

        import administrative_agent.config as config_module

        monkeypatch.setattr(config_module.settings, "agent_database_url", url)
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        if downgrade:
            command.downgrade(cfg, revision)
        else:
            command.upgrade(cfg, revision)
    finally:
        sys.path[:] = _orig_path


def _q(engine: Engine, sql: str) -> list[str]:
    with engine.connect() as conn:
        return [str(r[0]) for r in conn.execute(text(sql))]


def test_fresh_database_chain_upgrade_downgrade_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """12.2-3 口径：全新库从零 upgrade head 一次成功；downgrade→upgrade 循环完整。

    核心回归点：D3→D8 不再因重名触发器 DuplicateObject（方案 A 实证）。
    """
    db_name = _chain_db_name()
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))  # 不可达则抛 DBAPIError（下方 skip）
    except DBAPIError as exc:  # pragma: no cover - 容器未启动
        admin.dispose()
        pytest.skip(f"PostgreSQL 不可达（{exc}）")

    target_url = normalize_db_url(_with_db(db_name))
    try:
        # ---- 幂等开头（O-1 先例）：清理上一轮残留，全新建库 ----
        # DROP/CREATE DATABASE 不能在事务块内执行，用 AUTOCOMMIT 连接。
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {db_name}"))

        # ---- 全新库 upgrade head（16.7 方案 A 核心实证）----
        _run_alembic(target_url, "head", monkeypatch)
        engine = create_engine(target_url)
        try:
            assert _q(engine, "SELECT version_num FROM alembic_version") == [
                _HEAD_REVISION
            ], "全新库 upgrade head 后 alembic_version 应为最新 revision（12.2-3）"

            # 终态触发器 = D8 函数；D3 函数已清理（不残留）
            trigger_defs = _q(
                engine,
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger"
                f" WHERE tgname = '{_TRIGGER}'",
            )
            assert len(trigger_defs) == 1, "审计 append-only 触发器在位"
            assert _D8_FUNCTION in trigger_defs[0], "终态触发器执行 D8 函数"
            assert _D3_FUNCTION not in trigger_defs[0], "D3 函数不应残留"
            assert _q(
                engine,
                f"SELECT proname FROM pg_proc WHERE proname = '{_D3_FUNCTION}'",
            ) == [], "D3 函数 f_audit_events_append_only 应已被 D8 upgrade 清理"

            # server_default 生效（O-1/RV-04 决策 c 保持）
            assert _q(
                engine,
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name = 'audit_events' AND column_name = 'source'",
            ) == ["'agent'::character varying"]

            # ---- downgrade 到 D3：对称恢复 D3 对象（方案 A 对称性）----
            _run_alembic(target_url, _D3_REVISION, monkeypatch, downgrade=True)
            assert _q(engine, "SELECT version_num FROM alembic_version") == [_D3_REVISION]
            trigger_defs = _q(
                engine,
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger"
                f" WHERE tgname = '{_TRIGGER}'",
            )
            assert _D3_FUNCTION in trigger_defs[0], "downgrade 后触发器恢复为 D3 函数"
            assert _D8_FUNCTION not in trigger_defs[0], "D8 函数应随 downgrade 移除"

            # ---- 再 upgrade head：循环可用（不残留、不重复）----
            _run_alembic(target_url, "head", monkeypatch)
            assert _q(engine, "SELECT version_num FROM alembic_version") == [
                _HEAD_REVISION
            ], "downgrade→upgrade 循环后回到 head"
            trigger_defs = _q(
                engine,
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger"
                f" WHERE tgname = '{_TRIGGER}'",
            )
            assert len(trigger_defs) == 1 and _D8_FUNCTION in trigger_defs[0]
        finally:
            engine.dispose()
    finally:
        # ---- 清理（幂等结尾：独立库删除，不残留）----
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"))
        admin.dispose()
