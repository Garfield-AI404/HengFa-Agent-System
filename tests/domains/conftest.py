"""D14 钣金订单创建域测试共享夹具（5.3/9.3/14.2/14.3 场景 11）。

与 tests/integration/conftest.py 同模式：在本 conftest 顶层先于任何
administrative_agent 导入设置测试 ERP 环境变量；FakeOrdersErp 为 respx
假服务（D5 规则：测试夹具，不代表 ERP 真实运行态；材料中明确标注来源）。

FakeOrdersErp 实现 mcp-api.md §6 幂等契约子集：
- 唯一键 principal_id + operation_type + idempotency_key（简化：key 唯一）；
- 同 key 同 payload → 返回原订单 replayed=true（§6.3）；
- 同 key 不同 payload → IDEMPOTENCY_KEY_CONFLICT（§6.3）；
- 失败注入：network_unknown（已写入但响应丢失，14.3 场景 11）、
  inject_in_progress（异常遗留 PENDING，requires_operator_action）、
  inject_conflict、reject（BUSINESS_ERROR）。
注：并发同 key 的锁等待行为属 ERP InnoDB（mcp-api §6.3 L269），
SQLite/内存假服务不模拟；Agent 侧同 task 不并发由 D6 WriteTaskLock 保证。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal

os.environ["ERP_MCP_URL"] = "http://erp-mcp.test"
os.environ["ERP_MCP_API_KEY"] = "test-api-key-not-for-production"
os.environ["ERP_MCP_JWT"] = ""
os.environ["ERP_MCP_CONNECT_TIMEOUT"] = "5"
os.environ["ERP_MCP_READ_TIMEOUT"] = "30"
os.environ["ERP_MCP_TOTAL_TIMEOUT"] = "60"
os.environ["ERP_MCP_METADATA_TTL"] = "3600"

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from administrative_agent.agent.prompts.policies import policy_for  # noqa: E402
from administrative_agent.audit.service import AuditService  # noqa: E402
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.domains.orders import MetalOrderExecutor  # noqa: E402
from administrative_agent.mcp.client import MCPClient  # noqa: E402
from administrative_agent.tasks.records import NewTaskSpec  # noqa: E402
from administrative_agent.tasks.service import TaskService  # noqa: E402
from tests.audit.conftest import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.tasks.conftest import InMemoryTaskRepository  # noqa: E402

ERP_BASE = "http://erp-mcp.test"
ERP_MCP_URL = f"{ERP_BASE}/mcp"

TRACE_ID = "trace-d14-test-000001"
ACTOR_ACCOUNT = "manager01"
ACTOR_ROLE = "manager"


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class FakeOrdersErp:
    """respx 假服务（D5 规则：测试夹具，不代表 ERP 真实运行态）。

    create_order_bundle 幂等语义（mcp-api.md §6.3）：
    - key → (payload, order_id) 映射；同 key 同 payload → replayed=true
      返回原订单；同 key 不同 payload → IDEMPOTENCY_KEY_CONFLICT；
    - network_unknown_keys：首次调用已写入但响应丢失（抛 ConnectError，
      恢复后同 key 调用返回 replayed=true——14.3 场景 11）；
    - inject_in_progress_keys / inject_conflict_keys / reject_keys：注入
      结构化错误分支。
    """

    def __init__(self) -> None:
        self.orders: dict[int, dict] = {}
        self.next_id = 5000
        self.keys: dict[str, dict] = {}  # key -> {"payload": str, "order_id": int}
        self.calls: list[dict] = []
        self.network_unknown_keys: set[str] = set()
        self.network_fail_keys: set[str] = set()
        self.inject_in_progress_keys: set[str] = set()
        self.inject_conflict_keys: set[str] = set()
        self.reject_keys: set[str] = set()
        # D16：update_order（mcp-api.md §7 子集）
        #: update_order 对该订单"已应用但响应丢失"（规则 7：结果未知）
        self.network_unknown_updates: set[int] = set()
        #: update_order 对该订单持续网络失败（未应用；规则 7 未应用分支）
        self.network_fail_updates: set[int] = set()
        #: update_order 调用瞬间模拟他人抢先更新（竞态窗口，14.3 场景 10）
        self._version_conflict_others: dict[int, dict] = {}
        #: 假服务时间戳发生器（updated_at 每次更新递增 1 秒，模拟版本演进）
        self._clock = "2026-08-23T10:00:01.000000"

    # ---- 失败注入 ----

    def fail_network_unknown(self, key: str) -> None:
        """该 key 首次调用已写入但响应丢失（网络结果未知，14.3 场景 11）。"""
        self.network_unknown_keys.add(key)

    def fail_network_persistent(self, key: str) -> None:
        """该 key 每次调用都抛网络异常（达重试上限场景，13.2）。"""
        self.network_fail_keys.add(key)

    def fail_in_progress(self, key: str) -> None:
        """返回 IDEMPOTENCY_IN_PROGRESS（异常遗留 PENDING，需人工处理）。"""
        self.inject_in_progress_keys.add(key)

    def fail_conflict(self, key: str) -> None:
        """返回 IDEMPOTENCY_KEY_CONFLICT（key 已用于不同载荷）。"""
        self.inject_conflict_keys.add(key)

    def fail_reject(self, key: str) -> None:
        """返回 BUSINESS_ERROR（普通拒绝）。"""
        self.reject_keys.add(key)

    def seed_order(self, key: str, payload: dict) -> int:
        """预置已存在订单（replayed 场景：同 key 同 payload 重放）。"""
        rid = self._insert(key, payload)
        self.keys[key] = {"payload": canonical(payload), "order_id": rid}
        return rid

    # ---- D16 更新注入（mcp-api.md §7 子集） ----

    def seed_update_order(
        self, order_id: int, payload: dict, updated_at: str | None = None
    ) -> int:
        """预置已存在订单（更新场景；updated_at 缺省当天固定值）。"""
        order = {"id": order_id, **payload}
        order["updated_at"] = updated_at or "2026-08-23T10:00:00.000000"
        self._recalc_amount(order)
        self.orders[order_id] = order
        return order_id

    def apply_other_writer_update(self, order_id: int, changes: dict) -> None:
        """模拟他人抢先更新（14.3 场景 10）：直接应用他人变更并推进
        updated_at；此后以旧 expected_updated_at 调 update_order 自然
        返回 ORDER_VERSION_CONFLICT（mcp-api.md §7.2 行锁比较）。"""
        self._apply_update(order_id, changes)

    def fail_version_conflict_at_call(self, order_id: int, applied_by_other: dict) -> None:
        """模拟"调用瞬间他人抢先更新"（竞态窗口）：写前查询返回旧快照，
        update_order 被调用时先应用他人变更再返回 ORDER_VERSION_CONFLICT。"""
        self._version_conflict_others[order_id] = dict(applied_by_other)

    def fail_network_unknown_update(self, order_id: int) -> None:
        """该订单 update_order"已应用但响应丢失"（5.4 规则 7 场景）。"""
        self.network_unknown_updates.add(order_id)

    def fail_network_persistent_update(self, order_id: int) -> None:
        """该订单 update_order 持续网络失败（未应用；规则 7 未应用分支）。"""
        self.network_fail_updates.add(order_id)

    # ---- respx 路由 ----

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return self._resp(
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-orders-erp", "version": "0.0.0"},
                }
            )
        assert body["method"] == "tools/call"
        params = body["params"]
        return self._dispatch(params["name"], params.get("arguments") or {})

    def _dispatch(self, name: str, args: dict) -> httpx.Response:
        if name == "create_order_bundle":
            self.calls.append(args)
            key = args.get("idempotency_key")
            if key in self.inject_in_progress_keys:
                return self._error(
                    "IDEMPOTENCY_IN_PROGRESS", "same idempotency key pending",
                    requires_operator_action=True,
                )
            if key in self.inject_conflict_keys:
                return self._error(
                    "IDEMPOTENCY_KEY_CONFLICT", "key used for different payload"
                )
            if key in self.reject_keys:
                return self._error("BUSINESS_ERROR", "erp rejected")
            payload = args["order"]
            canonical_payload = canonical(payload)
            if key in self.keys:
                prior = self.keys[key]
                if prior["payload"] == canonical_payload:
                    # 同 key 同 payload → 原订单 + replayed=true（§6.3）
                    return self._order_response(
                        self.orders[prior["order_id"]], replayed=True, args=args
                    )
                return self._error(
                    "IDEMPOTENCY_KEY_CONFLICT", "key used for different payload"
                )
            if key in self.network_fail_keys:
                # 持续网络失败：不写入、抛网络异常（达上限场景，13.2）
                raise httpx.ConnectError("connection reset (fake orders erp)")
            if key in self.network_unknown_keys:
                # 已写入但响应丢失：注册 key 后抛网络异常（场景 11）
                rid = self._insert(key, payload)
                self.keys[key] = {"payload": canonical_payload, "order_id": rid}
                self.network_unknown_keys.discard(key)
                raise httpx.ConnectError("connection reset (fake orders erp)")
            rid = self._insert(key, payload)
            self.keys[key] = {"payload": canonical_payload, "order_id": rid}
            return self._order_response(self.orders[rid], replayed=False, args=args)
        if name == "query":
            # mcp-api.md §4：声明式只读查询（执行器写前查询用；R4 显式字段）
            self.calls.append(args)
            entity = args.get("entity")
            filt = args.get("filter") or {}
            items: list[dict] = []
            if "id" in filt and filt["id"] in self.orders:
                items = [dict(self.orders[filt["id"]])]
            fields = args.get("fields")
            if fields:
                items = [
                    {k: v for k, v in it.items() if k in fields} for it in items
                ]
            return self._ok(
                {
                    "items": items,
                    "total": len(items),
                    "limit": args.get("limit", 50),
                    "offset": args.get("offset", 0),
                    "entity": entity,
                }
            )
        if name == "update_order":
            # mcp-api.md §7 子集：expected_updated_at 行锁比较、金额重算、
            # 状态/out_date 联动、other_effects 当前派生影响
            self.calls.append(args)
            oid = args["order_id"]
            changes = dict(args.get("changes") or {})
            if oid not in self.orders:
                return self._error("ORDER_NOT_FOUND", f"order {oid} not found")
            if oid in self._version_conflict_others:
                # 调用瞬间他人抢先更新（竞态窗口，14.3 场景 10）
                other = self._version_conflict_others.pop(oid)
                self._apply_update(oid, other)
            if oid in self.network_fail_updates:
                # 持续网络失败（未应用；规则 7 未应用分支）
                raise httpx.ConnectError("connection reset (fake orders erp)")
            order = self.orders[oid]
            # 已完成订单只改 out_date 传 null → VALIDATION_ERROR（§7.2）
            if (
                order.get("status") == "完成"
                and changes.get("out_date") is None
                and "status" not in changes
            ):
                return self._error(
                    "VALIDATION_ERROR", "out_date cannot be null on completed order"
                )
            if oid in self.network_unknown_updates:
                # 已应用但响应丢失（规则 7 场景）：应用后抛网络异常
                self.network_unknown_updates.discard(oid)
                self._apply_update(oid, changes)
                raise httpx.ConnectError("connection reset (fake orders erp)")
            if args.get("expected_updated_at") != order.get("updated_at"):
                # §7.2 行锁 + 乐观锁比较：不一致 → 冲突，原订单不变化
                return self._error(
                    "ORDER_VERSION_CONFLICT", "order updated by another writer"
                )
            return self._update_order_response(oid, changes)
        raise AssertionError(f"unexpected tool: {name}")

    # ---- D16 内部（update_order 语义，mcp-api.md §7.2 模拟） ----

    def _update_order_response(self, order_id: int, changes: dict) -> httpx.Response:
        """应用 changes → 金额重算 → updated（§7.4 响应）。

        changed_fields 含联动写入字段（§7.4 示例：out_date/status/
        total_amount 一并列出）与金额重算字段。
        """
        changed = self._apply_update(order_id, changes)
        order = self.orders[order_id]
        changed_fields = sorted(changed | {"total_amount"})
        is_powder = "color_code" in order
        key = "supplier_id" if is_powder else "customer_id"
        other_effects = [{
            "type": "payable_current_impact" if is_powder
            else "receivable_current_impact",
            "mode": "derived",
            key: order[key],
            "amount": order["total_amount"],
        }]
        return self._ok(
            {
                "status": "updated",
                "order_type": "powder" if is_powder else "metal",
                "order": dict(order),
                "changed_fields": changed_fields,
                "other_effects": other_effects,
            }
        )

    def _apply_update(self, order_id: int, changes: dict) -> set:
        """应用更新：字段合并 + 金额重算 + updated_at 推进 + §7.2 联动。

        返回实际变化字段集（含联动写入字段，供 changed_fields 使用）。
        """
        order = self.orders[order_id]
        changed = set(changes)
        for k, v in changes.items():
            order[k] = v
        self._recalc_amount(order)
        order["updated_at"] = self._next_updated_at()
        # §7.2 状态/日期联动：status→完成 且 out_date 未提供或 null → 当天；
        # status→未完成 未提供或 null → 保留历史日期（不改动）
        if changes.get("status") == "完成" and not changes.get("out_date"):
            order["out_date"] = "2026-08-23"
            changed.add("out_date")
        return changed

    def _recalc_amount(self, order: dict) -> None:
        """金额重算（§7.2：钣金 quantity×weight_per_unit×unit_price＋
        附加款 surcharge_amount（[2026-09-08 ERP 上线]，缺省 0.00）；
        塑粉 total_weight×price_per_kg；同事务由 ERP 保证）。"""
        is_powder = "color_code" in order
        if is_powder:
            amount = Decimal(str(order.get("total_weight") or 0)) * Decimal(
                str(order.get("price_per_kg") or 0)
            )
        else:
            amount = (
                Decimal(str(order.get("quantity") or 0))
                * Decimal(str(order.get("weight_per_unit") or 0))
                * Decimal(str(order.get("unit_price") or 0))
                + Decimal(str(order.get("surcharge_amount") or 0))
            )
        order["total_amount"] = f"{amount:.2f}"

    def _next_updated_at(self) -> str:
        base = datetime.fromisoformat(self._clock)
        self._clock = (base + timedelta(seconds=1)).isoformat()
        return self._clock

    # ---- 内部 ----

    def _insert(self, key: str, payload: dict) -> int:
        rid = self.next_id
        self.next_id += 1
        self.orders[rid] = {"id": rid, **payload}
        return rid

    def _order_response(self, order: dict, *, replayed: bool, args: dict) -> httpx.Response:
        # 载荷含 color_code → 塑粉（5.2）；否则钣金（mcp-api §6 类型区分）
        is_powder = "color_code" in order
        if is_powder:
            other_effects = [{
                "type": "payable_current_impact", "mode": "derived",
                "supplier_id": order["supplier_id"], "amount": "0.00",
            }]
        else:
            other_effects = [{
                "type": "receivable_current_impact", "mode": "derived",
                "customer_id": order["customer_id"], "amount": "0.00",
            }]
        return self._ok(
            {
                "status": "created",
                "replayed": replayed,
                "operation_id": args.get("operation_id"),
                "trace_id": args.get("trace_id"),
                "order_type": "powder" if is_powder else "metal",
                "order": order,
                "receivables": [],
                "payables": [],
                "transactions": [],
                "other_effects": other_effects,
            }
        )

    def _resp(self, result: dict) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def _ok(self, data: dict) -> httpx.Response:
        return self._resp(
            {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "isError": False,
                "structuredContent": data,
            }
        )

    def _error(self, code: str, message: str,
               requires_operator_action: bool = False) -> httpx.Response:
        return self._resp(
            {
                "content": [{"type": "text", "text": f"{code}: {message}"}],
                "isError": True,
                "structuredContent": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "requires_operator_action": requires_operator_action,
                    "details": {},
                },
            }
        )


def create_task(tasks: TaskService, **overrides) -> object:
    """创建 R2 写任务（D6：is_write=True 自动取串行锁并进入 running）。"""
    base = dict(
        thread_id="thread-mgr-d14",
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        duty_code="R2",
        original_user_message="创建钣金订单",
        structured_intent={"duty": "R2", "order_type": "metal"},
        is_write=True,
    )
    base.update(overrides)
    return tasks.create_task(NewTaskSpec(**base))


@pytest.fixture
def erp() -> FakeOrdersErp:
    return FakeOrdersErp()


@pytest.fixture
def ctx(erp: FakeOrdersErp, request):
    """组装执行器上下文：respx 假服务 + 真 MCPClient + 内存仓储。

    yield (erp, tasks, audit, executor, client)；request.param 可传
    retry_limit（默认 3，达上限演示用）——13.2 每次业务操作最多尝试次数。
    client 一并暴露，供测试自建 executor（如注入固定 key_factory 以
    复现 ERP 已有同 key 记录的直接重放）。
    """
    retry_limit = getattr(request, "param", 3)
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            executor = MetalOrderExecutor(
                client=client,
                tasks=tasks,
                audit=audit,
                trace_id=TRACE_ID,
                actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE,
                policy=policy_for("R2.order.create.metal"),
                retry_limit=retry_limit,
            )
            yield erp, tasks, audit, executor, client
        finally:
            client.close()
