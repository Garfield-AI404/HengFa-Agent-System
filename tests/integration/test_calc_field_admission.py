"""[2026-09-15] erp_calc 字段准入守护：measure 数值型 + 敏感字段禁分组/聚合。

背景（两类校验缺失，根因相同——calc 的字段校验**只认「字段名在元数据里」**，
不看类型、不看敏感标记）：

1. **measure 只认数值型字段（fail closed）**：``calc.py:_cell_decimal`` 对 str
   做 ``Decimal(text)``——**纯数字串会转换成功**。于是 ``sum(tax_id)`` /
   ``sum(bank_routing_no)`` 这类字符串数字字段会被**真的加总、静默返回一个无
   意义的数**（含字母的行被当「缺失」跳过、n 悄悄变小）。**静默给错数，不是
   fail closed。** 修复：sum/avg/min/max 及其 multiply_field 只接受
   int/decimal；**count 不受约束**（数非空行有意义）。
2. **敏感字段禁止用于 group_by/measure（fail closed）**：数据源 = ERP 元数据
   的 ``sensitive`` 键（agent 侧 ``FieldSpec.sensitive`` 已解析、此前无人消费）。
   ``erp_calc`` 取数走 ``call_tool("query")`` **不经 crop 裁剪**，
   ``group_by="bank_account"`` 会把每个账号原样列进 summary 回上下文。修复：
   group_by 与 measure 上 fail closed。**有意放行 filter/order_by**（见 calc.py
   内注释）。

覆盖：
- [1] 12 个敏感字段**逐个**（参数化）在 group_by / measure 上被拒、措辞正确；
- [2] 非敏感字段（employees.daily_wage / customers.company_name /
  metal_orders.quantity）仍可正常 group_by/measure（防过度封锁）；
- [3] 类型校验：str/date/bool 上 sum/avg 被拒、int/decimal 通过；count
  （含带 field）不受影响；派生 decimal 列 stock 仍可用；
- [4] 跨仓一致性守卫（子进程导入 ERP 真源 registry 取 sensitive 标记，定位
  不到显式 skip）。

本文件的假 ERP（CalcErp）只提供 list_entities 元数据——字段门在取数之前触发，
无需 query 分支；跨仓用例直接用 ERP 真源元数据构造，端到端钉住「ERP 标谁、
agent 拦谁」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.calc import ErpCalcArgs, run_erp_calc
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache

# ---------------------------------------------------------------------------
# 假 ERP：仅 list_entities（字段门在取数前触发，无需 query）
# ---------------------------------------------------------------------------


class CalcErp:
    """假 ERP：list_entities 回元数据，query 恒回空页（字段门先于取数触发；
    正向对照用例需取数成功返回空集，故 query 也须可用）。"""

    def __init__(self, entities: list[dict]):
        self.entities = list(entities)
        self.calls: list[dict] = []

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  operation_id: str | None = None,
                  trace_id: str | None = None) -> ToolResult:
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
            return ToolResult(ok=True, data={"entities": self.entities})
        if tool_name == "query":
            window = (arguments or {}).get("limit") or 200
            return ToolResult(ok=True, data={
                "items": [], "total": 0, "limit": window, "offset": 0,
            })
        raise AssertionError(f"unexpected tool: {tool_name}")

    def list_entities(self, operation_id: str | None = None,
                      trace_id: str | None = None) -> ToolResult:
        return self.call_tool("list_entities", {})


def _ctx(erp: CalcErp) -> ToolContext:
    return ToolContext(
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}),
        ),
        tasks=None, audit=None, sink=None, client=erp,
        metadata=EntityCache(erp),
    )


#: 12 个敏感字段（entity, field）——测试侧参数化清单，与 ERP 真源一致性由
#: 用例 [4] 守护；生产侧不另立清单（直接消费 ERP 元数据的 sensitive 键）。
SENSITIVE_FIELDS: list[tuple[str, str]] = [
    ("employees", "phone"),
    ("employees", "emergency_phone"),
    ("suppliers", "phone"),
    ("suppliers", "email"),
    ("suppliers", "tax_id"),
    ("suppliers", "bank_branch"),
    ("suppliers", "bank_routing_no"),
    ("suppliers", "bank_account"),
    ("customers", "tax_id"),
    ("customers", "bank_branch"),
    ("customers", "bank_routing_no"),
    ("customers", "bank_account"),
]

#: 敏感字段的类型（ERP 侧均为字符串列；照抄真源语义）
_SENSITIVE_TYPE = "str"

#: 本地假 ERP 实体目录：三个敏感实体 + metal_orders（类型校验）+ derived
#: powder_inventory（派生 decimal 放行）。
def _entities() -> list[dict]:
    def field(type_: str, **extra) -> dict:
        base = {"type": type_, "label": type_}
        base.update(extra)
        return base

    emp = {"id": field("int", label="ID"), "name": field("str"),
           "daily_wage": field("decimal"), "is_active": field("bool")}
    cus = {"id": field("int", label="ID"),
           "company_name": field("str")}
    sup = {"id": field("int", label="ID"), "name": field("str")}
    for entity, target in (("employees", emp), ("customers", cus),
                           ("suppliers", sup)):
        for ent, fname in SENSITIVE_FIELDS:
            if ent == entity:
                target[fname] = field(_SENSITIVE_TYPE, sensitive=True)
    return [
        {"name": "employees", "label": "员工", "readable": True,
         "creatable": True, "updatable": True, "deletable": False,
         "business_operations": [], "fields": emp},
        {"name": "customers", "label": "客户", "readable": True,
         "creatable": True, "updatable": True, "deletable": False,
         "business_operations": [], "fields": cus},
        {"name": "suppliers", "label": "供货商", "readable": True,
         "creatable": True, "updatable": True, "deletable": False,
         "business_operations": [], "fields": sup},
        {"name": "metal_orders", "label": "钣金订单", "readable": True,
         "creatable": True, "updatable": True, "deletable": False,
         "business_operations": [], "fields": {
             "id": field("int", label="ID"),
             "model": field("str"),
             "quantity": field("int"),
             "in_date": field("date"),
             "total_amount": field("decimal"),
             "weight_unit": field("str", enum_values=["kg", "m²"]),
         }},
        {"name": "powder_inventory", "label": "塑粉库存", "readable": True,
         "creatable": False, "updatable": False, "deletable": False,
         "business_operations": [], "fields": {
             "id": field("int", label="ID"),
             "color_code": field("str"),
             "order_total": field("decimal", computed=True),
             "consumed_total": field("decimal", computed=True),
             "stock": field("decimal", computed=True),
         }},
    ]


def _run(args: ErpCalcArgs) -> tuple[bool, str, CalcErp]:
    erp = CalcErp(_entities())
    result = run_erp_calc(_ctx(erp), args)
    return result.ok, str(result.summary.get("error", "")), erp


def _assert_rejected_before_fetch(erp: CalcErp) -> None:
    """字段门必须在取数之前：拒绝路径未触达 query。"""
    assert [c["tool"] for c in erp.calls] == ["list_entities"]


# ---------------------------------------------------------------------------
# [1] 12 个敏感字段：group_by / measure 一律拒绝，措辞正确
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity,field", SENSITIVE_FIELDS)
def test_sensitive_field_rejected_in_group_by(entity: str, field: str) -> None:
    """敏感字段用于 group_by → 拒绝，措辞为「敏感字段」（非「不在目录」）。"""
    ok, error, erp = _run(ErpCalcArgs(entity=entity, group_by=field,
                                      measures=[{"op": "count"}]))
    assert ok is False
    assert "敏感字段" in error
    assert field in error
    # 拒绝措辞须给出可操作替代路径：该字段仍可用于 filter/order_by（目录第三段
    # 口径一致；不得写成「可用 erp_query 的 fields 取回」——crop 会裁掉那些值）
    assert "filter/order_by" in error
    assert "fields 取回" not in error
    # 明确区分「字段不在目录」类错误
    assert "不在实体字段目录" not in error
    assert "不在元数据" not in error
    _assert_rejected_before_fetch(erp)


@pytest.mark.parametrize("entity,field", SENSITIVE_FIELDS)
def test_sensitive_field_rejected_in_measure(entity: str, field: str) -> None:
    """敏感字段作 measure.field → 拒绝，措辞为「敏感字段」。"""
    ok, error, erp = _run(ErpCalcArgs(entity=entity,
                                      measures=[{"field": field, "op": "count"}]))
    assert ok is False
    assert "敏感字段" in error
    assert field in error
    _assert_rejected_before_fetch(erp)


@pytest.mark.parametrize("entity,field", SENSITIVE_FIELDS)
def test_sensitive_field_rejected_as_multiply_field(entity: str, field: str) -> None:
    """敏感字段作 multiply_field → 拒绝（与非 count 聚合的乘数同权）。"""
    ok, error, erp = _run(ErpCalcArgs(entity=entity, measures=[
        {"field": "id", "op": "sum", "multiply_field": field},
    ]))
    assert ok is False
    assert "敏感字段" in error
    assert field in error
    _assert_rejected_before_fetch(erp)


# ---------------------------------------------------------------------------
# [2] 非敏感字段仍可用（防过度封锁）
# ---------------------------------------------------------------------------


def test_non_sensitive_numeric_field_allowed() -> None:
    """employees.daily_wage（decimal，非敏感）可 group_by 与 sum。"""
    ok, _, _ = _run(ErpCalcArgs(entity="employees", group_by="daily_wage",
                                measures=[{"op": "count"}]))
    assert ok is True
    ok2, _, _ = _run(ErpCalcArgs(entity="employees",
                                 measures=[{"field": "daily_wage", "op": "sum"}]))
    assert ok2 is True


def test_non_sensitive_str_field_group_by_allowed() -> None:
    """customers.company_name（str，非敏感）可 group_by（分组不涉类型限制）。"""
    ok, _, _ = _run(ErpCalcArgs(entity="customers", group_by="company_name",
                                measures=[{"op": "count"}]))
    assert ok is True


def test_non_sensitive_int_field_measure_allowed() -> None:
    """metal_orders.quantity（int，非敏感）可 sum。"""
    ok, _, _ = _run(ErpCalcArgs(entity="metal_orders",
                                measures=[{"field": "quantity", "op": "sum"}]))
    assert ok is True


# ---------------------------------------------------------------------------
# [3] 类型校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["sum", "avg", "min", "max"])
def test_non_numeric_measure_rejected(op: str) -> None:
    """str 字段上的 sum/avg/min/max 一律拒绝，措辞为「类型不是数值」。"""
    ok, error, erp = _run(ErpCalcArgs(entity="metal_orders",
                                      measures=[{"field": "model", "op": op}]))
    assert ok is False
    assert "类型不是数值" in error
    assert "count" in error  # 提示改用 count
    assert "不在实体字段目录" not in error
    _assert_rejected_before_fetch(erp)


def test_date_and_bool_measure_rejected() -> None:
    """date / bool 字段不可聚合（sum 拒绝）。"""
    ok, error, _ = _run(ErpCalcArgs(entity="metal_orders",
                                    measures=[{"field": "in_date", "op": "sum"}]))
    assert ok is False
    assert "类型不是数值" in error
    assert "date" in error

    ok2, error2, _ = _run(ErpCalcArgs(entity="employees",
                                      measures=[{"field": "is_active", "op": "sum"}]))
    assert ok2 is False
    assert "类型不是数值" in error2
    assert "bool" in error2


def test_str_field_as_multiply_field_rejected() -> None:
    """str 字段作 multiply_field → 拒绝。"""
    ok, error, _ = _run(ErpCalcArgs(entity="metal_orders", measures=[
        {"field": "quantity", "op": "sum", "multiply_field": "model"},
    ]))
    assert ok is False
    assert "类型不是数值" in error


def test_count_unaffected_by_type_rule() -> None:
    """count 不受类型规则约束：count 带 str/date field 仍可用。"""
    ok, _, _ = _run(ErpCalcArgs(entity="metal_orders",
                                measures=[{"field": "model", "op": "count"}]))
    assert ok is True
    ok2, _, _ = _run(ErpCalcArgs(entity="metal_orders",
                                 measures=[{"field": "in_date", "op": "count"}]))
    assert ok2 is True
    ok3, _, _ = _run(ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]))
    assert ok3 is True


def test_derived_decimal_column_still_usable() -> None:
    """派生 decimal 列 stock 仍可用（勿把类型名单写窄 → 库存查询静默失效）。

    powder_inventory 的 stock/order_total/consumed_total 是 ERP 声明为
    ``decimal`` 的派生列（hengfa-erp/backend/app/mcp/registry.py
    ``_DERIVED_SPECS``），必须继续放行。
    """
    ok, _, _ = _run(ErpCalcArgs(entity="powder_inventory",
                                measures=[{"field": "stock", "op": "sum"}]))
    assert ok is True
    ok2, _, _ = _run(ErpCalcArgs(entity="powder_inventory", group_by="color_code",
                                 measures=[{"field": "stock", "op": "sum"}]))
    assert ok2 is True


# ---------------------------------------------------------------------------
# [4] 跨仓一致性守卫：agent 的拦截面 == ERP 标记为敏感的字段集
# ---------------------------------------------------------------------------

#: 本文件位于 <repo>/tests/integration/ → 仓库根 = parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SENTINEL = "__HENGFA_ERP_SENSITIVE_FIELDS__"

#: ERP 环境内执行的取数脚本：导入注册表，输出 {entity: {field: {type,sensitive,
#: computed}}}——只读 ORM 元数据，无 DB / 无网络（同 test_crop_whitelist_matches_metadata）。
_PROBE = (
    "import json;"
    "from app.mcp.registry import ENTITY_REGISTRY;"
    "print('{sentinel}' + json.dumps("
    "{{k: {{fn: {{'type': f.get('type'), 'sensitive': bool(f.get('sensitive')),"
    " 'computed': bool(f.get('computed'))}}"
    " for fn, f in v['fields'].items()}}"
    " for k, v in ENTITY_REGISTRY.items()}}))"
).format(sentinel=_SENTINEL)


def _erp_backend_dir() -> Path | None:
    override = os.environ.get("HENGFA_ERP_BACKEND")
    candidates = ([Path(override)] if override else []) + [
        _REPO_ROOT.parent / "hengfa-erp" / "backend",
    ]
    for cand in candidates:
        if (cand / "app" / "mcp" / "registry.py").is_file():
            return cand
    return None


def _erp_python(backend: Path) -> tuple[Path | None, list[str]]:
    subdirs = ("Scripts",) if os.name == "nt" else ("bin",)
    exe = "python.exe" if os.name == "nt" else "python"
    candidates: list[Path] = [
        backend / venv_name / sub / exe
        for venv_name in ("venv", ".venv")
        for sub in subdirs
    ]
    env_python = os.environ.get("HENGFA_ERP_PYTHON")
    if env_python:
        candidates.append(Path(env_python))
    candidates.append(Path(sys.executable))
    for cand in candidates:
        if cand.is_file():
            return cand, [str(c) for c in candidates]
    return None, [str(c) for c in candidates]


@lru_cache(maxsize=1)
def _erp_sensitive_fields() -> dict[str, dict[str, dict]]:
    """经 ERP 自身解释器导入 ENTITY_REGISTRY，取回 {entity: {field: meta}}。

    失败/缺失一律 pytest.skip（显式原因），不静默放行。
    """
    backend = _erp_backend_dir()
    if backend is None:
        pytest.skip(
            "未找到 ERP 仓库（hengfa-erp/backend）——"
            "设 HENGFA_ERP_BACKEND 指向 ERP backend 目录后重跑"
        )
    python, tried = _erp_python(backend)
    if python is None:
        pytest.skip("ERP 仓库存在但未找到可用解释器；已尝试：\n  " + "\n  ".join(tried))
    try:
        proc = subprocess.run(
            [str(python), "-c", _PROBE],
            cwd=str(backend), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"无法执行 ERP 解释器导入注册表：{exc}")
    if proc.returncode != 0:
        pytest.skip(
            f"ERP 注册表导入失败（环境/依赖问题）；解释器={python}；已尝试：\n  "
            + "\n  ".join(tried) + "\n" + (proc.stderr or "")[-2000:]
        )
    marker_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_SENTINEL)]
    if not marker_lines:
        pytest.skip(f"ERP 取数脚本无输出（{_SENTINEL} 未出现）")
    try:
        data = json.loads(marker_lines[-1][len(_SENTINEL):])
    except json.JSONDecodeError as exc:  # pragma: no cover
        pytest.skip(f"ERP 注册表输出非 JSON：{exc}")
    if not isinstance(data, dict) or not data:
        pytest.skip("ERP 注册表为空")
    return data


def _erp_metadata_entities(erp: dict[str, dict[str, dict]]) -> list[dict]:
    """把 ERP 真源元数据转成 list_entities 形态（字段 raw 含 sensitive/type）。"""
    out: list[dict] = []
    for entity, fields in erp.items():
        out.append({
            "name": entity, "label": entity, "readable": True,
            "creatable": True, "updatable": True, "deletable": False,
            "business_operations": [],
            "fields": {
                fn: {"type": meta["type"], "label": fn,
                     "sensitive": meta["sensitive"], "computed": meta["computed"]}
                for fn, meta in fields.items()
            },
        })
    return out


def test_agent_blocks_exactly_erp_sensitive_fields() -> None:
    """跨仓一致性：ERP 标 sensitive 的每个字段，agent 都在 group_by 上拒绝。

    真源 = ERP registry（子进程导入）。若 ERP 侧 sensitive 标记尚未落地
    （全部为 False），本用例显式 skip（而非 fail）——同时把这一事实报出来。
    """
    erp = _erp_sensitive_fields()
    sensitive = {
        ent: [fn for fn, meta in fields.items() if meta.get("sensitive")]
        for ent, fields in erp.items()
    }
    sensitive = {ent: fns for ent, fns in sensitive.items() if fns}
    if not sensitive:
        pytest.skip(
            "ERP registry 当前没有任何 sensitive=True 字段（ERP 侧改动可能尚未"
            "落地）——待 ERP 子代理落地后本守护自动生效"
        )
    entities = _erp_metadata_entities(erp)
    from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES
    for entity, fns in sensitive.items():
        if entity not in QUERYABLE_ENTITIES:
            continue  # 不可查实体不在 erp_calc 开放面
        for fname in fns:
            erp_fake = CalcErp(entities)
            result = run_erp_calc(
                _ctx(erp_fake),
                ErpCalcArgs(entity=entity, group_by=fname,
                            measures=[{"op": "count"}]),
            )
            assert result.ok is False, (
                f"ERP 标 {entity}.{fname} 为敏感，agent 未拦（group_by）"
            )
            assert "敏感字段" in str(result.summary.get("error", "")), (
                f"{entity}.{fname} 拒绝措辞非「敏感字段」：{result.summary}"
            )


def test_erp_sensitive_set_matches_expected_when_landed() -> None:
    """ERP 侧 sensitive 落地后，其字段集应 ⊇ 12 个约定敏感字段（防真源漂移）。

    未落地（空集）→ skip；落地则断言约定的 12 个键都在 ERP 真源里。
    """
    erp = _erp_sensitive_fields()
    landed = {
        f"{ent}.{fn}"
        for ent, fields in erp.items()
        for fn, meta in fields.items()
        if meta.get("sensitive")
    }
    if not landed:
        pytest.skip("ERP 侧 sensitive 尚未落地，跳过约定集比对")
    expected = {f"{ent}.{fn}" for ent, fn in SENSITIVE_FIELDS}
    missing = expected - landed
    assert not missing, f"ERP sensitive 缺约定字段（真源漂移）：{sorted(missing)}"
