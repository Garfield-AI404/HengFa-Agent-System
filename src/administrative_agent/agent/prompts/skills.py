"""Skill 层：流程包注册表与渲染。

skill 与 policy 是**正交两层**（勿混淆，见 ``skill_texts/README.md`` 对照表）：

- **policy**（``policies.py``）答"什么是对的"（领域口径，按领域粒度）；
- **skill**（本模块）答"先做什么后做什么"（编排，按任务粒度）。

skill **引用** policy（frontmatter ``policies:`` 声明依赖），**不承载、不替换、
不复制**：注入时两段并列——编排段来自 skill 正文，口径段来自被引用的规则包
原文。故本模块只渲染 skill 自己的步骤，引用规则包正文由调用方（``model_node``）
另段拼入。

加载纪律（fail closed，与 ``policies.py`` 的 ``_load_policy_texts`` 同款）：
目录缺失 / 文件缺失 / 存在未注册文件 / frontmatter 缺字段 / ``skill_key`` 与
文件名不一致 / ``policies`` 引用未注册 policy_key / ``tools`` 引用未注册工具 /
``native_tools`` 非空 / ``index``·``when_not`` 超长 / 正文为空 / 键集与代码内
``_EXPECTED_SKILL_KEYS`` 不一致 → ``RuntimeError``，**import 即失败**（不在
运行期静默降级）。新增或删除 skill 必须同步 ``_EXPECTED_SKILL_KEYS``。

审计口径：``SkillPackage.hash`` 与 ``PolicyPackage.hash`` 同构（
``sha256("{key}|{version}|{text}")``），但 skill **不写入** ``audit_events`` 的
``policy_key/policy_version/policy_hash`` 三列——那三列语义是"写操作当时的领域
规则版本"，skill 是编排、不改变规则。一期不新增审计列。

【扩展位·一期不实现】``SkillPackage.native_tools`` 为"skill 自带原生工具"的预留
字段，一期恒为空（非空即 RuntimeError）。方案原文设想 loader 返回
``{skill, tools}`` 嵌套结构；实际以本 frozen dataclass 上的预留字段承载同一意图
——未来加原生工具只需填该字段并在注册表合并点复用同一道校验闸（方案 §5），
已写 skill 的格式与调用方签名都不动，不返工。

设计全文见 `docs/plans/skill-layer-plan.md`（本地规划文档，gitignored）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from administrative_agent.agent.prompts.policies import ALL_POLICY_KEYS

#: skill 版本（正文 frontmatter 未写 version 时使用；11.1 审计口径同 policy）
SKILL_VERSION: str = "2026-09-13.1"

#: skill 正文文件目录（随代码包分发；文件名 = skill_key + ".md"，另有
#: README.md 说明文件——加载时跳过）
SKILL_TEXT_DIR: Path = Path(__file__).parent / "skill_texts"

#: ``index`` / ``when_not`` 单行长度上限（防决策轮索引表膨胀——索引表每轮
#: 恒定注入全部 skill，超长即每轮白烧上下文；方案 §1.4）
SKILL_ADMISSION_MAX_CHARS: int = 200

#: 代码内期望键集（防 skill_texts/ 下文件删错/增错——加载时校验键集一致）
_EXPECTED_SKILL_KEYS: frozenset[str] = frozenset(
    {"SK.financial_report", "SK.receivables", "SK.tax_estimate"}
)


@dataclass(frozen=True)
class SkillPackage:
    """一个 skill_key 的流程包（不可变；正文来自独立文件，非外部输入）。"""

    skill_key: str
    title: str
    steps: tuple[str, ...]
    index: str
    version: str = SKILL_VERSION
    #: 引用的 policy_key（已校验 ∈ ALL_POLICY_KEYS）
    policies: tuple[str, ...] = ()
    #: 允许调用的工具名（已校验 ∈ TOOL_REGISTRY）。**不做强制白名单**——
    #: 选了表外工具不拦截（方案 §4：避免误伤已验收流程）。一期唯一的消费点
    #: 是参数轮：`tool_args_node` 按"本轮工具是否 ∈ 本列表"决定注不注入流程
    #: 编排段（表外工具即模型已跑偏，此时不再把"本流程全程只读"之类摆在
    #: 同一条 prompt 里与写工具参数要求打架）。
    tools: tuple[str, ...] = ()
    #: 不适用边界（何时**不要**用本 skill）；与 index 一正一反，共同构成
    #: 决策轮索引表里的准入描述
    when_not: str = ""
    #: 【预留位·一期必须为空】skill 自带原生工具（方案 §5）
    native_tools: tuple[Any, ...] = ()

    @property
    def text(self) -> str:
        """渲染正文：编号步骤（注入 system prompt 用）。"""
        return "\n".join(f"{i + 1}. {step}" for i, step in enumerate(self.steps))

    @property
    def hash(self) -> str:
        """skill_hash：skill_key + version + 正文 的 sha256（与 policy 同口径）。"""
        raw = f"{self.skill_key}|{self.version}|{self.text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def admission(self) -> str:
        """准入描述：``index``（何时用）+ ``when_not``（何时不要用）。"""
        if self.when_not:
            return f"{self.index}；不适用于：{self.when_not}"
        return self.index


#: frontmatter 合法键集合（未知键即 fail closed）
_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "skill_key",
        "title",
        "version",
        "index",
        "policies",
        "tools",
        "when_not",
        "native_tools",
    }
)

#: 正文行编号前缀（"1. " 等；加载时剥离存入 steps，渲染时重新编号）
_NUMBER_PREFIX: re.Pattern[str] = re.compile(r"^\s*\d+\.\s+")


def _split_list(raw: str) -> tuple[str, ...]:
    """frontmatter 逗号分隔列表 → 元组（去空白、丢空项、保序去重）。"""
    seen: dict[str, None] = {}
    for item in raw.split(","):
        name = item.strip()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def _registered_tool_names() -> frozenset[str]:
    """工具注册表键集（延迟导入——本模块只做 import 期校验，无运行期依赖）。"""
    from administrative_agent.agent.tools import TOOL_REGISTRY

    return frozenset(TOOL_REGISTRY)


def _parse_md(path: Path) -> SkillPackage:
    """解析单个 skill 正文文件（手写 frontmatter 解析，无第三方依赖）。

    格式：``---`` 起止的 frontmatter（每行 ``key: value``）+ 编号步骤行
    （``1. ...`` 起，空行忽略）。任何校验失败 → RuntimeError（fail closed，
    import 即失败）。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 缺少 frontmatter 起始行 '---'"
        )
    frontmatter: dict[str, str] = {}
    idx = 1
    while idx < len(lines) and lines[idx].strip() != "---":
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if ":" not in line:
            # [2026-09-13 reviewer] 非空、无冒号的行只可能是"值折行"或误写。
            # 原实现对这种行静默跳过 → 折行的值被无声截断。改为 fail closed。
            raise RuntimeError(
                f"skill_texts 文件 {path.name!r} 的 frontmatter 第 {idx + 1} 行"
                f"既非空行也不含 ':'（{line.strip()!r}）——值必须与键同行，"
                "折行会被静默截断，故 fail closed"
            )
        key, _, value = line.partition(":")
        key = key.strip()
        # [2026-09-13 reviewer] 未知键与重复键都必须拦：
        # - 未知键：把 `policies:` 拼成 `policy:` 会让引用**静默变空**，
        #   与 fail-closed 声明不符；
        # - 重复键：后值覆盖前值 → 先写非法值再写一行合法值即可绕过
        #   policies 越界 / native_tools 非空两道闸（已实测复现）。
        if key not in _FRONTMATTER_KEYS:
            raise RuntimeError(
                f"skill_texts 文件 {path.name!r} 的 frontmatter 含未知键 {key!r}"
                f"（legal = {sorted(_FRONTMATTER_KEYS)}）——拼错字段名会静默丢"
                "引用，故 fail closed"
            )
        if key in frontmatter:
            raise RuntimeError(
                f"skill_texts 文件 {path.name!r} 的 frontmatter 重复声明键 {key!r}"
                "——后值会静默覆盖前值，可绕过越界引用/native_tools 校验，"
                "故 fail closed"
            )
        frontmatter[key] = value.strip()
        idx += 1
    if idx >= len(lines):
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 缺少 frontmatter 结束行 '---'"
        )

    skill_key = frontmatter.get("skill_key", "")
    title = frontmatter.get("title", "")
    index = frontmatter.get("index", "")
    when_not = frontmatter.get("when_not", "")
    version = frontmatter.get("version") or SKILL_VERSION
    if not skill_key or not title or not index:
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 的 frontmatter 缺少 "
            "skill_key/title/index 字段（三字段必填；version 可缺省用 SKILL_VERSION）"
        )
    if skill_key != path.stem:
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 的 skill_key {skill_key!r}"
            f" 与文件名（{path.stem!r}）不一致"
        )
    for name, value in (("index", index), ("when_not", when_not)):
        if len(value) > SKILL_ADMISSION_MAX_CHARS:
            raise RuntimeError(
                f"skill_texts 文件 {path.name!r} 的 {name} 超长"
                f"（{len(value)} > {SKILL_ADMISSION_MAX_CHARS} 字）——索引表每轮"
                "恒定注入全部 skill，超长即每轮白烧上下文（fail closed）"
            )
    # native_tools 是一期预留位：字段可以存在（便于格式先行），但值必须为空
    if frontmatter.get("native_tools", ""):
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 声明了 native_tools —— 一期不支持"
            "skill 自带原生工具（预留位必须留空，见方案 §5）"
        )

    policies = _split_list(frontmatter.get("policies", ""))
    unknown_policies = [k for k in policies if k not in ALL_POLICY_KEYS]
    if unknown_policies:
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 引用了未注册的 policy_key："
            f"{sorted(unknown_policies)}（fail closed；legal = ALL_POLICY_KEYS）"
        )
    tools = _split_list(frontmatter.get("tools", ""))
    registered_tools = _registered_tool_names()
    unknown_tools = [t for t in tools if t not in registered_tools]
    if unknown_tools:
        raise RuntimeError(
            f"skill_texts 文件 {path.name!r} 引用了未注册的工具："
            f"{sorted(unknown_tools)}（fail closed；legal = TOOL_REGISTRY）"
        )

    steps = tuple(
        _NUMBER_PREFIX.sub("", line.strip())
        for line in lines[idx + 1 :]
        if line.strip()
    )
    if not steps:
        raise RuntimeError(f"skill_texts 文件 {path.name!r} 正文为空（无步骤条目）")

    return SkillPackage(
        skill_key=skill_key,
        title=title,
        steps=steps,
        index=index,
        version=version,
        policies=policies,
        tools=tools,
        when_not=when_not,
    )


