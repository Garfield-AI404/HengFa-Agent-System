"""客户、供货商、员工唯一匹配纯函数（规格 2.6 business/matching.py）。

4.3 顺序 2：exceptions 中的员工引用 → 唯一匹配；任何歧义先询问，不执行
写入。5.1/5.2 客户、供货商同口径（"按客户名称唯一查询；不唯一则询问"、
"不唯一则询问"）——本模块的匹配语义供 R1 员工与 R2/R3 客户、供货商共用
（match_employee / match_customer / match_supplier）。

纯规则层（2.6 规则 4）：不发起网络请求、不读写数据库。候选集合由调用方
（domains 适配层）经 ERP query 查询后传入；本函数只做确定性的引用归一化
与唯一匹配决策。

匹配策略（保守，不做模糊猜测）：
- 引用归一化：去除首尾空白、压缩内部连续空白、全角空格转半角；
- 精确匹配（归一化后全等）：唯一 → matched；多个（同名）→ ambiguous；
  零个 → not_found（由调用方询问"不存在/请核对名称"，不猜测）。

客户资料价格/价格类型/重量单位的多值冲突（5.1：多个冲突值按数据异常
处理，不能任选）属资料值解析，归 business/order_rules.py（本模块只做
实体名称唯一匹配）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

#: 匹配结果状态（4.3 顺序 2 三分支；ambiguous/not_found 均不得执行写入）
MATCHED = "matched"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"

#: 有效匹配状态集合：只有 MATCHED 允许继续执行写入
WRITABLE_MATCHES = frozenset({MATCHED})


@dataclass(frozen=True)
class EmployeeCandidate:
    """ERP employees 实体候选（id + 展示名）。"""

    employee_id: int
    name: str


@dataclass(frozen=True)
class CustomerCandidate:
    """ERP customers 实体候选（id + 展示名；5.1：按客户名称唯一查询）。"""

    customer_id: int
    name: str


@dataclass(frozen=True)
class SupplierCandidate:
    """ERP suppliers 实体候选（id + 展示名；5.2：按供货商名称唯一查询）。"""

    supplier_id: int
    name: str


@dataclass(frozen=True)
class MatchOutcome:
    """一次唯一匹配的决策结果（员工/客户/供货商共用）。

    - status=matched：entity_id/matched_name 给出唯一命中；
    - status=ambiguous：candidates 给出全部同名候选（(id, name)），
      调用方转 waiting_user_input 询问，不执行写入；
    - status=not_found：无精确匹配（引用错误或实体不在有效集合），
      调用方询问核对名称，不执行写入。

    employee_id 为员工场景的兼容别名（D12 调用方沿用）。
    """

    ref: str
    status: str
    entity_id: int | None = None
    matched_name: str | None = None
    candidates: tuple[tuple[int, str], ...] = ()

    @property
    def writable(self) -> bool:
        return self.status in WRITABLE_MATCHES

    @property
    def employee_id(self) -> int | None:
        """员工场景别名：entity_id（R1 考勤调用方兼容）。"""
        return self.entity_id


def normalize_ref(ref: str) -> str:
    """引用归一化：全角空格转半角、压缩连续空白、去除首尾空白。

    归一化只用于比较，不改变展示名（展示仍用 ERP 原始 name）。
    """
    return " ".join(str(ref).replace("　", " ").split())


def _unique_match(ref: str, named: Iterable[tuple[int, str]]) -> MatchOutcome:
    """(id, name) 候选序列 → 唯一匹配决策（员工/客户共用核心）。"""
    key = normalize_ref(ref)
    if not key:
        return MatchOutcome(ref=str(ref), status=NOT_FOUND)
    hits = [
        (entity_id, name)
        for entity_id, name in named
        if normalize_ref(name) == key
    ]
    if len(hits) == 1:
        entity_id, name = hits[0]
        return MatchOutcome(
            ref=str(ref),
            status=MATCHED,
            entity_id=entity_id,
            matched_name=name,
            candidates=(hits[0],),
        )
    if len(hits) > 1:
        return MatchOutcome(
            ref=str(ref),
            status=AMBIGUOUS,
            candidates=tuple(hits),
        )
    return MatchOutcome(ref=str(ref), status=NOT_FOUND)


def match_employee(
    ref: str,
    candidates: Iterable[EmployeeCandidate],
) -> MatchOutcome:
    """员工引用 → 唯一匹配（4.3 顺序 2）。

    候选为空或引用为空 → not_found；精确匹配唯一 → matched；
    多个同名命中 → ambiguous（携带全部候选供询问展示）。
    """
    return _unique_match(ref, [(c.employee_id, c.name) for c in candidates])


def match_customer(
    ref: str,
    candidates: Iterable[CustomerCandidate],
) -> MatchOutcome:
    """客户名称 → 唯一匹配（5.1：customer_id 按客户名称唯一查询）。

    不唯一则询问（ambiguous，携带全部同名候选）；无精确匹配 → not_found
    （由调用方询问核对客户名称）。匹配成功后调用方再查询客户资料做
    价格/价格类型/重量单位补全（5.1 L715）。
    """
    return _unique_match(ref, [(c.customer_id, c.name) for c in candidates])


def match_supplier(
    ref: str,
    candidates: Iterable[SupplierCandidate],
) -> MatchOutcome:
    """供货商名称 → 唯一匹配（5.2：supplier_id 按供货商名称唯一查询）。

    不唯一则询问（ambiguous，携带全部同名候选）；无精确匹配 → not_found
    （由调用方询问核对供货商名称）。匹配成功后调用方再按"供货商 + 色号"
    查询历史价格补全（5.2 价格补全算法）。
    """
    return _unique_match(ref, [(c.supplier_id, c.name) for c in candidates])
