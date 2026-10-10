"""多 manager 账号（MANAGER_ACCOUNTS JSON 列表）测试。

覆盖四组行为：
- 多账号登录：列表内第二个 manager 能登录，角色为 manager；
- 会话隔离：两个 manager 账号名不同、会话 Cookie 不同、thread_id 前缀不同、
  会话索引/删除归属互相隔离；
- fail closed：条目半配（只配账号或只配口令）该条整条丢弃；账号名重名按
  「首个生效、后者丢弃」处理——歧义身份仍被拒绝，但一次配置笔误不得锁死整机；
  列表为空 = 无 manager 可登录；
- 配置形态与恶性配置：JSON 正常解析多账号；空串=未配置不炸；键名笔误 / JSON
  语法错 → Settings 实例化即抛（应用起不来）；口令绝不进 repr/日志/响应。

约定：设置里的 manager 列表通过 monkeypatch 覆盖共享 `settings` 单例
（`auth.service` 与 `routes_auth` 读同一实例），用例结束自动还原，避免用例间串扰。
"""

from __future__ import annotations

import hmac
import json
import time

import pytest
import structlog.testing
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

from administrative_agent import main as main_module
from administrative_agent.agent.checkpoint import CheckpointService
from administrative_agent.auth import service as auth_service
from administrative_agent.auth.models import Role
from administrative_agent.auth.service import (
    SESSION_COOKIE_NAME,
    AuthenticationError,
    CredentialsNotConfiguredError,
    authenticate,
    dropped_duplicate_accounts,
    dropped_incomplete_accounts,
)
from administrative_agent.config import ManagerAccount, Settings, settings
from administrative_agent.main import app
from administrative_agent.persistence.repositories import SqlAlchemyThreadRepository
from administrative_agent.web import routes_chat

from tests.api.conftest import ScriptedDecisionClient, _unset_state, headers

VALID_ORIGIN = "http://127.0.0.1:5173"
#: conftest 中固定的第一个 manager
MGR1 = "manager01"
MGR1_PW = "mgr-pass-01"
#: 第二个（个人）manager
MGR2 = "manager02"
MGR2_PW = "mgr-pass-02"


def _login(client: TestClient, account: str, password: str, origin: str | None = VALID_ORIGIN):
    req_headers = {"Origin": origin} if origin else {}
    return client.post(
        "/api/v1/auth/login",
        json={"account": account, "password": password},
        headers=req_headers,
    )


def _set_managers(monkeypatch, *entries: tuple[str, str]) -> None:
    """把生效 manager 列表覆盖为给定条目（按顺序）。"""
    monkeypatch.setattr(
        settings,
        "manager_accounts",
        [ManagerAccount(account=a, password=p) for a, p in entries],
    )


