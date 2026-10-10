"""agent 类型 → 工具子集解析层（多 agent 演进 · 机制前置）。

**本轮只建机制、不接线**：建立「agent 类型 → 可见工具子集」的解析能力与
接口口子，默认行为**零变更**——``resolve_tools(None)`` 返回 ``TOOL_REGISTRY``
本身（同一对象），提示词、工具枚举表、分派路径逐字节不变。agent 类型的
**来源先不定**（不读 ``account.role``、不加配置项），只留参数口子。

[2026-09-22 F7] 注：装配层 ``routes_chat._harness_runtime`` 现把
``resolve_tools`` 结果再经 ``without_unconfigured_rag_tools`` 过滤——RAG 未
配置时得到的是**新 dict**，故「返回值 is TOOL_REGISTRY」这一身份断言在装配层
**已不成立**（本函数自身仍满足；行为等价、顺序与内容不变）。下游若需身份
断言，应针对 ``resolve_tools`` 的直接返回值，而非装配层过滤后的集合。

模块名用 ``toolsets`` 而非 ``agents``：本模块只解决「工具子集」这一件正交
的事，避免含义过宽的模块名。

**口径**（工具子集按 agent 类型隔离的能力边界）：

- ``AgentKind`` 与 ``auth.models.Role`` 同构（``str, Enum``）——既可与既有
  枚举风格一致，``AgentKind("office")`` 又顺带做输入校验；
- ``AGENT_TOOLSETS`` 是 **agent 类型 → 工具名集合** 的手写分组表。**不从任何
  字段推导**——已核验 22 个工具里没有任何字段能充当 agent 类型维度
  （``duty_code`` 多值但 ``R6`` 混了 1 写 + 4 读；``policy_family`` 多键但
  只读工具里多为空元组；``is_write`` 太粗；``requires_manager``
  全部 True；``required_scopes`` 与业务域不对齐）；
- ``SALES`` / ``FINANCE`` 是**暂定清单 · 未接线 · 未经业务签核**——当前
  未被任何调用方使用（本轮分派恒传 ``None``），清单本身等待业务确认；
- ``OFFICE`` 是全部 22 个工具（显式逐个写名，不写 ``frozenset(TOOL_REGISTRY)``
  ——要能被 import 期 G3 断言钉住，且改注册表时强制人工过一遍本表）。

**不做**：不提供 ``DEFAULT_AGENT_KIND`` 常量——默认值是 ``None``（「未指定」），
不是某一种角色。
"""

from __future__ import annotations

from enum import Enum

from administrative_agent.agent.tools import TOOL_REGISTRY, ToolSpec

__all__ = ["AgentKind", "AGENT_TOOLSETS", "resolve_tools"]


class AgentKind(str, Enum):
    """agent 类型（多 agent 隔离维度的占位枚举）。

    与 ``auth.models.Role(str, enum.Enum)`` 同构：``str`` 混入使得
    ``AgentKind("office")`` 顺带做输入校验（非法值即 ``ValueError``），
    且可与其他 str 枚举/字符串自然互操作。三值对应用户已确认的多 agent
    演进方向（内勤/销售/财务）。
    """

    OFFICE = "office"
    SALES = "sales"
    FINANCE = "finance"


#: agent 类型 → 工具名集合（手写清单，不从任何字段推导）。
#:
#: ``OFFICE``：内勤档 = 全部 22 个工具（显式逐个写名，非
#: ``frozenset(TOOL_REGISTRY)``）。import 期 G3 断言钉住它与注册表全集
#: 相等：改注册表而漏改本表 → 应用起不来（响亮而非静默）。
#:
#: ``SALES`` / ``FINANCE``：**暂定 · 未接线 · 未经业务签核**。当前没有任何
#: 调用方使用它们（分派路径恒传 ``None``）；清单本身等待业务签核后再定稿。
AGENT_TOOLSETS: dict[AgentKind, frozenset[str]] = {
    AgentKind.OFFICE: frozenset(
        {
            "attendance_plan_execute",
            "order_create",
            "order_update",
            "finance_create",
            "finance_update",
            "reconciliation_execute",
            "reconcile_analyze",
            "attachment_preview",
            "list_entities",
            "erp_calc",
            "erp_salary_calc",
            "render_financial_report",
            "render_tax_worksheet",
            "erp_query",
            "export_data",
            "memory_lookup",
            "rag_remember",
            "rag_recall",
            "rag_forget",
            "rag_search_documents",
            "mapping_template",
            "mapping_list",
        }
    ),
    # 以下两档为 **暂定 · 未接线 · 未经业务签核**（清单待业务确认）。
    #: 销售档（8）：订单读写 + 通用查询/计算/枚举/导出/记忆/附件预览。
    AgentKind.SALES: frozenset(
        {
            "order_create",
            "order_update",
            "erp_query",
            "erp_calc",
            "list_entities",
            "export_data",
            "memory_lookup",
            "attachment_preview",
        }
    ),
    #: 财务档（9）：收支读写 + 财报/工资 + 通用查询/枚举/导出/记忆/附件预览。
    AgentKind.FINANCE: frozenset(
        {
            "finance_create",
            "finance_update",
            "render_financial_report",
            "erp_salary_calc",
            "erp_query",
            "list_entities",
            "export_data",
            "memory_lookup",
            "attachment_preview",
        }
    ),
}


