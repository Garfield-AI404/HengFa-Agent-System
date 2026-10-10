"""身份模型（规格 12.4/12.5、10.5）。

一期只有 manager 与 developer 两种角色；身份不可变（frozen），
每次请求由依赖注入，不允许模型或消息改写（8.4）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Role(str, enum.Enum):
    """一期仅有的两种角色（1.2）。"""

    MANAGER = "manager"
    DEVELOPER = "developer"


@dataclass(frozen=True)
class Account:
    """不可变身份 context：账号与角色。

    manager 为共享账号（12.5：不虚构具体自然人身份）。
    """

    account: str
    role: Role
