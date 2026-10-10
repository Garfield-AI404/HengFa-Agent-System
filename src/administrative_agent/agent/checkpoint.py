"""8.5 Checkpoint 写入、恢复与清理（D7/RV-10）。

职责与边界：
- **Checkpointer 工厂**：构建 langgraph-checkpoint-postgres 的 ``PostgresSaver``
  （2.6 代码树 agent/checkpoint.py）。连接串只从环境变量读取（15.2/12.4：
  CHECKPOINT_DATABASE_URL，未配置时回退 AGENT_DATABASE_URL 实现共库默认；
  两者都未配置则 fail closed，绝不降级为内存存储）。连接串与口令不进入
  日志、代码与材料。
- **写入集成点**：LangGraph 图在每个节点（super-step）执行后自动保存
  checkpoint（8.5"每次模型或工具调用完成后保存"）。本服务提供
  ``new_graph_config`` 把 thread_id/task_id 写入 graph config（configurable），
  二者自动进入 checkpoint metadata，作为恢复与跨任务隔离的关联键；
  并提供 ``attach_checkpoint_id`` 把最新 checkpoint_id 回写 tasks.checkpoint_id
  （12.1 字段，8.5"以 thread_id 和 task_id 关联"的显式引用）。
- **恢复**：``load_for_task`` 要求显式 thread_id+task_id（8.3 第 2 条），
  优先按 tasks.checkpoint_id 精确取回，其次按 metadata.task_id 在线程内过滤
  最新快照；metadata.task_id 不匹配（旧任务/他任务快照）一律拒绝——checkpoint
  不作为跨任务知识库（8.5），旧任务信息不注入新任务（RV-10 通过标准）。
  恢复返回 checkpoint 中的结构化 State 字段（8.4 清单），调用方（D9+ 业务
  节点）优先使用结构化业务字段，不重新解析 messages（8.3 第 4 条）。
- **对话历史重放**：``load_thread(thread_id)`` 按
  thread_id 重放最近 checkpoint（会话循环图的对话历史持久层，8.5 定位升级），
  不做跨任务隔离；``new_graph_config`` 的 task_id 可 None（首轮纯对话无任务，
  None 时不写入 configurable，metadata 无 task_id 键——孤儿快照判定语义不变）。
- **清理**：``cleanup`` 按 8.5 保留策略——waiting_confirmation、waiting_review、
  running 任务不能因普通清理删除；succeeded/failed/cancelled 任务的
  checkpoint 默认保留 CHECKPOINT_RETENTION_DAYS（30 天，可配置）后删除，
  时间基准为 tasks.completed_at（无任务记录的孤儿快照以快照时间兜底）；
  审计事件、复核记录、映射版本不在本模块范围内，checkpoint 清理只操作
  langgraph 的 checkpoint 表，不影响 Agent 自有表（L1275：checkpoint 表由
  对应依赖管理；清理只调用依赖公开 API）。
- **PostgresStore 关闭**（8.5 末条）：一期不初始化 LangGraph PostgresStore，
  不启用跨会话自动长期记忆。本模块不导入、不导出、不实例化 PostgresStore，
  测试断言关闭。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import structlog
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from administrative_agent.config import Settings, get_settings
from administrative_agent.tasks.machine import (
    TERMINAL_STATES,
    TaskStatus,
    _coerce_status,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger("agent.checkpoint")

#: 8.5 保护状态：这三类任务的 checkpoint 不因普通清理删除。
PROTECTED_STATES = frozenset(
    {
        TaskStatus.RUNNING,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_REVIEW,
    }
)

#: 连接池默认大小（checkpoint 写入随图执行并发发生，小池足够一期规模）。
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 4
#: 建连超时（秒），防止数据库不可达时挂死图执行。
_CONNECT_TIMEOUT_SECONDS = 5


class CheckpointError(RuntimeError):
    """Checkpoint 基础设施错误基类。"""


class CheckpointUnconfiguredError(CheckpointError):
    """CHECKPOINT_DATABASE_URL 与 AGENT_DATABASE_URL 均未配置。

    12.4/15.2：连接串只走环境变量；未配置时 fail closed，不允许静默
    降级为内存 checkpoint（否则重启后恢复语义被破坏）。
    """


class TaskRepository(Protocol):
    """D6 tasks.service.TaskRepository 契约的子集（本模块只读 task 记录
    并回写 checkpoint_id）。"""

    def get(self, task_id: str) -> Any: ...

    def update(self, task_id: str, **fields) -> Any: ...


def checkpointer_factory(settings: Settings | None = None) -> PostgresSaver:
    """构建长生命周期 PostgresSaver（8.5/2.6）。

    - 连接串优先级：CHECKPOINT_DATABASE_URL → AGENT_DATABASE_URL。
      共库默认的理由：L1275 要求 checkpoint 表的初始化与备份纳入 Agent
      数据库运维——同一 PostgreSQL 实例便于统一备份/恢复与 15.4 演练；
      15.2 保留独立变量，允许部署时改为分库（如 checkpoint 体积增长）。
    - 使用 psycopg_pool.ConnectionPool 而不是 from_conn_string：
      from_conn_string 是 contextmanager（连接生命周期限定在 with 块内），
      而 checkpointer 需要跨图执行长期持有（每次 super-step 后写入）。
    - 连接串不写入日志；构造失败抛出，不降级。
    """
    s = settings or get_settings()
    dsn = s.checkpoint_database_url or s.agent_database_url
    if not dsn:
        raise CheckpointUnconfiguredError(
            "checkpoint 连接串未配置：CHECKPOINT_DATABASE_URL / "
            "AGENT_DATABASE_URL 均未注入（连接串只走环境变量，规格 15.2/12.4）"
        )
    shared_db = not s.checkpoint_database_url
    try:
        pool = ConnectionPool(
            dsn,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            # autocommit=True：langgraph 的 setup() 迁移含 CREATE INDEX
            # CONCURRENTLY，不能在事务块内执行（官方 from_conn_string 同口径）；
            # 显式事务由 psycopg conn.transaction() 管理。
            kwargs={
                "autocommit": True,
                "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
            },
        )
    except Exception as exc:  # 连接池构造失败（如 URL 非法）：fail closed
        logger.error("checkpoint_pool_failed", error=type(exc).__name__)
        raise CheckpointError(f"checkpoint 连接池创建失败：{exc}") from exc
    logger.info(
        "checkpoint_configured",
        shared_db=shared_db,
        engine="postgresql",
    )
    return PostgresSaver(pool)


@dataclass(frozen=True)
class CleanupResult:
    """一次清理的结果（供日志与材料核验）。"""

    deleted_threads: list[str] = field(default_factory=list)
    protected_threads: list[str] = field(default_factory=list)
    deleted_snapshot_count: int = 0
    protected_snapshot_count: int = 0
    orphan_deleted_snapshot_count: int = 0

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "deleted_threads": len(self.deleted_threads),
            "protected_threads": len(self.protected_threads),
            "deleted_snapshot_count": self.deleted_snapshot_count,
            "protected_snapshot_count": self.protected_snapshot_count,
            "orphan_deleted_snapshot_count": self.orphan_deleted_snapshot_count,
        }


class CheckpointService:
    """checkpoint 写入/恢复/清理服务（8.5），与 D6 TaskService/TaskState 衔接。

    - 写入：由编译进 checkpointer 的图自动完成（每次节点执行后保存）；
      本服务负责图 config 构造（thread_id/task_id 关联键）与
      checkpoint_id 回写 tasks 表。
    - 恢复：``load_for_task`` 返回结构化 State 字段（8.4 清单），
      不重新解析 messages 生成业务参数（8.3 第 4 条）。
    - 清理：``cleanup`` 按 8.5 保留策略删除到期快照。

    ``task_repository`` 可空：仅做 saver 操作时（如纯展示/清理孤儿）不要求
    task 记录；恢复与回写需要注入 D6 TaskRepository 实现。
    """

    def __init__(self, saver: PostgresSaver, task_repository: TaskRepository | None = None) -> None:
        self._saver = saver
        self._repo = task_repository

    @property
    def checkpointer(self) -> PostgresSaver:
        """底层 checkpointer（编译图的恢复锚点）。

        [2026-08-24 harness 改造] Phase 2：/chat/confirm 端点用
        ``build_graph(checkpointer=service.checkpointer)`` 编译图并以
        ``new_graph_config(thread_id)`` 恢复挂起的 interrupt——同一实例
        才能读到挂起 checkpoint（测试注入的 MemorySaver 同口径）。
        """
        return self._saver

    # ---- 初始化 ----

    def setup(self) -> None:
        """建 checkpoint 表（幂等，L1275：checkpoint 表由对应依赖管理——
        setup 即依赖的建表入口，不手写 DDL）。"""
        try:
            self._saver.setup()
        except Exception as exc:
            logger.error("checkpoint_setup_failed", error=type(exc).__name__)
            raise CheckpointError(f"checkpoint 表初始化失败：{exc}") from exc
        logger.info("checkpoint_setup_done")

    # ---- 写入集成 ----

    def new_graph_config(self, thread_id: str, task_id: str | None = None) -> dict[str, Any]:
        """构造图执行 config：thread_id + task_id 作为 checkpoint 关联键。

        LangGraph 会把 configurable 中自定义键写入 checkpoint metadata，
        供恢复与跨任务隔离使用（8.5"以 thread_id 和 task_id 关联"）。

        [2026-08-24 harness 改造] ``task_id`` 可 None（首轮纯对话无任务）：
        为 None 时不写入 configurable——metadata 中无 task_id 键（而非
        显式 None），保证清理逻辑的"孤儿快照（metadata 无 task_id）"
        判定语义不因可空化而改变。
        """
        configurable: dict[str, Any] = {"thread_id": thread_id}
        if task_id is not None:
            configurable["task_id"] = task_id
        return {"configurable": configurable}

    def latest_checkpoint_id(self, thread_id: str, task_id: str) -> str | None:
        """线程内该 task 的最新 checkpoint_id（未找到返回 None）。"""
        best: CheckpointTuple | None = None
        for tup in self._saver.list({"configurable": {"thread_id": thread_id}}):
            if tup.metadata.get("task_id") != task_id:
                continue
            if best is None or _ts(tup) > _ts(best):
                best = tup
        return best.checkpoint["id"] if best is not None else None

    def attach_checkpoint_id(self, thread_id: str, task_id: str) -> str | None:
        """图执行后回写 tasks.checkpoint_id（12.1 字段，恢复的精确锚点）。

        无 task_repository 时跳过回写（只返回最新 checkpoint_id）。
        """
        ckpt_id = self.latest_checkpoint_id(thread_id, task_id)
        if ckpt_id and self._repo is not None:
            task = self._repo.get(task_id)
            if task is not None and getattr(task, "checkpoint_id", None) != ckpt_id:
                self._repo.update(task_id, checkpoint_id=ckpt_id)
                logger.info(
                    "task_checkpoint_attached",
                    task_id=task_id,
                    checkpoint_id=ckpt_id,
                )
        return ckpt_id

    # ---- 恢复 ----

    def load_for_task(self, thread_id: str, task_id: str) -> dict[str, Any] | None:
        """恢复该任务的 checkpoint（8.3 第 2/4 条、8.5）。

        返回 checkpoint 中的结构化 State 字段（8.4 清单）——调用方优先
        使用结构化业务字段，不重新解析 messages；找不到匹配快照返回 None
        （上层按 tasks 表状态提示，不猜测、不重放旧任务）。
        跨任务隔离：metadata.task_id 与请求 task_id 不一致的快照一律拒绝，
        旧任务信息不注入新任务（RV-10 通过标准）。
        """
        if self._repo is not None:
            task = self._repo.get(task_id)
            if task is not None and task.thread_id != thread_id:
                raise ValueError(
                    f"task {task_id!r} belongs to thread {task.thread_id!r}, "
                    f"not {thread_id!r}"
                )
            # 优先精确锚点：tasks.checkpoint_id（图执行后回写）
            if task is not None and task.checkpoint_id:
                anchored = self._saver.get_tuple(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": task.checkpoint_id,
                        }
                    }
                )
                if anchored is not None and anchored.metadata.get("task_id") == task_id:
                    logger.info(
                        "checkpoint_restored",
                        thread_id=thread_id,
                        task_id=task_id,
                        source="checkpoint_id",
                    )
                    return dict(anchored.checkpoint.get("channel_values") or {})
                # 锚点失效（如被外部清理）：回退线程扫描
                logger.info(
                    "checkpoint_anchor_stale",
                    thread_id=thread_id,
                    task_id=task_id,
                    checkpoint_id=task.checkpoint_id,
                )
        # 回退：线程内按 metadata.task_id 过滤最新快照
        best = self._latest_thread_snapshot(thread_id, task_id=task_id)
        if best is None:
            logger.info("checkpoint_not_found", thread_id=thread_id, task_id=task_id)
            return None
        logger.info(
            "checkpoint_restored",
            thread_id=thread_id,
            task_id=task_id,
            source="thread_scan",
        )
        return dict(best.checkpoint.get("channel_values") or {})

    # ---- 对话历史重放（[2026-08-24 harness 改造] Phase 1）----

    def load_thread(self, thread_id: str) -> dict[str, Any] | None:
        """重放线程的最近 checkpoint（会话循环图：对话历史持久层，8.5）。

        [2026-08-24 harness 改造] 对话式 harness 以 thread_id 粒度恢复
        整段会话（不做跨任务隔离——同线程先后对话共享历史，模型读重放
        历史自决，8.3 修订口径）。返回 channel_values（调用方取
        {"messages": [...], "pending_confirmation": ...}——Phase 1 只需
        messages；挂起确认卡 Phase 2 接入）。找不到快照返回 None
        （新会话从零开始，不猜测）。
        """
        best = self._latest_thread_snapshot(thread_id)
        if best is None:
            logger.info("thread_checkpoint_not_found", thread_id=thread_id)
            return None
        logger.info(
            "thread_checkpoint_restored",
            thread_id=thread_id,
            source="thread_scan",
            snapshot_id=best.checkpoint.get("id"),
        )
        return dict(best.checkpoint.get("channel_values") or {})

    def _latest_thread_snapshot(
        self, thread_id: str, *, task_id: str | None = None
    ) -> CheckpointTuple | None:
        """线程内最新快照（load_for_task 与 load_thread 共享的底层取数）。

        - task_id 为 None：线程内任意最新快照（对话重放，不做跨任务隔离）；
        - task_id 给定：按 metadata.task_id 过滤（load_for_task 的跨任务
          隔离语义，RV-10 通过标准——行为与改造前一致）。
        """
        best: CheckpointTuple | None = None
        for tup in self._saver.list({"configurable": {"thread_id": thread_id}}):
            if task_id is not None and tup.metadata.get("task_id") != task_id:
                continue  # 跨任务隔离：非本任务的快照不注入
            if best is None or _ts(tup) > _ts(best):
                best = tup
        return best

    # ---- 清理 ----

    def cleanup(self, retention_days: int, *, now: datetime | None = None) -> CleanupResult:
        """按 8.5 保留策略删除到期 checkpoint。

        - 保护集：waiting_confirmation / waiting_review / running 任务
          （8.5 字面三状态）的快照不因普通清理删除；
        - 终态（succeeded/failed/cancelled）：tasks.completed_at 起算保留
          ``retention_days`` 天后删除（12.3 CHECKPOINT_RETENTION_DAYS）；
        - pending 等未终态任务：保留（排队任务不清理）；
        - 孤儿快照（无 task 记录或 metadata 无 task_id）：以快照时间兜底；
        - 删除粒度：线程级（只调用依赖公开 API delete_thread，不手写 SQL）——
          线程内任一快照受保护则整线程保留并记录，等待线程内全部到期再删；
        - 只操作 langgraph checkpoint 表；审计/复核/映射等 Agent 表不随
          清理（8.5），任务记录保持原样。
        """
        if retention_days < 0:
            raise ValueError(f"retention_days must be >= 0, got {retention_days}")
        now = now or datetime.now(timezone.utc)
        deadline = now - timedelta(days=retention_days)

        # 按线程分组全部快照
        by_thread: dict[str, list[CheckpointTuple]] = {}
        try:
            for tup in self._saver.list(None):
                tid = _thread_of(tup)
                if tid is None:
                    continue
                by_thread.setdefault(tid, []).append(tup)
        except Exception as exc:
            logger.error("checkpoint_list_failed", error=type(exc).__name__)
            raise CheckpointError(f"checkpoint 扫描失败：{exc}") from exc

        deleted = 0
        protected_count = 0
        orphan_deleted = 0
        deleted_threads: list[str] = []
        protected_threads: list[str] = []
        for thread_id, snapshots in by_thread.items():
            tally = _snapshot_tally(snapshots, self._repo, deadline)
            deleted += tally.deleted
            protected_count += tally.protected
            orphan_deleted += tally.orphan_deleted
            if tally.protected > 0 or not snapshots:
                protected_threads.append(thread_id)
                continue
            try:
                self._saver.delete_thread(thread_id)
            except Exception as exc:
                logger.error(
                    "checkpoint_delete_failed",
                    thread_id=thread_id,
                    error=type(exc).__name__,
                )
                raise CheckpointError(f"checkpoint 线程删除失败：{exc}") from exc
            deleted_threads.append(thread_id)
            logger.info(
                "checkpoint_thread_deleted",
                thread_id=thread_id,
                snapshot_count=len(snapshots),
            )
        result = CleanupResult(
            deleted_threads=deleted_threads,
            protected_threads=protected_threads,
            deleted_snapshot_count=deleted,
            protected_snapshot_count=protected_count,
            orphan_deleted_snapshot_count=orphan_deleted,
        )
        logger.info("checkpoint_cleanup_done", **result.summary)
        return result


# ---- 内部 ----

def _ts(tup: CheckpointTuple) -> datetime:
    """快照时间（checkpoint['ts']，ISO 字符串或 datetime）。"""
    ts = tup.checkpoint.get("ts")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    try:
        parsed = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _thread_of(tup: CheckpointTuple) -> str | None:
    cfg = tup.config or {}
    configurable = cfg.get("configurable") or {}
    return configurable.get("thread_id")


@dataclass(frozen=True)
class _ThreadTally:
    """单个线程的快照判定汇总（deleted=可删、protected=受保护、
    orphan_deleted=孤儿已超期可删）。"""

    deleted: int = 0
    protected: int = 0
    orphan_deleted: int = 0


def _snapshot_tally(
    snapshots: list[CheckpointTuple],
    repo: TaskRepository | None,
    deadline: datetime,
) -> _ThreadTally:
    """逐快照判定可删性（8.5 保留策略），返回该线程汇总。

    - 保护状态（waiting_confirmation/waiting_review/running）不删；
    - 终态（succeeded/failed/cancelled）以 tasks.completed_at 起算，
      超过保留期可删；pending 等未终态保留；
    - 孤儿（metadata 无 task_id 或任务记录不存在）以快照时间兜底；
    - 无 repo 时：带 task_id 的快照无法查证任务状态，保守保留（防误删）。
    """
    deleted = 0
    protected = 0
    orphan_deleted = 0
    for tup in snapshots:
        task_id = tup.metadata.get("task_id")
        if task_id is None:
            # 孤儿快照：以快照时间兜底，超期可删
            if _ts(tup) <= deadline:
                orphan_deleted += 1
                continue
            protected += 1
            continue
        if repo is None:
            protected += 1
            continue
        record = repo.get(task_id)
        if record is None:
            # 任务记录已不存在（孤儿）：快照时间兜底
            if _ts(tup) <= deadline:
                orphan_deleted += 1
                continue
            protected += 1
            continue
        status = _coerce_status(record.status)
        if status in PROTECTED_STATES:
            # 8.5：waiting_confirmation / waiting_review / running 不因普通清理删除
            protected += 1
            continue
        if status in TERMINAL_STATES:
            base = record.completed_at or record.updated_at or _ts(tup)
            if base is not None and base <= deadline:
                deleted += 1
                continue
            protected += 1
            continue
        # pending 等未终态：保留
        protected += 1
    return _ThreadTally(deleted=deleted, protected=protected, orphan_deleted=orphan_deleted)


logging.getLogger("psycopg.pool").setLevel(logging.INFO)
