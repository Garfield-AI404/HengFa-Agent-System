"""[2026-09-15] export_data 默认导出列排除敏感字段（默认排除、显式放行）。

背景：sensitive 标记（ERP 元数据 ``sensitive`` 键 / agent ``FieldSpec.sensitive``）
此前只被 ``erp_calc`` 消费（group_by/measure fail closed，见
tests/integration/test_calc_field_admission.py）。但 ``export_data`` 能把字段值
写进**可下载的 xlsx 文件**——默认导出列若含敏感字段，等于把电话/银行账号/税号
原样交付给用户。口径：**默认排除、显式放行**（见
``domains/export_fields.validate_export_fields``）：默认导出列剔除元数据里
``sensitive=True`` 的字段；``fields`` 显式点名时照旧导出，并在结果 note 里注明。

真源 = agent 侧已解析的 ``FieldSpec.sensitive``（``mcp/metadata.py``）——**不另立
清单**：ERP 标了谁就排除谁，ERP 增删敏感字段无需改 agent。

本模块钉住：
- 默认导出：customers/suppliers/employees 的默认列**逐字段**不含敏感字段，且
  **非敏感字段仍在**（防过度排除）；
- metal_orders/powder_orders（已登记 ``EXPORT_DEFAULT_COLUMNS``）默认列**逐字
  未变**（零回归钉子）；
- 显式 ``fields=["bank_account"]`` 仍能导出（合法性判定不变）且结果 note 含
  「按显式请求导出敏感字段」（只列字段名，不含字段值）；
- 跨仓真源（子进程导入 ERP registry）派生敏感集，断言默认列不含之——ERP 改
  名单时本测试自动跟随（缺 ERP 仓 → 显式 skip）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.domains.export import ExportResult
from administrative_agent.domains.export_fields import (
    EXPORT_DEFAULT_COLUMNS,
    validate_export_fields,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache

# ---------------------------------------------------------------------------
# 本地假 ERP：仅 list_entities（validate_export_fields 只消费 get_entity.fields）
# ---------------------------------------------------------------------------


def _f(ftype: str = "str", **extra: object) -> dict:
    return {"type": ftype, "label": ftype, **extra}


#: 本地元数据：customers/suppliers/employees（含敏感字段）+ 两个已登记默认列的
#: 订单实体。字段名照抄 ERP 真源（hengfa-erp/.../mcp/registry.py）。
_ENTITIES: list[dict] = [
    {
        "name": "customers", "label": "客户", "readable": True,
        "creatable": True, "updatable": True, "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _f("int"),
            "company_name": _f(),
            "unit_price": _f("decimal"),
            "price_type": _f(),
            "price_unit": _f(),
            "tax_id": _f(sensitive=True),
            "bank_branch": _f(sensitive=True),
            "bank_routing_no": _f(sensitive=True),
            "bank_account": _f(sensitive=True),
        },
    },
    {
        "name": "suppliers", "label": "供货商", "readable": True,
        "creatable": True, "updatable": True, "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _f("int"),
            "name": _f(),
            "contact_person": _f(),
            "address": _f(),
            "phone": _f(sensitive=True),
            "email": _f(sensitive=True),
            "tax_id": _f(sensitive=True),
            "bank_branch": _f(sensitive=True),
            "bank_routing_no": _f(sensitive=True),
            "bank_account": _f(sensitive=True),
        },
    },
    {
        "name": "employees", "label": "员工", "readable": True,
        "creatable": True, "updatable": True, "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _f("int"),
            "name": _f(),
            "employee_no": _f(),
            "position": _f(),
            "daily_wage": _f("decimal"),
            "work_hours_per_day": _f(),
            "is_active": _f("bool"),
            "phone": _f(sensitive=True),
            "emergency_phone": _f(sensitive=True),
        },
    },
    {
        "name": "metal_orders", "label": "钣金订单", "readable": True,
        "creatable": True, "updatable": True, "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _f("int"), "customer_id": _f("int"), "model": _f(),
            "unit_price": _f("decimal"), "quantity": _f("int"),
            "weight_per_unit": _f("decimal"), "weight_unit": _f(),
            "in_date": _f("date"), "surcharge_amount": _f("decimal"),
        },
    },
    {
        "name": "powder_orders", "label": "塑粉订单", "readable": True,
        "creatable": True, "updatable": True, "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _f("int"), "supplier_id": _f("int"), "color_code": _f(),
            "total_weight": _f("decimal"), "price_per_kg": _f("decimal"),
            "in_date": _f("date"),
        },
    },
]


class _FakeErp:
    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": list(_ENTITIES)})

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  operation_id: str | None = None,
                  trace_id: str | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            fields = (arguments or {}).get("fields")
            rows = [
                ({k: r[k] for k in fields if k in r} if fields else dict(r))
                for r in _CUSTOMER_ROWS
            ]
            return ToolResult(ok=True, data={
                "items": rows, "total": len(rows), "limit": 200, "offset": 0,
            })
        raise AssertionError(f"unexpected tool: {tool_name}")


#: customers 数据行（含敏感列值——默认导出不得带出 bank_account/tax_id）
_CUSTOMER_ROWS: list[dict] = [
    {"id": 1, "company_name": "甲公司", "unit_price": "10.0000",
     "price_type": "含税", "price_unit": "元/kg",
     "tax_id": "91330100XXXXXXX", "bank_account": "6222000000000000001"},
]


class _RecordingAudit:
    """审计替身：捕获 record_export 的 kwargs（导出器内审计入口）。"""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record_export(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def _cache() -> EntityCache:
    return EntityCache(_FakeErp())


#: 各实体的敏感字段（本地元数据口径）与非敏感字段（须仍在默认列里）。
_LOCAL_SENSITIVE: dict[str, set[str]] = {
    "customers": {"tax_id", "bank_branch", "bank_routing_no", "bank_account"},
    "suppliers": {"phone", "email", "tax_id", "bank_branch",
                  "bank_routing_no", "bank_account"},
    "employees": {"phone", "emergency_phone"},
}
_LOCAL_NON_SENSITIVE: dict[str, set[str]] = {
    "customers": {"company_name", "unit_price", "price_type", "price_unit"},
    "suppliers": {"name", "contact_person", "address"},
    "employees": {"name", "employee_no", "position", "daily_wage",
                  "work_hours_per_day", "is_active"},
}


# ---------------------------------------------------------------------------
# [0] 安全过滤器 fail closed：meta.fields 非 mapping → 显式 TypeError
# ---------------------------------------------------------------------------


def test_is_sensitive_raises_on_non_mapping_fields() -> None:
    """``_is_sensitive`` 是默认导出列的敏感过滤器：``meta.fields`` 非
    ``dict[str, FieldSpec]`` 时**必须抛 TypeError**，绝不「静默不排除」
    （生产 ``EntityMetadata.fields`` 恒为 mapping，非 mapping 只会是接线错误，
    宁可 fail closed）。本锁在「有人把 raise 改回 return False」时变红。
    """
    from administrative_agent.domains.export_fields import _is_sensitive

    class _BadMeta:
        fields = ["id", "bank_account"]  # 非 mapping（旧替身形状）

    with pytest.raises(TypeError, match="mapping"):
        _is_sensitive(_BadMeta(), "bank_account")
    # 具名兜底：return False 的实现会让上面不抛错、落到这里报错
    with pytest.raises(TypeError):
        _is_sensitive(_BadMeta(), "id")


# ---------------------------------------------------------------------------
# [1] 默认导出列：逐字段不含敏感字段，且非敏感字段仍在
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", sorted(_LOCAL_SENSITIVE))
def test_default_columns_exclude_sensitive_keep_non_sensitive(entity: str) -> None:
    cols = set(validate_export_fields(entity, None, _cache()))
    assert cols & _LOCAL_SENSITIVE[entity] == set(), (
        f"{entity} 默认导出列混入敏感字段：{sorted(cols & _LOCAL_SENSITIVE[entity])}"
    )
    missing = _LOCAL_NON_SENSITIVE[entity] - cols
    assert not missing, (
        f"{entity} 默认导出列过度排除，丢了非敏感字段：{sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# [2] 已登记 EXPORT_DEFAULT_COLUMNS 的订单实体：默认列逐字未变（零回归）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", ["metal_orders", "powder_orders"])
def test_registered_default_columns_unchanged(entity: str) -> None:
    result = validate_export_fields(entity, None, _cache())
    assert result == list(EXPORT_DEFAULT_COLUMNS[entity])


# ---------------------------------------------------------------------------
# [3] 显式放行：fields 点名敏感字段仍导出 + 结果 note 注明
# ---------------------------------------------------------------------------


def test_explicit_sensitive_field_still_passes_validation() -> None:
    """合法性判定不变：显式点名敏感字段仍在返回清单内。"""
    assert validate_export_fields("customers", ["bank_account"], _cache()) == [
        "bank_account"
    ]


def _tool_ctx(audit: Any) -> ToolContext:
    return ToolContext(
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}),
        ),
        tasks=None, audit=audit, sink=None,
        client=_FakeErp(), metadata=_cache(),
    )


def test_explicit_sensitive_export_adds_note(monkeypatch) -> None:
    """显式点名敏感字段导出：结果 note 含说明，且只出现**字段名**。"""
    from administrative_agent.agent.tools.export import (
        ExportDataArgs,
        run_export_data,
    )
    from administrative_agent.domains import export as export_mod

    def fake_export(self, *, entity, fields, filter, order_by, filename,
                    sensitive_fields=None):
        return ExportResult(
            file_id="a" * 32, filename="f.xlsx", path="p", row_count=0,
            total=0, expires_at=datetime.now(timezone.utc),
            columns=list(fields or []),
        )

    monkeypatch.setattr(export_mod.ExcelExporter, "export", fake_export)
    result = run_export_data(
        _tool_ctx(object()),
        ExportDataArgs(entity="customers", fields=["bank_account"]),
    )
    assert result.ok is True
    note = str(result.summary.get("note", ""))
    assert "按显式请求导出敏感字段" in note
    assert "bank_account" in note


def test_non_sensitive_explicit_export_has_no_note(monkeypatch) -> None:
    """显式点名非敏感字段：不得附敏感说明（防误报）。"""
    from administrative_agent.agent.tools.export import (
        ExportDataArgs,
        run_export_data,
    )
    from administrative_agent.domains import export as export_mod

    def fake_export(self, *, entity, fields, filter, order_by, filename,
                    sensitive_fields=None):
        return ExportResult(
            file_id="a" * 32, filename="f.xlsx", path="p", row_count=0,
            total=0, expires_at=datetime.now(timezone.utc),
            columns=list(fields or []),
        )

    monkeypatch.setattr(export_mod.ExcelExporter, "export", fake_export)
    result = run_export_data(
        _tool_ctx(object()),
        ExportDataArgs(entity="customers", fields=["company_name"]),
    )
    assert result.ok is True
    assert "敏感" not in str(result.summary.get("note", ""))


# ---------------------------------------------------------------------------
# [3b] 耐久审计：记录实际导出列 + 显式点名导出的敏感字段（存续期不依赖 meta.json）
# ---------------------------------------------------------------------------


def test_explicit_sensitive_export_recorded_in_audit(tmp_path, monkeypatch) -> None:
    """显式点名敏感字段导出 → 审计载荷含该列，并落进 sensitive_fields。"""
    from administrative_agent.agent.tools.export import (
        ExportDataArgs,
        run_export_data,
    )
    from administrative_agent.config import settings

    monkeypatch.setattr(settings, "export_dir", str(tmp_path))
    audit = _RecordingAudit()
    result = run_export_data(
        _tool_ctx(audit),
        ExportDataArgs(entity="customers", fields=["bank_account"]),
    )
    assert result.ok is True
    rec = audit.records[-1]
    assert rec["entity"] == "customers"
    assert rec["columns"] == ["bank_account"]
    assert rec["sensitive_fields"] == ["bank_account"]


def test_default_export_audit_columns_exclude_sensitive(tmp_path, monkeypatch) -> None:
    """默认导出 → 审计记下列名、sensitive_fields 为空，且列名不含敏感字段。"""
    from administrative_agent.agent.tools.export import (
        ExportDataArgs,
        run_export_data,
    )
    from administrative_agent.config import settings

    monkeypatch.setattr(settings, "export_dir", str(tmp_path))
    audit = _RecordingAudit()
    result = run_export_data(_tool_ctx(audit), ExportDataArgs(entity="customers"))
    assert result.ok is True
    rec = audit.records[-1]
    assert rec["columns"], "默认导出也须在审计里记下列名"
    assert rec["sensitive_fields"] is None
    assert not (set(rec["columns"]) & _LOCAL_SENSITIVE["customers"])


def test_old_shape_audit_payload_reads_without_new_keys() -> None:
    """向后兼容钉子：旧形状（无 columns/sensitive_fields）记录读取不崩。"""
    from administrative_agent.audit.service import AuditEventRecord
    from administrative_agent.web.routes_audit import _audit_out

    old = AuditEventRecord(
        audit_id="a1", trace_id="t1", actor_account="manager01",
        actor_role="manager", event_type="export", target_type="export",
        target_id="f" * 32,
        structured_payload={"entity": "customers", "filter": None, "row_count": 3},
        source="chat",
    )
    out = _audit_out(old)
    assert out.structured_payload == {
        "entity": "customers", "filter": None, "row_count": 3,
    }


# ---------------------------------------------------------------------------
# [4] 跨仓真源：默认列不含 ERP 真源敏感字段（真源漂移自动跟随；缺 ERP → skip）
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SENTINEL = "__HENGFA_ERP_EXPORT_SENSITIVE__"

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


def _erp_python(backend: Path) -> Path | None:
    subdirs = ("Scripts",) if os.name == "nt" else ("bin",)
    exe = "python.exe" if os.name == "nt" else "python"
    candidates = [
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
            return cand
    return None


@lru_cache(maxsize=1)
def _erp_registry() -> dict[str, dict[str, dict]]:
    backend = _erp_backend_dir()
    if backend is None:
        pytest.skip("未找到 ERP 仓库（hengfa-erp/backend）——设 HENGFA_ERP_BACKEND 后重跑")
    python = _erp_python(backend)
    if python is None:
        pytest.skip("ERP 仓库存在但未找到可用解释器")
    try:
        proc = subprocess.run(
            [str(python), "-c", _PROBE], cwd=str(backend),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"无法执行 ERP 解释器导入注册表：{exc}")
    if proc.returncode != 0:
        pytest.skip(f"ERP 注册表导入失败：{(proc.stderr or '')[-1500:]}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_SENTINEL)]
    if not lines:
        pytest.skip("ERP 取数脚本无输出")
    data = json.loads(lines[-1][len(_SENTINEL):])
    if not isinstance(data, dict) or not data:
        pytest.skip("ERP 注册表为空")
    return data


def _erp_entities(erp: dict[str, dict[str, dict]]) -> list[dict]:
    return [
        {
            "name": entity, "label": entity, "readable": True,
            "creatable": True, "updatable": True, "deletable": False,
            "business_operations": [],
            "fields": {
                fn: {"type": meta["type"], "label": fn,
                     "sensitive": meta["sensitive"], "computed": meta["computed"]}
                for fn, meta in fields.items()
            },
        }
        for entity, fields in erp.items()
    ]


class _TrueSourceErp:
    def __init__(self, entities: list[dict]) -> None:
        self._entities = entities

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": list(self._entities)})

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  operation_id: str | None = None,
                  trace_id: str | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.parametrize("entity", ["customers", "suppliers", "employees"])
def test_default_columns_exclude_erp_true_source_sensitive(entity: str) -> None:
    """默认列与 ERP 真源敏感集不相交；且真源里该实体确有敏感字段（探针有效）。"""
    erp = _erp_registry()
    fields = erp.get(entity)
    if fields is None:
        pytest.skip(f"ERP 真源无实体 {entity}")
    sensitive = {fn for fn, meta in fields.items() if meta.get("sensitive")}
    if not sensitive:
        pytest.skip(f"ERP 真源 {entity} 当前无 sensitive 字段（标记未落地）")
    cache = EntityCache(_TrueSourceErp(_erp_entities(erp)))
    cols = set(validate_export_fields(entity, None, cache))
    assert cols & sensitive == set(), (
        f"{entity} 默认导出列混入 ERP 真源敏感字段：{sorted(cols & sensitive)}"
    )