def _load_skill_texts() -> list[SkillPackage]:
    """加载 skill_texts/ 下全部流程包（文件齐全 + 键集与代码一致）。

    fail closed：目录缺失/文件缺失/存在未注册文件/解析失败 → RuntimeError
    （import 即失败，不在运行期静默降级）。README.md 为说明文件，跳过。
    """
    if not SKILL_TEXT_DIR.is_dir():
        raise RuntimeError(f"skill 正文目录缺失（fail closed）：{SKILL_TEXT_DIR}")
    packages = [
        _parse_md(path)
        for path in sorted(SKILL_TEXT_DIR.glob("*.md"))
        if path.name != "README.md"
    ]
    keys = frozenset(p.skill_key for p in packages)
    missing = _EXPECTED_SKILL_KEYS - keys
    extra = keys - _EXPECTED_SKILL_KEYS
    if missing:
        raise RuntimeError(f"skill_texts 缺少流程包文件：{sorted(missing)}（fail closed）")
    if extra:
        raise RuntimeError(
            f"skill_texts 存在未注册流程包文件：{sorted(extra)}（fail closed）"
        )
    return packages


#: 流程包注册表（键 = skill_key）
SKILL_REGISTRY: dict[str, SkillPackage] = {
    p.skill_key: p for p in _load_skill_texts()
}

