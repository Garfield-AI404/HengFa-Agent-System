"""登录、注销与会话接口（规格 2.7.3、2.7.1、12.5）。

- POST /api/v1/auth/login   登录并建立会话（公共）
- POST /api/v1/auth/logout  注销（已登录；写 API，必须通过 CSRF 校验）
- GET  /api/v1/auth/me      当前账号和角色（已登录）

会话：SESSION_SECRET 签名 Cookie（无状态，不落库——12.1 无用户表），
HttpOnly + Secure（COOKIE_SECURE 可配置，本地 http 联调置 False）+ SameSite=lax。

CSRF 方案（与 SameSite=lax Cookie 配合）：
- 写 API 校验 Origin/Referer 头必须命中 FRONTEND_ORIGIN 白名单；
- 缺失或失配一律 403 CSRF_VALIDATION_FAILED（fail closed）。
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from administrative_agent.auth.dependencies import get_current_account
from administrative_agent.auth.models import Account, Role
from administrative_agent.auth.service import (
    SESSION_COOKIE_NAME,
    AuthenticationError,
    CredentialsNotConfiguredError,
    authenticate,
    create_session,
)
from administrative_agent.config import settings
from administrative_agent.web.schemas import (
    AuthUserResponse,
    ErrorCode,
    LoginRequest,
    MessageResponse,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: 12.5：共享 manager 账号提示（登录/me 响应携带，前端展示）
_SHARED_MANAGER_NOTE = "当前为共享 manager 账号，操作将归集到同一账号，不区分具体操作人。"


def _origin_of(referer: str) -> str | None:
    """从 Referer 提取 origin（scheme://netloc）。"""
    try:
        parsed = urlparse(referer)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_allowed_origin(value: str | None) -> bool:
    if not value:
        return False
    origin = _origin_of(value) if value.startswith(("http://", "https://")) else None
    if origin is None:
        return False
    return origin in settings.frontend_origins


def check_csrf(request: Request) -> None:
    """写 API 的 CSRF 校验（fail closed）。

    - Origin 存在且命中白名单 → 通过；
    - Origin 缺失时校验 Referer 的 origin 部分；
    - 均缺失或失配 → 403 CSRF_VALIDATION_FAILED。
    """
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin:
        ok = _is_allowed_origin(origin)
    elif referer:
        ok = _is_allowed_origin(_origin_of(referer))
    else:
        ok = False
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": ErrorCode.CSRF_VALIDATION_FAILED.value,
                "message": "跨站请求校验失败，请从受信任来源访问。",
            },
        )


def _with_session_cookie(
    value: str, payload: object, max_age: int | None, status_code: int
) -> JSONResponse:
    """构造 JSON 响应并附带会话 Cookie（2.7.1 属性）。"""
    response = JSONResponse(content=payload, status_code=status_code)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=value,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    return response


@router.post("/login", response_model=AuthUserResponse)
def login(body: LoginRequest) -> JSONResponse:
    """登录：校验凭据并签发签名会话 Cookie。"""
    try:
        account = authenticate(body.account, body.password)
    except CredentialsNotConfiguredError:
        # fail closed：凭据未配置时拒绝一切登录，不提示配置细节
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": ErrorCode.CREDENTIALS_NOT_CONFIGURED.value,
                "message": "账号或密码错误。",
            },
        ) from None
    except AuthenticationError:
        # 统一 INVALID_CREDENTIALS，不区分账号不存在/密码错误（10.5）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": ErrorCode.INVALID_CREDENTIALS.value,
                "message": "账号或密码错误。",
            },
        ) from None

    token = create_session(account)
    content = AuthUserResponse(
        account=account.account,
        role=account.role.value,
        shared_manager_note=_SHARED_MANAGER_NOTE if account.role is Role.MANAGER else None,
    )
    return _with_session_cookie(
        token,
        content.model_dump(),
        max_age=settings.session_ttl_minutes * 60,
        status_code=200,
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    dependencies=[Depends(get_current_account), Depends(check_csrf)],
)
def logout() -> JSONResponse:
    """注销：失效会话 Cookie（写 API，CSRF 校验）。"""
    return _with_session_cookie(
        "", MessageResponse(message="已注销。").model_dump(), max_age=0, status_code=200
    )


@router.get("/me", response_model=AuthUserResponse)
def me(account: Account = Depends(get_current_account)) -> AuthUserResponse:
    """当前账号和角色（身份由不可变 context 注入，不信任前端声明）。"""
    return AuthUserResponse(
        account=account.account,
        role=account.role.value,
        shared_manager_note=_SHARED_MANAGER_NOTE if account.role is Role.MANAGER else None,
    )