def _wait_run_finished(thread_id: str, timeout: float = 10.0) -> None:
    """等待 POST /chat/messages 启动的后台图循环结束。

    不等待会让后台线程逃出用例：它可能在本用例 teardown 清掉
    `app.state.model_decision_client` 之后才向模型取决策，从而回退到真实
    模型客户端发起网络调用，拖住后续用例（实测会让 test_chat_api 卡死）。
    `_leave_thread` 在后台循环 finally 中无条件执行，轮询其登记即可判定结束。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with routes_chat._ACTIVE_THREADS_LOCK:  # noqa: SLF001
            if thread_id not in routes_chat._ACTIVE_THREADS:  # noqa: SLF001
                return
        time.sleep(0.02)
    raise AssertionError(f"会话 {thread_id} 的后台图循环未在 {timeout}s 内结束")


# ---------------------------------------------------------------------------
# 配置形态：JSON 列表解析 / 未配置容忍 / 恶性配置响亮
# ---------------------------------------------------------------------------


def test_manager_accounts_json_parsed_in_order(monkeypatch) -> None:
    """MANAGER_ACCOUNTS 是 JSON 列表 → 解析为多条 ManagerAccount，顺序保持。"""
    raw = json.dumps(
        [
            {"account": "hengfa", "password": "pw-a"},
            {"account": "manager02", "password": "pw-b"},
        ],
        separators=(",", ":"),
    )
    monkeypatch.setenv("MANAGER_ACCOUNTS", raw)
    parsed = Settings(_env_file=None).manager_accounts
    assert [(m.account, m.password) for m in parsed] == [
        ("hengfa", "pw-a"),
        ("manager02", "pw-b"),
    ]


def test_empty_manager_accounts_env_is_unconfigured_not_crash(monkeypatch) -> None:
    """空串 / 纯空白 = 未配置（[]），绝不抛错。

    默认部署 docker-compose 传的是 `${MANAGER_ACCOUNTS:-}`（未设即空串）——
    这里钉住「空串不能把默认部署炸起来」，否则所有未配 manager 的环境都起不来。
    """
    for value in ("", "   "):
        monkeypatch.setenv("MANAGER_ACCOUNTS", value)
        fresh = Settings(_env_file=None)
        assert fresh.manager_accounts == []
        assert fresh.is_configured("manager_accounts") is False


def test_manager_accounts_env_unset_is_unconfigured_not_crash(monkeypatch) -> None:
    """**未设** MANAGER_ACCOUNTS = 未配置（默认空列表），绝不抛错。

    与空串/空白并列的合法形态：容器环境完全不注入该键时，Settings 必须取
    default_factory 的空列表（不得因缺键而 ValidationError）。
    """
    monkeypatch.delenv("MANAGER_ACCOUNTS", raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.manager_accounts == []
    assert fresh.is_configured("manager_accounts") is False


def test_manager_accounts_empty_json_array_is_unconfigured_not_crash(monkeypatch) -> None:
    """显式空数组 ``[]`` = 未配置（合法），绝不抛错。"""
    monkeypatch.setenv("MANAGER_ACCOUNTS", "[]")
    fresh = Settings(_env_file=None)
    assert fresh.manager_accounts == []
    assert fresh.is_configured("manager_accounts") is False


@pytest.mark.parametrize(
    "bad",
    [
        "{bad json",  # JSON 语法错误
        '{"account":"a","password":"p"}',  # 结构错误（不是数组）
        '[{"account":"a","pasword":"p"}]',  # 键名笔误
        '[{"account":"a"}]',  # 缺 password 键（不得静默成空口令条目）
        '[{"account":"a","password":"p","extra":1}]',  # 多余键
    ],
)
def test_malformed_manager_accounts_fail_startup(monkeypatch, bad: str) -> None:
    """恶性配置 → Settings 实例化即抛 ValidationError（应用直接起不来）。

    这是有意的取舍：起不来（响亮、被 health check 与日志立刻发现）好过静默变成
    "没有 manager 可登录"（没人能进、还看不出原因）。
    """
    monkeypatch.setenv("MANAGER_ACCOUNTS", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_manager_accounts_passwords_never_in_repr(monkeypatch) -> None:
    """口令不得出现在 Settings / 列表 / 条目的 repr 与 str 中。"""
    monkeypatch.setenv(
        "MANAGER_ACCOUNTS",
        '[{"account":"hengfa","password":"SECRET-PW-XYZ"}]',
    )
    fresh = Settings(_env_file=None)
    secret = "SECRET-PW-XYZ"
    assert secret not in repr(fresh)
    assert secret not in repr(fresh.manager_accounts)
    assert secret not in str(fresh.manager_accounts)
    assert secret not in repr(fresh.manager_accounts[0])
    assert secret not in str(fresh.manager_accounts[0])
    # 账号名仍可见（便于排障），只是口令被摘除
    assert "hengfa" in repr(fresh.manager_accounts)


# ---------------------------------------------------------------------------
# 多账号登录
# ---------------------------------------------------------------------------


def test_both_managers_can_login_as_manager(client, monkeypatch) -> None:
    """配了第二个 manager：两个账号都能登录成功，role 均为 manager。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    for acc, pw in ((MGR1, MGR1_PW), (MGR2, MGR2_PW)):
        resp = _login(client, acc, pw)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["account"] == acc
        assert body["role"] == "manager"
        assert resp.cookies.get(SESSION_COOKIE_NAME)