#: 全部合法 skill_key（AgentDecision.skill_name 枚举与 active_skill 校验用）
ALL_SKILL_KEYS: frozenset[str] = frozenset(SKILL_REGISTRY)

#: skill_key → skill_hash（**一期暂无消费方**——不写审计列、不参与校验，见模块
#: docstring；留给后续的一致性校验与回放取证，故不删）
SKILL_HASHES: dict[str, str] = {k: p.hash for k, p in SKILL_REGISTRY.items()}


def skill_for(skill_key: str | None) -> SkillPackage | None:
    """skill_key → 流程包；``None`` / 未注册键 → ``None``（不进入任何流程）。

    未注册键按"不进入流程"处理而非报错：模型幻觉的 skill_name 只会让本轮的
    流程引导缺席（退化为常规对话），不会中断对话——与 ``tool_name`` 的
    fail closed 口径不同，因为 skill 是**引导**而非**执行**，没有可被绕过的
    副作用（工具名幻觉会打到执行层，故那里必须拦）。
    """
    if not skill_key:
        return None
    return SKILL_REGISTRY.get(skill_key)


def render_index() -> str:
    """决策轮索引表正文（每个 skill 一行：``key：title｜准入描述``）。

    无 skill 时返回空串（调用方据此跳过整段，不留空标题）。
    """
    return "\n".join(
        f"- {p.skill_key}：{p.title}｜{p.admission}" for p in SKILL_REGISTRY.values()
    )


def render_schema_instruction() -> str:
    """决策轮结构化输出说明的**追加段**（``skill_name`` 字段契约 + 枚举表）。

    与 ``build_select_schema`` 的既有分工一致：那里生成字段结构，本函数补
    流程字段。返回空串时调用方原样拼接（无 skill 即字节不变）。措辞含
    "json"/"字段结构" 与 deepseek json_mode 约束同纪律。
    """
    if not SKILL_REGISTRY:
        return ""
    return (
        "\n字段结构在上述基础上增加一项 skill_name —— 本轮要进入的流程。"
        "取下列枚举表之一，或 null（不进入任何流程，按常规对话处理）：\n"
        + render_index()
        + "\n进入某流程后，**每一轮都必须继续声明该流程**（用户明确转向别的话题"
        "才置 null）——skill_name 每轮重新判定、以本轮为准，不声明即视为退出。"
    )
