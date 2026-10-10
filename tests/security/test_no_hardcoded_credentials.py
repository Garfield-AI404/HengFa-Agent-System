"""密钥泄漏：源码级扫描 + State 不含身份/凭据键。

## 2026-08-29 说明

本文件从 `tests/security/test_secrets_leak.py` 救回。原文件因导入已删
模块（`agent.intent`、`agent.state` 的 `TaskState`/`init_state`/
`structured_intent_payload`）整体无法收集，但其中 **源码级凭据扫描**
一段完全不依赖那些模块，且是唯一能自动守住「src/ 不硬编码凭据」的
手段——D26 后该能力一度零覆盖，本文件把它恢复。

原有的 `TestStateHasNoCredentialOrIdentityKeys` 已重定向：
被测对象从 `TaskState` 改为 D26 的 `ConversationState`，不变量本身
（身份与权限不进入 State，模型无法经 State 读写或伪造身份，8.4 末段）
未变且被 `agent/state.py` 的注释明确重申。

运行时（API 响应体）脱敏断言见 `tests/api/test_secrets_leak_api.py`，
与本文件互补：那边守输出，这边守源码与 State 结构。
"""

from __future__ import annotations

from pathlib import Path

from administrative_agent.agent.state import ConversationState
from administrative_agent.observability.log_capture import _CREDENTIAL_VALUE_RE

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "administrative_agent"

#: 身份/权限/凭据相关键（State 中一律不得出现）
_FORBIDDEN_STATE_KEYS = {
    "user_id", "user_role", "scope", "account", "password", "api_key",
    "token", "secret", "credential", "session",
}


class TestNoHardcodedCredentialsInSrc:
    """14.4 L1415：src/ 全仓无硬编码凭据形态（连接串只进 .env）。"""

    def test_src_has_no_credential_shaped_literals(self) -> None:
        """逐个 .py 文件扫描，命中凭据形态字面量即失败。

        与脱敏实现共用 `_CREDENTIAL_VALUE_RE` 单一事实来源：
        新增一种凭据形态时，脱敏与扫描同时生效，不会漏扫。
        """
        hits: list[str] = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if _CREDENTIAL_VALUE_RE.search(line):
                    hits.append(f"{path.relative_to(SRC_ROOT)}:{lineno}")
        assert not hits, f"src/ 出现凭据形态字面量：{hits}"

    def test_sensitive_env_keys_only_exported_as_names(self) -> None:
        """config.py 只列键名（SENSITIVE_SETTINGS），绝不含键值。"""
        text = (SRC_ROOT / "config.py").read_text(encoding="utf-8")
        assert "SENSITIVE_SETTINGS" in text
        for pattern in ("sk-", "AKIA", "-----BEGIN", "ghp_"):
            assert pattern not in text, f"config.py 出现凭据形态：{pattern}"


class TestStateHasNoCredentialOrIdentityKeys:
    """8.4 末段：State 不含身份/权限/凭据键。

    D26 前被测对象是 `TaskState`（`init_state` 构造）；现在是被
    `ConversationState`（TypedDict）取代。不变量未变：身份与权限只经
    请求级 `AgentContext` 注入，不进入 LangGraph State，模型无法经
    State 读取或伪造身份。
    """

    def test_conversation_state_has_no_forbidden_keys(self) -> None:
        keys = set(ConversationState.__annotations__)
        assert not (_FORBIDDEN_STATE_KEYS & keys), (
            f"ConversationState 出现身份/凭据键：{_FORBIDDEN_STATE_KEYS & keys}"
        )

    def test_agent_context_is_the_only_identity_carrier(self) -> None:
        """正向对照：身份三件套只出现在 AgentContext 上（不是 State 的一部分）。

        防止有人为了"图里方便"把 user_id/scope 塞进 State——那样模型
        就能读到乃至改写自己的权限。
        """
        from administrative_agent.agent.state import AgentContext

        context_keys = set(AgentContext.__annotations__)
        assert {"user_id", "user_role", "scope"} <= context_keys
        # State 与 Context 不相交（身份不随 State 落 checkpoint）
        assert not (context_keys & set(ConversationState.__annotations__))