def test_second_manager_wrong_password_and_unknown_account(client, monkeypatch) -> None:
    """第二个 manager 口令错 / 账号不存在 → 统一 INVALID_CREDENTIALS，不泄露细节。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    bad_pw = _login(client, MGR2, "wrong-password")
    assert bad_pw.status_code == 401
    assert bad_pw.json()["error_code"] == "INVALID_CREDENTIALS"
    assert "wrong-password" not in bad_pw.text
    assert MGR2 not in bad_pw.text  # 不回显账号

    unknown = _login(client, "nobody", MGR2_PW)
    assert unknown.status_code == 401
    assert unknown.json()["error_code"] == "INVALID_CREDENTIALS"


# ---------------------------------------------------------------------------
# 会话隔离
# ---------------------------------------------------------------------------


def test_two_managers_have_distinct_account_and_session(client, monkeypatch) -> None:
    """两个 manager 登录后 account 名不同、会话 Cookie 不同、/me 各自正确。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    r1 = _login(client, MGR1, MGR1_PW)
    r2 = _login(client, MGR2, MGR2_PW)
    assert r1.json()["account"] != r2.json()["account"]
    c1 = r1.cookies.get(SESSION_COOKIE_NAME)
    c2 = r2.cookies.get(SESSION_COOKIE_NAME)
    assert c1 and c2 and c1 != c2

    me1 = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: c1})
    me2 = client.get("/api/v1/auth/me", cookies={SESSION_COOKIE_NAME: c2})
    assert me1.json()["account"] == MGR1
    assert me2.json()["account"] == MGR2
    assert me1.json()["role"] == me2.json()["role"] == "manager"


def test_thread_id_prefix_per_account(client, chat_harness, monkeypatch) -> None:
    """缺省 thread_id 由服务端按登录账号生成 → 两个 manager 前缀不同。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    app.state.model_decision_client = ScriptedDecisionClient()

    r1 = _login(client, MGR1, MGR1_PW)
    r2 = _login(client, MGR2, MGR2_PW)
    c1 = r1.cookies.get(SESSION_COOKIE_NAME)
    c2 = r2.cookies.get(SESSION_COOKIE_NAME)

    resp1 = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单"},
        cookies={SESSION_COOKIE_NAME: c1},
        headers=headers(),
    )
    assert resp1.status_code == 200, resp1.text
    t1 = resp1.json()["thread_id"]
    # 串行化：本用例是全仓唯一在同一 client 夹具（StaticPool 共享单条 SQLite
    # 连接）上并发跑两个后台图循环的用例。不等第一个收尾就发第二个 POST，两条
    # 后台图线程会同时卡在 upsert → sqlite3 cursor.execute（共享连接上的争用，
    # 实测本文件单独收集时无限挂起、>600s；全量跑因前序用例的时序而侥幸不触发）。
    # 生产是 Postgres 连接池，无此问题，故只在测试侧串行化，不改产品代码。
    _wait_run_finished(t1)

    resp2 = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单"},
        cookies={SESSION_COOKIE_NAME: c2},
        headers=headers(),
    )
    assert resp2.status_code == 200, resp2.text
    t2 = resp2.json()["thread_id"]
    _wait_run_finished(t2)
    assert t1.startswith(f"thread-{MGR1}-")
    assert t2.startswith(f"thread-{MGR2}-")
    assert t1 != t2


def test_thread_index_isolated_between_managers(client, monkeypatch) -> None:
    """会话索引按账号隔离：list_by_account 只返回本账号的会话。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    repo = SqlAlchemyThreadRepository(app.state.session_factory)
    repo.upsert(f"thread-{MGR1}-aaaaaaaa", MGR1, "一")
    repo.upsert(f"thread-{MGR2}-bbbbbbbb", MGR2, "二")

    assert [r.thread_id for r in repo.list_by_account(MGR1)] == [
        f"thread-{MGR1}-aaaaaaaa"
    ]
    assert [r.thread_id for r in repo.list_by_account(MGR2)] == [
        f"thread-{MGR2}-bbbbbbbb"
    ]

    # 经端点核对：各自登录后 GET /chat/threads 只看到自己的会话
    r1 = _login(client, MGR1, MGR1_PW)
    r2 = _login(client, MGR2, MGR2_PW)
    list1 = client.get(
        "/api/v1/chat/threads",
        cookies={SESSION_COOKIE_NAME: r1.cookies.get(SESSION_COOKIE_NAME)},
    )
    list2 = client.get(
        "/api/v1/chat/threads",
        cookies={SESSION_COOKIE_NAME: r2.cookies.get(SESSION_COOKIE_NAME)},
    )
    assert [t["thread_id"] for t in list1.json()["threads"]] == [
        f"thread-{MGR1}-aaaaaaaa"
    ]
    assert [t["thread_id"] for t in list2.json()["threads"]] == [
        f"thread-{MGR2}-bbbbbbbb"
    ]


