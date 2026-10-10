"""FastAPI 认证依赖（规格 10.5 fail closed、2.6 auth/dependencies.py）。

未知角色、缺失身份、失效会话一律拒绝；拒绝响应只说明所需权限，
不透露内部认证细节（10.5）。错误响应统一格式（error_code/message/
details/trace_id，2.7.3）由 main.py 全局 handler 构造，本模块只抛
HTTPException（detail 为 ApiError dict），不依赖 web 层模块。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from administrative_agent.auth.models import Account, Role
from administrative_agent.auth.service import (
    SESSION_COOKIE_NAME,
    ExpiredSessionError,
    InvalidSessionError,
    MissingSessionError,
    validate_session,
)

#: 统一错误码（与 web/schemas.py ErrorCode 一致；2.7.3 前端分支只读 error_code）
CODE_UNAUTHENTICATED = "UNAUTHENTICATED"
CODE_SESSION_EXPIRED = "SESSION_EXPIRED"
CODE_FORBIDDEN = "FORBIDDEN"


def _unauthorized(code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error_code": code, "message": "未登录或会话已失效，请重新登录。"},
    )


def get_current_account(request: Request) -> Account:
    """从 HttpOnly 会话 Cookie 解析不可变身份；任何失败一律 401。

    错误码映射（10.5：响应不透露内部细节，只区分前端行为）：
    - 无 Cookie / 签名无效 / 未知角色：UNAUTHENTICATED
    - 签名有效但过期：SESSION_EXPIRED（2.7.4 前端据此清理缓存并跳转登录）
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise _unauthorized(CODE_UNAUTHENTICATED)
    try:
        return validate_session(cookie)
    except ExpiredSessionError:
        raise _unauthorized(CODE_SESSION_EXPIRED) from None
    except (InvalidSessionError, MissingSessionError):
        raise _unauthorized(CODE_UNAUTHENTICATED) from None


def require_manager(account: Account = Depends(get_current_account)) -> Account:
    """业务工具/路由只允许 manager（10.5）；developer 一律 403。"""
    if account.role is not Role.MANAGER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": CODE_FORBIDDEN,
                "message": "需要 manager 权限；当前角色无权限访问该接口。",
            },
        )
    return account


def require_developer(account: Account = Depends(get_current_account)) -> Account:
    """developer 专属接口（健康、脱敏技术日志）；manager 一律 403。"""
    if account.role is not Role.DEVELOPER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": CODE_FORBIDDEN,
                "message": "需要 developer 权限；当前角色无权限访问该接口。",
            },
        )
    return account
