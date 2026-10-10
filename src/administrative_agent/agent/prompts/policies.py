"""版本化规则包：policy_key 注册表与 15 个规则包正文（10.2 表 L1119-1127）。

- 规则包正文来自独立文件（``agent/prompts/policy_texts/*.md``，随代码包
  分发）：改规则改文件不改代码（10.2 要求 2：不从聊天、Excel 或 ERP
  文本生成）；
- 每个包携带 policy_version 与 policy_hash（11.1 审计与回放）；
- 正文按 10.2 注入内容列逐条展开为模型可执行的业务口径；
  条件式注入只减少上下文和混淆，不能代替领域工具中的确定性校验
  （10.2 要求 7——D11+ 领域工具负责执行侧校验）。
- ``R4.balance.scope``：当期应收口径已开放（只经 SK.receivables 流程给出）、
  未结余额以 ERP 财务-资产管理页为准、未走流程禁止自行推算欠款，
  实际收付流水照常可查。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

#: 规则包版本（变更规则正文时递增；11.1 记录到 audit_events.policy_version；
#: 缺省 version——正文文件 frontmatter 未写 version 时使用）
POLICY_VERSION: str = "2026-08-23.1"

#: 规则正文文件目录（随代码包分发；文件名 = policy_key + ".md"，另有
#: README.md 说明文件——加载时跳过）
POLICY_TEXT_DIR: Path = Path(__file__).parent / "policy_texts"

#: 代码内期望键集（防 policy_texts/ 下文件删错/增错——加载时校验键集一致）
_EXPECTED_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "R1.attendance.daily",
        "R1.salary.calculation",
        "R2.order.create.metal",
        "R2.order.create.powder",
        "R3.order.update.metal",
        "R3.order.update.powder",
        "R4.query",
        "R4.export.fields",
        "R4.balance.scope",
        "R5.analysis",
        "R6.reconciliation.plan",
        "R6.reconciliation.execute",
        "R6.mapping",
        "R7.finance.create",
        "R7.finance.update",
    }
)


@dataclass(frozen=True)
class PolicyPackage:
    """一个 policy_key 的规则包（不可变；正文来自独立文件，非外部输入）。"""

    policy_key: str
    title: str
    rules: tuple[str, ...]
    version: str = POLICY_VERSION

    @property
    def text(self) -> str:
        """渲染正文：编号条目（注入 system prompt 用）。"""
        return "\n".join(f"{i + 1}. {rule}" for i, rule in enumerate(self.rules))

    @property
    def hash(self) -> str:
        """policy_hash：policy_key + version + 正文 的 sha256（11.1）。"""
        raw = f"{self.policy_key}|{self.version}|{self.text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


#: 正文行编号前缀（"1. " 等；加载时剥离存入 rules，渲染时重新编号）
_NUMBER_PREFIX: re.Pattern[str] = re.compile(r"^\s*\d+\.\s+")


def _parse_md(path: Path) -> PolicyPackage:
    """解析单个规则正文文件（手写 frontmatter 解析，无第三方依赖）。

    格式：``---`` 起止的 frontmatter（每行 ``key: value``）+ 编号规则行
    （``1. ...`` 起，空行忽略）。任何校验失败 → RuntimeError（fail
    closed，import 即失败）。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(
            f"policy_texts 文件 {path.name!r} 缺少 frontmatter 起始行 '---'"
        )
    frontmatter: dict[str, str] = {}
    idx = 1
    while idx < len(lines) and lines[idx].strip() != "---":
        line = lines[idx]
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()
        idx += 1
    if idx >= len(lines):
        raise RuntimeError(
            f"policy_texts 文件 {path.name!r} 缺少 frontmatter 结束行 '---'"
        )
    policy_key = frontmatter.get("policy_key", "")
    title = frontmatter.get("title", "")
    version = frontmatter.get("version") or POLICY_VERSION
    if not policy_key or not title:
        raise RuntimeError(
            f"policy_texts 文件 {path.name!r} 的 frontmatter 缺少 policy_key/title"
            " 字段（三字段必须齐全；version 可缺省用 POLICY_VERSION）"
        )
    if policy_key != path.stem:
        raise RuntimeError(
            f"policy_texts 文件 {path.name!r} 的 policy_key {policy_key!r}"
            f" 与文件名（{path.stem!r}）不一致"
        )
    rules = tuple(
        _NUMBER_PREFIX.sub("", line.strip()) for line in lines[idx + 1:] if line.strip()
    )
    if not rules:
        raise RuntimeError(f"policy_texts 文件 {path.name!r} 正文为空（无规则条目）")
    return PolicyPackage(policy_key, title, rules, version=version)