def test_delete_thread_ownership_enforced(client, chat_harness, monkeypatch) -> None:
    """删除归属校验：第二个 manager 删不掉第一个 manager 的会话（404）。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    repo = SqlAlchemyThreadRepository(app.state.session_factory)
    repo.upsert(f"thread-{MGR1}-owned", MGR1, "归属测试")

    r2 = _login(client, MGR2, MGR2_PW)
    resp = client.delete(
        f"/api/v1/chat/threads/thread-{MGR1}-owned",
        cookies={SESSION_COOKIE_NAME: r2.cookies.get(SESSION_COOKIE_NAME)},
        headers={"Origin": VALID_ORIGIN},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# fail closed：半配、重名、空列表
# ---------------------------------------------------------------------------


def test_half_configured_entry_is_dropped(client, monkeypatch) -> None:
    """条目 account 或 password 为空 → 整条丢弃（不得变成"口令为空即可登录"）。"""
    # 半配之一：口令为空 → 该账号登不进（空口令由 LoginRequest 校验拦下 422，
    # 用任意非空口令试图登录该条也必须失败）
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, ""))
    assert _login(client, MGR2, "").status_code == 422
    assert _login(client, MGR2, MGR2_PW).status_code == 401

    # 半配之二：账号为空 → 同样不生效
    _set_managers(monkeypatch, (MGR1, MGR1_PW), ("", MGR2_PW))
    resp = _login(client, MGR2, MGR2_PW)
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "INVALID_CREDENTIALS"


def test_half_configured_entries_excluded_from_effective_credentials(
    monkeypatch,
) -> None:
    """服务层钉住 `if a and p`：空 account / 空 password 条目一律不进生效凭据。

    HTTP 层登录接口对空账号/空口令先返回 422，遮住了服务层的这条不变量；这里
    直接查生效清单 + 直调 authenticate，确保任一为空都是 AuthenticationError
    （不得变成"口令为空即可登录"）。
    """
    _set_managers(monkeypatch, (MGR1, MGR1_PW), ("", MGR2_PW), (MGR2, ""))
    effective = auth_service._configured_credentials()  # noqa: SLF001
    assert [c.account for c in effective] == [MGR1, "dev01"]
    with pytest.raises(AuthenticationError):
        authenticate("", MGR2_PW)   # 空 account 条目不得生效
    with pytest.raises(AuthenticationError):
        authenticate(MGR2, "")      # 空 password 条目不得生效


def test_duplicate_manager_account_first_entry_wins(client, monkeypatch) -> None:
    """重名（列表内两条同名）→ 首个生效，主 manager 仍能登录。

    最关键的一条：钉住"首个生效、后者丢弃"不再锁死整机——被丢弃的第二条目
    整条失效（其口令登不进），但靠前的主 manager 照常登录、角色正确。
    """
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR1, MGR2_PW))
    resp = _login(client, MGR1, MGR1_PW)
    assert resp.status_code == 200, resp.text
    assert resp.json()["account"] == MGR1
    assert resp.json()["role"] == "manager"
    assert resp.cookies.get(SESSION_COOKIE_NAME)
    # 第二个条目整条丢弃：其口令不得再能登录同一账号
    assert _login(client, MGR1, MGR2_PW).status_code == 401


def test_duplicate_manager_developer_first_entry_wins(client, monkeypatch) -> None:
    """重名跨角色（manager 列表条目与 developer 同名）→ 靠前的 manager 生效；
    developer 条目被丢弃；不整机拒登（锁机问题只在登录时才暴露，故必须经 HTTP 钉住）。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), ("dev01", "mgr2-dev-pass"))
    resp = _login(client, "dev01", "mgr2-dev-pass")
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "manager"
    # developer 条目被丢弃：原 developer 口令不再可用
    dropped = _login(client, "dev01", "dev-pass-01")
    assert dropped.status_code == 401
    assert dropped.json()["error_code"] == "INVALID_CREDENTIALS"


