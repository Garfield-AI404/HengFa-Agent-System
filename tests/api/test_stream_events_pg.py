"""事件流落库的**真实 PostgreSQL** 集成测试（可观测性第 0 步；方案 §八 17-19）。

默认跳过（未设置 ``TEST_AGENT_DATABASE_URL``）。

⚠ **绝不要指向开发库 / 生产库**：本文件的 ``_gone()`` 会对
``chat_stream_events`` 执行 ``upgrade head → downgrade -1 → upgrade head``，
其中 ``downgrade`` 就是 **DROP TABLE**——指向真实库即清掉 14 天窗口的**全部行**。
**必须指向一次性隔离库**（示例库名 ``agent_obs_pgtest``，先建好）执行：

    TEST_AGENT_DATABASE_URL=postgresql+psycopg://agent:...@127.0.0.1:15432/agent_obs_pgtest \\
        python -m pytest tests/api/test_stream_events_pg.py -q

三项（**有效性 ≠ 存在性**——本仓踩过「变异比判据弱 → 全绿」）：
- 17 迁移对称：upgrade head → 表/索引/PK 存在 → downgrade -1 → 全消失 →
  再 upgrade head 可用；
- 18 **约束有效性**：真插非法行必须抛错（8000 字符 ≈8030 字节 payload 超限 /
  `[]`::jsonb / chunk 事件非空 payload / sequence=-1 / 非法 record_kind），
  且 4000 字符 ≈4020 字节**恰好低于阈值必须放行**（两侧边界 + 直读约束定义）；
- 19 幂等：同一 event_id 连插两次 → 只有 1 行。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _strip_repo_root_from_sys_path() -> None:
    """把仓库根从 ``sys.path`` 剔除，消除其对 site-packages ``alembic`` 的遮蔽。

    仓库根 ``alembic/`` 目录（迁移脚本目录，含占位 ``__init__.py``）会被
    解释器当成 alembic 包，``from alembic import command`` 因而报
    ImportError——这正是 d12-d21 demo 的 upgrade 分支从未真正执行过的原因
    之一。先移除该项目根项；src 包由 editable 安装的 .pth 提供（src/ 仍在
    sys.path），不受影响。写法与 tests/audit/test_migration_chain_pg.py 一致。

    注意：**必须在每次 ``import alembic`` 之前调用**，而非只在模块顶部调用
    一次——pytest 在收集期（``tests/`` 是包，conftest 以包形式导入）会把
    仓库根重新塞回 sys.path，模块顶部的过滤到用例执行时已失效（实测）。
    """
    sys.path[:] = [
        p for p in sys.path
        if p
        and os.path.normcase(os.path.abspath(p))
        != os.path.normcase(str(_REPO_ROOT))
    ]


_strip_repo_root_from_sys_path()

import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from administrative_agent.persistence.database import normalize_db_url  # noqa: E402
from administrative_agent.web.chat_streams import StreamStore  # noqa: E402
from administrative_agent.web.stream_persistence import (  # noqa: E402
    StreamEventRecord,
    StreamEventSink,
)
from administrative_agent.web.stream_projection import (  # noqa: E402
    PROJECTION,
    project,
    project_suppressed,
)

TEST_URL = os.getenv("TEST_AGENT_DATABASE_URL")
if TEST_URL is None:
    pytest.skip(
        "TEST_AGENT_DATABASE_URL 未设置：真实 PG 集成测试跳过",
        allow_module_level=True,
    )

_RUN_ID = uuid.uuid4().hex[:8]

_INSERT_SQL = text(
    "INSERT INTO chat_stream_events"
    " (event_id, stream_id, event_type, record_kind, sequence, payload)"
    " VALUES (:event_id, :stream_id, :event_type, :record_kind, :sequence,"
    " CAST(:payload AS jsonb))"
)


def _alembic_command():
    """惰性取 ``alembic.command``（导入前先剔除仓库根的遮蔽，见上）。"""
    _strip_repo_root_from_sys_path()
    from alembic import command

    return command


def _alembic_config():
    _strip_repo_root_from_sys_path()
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(normalize_db_url(TEST_URL), pool_pre_ping=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        eng.dispose()
        pytest.skip(f"测试 PG 不可达：{exc}")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def _pin_alembic_to_test_db() -> Iterator[None]:
    """把 alembic 的目标库钉死在 ``TEST_AGENT_DATABASE_URL`` 上（消除 footgun）。

    ⚠️ **背景**：``alembic/env.py`` 的 ``_database_url()`` 只读
    ``settings.agent_database_url``（= ``AGENT_DATABASE_URL``），与本文件断言
    所用的 ``TEST_AGENT_DATABASE_URL`` 是**两个**变量。本文件 docstring 的示例
    命令只设后者——于是 ``_gone()`` / ``test_migration_round_trip`` 里的
    ``downgrade -1`` 会把 ``AGENT_DATABASE_URL`` 指向的库（开发库）的
    ``chat_stream_events`` **DROP 后重建**。2026-09-19 因此已两次打中开发库。

    **修法**：``config.settings`` 是模块级单例（``settings = get_settings()``），
    而 env.py 每次迁移都现场读 ``settings.agent_database_url``。故在**模块作用
    域**把它钉到本文件的 ``TEST_URL``，迁移目标与断言目标合一。此后**只设
    ``TEST_AGENT_DATABASE_URL`` 一个变量也安全**——不再要求调用方同时设两个
    （那正是要被消灭的口径）。退出时还原，避免泄漏到同进程其它用例。

    app under test 不受影响：``tests/api`` 的被测应用用 SQLite 注入
    （``app.state.session_factory``，见 ``tests/api/conftest.py``），不读该属性。
    """
    from administrative_agent.config import settings

    original = settings.agent_database_url
    settings.agent_database_url = TEST_URL
    try:
        yield
    finally:
        settings.agent_database_url = original


def _gone(engine: Engine) -> None:
    """回到 head（干净状态，且约束与迁移文件逐字一致）。

    仅 ``upgrade head`` 在库已位于 head 时是 no-op——若迁移文件在本批迭代中
    被改（如约束收紧），旧约束不会被重建。故：先 ``upgrade head`` 归位，再
    ``downgrade -1`` 落掉本迁移，最后 ``upgrade head`` 按**当前文件**重建。
    使断言与源码一致，且与用例执行顺序无关。
    """
    command = _alembic_command()
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# 17. 迁移对称
# ---------------------------------------------------------------------------


def test_migration_round_trip(engine):
    command = _alembic_command()

    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    insp = inspect(engine)
    assert "chat_stream_events" in insp.get_table_names()
    indexes = {i["name"] for i in insp.get_indexes("chat_stream_events")}
    assert {
        "ix_chat_stream_events_stream_seq",
        "ix_chat_stream_events_thread_created",
        "ix_chat_stream_events_task_created",
        "ix_chat_stream_events_created_at",
    } <= indexes
    pk = insp.get_pk_constraint("chat_stream_events")
    assert pk["constrained_columns"] == ["event_id"]

    # downgrade -1 → 表消失；再 upgrade head 可用
    command.downgrade(cfg, "-1")
    assert "chat_stream_events" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    assert "chat_stream_events" in inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# 18. 约束有效性（真插非法行必须失败）
# ---------------------------------------------------------------------------


def _insert(engine: Engine, **overrides) -> None:
    params = {
        "event_id": f"pg-{_RUN_ID}-{uuid.uuid4().hex[:8]}",
        "stream_id": f"stream-{_RUN_ID}",
        "event_type": "agent_started",
        "record_kind": "pushed",
        "sequence": 1,
        "payload": "{}",
    }
    params.update(overrides)
    with engine.begin() as conn:
        conn.execute(_INSERT_SQL, params)


_SIZE_CONSTRAINT = "ck_chat_stream_events_payload_size"
_PAYLOAD_SIZE_LIMIT = 4096


def _jsonb_size(engine: Engine, payload: str) -> int:
    """payload 作为 jsonb 落库后的实际字节数（= 约束里 pg_column_size 的口径）。"""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_column_size(CAST(:p AS jsonb))"), {"p": payload}
        ).scalar_one()


def _constraint_def(engine: Engine, name: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = :name"
                "   AND conrelid = 'chat_stream_events'::regclass"
            ),
            {"name": name},
        ).scalar_one()


def test_payload_size_constraint_is_4096_bytes(engine):
    """直接读约束定义——改的是值就断言值（本仓「断言与被改变量同层」教训）。

    只钉「大的被拦」不够：把上限整体调小到荒谬值（如 <= 100）时，这条
    字面量断言与本文件下方的「刚好在阈值之下必须放行」对照会一起变红。
    """
    _gone(engine)
    definition = _constraint_def(engine, _SIZE_CONSTRAINT)
    assert "pg_column_size(payload) <= 4096" in definition, definition


def test_payload_size_constraint_rejects_oversize(engine):
    _gone(engine)
    oversize = '{"text": "' + "x" * 8000 + '"}'  # ≈8030 字节，明显高于 4096
    assert _jsonb_size(engine, oversize) > _PAYLOAD_SIZE_LIMIT  # 探针确实在阈值之上
    with pytest.raises(IntegrityError):
        _insert(engine, payload=oversize)


def test_payload_size_constraint_allows_just_below_limit(engine):
    """对照：刚好在阈值之下必须放行（把上限调小到荒谬值时会变红）。"""
    _gone(engine)
    below = '{"text": "' + "x" * 4000 + '"}'  # ≈4020 字节，刚好低于 4096
    assert _jsonb_size(engine, below) < _PAYLOAD_SIZE_LIMIT  # 探针确实在阈值之下
    _insert(engine, payload=below)  # 不得抛错


def test_payload_must_be_object(engine):
    with pytest.raises(IntegrityError):
        _insert(engine, payload="[]")


_TEXT_EVENTS_CONSTRAINT = "ck_chat_stream_events_text_events_empty"
_CHUNK_EVENT_TYPES = ("agent_reply_chunk", "agent_reasoning_chunk")


@pytest.mark.parametrize("event_type", _CHUNK_EVENT_TYPES)
def test_chunk_events_must_have_empty_payload(engine, event_type):
    """chunk 事件的 payload **只允许 ``chunk_chars`` 这一个数字键**。

    这不是「必须为空」——投影把 ``chunk`` 原文降维成 ``{"chunk_chars": N}``
    （``stream_projection`` 规则 11/12），约束必须放行该真实形态；旧约束
    「必须等于 ``{}``」与投影互斥，PG 上会让整批写入被拒（P0）。
    同时仍 fail closed：非数字值 / 夹带其它键 / 缺键全部必须被拦。
    """
    _gone(engine)

    # 1. 应用的真实形态：{"chunk_chars": N} 必须放行（数字）
    _insert(engine, event_type=event_type, payload='{"chunk_chars": 3}')
    # 2. 空对象也合法（投影在 chunk 缺失时落 {"chunk_chars": 0}，此处覆盖 {}）
    _insert(engine, event_type=event_type, payload="{}")
    # 3. 非数字 chunk_chars 必须被拦
    with pytest.raises(IntegrityError):
        _insert(engine, event_type=event_type, payload='{"chunk_chars": "abc"}')
    # 4. 夹带其它键必须被拦（锁不放松：删掉 chunk_chars 后剩余必须为空）
    with pytest.raises(IntegrityError):
        _insert(
            engine, event_type=event_type, payload='{"chunk_chars": 3, "extra": 1}'
        )
    # 5. 完全没有 chunk_chars 必须被拦
    with pytest.raises(IntegrityError):
        _insert(engine, event_type=event_type, payload='{"other": 1}')


def test_chunk_constraint_definition_pins_chunk_chars(engine):
    """直接读约束定义——改的是语义就断言语义（措辞可调，键名与算子不可松）。"""
    _gone(engine)
    definition = _constraint_def(engine, _TEXT_EVENTS_CONSTRAINT)
    assert "chunk_chars" in definition, definition
    assert "jsonb_typeof" in definition, definition
    # 删键后要求剩余为空的收紧算子：把约束放宽回「必须为空」或「任意放行」
    # 都会让这条变红
    assert "'{}'::jsonb" in definition, definition


def test_sequence_must_be_non_negative(engine):
    with pytest.raises(IntegrityError):
        _insert(engine, sequence=-1)


def test_illegal_record_kind_rejected(engine):
    with pytest.raises(IntegrityError):
        _insert(engine, record_kind="totally_bogus")


# ---------------------------------------------------------------------------
# 19. 幂等：同一 event_id 连插两次 → 1 行
# ---------------------------------------------------------------------------


def test_same_event_id_insert_is_idempotent(engine):
    _gone(engine)
    event_id = f"pg-{_RUN_ID}-idem"
    params = {
        "event_id": event_id,
        "stream_id": f"stream-{_RUN_ID}",
        "event_type": "agent_started",
        "record_kind": "pushed",
        "sequence": 1,
        "payload": "{}",
    }
    with engine.begin() as conn:
        stmt = text(
            "INSERT INTO chat_stream_events"
            " (event_id, stream_id, event_type, record_kind, sequence, payload)"
            " VALUES (:event_id, :stream_id, :event_type, :record_kind, :sequence,"
            " CAST(:payload AS jsonb)) ON CONFLICT (event_id) DO NOTHING"
        )
        conn.execute(stmt, params)
        conn.execute(stmt, params)
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM chat_stream_events WHERE event_id = :e"),
            {"e": event_id},
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# 20. 端到端：17 类事件按 project() 真实产出经 Sink 写入迁移后的 PG
#
# 这条是**唯一**能永久关闭「应用投影 ↔ 迁移约束」漂移的一族：SQLite 夹具的
# 建表 DDL 里没有 PG-only 约束；上面 17-19 只做「约束存在性/有效性」；走图
# 的端到端用例用的 ScriptedDecisionClient 根本不发 chunk。P0（chunk payload
# 与「必须为空」约束互斥 → PG 上整批写入被拒）正是从这三层的缝隙里漏出来的。
# ---------------------------------------------------------------------------

#: 每类事件的**真实发射形态**样本（多余键会被投影丢弃，正是要覆盖的路径）。
#: 键集合必须与 ``PROJECTION`` 完全一致——新增事件类型时此处不同步即红。
_SAMPLE_RAW: dict[str, dict] = {
    "task_failed": {"error": "boom"},
    "task_cancelled": {"reason": "generation_cancelled", "task_id": "t1"},
    "SESSION_EXPIRED": {"reason": "session_expired"},
    "agent_started": {"thread_id": "th-1"},
    "tool_running": {"task_id": "t1", "tool_name": "erp_query", "stage": "start"},
    "tool_result": {
        "task_id": "t1",
        "tool_name": "erp_query",
        "ok": True,
        "summary": "含客户张三与金额 1234",
    },
    "confirmation_requested": {
        "task_id": "t1",
        "tool_name": "finance_create",
        "payload_hash": "hash-1",
        "card": {"company_name": "张三", "amount": 1234},
    },
    "agent_reply": {"reply_text": "回复张三的内容", "next_action": "reply"},
    "agent_error": {"error": "execution_error", "tool_name": "erp_query", "message": "人话"},
    "agent_reply_start": {},
    "agent_reply_chunk": {"chunk": "增量文本"},
    "agent_reasoning_chunk": {"chunk": "思考文本"},
    "memory_compressed": {
        "segment_no": 1,
        "tier": "hot",
        "summary_chars": 3,
        "archived_messages": 2,
        "index": "【段1】索引",
    },
    "memory_compression_failed": {"error": "summary_failed"},
    "reconcile_report": {
        "report_id": "r1",
        "row_count": 5,
        "subject_match_status": "matched",
        "summary_counts": {"a": 1},
    },
    "export_ready": {
        "file_id": "f1",
        "entity": "metal_orders",
        "row_count": 1,
        "total": 1,
        "expires_at": "2026-01-01T00:00:00Z",
        "filename": "订单张三.xlsx",
    },
    "re_requested": {
        "task_id": "t1",
        "tool_name": "finance_create",
        "payload_hash": "hash-1",
        "card": {"company_name": "张三"},
    },
    # [2026-09-19 第 2 步] 新增三类的真实发射形态（tool_name 用**幻觉串**覆盖
    # 收口路径：投影后必须变 "other"，且原文不得出现在任何键）
    "compression_decision": {
        "decision": "compressed",
        "occupancy_pct": 95,
        "messages_count": 12,
        "segment_messages": 7,
    },
    "decision_input_summary": {
        "history_raw": 12,
        "history_sent": 9,
        "evicted_details": 2,
        "summaries_count": 1,
        "active_skill": "SK.financial_report",
    },
    "args_input_summary": {
        "history_raw": 12,
        "history_sent": 9,
        "evicted_details": 2,
        "active_skill": None,
    },
}


def _scope_from(factory):
    @contextmanager
    def _scope() -> Iterator:
        session = factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


def test_projected_events_all_writable_to_pg(engine):
    _gone(engine)
    # 样本必须覆盖全部事件类型（新增类型未同步样本 → 红）
    assert set(_SAMPLE_RAW) == set(PROJECTION)

    stream_id = f"pg-e2e-{_RUN_ID}"
    thread_id = f"th-{_RUN_ID}"
    rows: list[StreamEventRecord] = []
    for i, (event_type, raw) in enumerate(_SAMPLE_RAW.items()):
        projected = project(event_type, raw)
        rows.append(
            StreamEventRecord(
                event_id=f"{stream_id}-e{i:04d}",
                stream_id=stream_id,
                event_type=event_type,
                record_kind=projected.record_kind,
                payload=projected.payload,
                thread_id=thread_id,
                sequence=i,
            )
        )
    # stream_created 行（create() 的落库形态）
    rows.append(
        StreamEventRecord(
            event_id=f"{stream_id}-c0001",
            stream_id=stream_id,
            event_type="stream_created",
            record_kind="stream_created",
            payload={},
            thread_id=thread_id,
            sequence=0,
        )
    )
    # 影子行（被展示门控抑制的中间轮 agent_reply，C1）
    sup = project_suppressed(
        "agent_reply", {"reply_text": "x"}, "final_step:model_failed"
    )
    rows.append(
        StreamEventRecord(
            event_id=f"{stream_id}-x0001",
            stream_id=stream_id,
            event_type="agent_reply",
            record_kind=sup.record_kind,
            payload=sup.payload,
            thread_id=thread_id,
            sequence=0,
        )
    )

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sink = StreamEventSink(
        session_factory_provider=lambda: _scope_from(factory),
        batch_interval_ms=20,
    )
    try:
        for row in rows:
            sink.submit(row)
        assert sink.flush_now(5.0), "队列未在规定时间内写尽"
    finally:
        sink.shutdown(2)

    stats = sink.stats()
    assert stats.write_failures == 0, stats  # 投影产出必须全部被迁移约束接受
    assert stats.oversize_dropped == 0, stats
    assert stats.written == len(rows)

    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM chat_stream_events WHERE stream_id = :s"
            ),
            {"s": stream_id},
        ).scalar_one()
        all_payloads = conn.execute(
            text(
                "SELECT payload::text FROM chat_stream_events WHERE stream_id = :s"
            ),
            {"s": stream_id},
        ).scalars().all()
    assert count == len(rows)
    # A 档：落库内容里不得出现任何样本中的模型/业务原文
    blob = json.dumps(all_payloads, ensure_ascii=False)
    assert "张三" not in blob
    assert "增量文本" not in blob
    assert "思考文本" not in blob


# ---------------------------------------------------------------------------
# 21. [2026-09-19 第 2 步 (c)] tool_name 收口：幻觉串**不落库**（真链三层锁）
#
# 走**真 StreamStore + 真 sink + flush_now + 从 DB 读回**——只断言 project()
# 的返回值**不算锁**（那是今天已有的行为，锁的是现有行为不是新行为）。
# ---------------------------------------------------------------------------

_HALLUCINATED_TOOL = "幻觉工具名_完全不存在"


def _fresh_sink(engine) -> "StreamEventSink":
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return StreamEventSink(
        session_factory_provider=lambda: _scope_from(factory),
        batch_interval_ms=20,
    )


def test_tool_name_hallucination_not_persisted(engine) -> None:
    """§6.1 三层锁（主锁 = 落库副本；反向锁 = 内存副本原样）。

    1. 落库副本层：``append`` → ``flush_now`` → **从 DB 读回该行** → 断言
       ``payload["tool_name"] == "other"`` **且**幻觉串不出现在 payload 里；
    2. SSE 未误伤（反向锁）：同一个 ``StreamStore`` 的**内存副本**里 tool_name
       必须**原样**——这是「收口只影响落库副本」**唯一针对 ``tool_name``
       语义**的机器判据；没有它，将来有人把收敛挪进 ``append`` 没有任何测试
       会红。（旁证，非本锁替代：``tests/api/test_steps_budget.py`` 里对
       ``agent_error`` 事件 payload 做**整字典相等**的断言，会在任何改动
       ``agent_error`` 投影规则——五条收口规则之一——时顺带变红；但它判的是
       ``agent_error`` 的 payload 形状，与 ``tool_name`` 语义无关。）
    """
    sink = _fresh_sink(engine)
    stream_id = f"pg-toolname-{_RUN_ID}"
    store = StreamStore(sink=sink)
    try:
        store.create(stream_id, thread_id=f"th-{_RUN_ID}")
        event = store.append(
            stream_id,
            event_type="tool_running",
            task_id="t1",
            payload={
                "task_id": "t1",
                "tool_name": _HALLUCINATED_TOOL,
                "stage": "collecting_arguments",
            },
        )
        assert sink.flush_now(5.0), "队列未在规定时间内写尽"

        # 反向锁：内存副本仍是原始 SSE 形态（收口不碰 SSE 契约）
        mem = [e for e in store.events(stream_id) if e.event_type == "tool_running"]
        assert len(mem) == 1
        assert mem[0].payload["tool_name"] == _HALLUCINATED_TOOL

        # 主锁：入库后的行
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT payload::text FROM chat_stream_events"
                    " WHERE event_id = :eid"
                ),
                {"eid": event.event_id},
            ).scalar_one()
    finally:
        sink.shutdown(2)

    stored = json.loads(row)
    assert stored["tool_name"] == "other", stored
    assert _HALLUCINATED_TOOL not in row