def _load_policy_texts() -> list[PolicyPackage]:
    """加载 policy_texts/ 下全部规则包（15 文件齐全 + 键集与代码一致）。

    fail closed：目录缺失/文件缺失/存在未注册文件/解析失败 → RuntimeError
    （import 即失败，不在运行期静默降级）。README.md 为说明文件，跳过。
    """
    if not POLICY_TEXT_DIR.is_dir():
        raise RuntimeError(f"规则正文目录缺失（fail closed）：{POLICY_TEXT_DIR}")
    packages = [
        _parse_md(path)
        for path in sorted(POLICY_TEXT_DIR.glob("*.md"))
        if path.name != "README.md"
    ]
    keys = frozenset(p.policy_key for p in packages)
    missing = _EXPECTED_POLICY_KEYS - keys
    extra = keys - _EXPECTED_POLICY_KEYS
    if missing:
        raise RuntimeError(f"policy_texts 缺少规则包文件：{sorted(missing)}（fail closed）")
    if extra:
        raise RuntimeError(
            f"policy_texts 存在未注册规则包文件：{sorted(extra)}（fail closed）"
        )
    return packages


#: 10.2 表顺序的注册表（15 键；顺序与本文档 L1119-1127 一致）
POLICY_REGISTRY: dict[str, PolicyPackage] = {
    p.policy_key: p for p in _load_policy_texts()
}

#: 全部合法 policy_key（10.2 表 15 键；代码枚举集合）
ALL_POLICY_KEYS: frozenset[str] = frozenset(POLICY_REGISTRY)

#: policy_key → policy_hash（11.1 审计；供一致性校验与回放）
POLICY_HASHES: dict[str, str] = {k: p.hash for k, p in POLICY_REGISTRY.items()}


#: [2026-09-16 P2-1 形态 A] 规则包**准入一句话**：每个 policy_key 一条「何时
#: 适用」的语义摘要，供决策轮规则包索引段渲染（``model_node`` 的「已加载规则包」
#: 段）——原渲染只有裸 key 名（``- R4.query``），模型看不出各包管什么、就无从
#: 判断该不该触发相应工具。本表**只描述适用场景，不承载正文**：正文仍由
#: policy_texts/*.md 提供（决策轮在选中 skill 后、参数轮在选中工具后按
#: policy_family 注入），故本表与规则正文不构成双真源。
#:
#: **键集恒等于 ALL_POLICY_KEYS**（多一个少一个即 import 期 RuntimeError，
#: fail closed——新增规则包时忘补准入句会在启动期炸，不在运行期静默降级）。
#: 与 ``PolicyPackage.hash`` **完全无关**：hash 只算 ``key|version|正文``
#: （见 ``PolicyPackage.hash``），本表的增删改**不得**影响 POLICY_HASHES。
POLICY_ADMISSION: dict[str, str] = {
    "R1.attendance.daily": "用户要记录当天考勤（谁没来、请假、加班）时适用",
    "R1.salary.calculation": "用户要算工资或问工资怎么算时适用",
    "R2.order.create.metal": "用户要新建钣金订单（客户、型号、数量）时适用",
    "R2.order.create.powder": "供货商送货到货、要新建塑粉订单（色号、kg）时适用",
    "R3.order.update.metal": "用户要改已有钣金订单的字段（状态、交期、数量等）时适用",
    "R3.order.update.powder": "用户要改已有塑粉订单的字段（色号、重量、价格）时适用",
    "R4.balance.scope": "问客户/供货商还欠多少、未结余额、当期应收时适用；不得自行推算未结余额",
    "R4.export.fields": "用户要导出 ERP 数据到 Excel（列、合计、文件名）时适用",
    "R4.query": "凡涉及 ERP 数据的查询都适用：先查后答、字段、分页、空结果",
    "R5.analysis": "对 ERP 数据做分析、下结论、给建议时适用",
    "R6.mapping": "管理对账映射模板（发布版本、列校验、版本递增）时适用",
    "R6.reconciliation.execute": "用户确认差异报告后要写回 ERP 订单时适用",
    "R6.reconciliation.plan": "生成对账差异清单时适用（只读，禁止写入）",
    "R7.finance.create": "用户要记录收入/支出/投资款流水时适用",
    "R7.finance.update": "用户要改已有收支记录的字段时适用",
}

if set(POLICY_ADMISSION) != set(ALL_POLICY_KEYS):
    raise RuntimeError(
        "POLICY_ADMISSION 键集与 ALL_POLICY_KEYS 不一致（fail closed，import 即失败）："
        f"缺 {sorted(set(ALL_POLICY_KEYS) - set(POLICY_ADMISSION))}、"
        f"多 {sorted(set(POLICY_ADMISSION) - set(ALL_POLICY_KEYS))}"
    )
if any(not text for text in POLICY_ADMISSION.values()):
    raise RuntimeError("POLICY_ADMISSION 存在空的准入描述（值一律非空，fail closed）")


def policy_for(policy_key: str) -> PolicyPackage:
    """按代码枚举取规则包；非注册键 KeyError → 调用方 fail closed。

    任何外部字符串都不能通过本函数取到包（10.2 要求 1：只接受代码枚举）。
    """
    if policy_key not in POLICY_REGISTRY:
        raise KeyError(f"unknown policy_key: {policy_key!r}（policy_key 只接受代码枚举）")
    return POLICY_REGISTRY[policy_key]
