"""客户/供货商唯一匹配纯函数测试（RV-19/RV-21；规格 5.1：customer_id
按客户名称唯一查询；5.2：supplier_id 按供货商名称唯一查询；不唯一则
询问；无精确匹配则询问核对）。

与 tests/unit/test_matching.py（员工匹配）共用 match_employee 语义；
本文件覆盖 match_customer / match_supplier 与 MatchOutcome.entity_id
泛化字段（employee_id 为员工场景兼容别名）。
"""

from __future__ import annotations

from administrative_agent.business.matching import (
    AMBIGUOUS,
    MATCHED,
    NOT_FOUND,
    CustomerCandidate,
    MatchOutcome,
    SupplierCandidate,
    match_customer,
    match_supplier,
    normalize_ref,
)

CANDIDATES = [
    CustomerCandidate(11, "示例机械有限公司"),
    CustomerCandidate(22, "示例装饰工程"),
    CustomerCandidate(33, "示例机械有限公司"),  # 同名客户（示例：不同法人主体）
]


# ---- 精确唯一命中 ----

def test_customer_exact_match_single() -> None:
    """客户名称精确唯一命中 → matched + customer_id/名。"""
    result = match_customer("示例装饰工程", CANDIDATES)
    assert result.status == MATCHED
    assert result.entity_id == 22  # 客户场景下 entity_id 即 customer_id
    assert result.matched_name == "示例装饰工程"
    assert result.writable is True


def test_customer_exact_match_among_many() -> None:
    """候选集合内精确匹配唯一 → matched（5.1：customer_id 按名称唯一查询）。"""
    result = match_customer("示例机械有限公司", [
        CustomerCandidate(11, "示例机械有限公司"),
        CustomerCandidate(22, "示例装饰工程"),
    ])
    assert result.status == MATCHED
    assert result.entity_id == 11


# ---- 同名歧义（不唯一则询问）----

def test_customer_duplicate_names_ambiguous() -> None:
    """同名客户（两个示例机械有限公司）→ ambiguous，不执行写入。"""
    result = match_customer("示例机械有限公司", CANDIDATES)
    assert result.status == AMBIGUOUS
    assert result.entity_id is None
    assert result.writable is False
    # 候选携带全部同名客户供询问展示（id + name）
    assert result.candidates == ((11, "示例机械有限公司"), (33, "示例机械有限公司"))


def test_customer_ambiguous_never_writable() -> None:
    """歧义状态不写入：writable 恒 False（与员工匹配同口径）。"""
    result = match_customer("示例机械有限公司", CANDIDATES)
    assert result.writable is False
    assert result.entity_id is None


# ---- 无匹配（询问核对名称，不做模糊猜测）----

def test_customer_not_found_when_no_candidate() -> None:
    """候选为空 → not_found。"""
    result = match_customer("示例", [])
    assert result.status == NOT_FOUND
    assert result.writable is False


def test_customer_not_found_no_fuzzy_match() -> None:
    """不做包含/前缀模糊猜测："示例" 不命中 "示例机械有限公司"。"""
    assert match_customer("示例", CANDIDATES).status == NOT_FOUND


# ---- 归一化与空引用 ----

def test_customer_ref_whitespace_matches_normalized_name() -> None:
    """引用带首尾空白仍精确命中（归一化只用于比较）。"""
    result = match_customer("  示例装饰工程  ", CANDIDATES)
    assert result.status == MATCHED
    assert result.entity_id == 22


def test_customer_empty_ref_not_found() -> None:
    """空引用 → not_found（不尝试匹配任何候选）。"""
    assert match_customer("", CANDIDATES).status == NOT_FOUND
    assert match_customer("   ", CANDIDATES).status == NOT_FOUND


# ---- 供货商唯一匹配（5.2：supplier_id 按供货商名称唯一查询）----

SUPPLIER_CANDIDATES = [
    SupplierCandidate(101, "示例塑粉材料有限公司"),
    SupplierCandidate(202, "汇通化工"),
    SupplierCandidate(303, "示例塑粉材料有限公司"),  # 同名供货商（示例）
]


def test_supplier_exact_match_single() -> None:
    """供货商名称精确唯一命中 → matched + supplier_id/名（5.2）。"""
    result = match_supplier("汇通化工", SUPPLIER_CANDIDATES)
    assert result.status == MATCHED
    assert result.entity_id == 202  # 供货商场景下 entity_id 即 supplier_id
    assert result.matched_name == "汇通化工"
    assert result.writable is True


def test_supplier_exact_match_among_many() -> None:
    """候选集合内精确匹配唯一 → matched。"""
    result = match_supplier("示例塑粉材料有限公司", [
        SupplierCandidate(101, "示例塑粉材料有限公司"),
        SupplierCandidate(202, "汇通化工"),
    ])
    assert result.status == MATCHED
    assert result.entity_id == 101


def test_supplier_duplicate_names_ambiguous() -> None:
    """同名供货商 → ambiguous，不执行写入（不唯一则询问）。"""
    result = match_supplier("示例塑粉材料有限公司", SUPPLIER_CANDIDATES)
    assert result.status == AMBIGUOUS
    assert result.entity_id is None
    assert result.writable is False
    assert result.candidates == (
        (101, "示例塑粉材料有限公司"),
        (303, "示例塑粉材料有限公司"),
    )


def test_supplier_not_found_no_fuzzy_match() -> None:
    """无精确匹配（含前缀不猜测）→ not_found，询问核对。"""
    assert match_supplier("示例", SUPPLIER_CANDIDATES).status == NOT_FOUND
    assert match_supplier("", SUPPLIER_CANDIDATES).status == NOT_FOUND


def test_supplier_ref_whitespace_matches_normalized_name() -> None:
    """引用带首尾空白仍精确命中（归一化与客户同口径）。"""
    result = match_supplier("  汇通化工  ", SUPPLIER_CANDIDATES)
    assert result.status == MATCHED
    assert result.entity_id == 202


# ---- MatchOutcome 泛化兼容 ----

def test_outcome_entity_id_and_employee_alias() -> None:
    """entity_id 为唯一 ID 字段；employee_id 为员工场景兼容别名。"""
    outcome = MatchOutcome(
        ref="张三", status=MATCHED, entity_id=5, matched_name="张三"
    )
    assert outcome.entity_id == 5
    assert outcome.employee_id == 5  # 兼容 D12 员工调用方
    outcome2 = MatchOutcome(ref="张三", status=MATCHED, entity_id=7)
    assert outcome2.employee_id == 7