def resolve_tools(kind: AgentKind | str | None) -> dict[str, ToolSpec]:
    """agent 类型 → 工具子集（本轮默认 ``None`` → 全量注册表对象）。

    三个**刻意选择**：

    1. **``kind is None`` → 返回 ``TOOL_REGISTRY`` 本身**（同一对象，不是
       副本、不是 ``dict(...)``）。这是「零行为变更」最强的表达：下游所有
       ``is TOOL_REGISTRY`` 身份断言、顺序、内容全部不动；
    2. **未知名 → 抛 ``ValueError``**（不静默降级为全量，也不返回 ``{}``）。
       能力隔离机制里 fail open 等于没有隔离——未知 agent 类型只能是调用方
       拼错，响亮失败好过静默给出全量能力。经 ``AgentKind(kind)`` 触发，
       包一层给中文错误信息；
    3. **子集按 ``TOOL_REGISTRY`` 注册顺序重建**——遍历 ``TOOL_REGISTRY.items()``
       而非遍历 ``names``。因为 ``agent/nodes/model_node.py`` 的工具枚举表按
       ``tools.values()`` 顺序出文本，**顺序即提示词内容**：遍历
       ``AGENT_TOOLSETS`` 里的 frozenset 会得到无序结果，提示词随之漂移。
    """
    if kind is None:
        return TOOL_REGISTRY
    try:
        resolved = AgentKind(kind)
    except ValueError:
        raise ValueError(
            f"未知的 agent 类型 {kind!r}：可选 "
            f"{[k.value for k in AgentKind]}（不静默降级为全量工具）。"
        ) from None
    names = AGENT_TOOLSETS[resolved]
    # 按注册顺序重建（顺序即提示词内容，见 docstring 第 3 点）
    return {name: spec for name, spec in TOOL_REGISTRY.items() if name in names}


# ---------------------------------------------------------------------------
# 一致性断言（import 即执行，fail closed；风格照 tools/__init__.py 的
# AssertionError 守护）
# ---------------------------------------------------------------------------

#: G1（必须）名字合法性：每个 kind 的每个名字必须 ∈ TOOL_REGISTRY。
#: 失败模式：手写名单里工具改名后静默留下死名字（子集里挂着一个永不存在的
#: 工具名，枚举表少一行却不报错）。
for _kind, _names in AGENT_TOOLSETS.items():
    for _name in _names:
        if _name not in TOOL_REGISTRY:
            raise AssertionError(
                f"agent 档位 {_kind.value} 的工具名 {_name!r} 不在 TOOL_REGISTRY 内"
                "（手写名单与注册表脱节：工具改名后必须同步本表）"
            )

#: G2（必须）非 office 档必须非空且为**真子集**。
#: 失败模式：有人把全量表复制三份 → 隔离机制空转（三档等价于同一份全量）。
for _kind, _names in AGENT_TOOLSETS.items():
    if _kind is AgentKind.OFFICE:
        continue
    if not (0 < len(_names) < len(TOOL_REGISTRY)):
        raise AssertionError(
            f"agent 档位 {_kind.value} 必须非空且为真子集"
            f"（当前 {len(_names)} / 注册表 {len(TOOL_REGISTRY)}）"
        )

#: G3（必须）本轮验收不变式：内勤档 == 注册表全集。
#: 钉住「改注册表必须同步本表」，也是「Office 档 = 全量」的显式证据。
if AGENT_TOOLSETS[AgentKind.OFFICE] != frozenset(TOOL_REGISTRY):
    raise AssertionError(
        "AGENT_TOOLSETS[OFFICE] 必须等于 frozenset(TOOL_REGISTRY)"
        "（新增/删除工具时同步本表；本表是显式清单，不是注册表拷贝）"
    )
