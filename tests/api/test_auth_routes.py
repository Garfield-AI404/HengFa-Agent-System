"""D4 认证权限矩阵测试（规格 2.7.3、10.5、16.6 RV-06）。

覆盖：未登录 401；错误凭据 401；manager/developer 登录与 me；
角色互斥 403；失效会话拒绝；CSRF；密钥不出现在响应/日志。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from administrative_agent.auth.dependencies import require_developer, require_manager
from administrative_agent.auth.service import (
    SESSION_COOKIE_NAME,
    create_session,
)
from administrative_agent.auth.models import Account, Role
from administrative_agent.main import app

#: 测试凭据（仅测试环境；生产凭据只来自 .env）
TEST_MANAGER = "manager01"
TEST_MANAGER_PASSWORD = "mgr-pass-01"
TEST_DEVELOPER = "dev01"
TEST_DEVELOPER_PASSWORD = "dev-pass-01"
VALID_ORIGIN = "http://127.0.0.1:5173"

# 角色互斥依赖需要真实路由：构造独立测试 app（不污染主 app），
# 并复用主 app 的统一错误 handler，保证 403 响应格式与生产一致（2.7.3）
_role_test_app = FastAPI()


@_role_test_app.get("/require-manager")
def _rm(account: Account = Depends(require_manager)) -> dict:
    return {"account": account.account, "role": account.role.value}


@_role_test_app.get("/require-developer")
def _rd(account: Account = Depends(require_developer)) -> dict:
    return {"account": account.account, "role": account.role.value}


for _exc_type, _handler in app.exception_handlers.items():
    _role_test_app.add_exception_handler(_exc_type, _handler)


client = TestClient(app)
role_client = TestClient(_role_test_app)


def _login(account: str, password: str, origin: str | None = VALID_ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return client.post("/api/v1/auth/login", json={"account": account, "password": password}, headers=headers)


def _session_cookie(role: Role, account: str = "mgr") -> dict:
    """直接构造有效会话 Cookie（绕过登录）。"""
    return {SESSION_COOKIE_NAME: create_session(Account(account=account, role=role))}


# ---------------------------------------------------------------------------
# 权限矩阵
# ---------------------------------------------------------------------------


def test_unauthenticated_me_returns_401() -> None:
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "UNAUTHENTICATED"
    assert "trace_id" in body


def test_wrong_credentials_returns_401() -> None:
    resp = _login("manager01", "wrong-password")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "INVALID_CREDENTIALS"
    # 10.5：不透露是账号还是密码错误，不出现真实凭据
    assert "wrong-password" not in resp.text
    assert "manager01" not in resp.text


def test_unknown_account_returns_401() -> None:
    resp = _login("nobody", "whatever")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "INVALID_CREDENTIALS"


def test_manager_login_and_me() -> None:
    resp = _login(TEST_MANAGER, TEST_MANAGER_PASSWORD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"] == TEST_MANAGER
    assert body["role"] == "manager"
    assert "共享 manager 账号" in (body["shared_manager_note"] or "")  # 12.5
    cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    # Cookie 属性（2.7.1：HttpOnly、SameSite；Secure 由 COOKIE_SECURE 决定）
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie

    me = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert me.status_code == 200
    assert me.json()["role"] == "manager"


def test_developer_login_and_me() -> None:
    resp = _login(TEST_DEVELOPER, TEST_DEVELOPER_PASSWORD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "developer"
    assert body["shared_manager_note"] is None

    me = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: resp.cookies.get(SESSION_COOKIE_NAME)})
    assert me.status_code == 200
    assert me.json()["role"] == "developer"


def test_require_manager_rejects_developer_403() -> None:
    resp = role_client.get(
        "/require-manager", cookies=_session_cookie(Role.DEVELOPER, "dev01")
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "FORBIDDEN"
    # 10.5：只说明所需权限，不透露内部细节
    assert "需要 manager 权限" in body["message"]


def test_require_developer_rejects_manager_403() -> None:
    resp = role_client.get(
        "/require-developer", cookies=_session_cookie(Role.MANAGER, "manager01")
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "FORBIDDEN"


def test_manager_allowed_on_require_manager() -> None:
    resp = role_client.get(
        "/require-manager", cookies=_session_cookie(Role.MANAGER, "manager01")
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "manager"


def test_developer_allowed_on_require_developer() -> None:
    resp = role_client.get(
        "/require-developer", cookies=_session_cookie(Role.DEVELOPER, "dev01")
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "developer"


# ---------------------------------------------------------------------------
# 失效会话
# ---------------------------------------------------------------------------


def test_tampered_session_rejected() -> None:
    cookie = create_session(Account(account="manager01", role=Role.MANAGER))
    tampered = cookie[:-4] + ("AAAA" if cookie[-4:] != "AAAA" else "BBBB")
    resp = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: tampered})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"


def test_expired_session_rejected(monkeypatch) -> None:
    import administrative_agent.auth.service as svc
    from administrative_agent.auth.service import create_session

    # 将 TTL 置为负数（任何合法签名的会话都超过 max_age）→ 必抛 SignatureExpired。
    # 不能依赖 sleep(1.1)+TTL=1：itsdangerous 的 age 计算为 int 秒差，
    # 只有签发时刻落在秒边界后 0.1s 内才会判定过期（约 10% 概率，属随机翻车）。
    monkeypatch.setattr(svc, "_SESSION_TTL_SECONDS", -1)
    cookie = create_session(Account(account="manager01", role=Role.MANAGER))
    resp = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "SESSION_EXPIRED"


def test_unknown_role_session_rejected() -> None:
    import json

    from itsdangerous import TimestampSigner

    from administrative_agent.config import settings

    signer = TimestampSigner(settings.session_secret)
    forged = signer.sign(
        json.dumps({"v": "v1", "account": "hacker", "role": "superuser"}).encode("utf-8")
    ).decode("utf-8")
    resp = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: forged})
    assert resp.status_code == 401  # 10.5：未知角色一律拒绝


# ---------------------------------------------------------------------------
# CSRF（写 API：logout）
# ---------------------------------------------------------------------------


def test_logout_requires_csrf_origin() -> None:
    # 无 Origin/Referer → 403
    resp = client.post(
        "/api/v1/auth/logout", cookies=_session_cookie(Role.MANAGER, "manager01")
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_logout_rejects_foreign_origin() -> None:
    resp = client.post(
        "/api/v1/auth/logout",
        cookies=_session_cookie(Role.MANAGER, "manager01"),
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_logout_with_valid_origin_clears_cookie() -> None:
    resp = client.post(
        "/api/v1/auth/logout",
        cookies=_session_cookie(Role.MANAGER, "manager01"),
        headers={"Origin": VALID_ORIGIN},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "已注销。"
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "max-age=0" in set_cookie


def test_logout_with_valid_referer_ok() -> None:
    resp = client.post(
        "/api/v1/auth/logout",
        cookies=_session_cookie(Role.DEVELOPER, "dev01"),
        headers={"Referer": f"{VALID_ORIGIN}/developer"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 密钥纪律：凭据不出现在响应/日志
# ---------------------------------------------------------------------------


def test_credentials_never_in_responses() -> None:
    # 登录成功响应不含密码
    resp = client.post(
        "/api/v1/auth/login",
        json={"account": TEST_MANAGER, "password": TEST_MANAGER_PASSWORD},
        headers={"Origin": VALID_ORIGIN},
    )
    assert resp.status_code == 200
    assert TEST_MANAGER_PASSWORD not in resp.text
    assert TEST_DEVELOPER_PASSWORD not in resp.text
    # 未登录拒绝响应不含凭据
    resp2 = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: "garbage"})
    assert resp2.status_code == 401
    assert TEST_MANAGER_PASSWORD not in resp2.text
    # 错误凭据响应不回显密码
    resp3 = client.post(
        "/api/v1/auth/login", json={"account": TEST_MANAGER, "password": "bad-pass"}
    )
    assert resp3.status_code == 401
    assert "bad-pass" not in resp3.text


def test_login_validation_error_uniform() -> None:
    resp = client.post("/api/v1/auth/login", json={"account": ""})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert "trace_id" in body