def test_manager_and_developer_same_account_first_wins_no_lockout(
    client, monkeypatch
) -> None:
    """`MANAGER_ACCOUNTS` 含 `DEVELOPER_ACCOUNT` 同名：manager（靠前）优先生效。

    改动前该组合下两个角色各自匹配各自口令都能登录；改为"丢弃后者"后仍必须
    可用：manager 条目生效、developer 条目被丢弃，且绝不退化为整机拒登
    （不得返回 CREDENTIALS_NOT_CONFIGURED）。
    """
    _set_managers(monkeypatch, ("dev01", "mgr-cross-pass"))
    resp = _login(client, "dev01", "mgr-cross-pass")
    assert resp.status_code == 200, resp.text
    assert resp.json()["account"] == "dev01"
    assert resp.json()["role"] == "manager"
    # 被丢弃的 developer 条目口令不可用，但仍是 INVALID_CREDENTIALS 而非整机拒登
    dropped = _login(client, "dev01", "dev-pass-01")
    assert dropped.status_code == 401
    assert dropped.json()["error_code"] == "INVALID_CREDENTIALS"


def test_empty_manager_list_fails_closed_but_developer_works(
    client, monkeypatch
) -> None:
    """空 manager 列表：无 manager 可登录；developer 独立照常（不受列表影响）。"""
    _set_managers(monkeypatch)  # 列表为空
    # developer 仍可用（其凭据来自独立键，不混入列表）
    dev = _login(client, "dev01", "dev-pass-01")
    assert dev.status_code == 200
    assert dev.json()["role"] == "developer"
    # 原 manager01 不再存在 → INVALID_CREDENTIALS（不是整机拒登，因为还有 developer）
    gone = _login(client, MGR1, MGR1_PW)
    assert gone.status_code == 401
    assert gone.json()["error_code"] == "INVALID_CREDENTIALS"


def test_all_credentials_empty_fails_closed(client, monkeypatch) -> None:
    """manager 列表空 + developer 也空 → 凭据全空，登录一律拒（CREDENTIALS_NOT_CONFIGURED）。"""
    _set_managers(monkeypatch)
    monkeypatch.setattr(settings, "developer_account", "")
    monkeypatch.setattr(settings, "developer_password", "")
    # 服务层：直接抛 CredentialsNotConfiguredError（fail closed 而非放行）
    with pytest.raises(CredentialsNotConfiguredError):
        authenticate(MGR1, MGR1_PW)
    # HTTP 层：401 CREDENTIALS_NOT_CONFIGURED，不提示配置细节
    resp = _login(client, MGR1, MGR1_PW)
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "CREDENTIALS_NOT_CONFIGURED"


