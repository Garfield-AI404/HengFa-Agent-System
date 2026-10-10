"""pytest 全局配置（必须先于任何 administrative_agent 导入执行）。

在进程启动早期设置测试凭据，保证 config.Settings 单例实例化时读到
确定性测试值；覆盖 .env 中的真实值，防止测试触碰生产凭据。
"""

from __future__ import annotations

import json
import os

os.environ["SESSION_SECRET"] = "test-session-secret-not-for-production"
# manager 账号唯一来源是 MANAGER_ACCOUNTS（JSON 列表，单行无空格）。此处**钉住**
# 测试凭据（覆盖本地 .env 的真实值，防止真实 manager 串进测试进程污染全套件）；
# 默认只含一个 manager01——需要额外 manager 的用例各自 monkeypatch
# settings.manager_accounts。旧编号键 MANAGER_ACCOUNT(_2) 已作废，不再设置。
os.environ["MANAGER_ACCOUNTS"] = json.dumps(
    [{"account": "manager01", "password": "mgr-pass-01"}],
    separators=(",", ":"),  # 与 .env 生产形状一致：单行无空格
)
os.environ["DEVELOPER_ACCOUNT"] = "dev01"
os.environ["DEVELOPER_PASSWORD"] = "dev-pass-01"
os.environ["SESSION_TTL_MINUTES"] = "480"
os.environ["COOKIE_SECURE"] = "false"
os.environ["FRONTEND_ORIGIN"] = "http://127.0.0.1:5173"

# ---------------------------------------------------------------------------
# 2026-08-29：隔离「被测架构层已删除」的测试文件（当前 0 个文件 / 0 个用例）
#
# D26 重构删除了整套规则化流水线，这些文件因导入失败会让 pytest 直接中断收集，
# 导致其余可用用例也跑不起来。此处仅**跳过收集**，文件原地保留，作为后续重写
# 时的参考（不要直接删除）。
#
# ## 2026-08-29 最终收口：20 个隔离文件全部处理完毕，collect_ignore 已清空。
#
# 已删除的模块与其替代方案：
#   agent/intent.py            规则化意图分类器 → 改由 LLM 直接决策（nodes/tools_node.py）
#   agent/policy_resolver.py   策略解析器        → 无等价实现
#   agent/prompts/renderer.py  提示词渲染器      → 无等价实现
#   agent/middleware/          审计钩子中间件    → 无等价实现
#   agent/state.py 的 TaskState / init_state / structured_intent_payload
#                                               → 改为 ConversationState（TypedDict）
#   web/chat_streams.py 的 EVENT_TASK_CREATED / EVENT_MODEL_STUB → 事件模型变更
#
# 重写时按新架构重写，不要试图恢复旧模块。
#
# ## 2026-08-29 收口进展（原 20 文件 / 183 用例）
#
# - 已重写并解除隔离（2 文件，覆盖 43 例）：
#     contract/test_reconciliation.py / test_reconciliation_execute.py
#     → 被测对象从已删的操作级 scope 映射改为仍存活的 mcp/scopes.py 本地
#       写保护层（此前整个 scopes.py 零覆盖）。
# - 已救回并解除隔离（3 文件，27 例）：
#     unit/test_prompts.py → 去掉已删的 renderer 段，保留 base + policies，
#       断言按 D26 现状更新（工具改名、钣金计价单位 套→kg/m²）。
#     security/test_no_hardcoded_credentials.py（4 例）→ 源码级凭据扫描
#       + State 不含身份键；是唯一自动守住「src/ 不硬编码凭据」的手段
#     security/test_excel_injection.py（6 例）→ 单元格注入只当作数据值。
#       tests/domains/test_excel_parser.py 只覆盖转换语义，与注入零交集
# - 已删除（12 文件 / 111 用例）：
#   - 10 个依赖已删的 agent/intent.py，被测的规则化分类器无等价实现、
#     无重写对象：intent/ 9 个 + unit/test_policy_resolver.py
#   - 2 个安全类旧文件，内容已被完整接替：
#     security/test_injection.py    → tests/api/test_injection_api.py
#                                   + 上列 test_excel_injection.py
#     security/test_secrets_leak.py → tests/api/test_secrets_leak_api.py
#                                   + 上列 test_no_hardcoded_credentials.py
#
# ## 最后一个文件 `audit/test_hooks.py` 的处置（2026-08-29 项目负责人批准删除）
#
# 6 个用例全部依赖 D26 已删的 `agent/middleware/audit_hooks.py`：
#   - 4 例测 `mark_manual_review_required` / `ERROR_CODE_AUDIT_WRITE_FAILED`
#     常量 —— 该字符串在整个 src/ 里从未出现过，能力在生产侧本就不存在；
#   - 1 例测 `AuditFailureError` 向上传播 —— 已由 tests/audit/test_service.py
#     的 19 例完整覆盖（record_event/attempt/result_failure_raises_*）；
#   - 1 例测 `result_hook` 组合封装 —— 随中间件一并消失。
# 结论：无重写对象，删除不损失覆盖。若日后要补「审计落库失败可聚合」（11.5
# 指标，见 integration/test_metrics_observability.py:298 的显式 skip），
# 需先在生产侧新增 AUDIT_WRITE_FAILED 标记能力，属新功能。
#
# ## 已知环境坑（2026-08-29 事故）
#
# `tests/` 曾于 20:09 被整体移入 Windows 回收站（非本仓库命令所为，疑为外部
# 进程/中断导致）。回收站里不含 12 个已批准删除的文件，其余 74 个文件均按
# 原路径还原成功。教训：未提交的大面积重写工作应尽早 `git commit` 落盘。
# ---------------------------------------------------------------------------
collect_ignore: list[str] = []


def mcp_max_retries() -> int:
    """MCP 客户端的瞬时故障重试上限（config.erp_mcp_max_retries）。

    测试一律从配置推导而非硬编码：该值已于 2026-08-27 由项目负责人批准
    上调至 3（服务重启窗口内自动重试），任何硬编码的调用次数/attempt 数
    断言都会随配置调整而碎。跨目录共享（tests/domains、tests/integration
    均依赖），修改默认值时只需改 config.py 一处。
    """
    from administrative_agent.config import Settings

    return max(0, int(Settings().erp_mcp_max_retries))
