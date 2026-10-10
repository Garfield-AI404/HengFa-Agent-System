"""登录与会话服务（规格 12.4/2.7.1/10.5）。

- 凭据只来自环境变量（MANAGER_ACCOUNTS（JSON 列表）/ DEVELOPER_ACCOUNT /
  DEVELOPER_PASSWORD），不硬编码、不落库（12.1 无用户表）。加 manager 只改
  `.env` + `docker compose up -d`，无需改代码/发版。
- 会话为 SESSION_SECRET 签名的无状态 Cookie（itsdangerous TimestampSigner）：
  篡改即失效（签名校验），过期即失效（时间戳 max_age），无需会话表。
- 密码比较用常量时间比较，防时序攻击。
- 失败语义不透露内部细节：登录失败统一 INVALID_CREDENTIALS；
  会话失效统一 UNAUTHENTICATED（10.5 拒绝响应只说明所需权限）。
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from administrative_agent.auth.models import Account, Role
from administrative_agent.config import settings

#: 会话 Cookie 名（HttpOnly+Secure+SameSite，2.7.1；不存 localStorage）
SESSION_COOKIE_NAME = "hfa_session"

#: 会话 payload 版本号：格式演进时旧 Cookie 直接失效
_PAYLOAD_VERSION = "v1"

#: 会话 max_age（秒）
_SESSION_TTL_SECONDS = settings.session_ttl_minutes * 60


class AuthenticationError(Exception):
    """认证失败（凭据错误或会话无效）。消息不携带内部细节。"""


class SessionError(AuthenticationError):
    """会话无效。"""


class MissingSessionError(SessionError):
    """无会话 Cookie。"""


class ExpiredSessionError(SessionError):
    """签名有效但已过期。"""


class InvalidSessionError(SessionError):
    """签名无效、格式错误或角色未知（10.5：一律拒绝）。"""


class CredentialsNotConfiguredError(Exception):
    """环境变量未配置时登录应 fail closed（10.5），而不是允许匿名。"""


def _signer() -> TimestampSigner:
    secret = settings.session_secret
    if not secret:
        raise CredentialsNotConfiguredError(
            "SESSION_SECRET 未配置：会话签名不可用，认证一律拒绝（fail closed）"
        )
    return TimestampSigner(secret)


@dataclass(frozen=True)
class _Credential:
    """一条已配置凭据：账号 + 口令 + 所属角色（不可变）。

    ``password`` 用 ``field(repr=False)``：dataclass 默认 ``repr`` 会打印
    全部字段，固化为口令字段不进 repr（纵深防御——日志/调试/异常拼接即使
    打印整个凭据对象也不会带出密钥值）。
    """

    account: str
    password: str = field(repr=False)
    role: Role


def _effective_credentials() -> tuple[list[_Credential], list[str]]:
    """构建生效凭据序列并做「首个生效」去重（纯函数，每次调用重建）。

    凭据顺序固定为 ``settings.manager_accounts`` 列表顺序，再追加 developer
    一条（tie-break 规则：**同一账号名出现多次时，靠前的条目生效，其余条目
    整条丢弃**）。返回 ``(生效凭据, 被丢弃的重名账号名)``；返回值只含账号名，
    绝不含口令。

    fail closed 口径（与既有 `if a and p` 一致）：
    - 账号或口令任一为空 → 该条**整体忽略**，不得变成"口令为空即可登录"；
    - 账号名重复（跨条目，含 manager 列表内 / manager 与 developer 交叉）→
      **前者生效、后者丢弃**。选择"丢弃"而非"全部拒绝"：一次配置笔误
      不应锁死整机（主 manager 与 developer 必须仍能登录，除非它自己正是
      被丢弃的那个）；同时保持**同一个账号名只对应一个角色与一个口令**，
      歧义身份仍被拒绝、不得放行。丢弃在启动期由 main.lifespan 记 error
      （见 ``dropped_duplicate_accounts``），运维先于用户看到。

    注意：本函数是登录热路径每次调用的纯函数，**不在内部打任何日志**
    （否则每次登录都刷），重名上报交给只读查询函数在启动期调用一次。
    """
    raw: list[tuple[str, str, Role]] = [
        *((m.account, m.password, Role.MANAGER) for m in settings.manager_accounts),
        (settings.developer_account, settings.developer_password, Role.DEVELOPER),
    ]
    creds = [
        _Credential(account=a, password=p, role=r) for a, p, r in raw if a and p
    ]
    effective: list[_Credential] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for cred in creds:
        if cred.account in seen:
            dropped.append(cred.account)
            continue
        seen.add(cred.account)
        effective.append(cred)
    return effective, dropped


def _configured_credentials() -> list[_Credential]:
    """已配置且生效的凭据清单（重名条目已按「首个生效」丢弃）。"""
    return _effective_credentials()[0]


def dropped_duplicate_accounts() -> list[str]:
    """因重名被丢弃的账号名（去重升序，**只含账号名、绝不含口令**）。

    供启动期调用一次以暴露配置笔误（main.lifespan 记 error，不阻断启动）。
    纯只读查询，不产生副作用、不打日志。
    """
    return sorted(set(_effective_credentials()[1]))


def dropped_incomplete_accounts() -> list[str]:
    """因 account 或 password 为空串而被整条丢弃的 manager 条目标识。

    返回条目账号名；``account`` 为空串的条目没有名字可报，用 ``index=<i>``
    标识（列表下标，便于运维定位到那一行）。**只含账号名/下标，绝不含口令。**

    供启动期调用一次（main.lifespan 记 error，不阻断启动）。动机：这类条目会被
    ``_effective_credentials`` 的 ``if a and p`` 整条丢弃；若列表内无一幸免，
    则一条 manager 凭据都不生效、无人能登录。本函数的覆盖面**比旧编号键更宽**：旧
    方案只有主 manager 对（``MANAGER_ACCOUNT``/``MANAGER_PASSWORD``）在
    ``SENSITIVE_SETTINGS`` 内，第二个 manager（``MANAGER_ACCOUNT_2``/
    ``MANAGER_PASSWORD_2``）半配从不进该清单、完全静默——覆盖面更宽是有意的取舍
    （半配属配置缺陷，error 级、只报账号名/索引、不阻断启动；漏报代价远大于误报）。
    纯只读查询，不产生副作用、不打日志。
    """
    return [
        (m.account or f"index={idx}")
        for idx, m in enumerate(settings.manager_accounts)
        if not (m.account and m.password)
    ]


def authenticate(account: str, password: str) -> Account:
    """校验凭据；成功返回不可变 Account，失败抛 AuthenticationError。

    - 比较使用 hmac.compare_digest（常量时间）；不区分"账号不存在"与
      "密码错误"，统一 INVALID_CREDENTIALS。
    - 账号数可变：**遍历全部候选条目、逐项完成账号与口令比较**，命中项只
      记录不提前返回，使耗时只与配置条目数有关、与命中位置无关，
      避免"第几个账号匹配"泄漏为可观测时序差异（原实现命中即 return，
      单账号下无此问题，多账号下会暴露命中序位）。
    """
    creds = _configured_credentials()
    if not creds:
        raise CredentialsNotConfiguredError(
            "认证凭据未配置（MANAGER_ACCOUNTS/DEVELOPER_*）：登录拒绝（fail closed）"
        )
    account_bytes = account.encode()
    password_bytes = password.encode()
    matched: _Credential | None = None
    for cred in creds:
        # 两项比较对每个候选都无条件执行（不做短路/提前返回），保持常量时间
        account_ok = hmac.compare_digest(account_bytes, cred.account.encode())
        password_ok = hmac.compare_digest(password_bytes, cred.password.encode())
        if account_ok and password_ok and matched is None:
            matched = cred
    if matched is None:
        raise AuthenticationError("invalid credentials")
    return Account(account=matched.account, role=matched.role)


def _serialize(payload: dict) -> bytes:
    """payload -> 签名前的稳定 JSON bytes（itsdangerous 只接受 bytes/str）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _deserialize(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise InvalidSessionError("malformed session payload") from None


def create_session(account: Account) -> str:
    """创建签名会话 Cookie 值（无状态，不落库）。"""
    payload = {"v": _PAYLOAD_VERSION, "account": account.account, "role": account.role.value}
    return _signer().sign(_serialize(payload)).decode("utf-8")


def validate_session(cookie_value: str) -> Account:
    """校验会话签名与有效期。

    失败语义细分（dependencies 据此映射 error_code，10.5 响应不透露细节）：
    - 无 Cookie：MissingSessionError
    - 签名有效但过期：ExpiredSessionError
    - 签名无效/格式错误/未知角色：InvalidSessionError
    """
    if not cookie_value:
        raise MissingSessionError("missing session")
    try:
        payload = _deserialize(_signer().unsign(cookie_value, max_age=_SESSION_TTL_SECONDS))
    except SignatureExpired as exc:
        raise ExpiredSessionError("expired session") from exc
    except BadSignature as exc:
        raise InvalidSessionError("invalid signature") from exc
    if not isinstance(payload, dict) or payload.get("v") != _PAYLOAD_VERSION:
        raise InvalidSessionError("unsupported session payload")
    account = payload.get("account")
    role = payload.get("role")
    if not isinstance(account, str) or not isinstance(role, str):
        raise InvalidSessionError("malformed session payload")
    try:
        return Account(account=account, role=Role(role))
    except ValueError as exc:
        raise InvalidSessionError("unknown role") from exc  # 10.5：未知角色一律拒绝