def test_dropped_duplicate_reports_account_name_only_no_password(
    client, monkeypatch
) -> None:
    """重名被丢弃：对外只报账号名，异常/日志/响应均不含口令。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR1, MGR2_PW))
    dropped = dropped_duplicate_accounts()
    assert dropped == [MGR1]
    assert MGR2_PW not in repr(dropped)
    # 条目 repr 不含口令字段（Field(repr=False) 纵深防御）
    entry = ManagerAccount(account=MGR1, password=MGR2_PW)
    assert MGR2_PW not in repr(entry)
    cred = auth_service._Credential(  # noqa: SLF001
        account=MGR1, password=MGR2_PW, role=Role.MANAGER
    )
    assert MGR2_PW not in repr(cred)
    # 登录响应同样不得回显被丢弃条目的口令
    resp = _login(client, MGR1, MGR2_PW)
    assert resp.status_code == 401
    assert MGR2_PW not in resp.text


def test_lifespan_reports_dropped_duplicates_without_blocking(monkeypatch) -> None:
    """启动期以 error 暴露重名（只报账号名、不阻断启动、不含口令）。

    进入真实 lifespan（`with TestClient`），捕获 structlog：不阻断启动意味着
    health 仍可达；告警事件带被丢弃的账号名；日志文本不含口令。
    """
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR1, MGR2_PW))
    # 保真：跳过真实日志初始化，避免其 structlog.configure 覆盖 capture
    monkeypatch.setattr(main_module, "_configure_logging", lambda level: None)
    # 进 lifespan 前注入 checkpoint 桩：main._wire_checkpoint 见已注入即跳过，
    # 否则会按 CHECKPOINT_DATABASE_URL 建真实 Postgres 连接池并建表（地址不可达
    # 时阻塞/抛 CheckpointError）。写法照抄既有模式（tests/api/conftest.py:255）。
    app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
    try:
        with structlog.testing.capture_logs() as logs:
            with TestClient(app) as tc:  # 进入 lifespan
                assert tc.get("/api/v1/health").status_code == 200
        events = [
            e for e in logs if e["event"] == "duplicate_credential_accounts_dropped"
        ]
        assert events, [e["event"] for e in logs]
        assert events[0]["log_level"] == "error"
        assert events[0]["accounts"] == [MGR1]
        assert MGR2_PW not in repr(events)
    finally:
        _unset_state("checkpoint_service")


def test_incomplete_entries_reported_by_name_or_index_no_password(
    monkeypatch,
) -> None:
    """半配条目（account/password 为空）→ 明细只报账号名/索引，绝不带口令。

    account 非空但 password 为空 → 报账号名；account 为空串 → 无名字可报，
    退化为 ``index=<i>``（列表下标）。整条凭据列表中的口令不出现在返回值 repr。
    """
    _set_managers(monkeypatch, (MGR1, MGR1_PW), ("", MGR2_PW), (MGR2, ""))
    labels = dropped_incomplete_accounts()
    assert labels == ["index=1", MGR2]
    assert MGR2_PW not in repr(labels)
    assert MGR1 not in labels  # 完整条目不得被误报


def test_lifespan_reports_incomplete_entries_without_blocking(monkeypatch) -> None:
    """启动期以 error 暴露半配条目（只报账号名/索引、不阻断启动、不含口令）。

    场景：MANAGER_ACCOUNTS 非空，但每条都因 account 或 password 为空被整条丢弃
    （developer 也清空）→ 一条凭据都不生效。此时除新告警外，manager_accounts 必须
    出现在 sensitive_settings_missing、**不得**打印 sensitive_settings_loaded。

    注意覆盖面**比旧编号键更宽**：旧方案只有主 manager 对
    （MANAGER_ACCOUNT/MANAGER_PASSWORD）在 SENSITIVE_SETTINGS 内，第二 manager
    （MANAGER_ACCOUNT_2/…）半配时完全静默。半配属配置缺陷，error 级、只报账号名/
    索引、不阻断启动——漏报代价远大于误报（有意取舍）。
    """
    _set_managers(monkeypatch, (MGR1, ""), ("", MGR2_PW))
    monkeypatch.setattr(settings, "developer_account", "")
    monkeypatch.setattr(settings, "developer_password", "")
    # 保真：跳过真实日志初始化，避免其 structlog.configure 覆盖 capture
    monkeypatch.setattr(main_module, "_configure_logging", lambda level: None)
    # 进 lifespan 前注入 checkpoint 桩（同上，避免真实 Postgres 接线）。
    app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
    try:
        with structlog.testing.capture_logs() as logs:
            with TestClient(app) as tc:  # 进入 lifespan
                assert tc.get("/api/v1/health").status_code == 200
        events = [
            e for e in logs if e["event"] == "incomplete_credential_entries_dropped"
        ]
        assert events, [e["event"] for e in logs]
        assert events[0]["log_level"] == "error"
        assert events[0]["entries"] == [MGR1, "index=1"]
        # 生效凭据确为空：一条 manager/developer 都不生效（fail closed）
        assert auth_service._configured_credentials() == []  # noqa: SLF001
        # 不得出现"敏感项全部加载"的误导信号；manager_accounts 走 missing 路径
        assert not [e for e in logs if e["event"] == "sensitive_settings_loaded"]
        missing = [e for e in logs if e["event"] == "sensitive_settings_missing"]
        assert missing and "manager_accounts" in missing[0]["missing"]
        # 口令绝不外泄（含异常/日志 repr）
        assert MGR2_PW not in repr(events)
    finally:
        _unset_state("checkpoint_service")


def test_lifespan_incomplete_warning_absent_for_empty_manager_list(
    monkeypatch,
) -> None:
    """合法的全空配置（``[]``）**不**冒出半配告警，只走 sensitive_settings_missing。

    判据是「列表非空、但无一条生效」而非「生效凭据为空」：空列表本就没有条目可
    丢弃，由既有 missing 路径覆盖，不得让这条新告警变噪音。
    """
    _set_managers(monkeypatch)  # 空列表
    monkeypatch.setattr(main_module, "_configure_logging", lambda level: None)
    # 进 lifespan 前注入 checkpoint 桩（同上，避免真实 Postgres 接线）。
    app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
    try:
        with structlog.testing.capture_logs() as logs:
            with TestClient(app) as tc:
                assert tc.get("/api/v1/health").status_code == 200
        assert not [
            e for e in logs if e["event"] == "incomplete_credential_entries_dropped"
        ]
        missing = [e for e in logs if e["event"] == "sensitive_settings_missing"]
        assert missing and "manager_accounts" in missing[0]["missing"]
    finally:
        _unset_state("checkpoint_service")


def test_credential_comparisons_are_position_independent(monkeypatch) -> None:
    """常量时间不变量：命中位置不影响比较次数（= 2 × 生效凭据条目数）。

    钉住本次改动的主要技术主张——遍历全部候选、命中不提前 return。后人若把
    比较改回"命中即 return"，首条命中会远少于末条命中 → 本用例变红。
    """
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))  # 3 条生效（+dev01）
    effective = auth_service._configured_credentials()  # noqa: SLF001
    assert len(effective) == 3
    real_compare = hmac.compare_digest
    counter = {"n": 0}

    def counting_compare(a, b):  # noqa: ANN001, ANN202
        counter["n"] += 1
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", counting_compare)

    def calls_for(account: str, password: str) -> int:
        counter["n"] = 0
        try:
            authenticate(account, password)
        except AuthenticationError:
            pass
        return counter["n"]

    expected = 2 * len(effective)  # 每条候选无条件做账号 + 口令两次比较
    observed = {
        "首条": calls_for(MGR1, MGR1_PW),
        "中间条": calls_for(MGR2, MGR2_PW),
        "末条(developer)": calls_for("dev01", "dev-pass-01"),
        "全不命中": calls_for("nobody", "no-such-password"),
    }
    assert set(observed.values()) == {expected}, observed


# ---------------------------------------------------------------------------
# 零回归：默认（conftest 单 manager）行为不变
# ---------------------------------------------------------------------------


def test_default_single_manager_behavior_unchanged(client) -> None:
    """conftest 默认只配 manager01：manager/developer 照常，陌生人照拒。"""
    assert _login(client, MGR1, MGR1_PW).status_code == 200
    assert _login(client, "dev01", "dev-pass-01").json()["role"] == "developer"
    assert (
        _login(client, MGR2, MGR2_PW).json()["error_code"] == "INVALID_CREDENTIALS"
    )
    assert _login(client, MGR1, "wrong").json()["error_code"] == "INVALID_CREDENTIALS"
