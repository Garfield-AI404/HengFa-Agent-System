"""[2026-09-13 白名单真源守护] QUERY_ITEM_WHITELIST ⊆ ERP 字段元数据。

问题背景：`agent/tools/crop.py` 的 `QUERY_ITEM_WHITELIST` 与 ERP 字段元数据
是**两个互不校验的真源**。白名单里写了 ERP 根本没有的字段（「幽灵项」），
`crop_items` 的 `if key in item`（crop.py）会静默吞掉——模型取不到值只能
反复重试。上一次生产事故（`employees.daily_wage` 被裁 → 财报「工资账款」
算不出）就是这类真源失配的表现。本测试把两个真源钉在一起：白名单的每个
字段都必须 ∈ ERP `list_entities` 元数据中该实体的字段集。

取数路径（真守护，非手抄清单）
--------------------------------
ERP 字段元数据的**唯一真源**是 ERP 仓库 `app/mcp/registry.py:ENTITY_REGISTRY`
——它由 ORM 模型列**自动构建**（`_build_fields`）并合并派生列声明
（`_DERIVED_SPECS`），因此与 ERP 实际库结构一致、不会手写漂移。本测试以
**子进程**调用 ERP 自身的解释器导入该注册表，打印 `{entity: [字段...]}`，
再与本地白名单比对。

- 子进程只**构建 ORM 元数据**（纯 SQLAlchemy 列/派生列声明），**不连任何
  数据库、不发起任何网络请求** → 离线、确定性；建立 `create_async_engine`
  也只是惰性对象，不实际连接。
- 若定位不到 ERP 仓库或其解释器，测试**显式 skip**（附原因），绝不静默
  通过——避免「守护悄悄失效」。

为何不用 agent 侧既有 fixture：`tests/unit/test_read_whitelist_extension.py`
与 `tests/unit/test_computed_columns.py` 里的 `list_entities` 元数据是**手抄**
的最小样例，拿它比对等于再造一个真源，发现不了 ERP 侧真实字段漂移。故本
测试直连 ERP 真源。

跨仓库运行依赖：本测试需要 `HengFa_Agent_system` 的同级目录存在 `hengfa-erp`
（`CLAUDE.md` 描述的并列布局）。可用环境变量 `HENGFA_ERP_BACKEND` 显式指定
ERP `backend` 目录（含 `venv` 与 `app.mcp.registry`）。ERP 不可用时 skip。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from administrative_agent.agent.tools.crop import QUERY_ITEM_WHITELIST
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

#: 本文件位于 <repo>/tests/unit/ → 仓库根 = parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 子进程输出约定：注册表 JSON 前加哨兵行，避免与导入期杂散打印混淆
_SENTINEL = "__HENGFA_ERP_ENTITY_FIELDS__"

#: 在 ERP 环境内执行的取数脚本（只读导入注册表，无 DB / 无网络）
_PROBE = (
    "import json;"
    "from app.mcp.registry import ENTITY_REGISTRY;"
    "print('{sentinel}' + json.dumps("
    "{{k: list(v['fields'].keys()) for k, v in ENTITY_REGISTRY.items()}}))"
).format(sentinel=_SENTINEL)


def _erp_backend_dir() -> Path | None:
    """定位 ERP backend 目录（含 app/ 与 venv/）。"""
    override = os.environ.get("HENGFA_ERP_BACKEND")
    candidates = ([Path(override)] if override else []) + [
        _REPO_ROOT.parent / "hengfa-erp" / "backend",
    ]
    for cand in candidates:
        if (cand / "app" / "mcp" / "registry.py").is_file():
            return cand
    return None


def _erp_python(backend: Path) -> tuple[Path | None, list[str]]:
    """定位可用于导入 ERP 注册表的解释器（agent venv 缺 ERP 依赖）。

    四级回退（依次尝试，全部不可用才 skip）：
    1. ``backend/venv``；2. ``backend/.venv``；3. 环境变量
    ``HENGFA_ERP_PYTHON``；4. 当前解释器 ``sys.executable``。

    返回 ``(解释器或 None, 已尝试路径列表)``——路径列表用于 skip 文案：
    ERP 侧若改用 ``.venv``/自建 virtualenv，守护不得退化成静默 skip，
    skip 时须把试过的路径列全，便于定位「守护消失」。
    """
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
def _erp_entity_fields() -> dict[str, list[str]]:
    """经 ERP 自身解释器导入 ENTITY_REGISTRY，取回 {entity: [字段...]}。

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
        pytest.skip(
            "ERP 仓库存在但未找到可用解释器；已尝试：\n  "
            + "\n  ".join(tried)
        )
    try:
        proc = subprocess.run(
            [str(python), "-c", _PROBE],
            cwd=str(backend),  # cwd=ERP backend：dotenv 从其 .env 读配置
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"无法执行 ERP 解释器导入注册表：{exc}")
    if proc.returncode != 0:
        pytest.skip(
            f"ERP 注册表导入失败（环境/依赖问题，非白名单问题）；"
            f"解释器={python}；已尝试：\n  "
            + "\n  ".join(tried)
            + "\n"
            + (proc.stderr or "")[-2000:]
        )
    marker_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_SENTINEL)]
    if not marker_lines:
        pytest.skip(f"ERP 取数脚本无输出（{_SENTINEL} 未出现）")
    payload = marker_lines[-1][len(_SENTINEL):]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover
        pytest.skip(f"ERP 注册表输出非 JSON：{exc}")
    if not isinstance(data, dict) or not data:
        pytest.skip("ERP 注册表为空")
    return {str(k): [str(f) for f in v] for k, v in data.items()}


def test_whitelist_entities_match_queryable_entities() -> None:
    """白名单实体集 == 读白名单 QUERYABLE_ENTITIES（防漏登/多登/等量替换）。

    用集合相等而非仅基数：等量替换（如把某实体换成 ``file_attachments``）
    长度不变，仅断言 ``len == 14`` 会漏过；集合相等断言可拦住。
    """
    assert set(QUERY_ITEM_WHITELIST) == set(QUERYABLE_ENTITIES)


def test_all_whitelist_entities_exist_in_erp_metadata() -> None:
    """白名单每个实体都必须在 ERP 元数据中存在（防实体改名/删除）。"""
    erp = _erp_entity_fields()
    missing = [e for e in QUERY_ITEM_WHITELIST if e not in erp]
    assert not missing, f"白名单实体在 ERP 元数据中不存在（实体已改名/删除？）：{missing}"


@pytest.mark.parametrize("entity", sorted(QUERY_ITEM_WHITELIST))
def test_whitelist_fields_subset_of_erp_metadata(entity: str) -> None:
    """核心守护：白名单字段 ⊆ ERP 该实体元数据字段集（逐实体给出幽灵清单）。"""
    erp = _erp_entity_fields()
    erp_fields = erp.get(entity)
    assert erp_fields is not None, f"实体 {entity!r} 不在 ERP 元数据中"
    erp_set = set(erp_fields)
    ghosts = [f for f in QUERY_ITEM_WHITELIST[entity] if f not in erp_set]
    assert not ghosts, (
        f"{entity} 白名单含 ERP 元数据不存在的字段（幽灵项，crop_items 会静默"
        f"吞掉致模型取不到值）：{ghosts}；ERP 现有字段：{sorted(erp_set)}"
    )
