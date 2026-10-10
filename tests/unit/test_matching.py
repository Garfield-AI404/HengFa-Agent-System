"""员工唯一匹配纯函数测试（4.3 顺序 2；规格 2.6 business/matching.py）。

覆盖：精确唯一命中、同名歧义（不执行写入）、无匹配、空白归一化、
空引用、writable 语义、候选为空。
"""

from __future__ import annotations

import pytest

from administrative_agent.business.matching import (
    AMBIGUOUS,
    MATCHED,
    NOT_FOUND,
    EmployeeCandidate,
    MatchOutcome,
    match_employee,
    normalize_ref,
)

CANDIDATES = [
    EmployeeCandidate(1, "张三"),
    EmployeeCandidate(2, "李四"),
    EmployeeCandidate(3, "王五"),
    EmployeeCandidate(4, "张三"),  # 同名员工（示例：不同部门）
]


def outcome(**overrides) -> MatchOutcome:
    base = dict(ref="张三", status=MATCHED, entity_id=1, matched_name="张三")
    base.update(overrides)
    return MatchOutcome(**base)


# ---- 精确唯一命中 ----

def test_exact_match_single() -> None:
    """精确唯一命中 → matched + 员工 ID/名。"""
    result = match_employee("张三", [EmployeeCandidate(1, "张三")])
    assert result.status == MATCHED
    assert result.employee_id == 1
    assert result.matched_name == "张三"
    assert result.writable is True


def test_exact_match_among_many() -> None:
    """候选集合内精确匹配唯一 → matched。"""
    result = match_employee("李四", CANDIDATES)
    assert result.status == MATCHED
    assert result.employee_id == 2
    assert result.writable is True


# ---- 同名歧义 ----

def test_duplicate_names_ambiguous() -> None:
    """同名员工（两个张三）→ ambiguous，不执行写入。"""
    result = match_employee("张三", CANDIDATES)
    assert result.status == AMBIGUOUS
    assert result.employee_id is None
    assert result.writable is False
    # 候选携带全部同名员工供询问展示（id + name）
    assert result.candidates == ((1, "张三"), (4, "张三"))
    assert len(result.candidates) == 2


def test_ambiguous_candidates_keep_original_names() -> None:
    """歧义候选展示用 ERP 原始名（归一化只用于比较，不改展示名）。"""
    result = match_employee(" 张三 ", [
        EmployeeCandidate(7, "张三"),
        EmployeeCandidate(8, "张三"),
    ])
    assert result.status == AMBIGUOUS
    assert result.candidates == ((7, "张三"), (8, "张三"))


# ---- 无匹配 ----

def test_not_found_when_no_candidate() -> None:
    """候选为空 → not_found（引用无法匹配，不执行写入）。"""
    result = match_employee("张三", [])
    assert result.status == NOT_FOUND
    assert result.writable is False


def test_not_found_when_no_exact_match() -> None:
    """无精确匹配（如"张四"）→ not_found，不做模糊猜测。"""
    result = match_employee("张四", CANDIDATES)
    assert result.status == NOT_FOUND
    assert result.employee_id is None


def test_not_found_does_not_fuzzy_match() -> None:
    """不做包含/前缀模糊猜测：'张' 或 '张三丰' 都不命中 '张三'。"""
    assert match_employee("张", CANDIDATES).status == NOT_FOUND
    assert match_employee("张三丰", CANDIDATES).status == NOT_FOUND


# ---- 归一化 ----

def test_normalize_whitespace() -> None:
    """归一化：全角空格转半角、连续空白压缩为单个、去首尾空白。"""
    assert normalize_ref("  张三  ") == "张三"
    assert normalize_ref("  张  三  ") == "张 三"  # 压缩为单个空格，不删除
    assert normalize_ref("张　三") == "张 三"  # 全角空格转半角
    assert normalize_ref("") == ""


def test_ref_whitespace_matches_normalized_name() -> None:
    """引用带首尾空白仍精确命中归一化后的候选名（内部空白保留）。"""
    result = match_employee("  张三  ", [EmployeeCandidate(1, "张三")])
    assert result.status == MATCHED
    assert result.employee_id == 1
    result = match_employee("  李 四  ", [EmployeeCandidate(2, "李 四")])
    assert result.status == MATCHED


def test_empty_ref_not_found() -> None:
    """空引用 → not_found（不尝试匹配任何候选）。"""
    assert match_employee("", CANDIDATES).status == NOT_FOUND
    assert match_employee("   ", CANDIDATES).status == NOT_FOUND
