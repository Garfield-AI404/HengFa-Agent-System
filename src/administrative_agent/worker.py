"""LangGraph worker 进程（规格 15.3 步骤 5 部署组件；D25/RV-38）。

规格 15.3：生产启动 FastAPI、LangGraph worker、前端静态服务和反向代理。
本模块是 worker 的进程入口——图执行运行时的部署宿主，负责：

- **启动检查（fail closed）**：AGENT_DATABASE_URL / CHECKPOINT_DATABASE_URL
  必须已注入（15.2/12.4）；未配置即退出，不静默降级；
- **运行时就绪验证**：编译 LangGraph 图（agent/graph.build_graph）并完成
  checkpoint 表就绪（CheckpointService.setup()，幂等——D7 同口径：
  checkpoint 表由对应依赖管理，不手写 DDL）。部署后即验证图可构建、
  checkpoint 基础设施可写；
- **健康端点**：进程内最小 HTTP 服务（``/healthz``，返回
  status/app/version），仅供编排 healthcheck 使用——本进程不承载
  FastAPI 业务路由（业务 API 属 api 服务）；
- **优雅停机（15.3 步骤 7）**：SIGTERM/SIGINT → ``begin_shutdown()`` →
  ``drain_writes(SHUTDOWN_DRAIN_TIMEOUT)`` 等待进行中写调用落审计或超时
  → 关闭健康服务与 checkpoint 连接池 → 退出 0。

执行循环说明（边界）：一期执行器经 chat 编排接线属后续单元（跟踪项
"真实模型调用链 D11+"；chat 流水线当前为同步 stub，见 web/routes_chat.py
模块注释）。worker 当前职责为运行时宿主就绪验证与运维生命周期；任务
分派循环在真实模型/执行器接线后落地（本模块 ``_run_worker`` 是唯一的
接入点，届时在心跳循环中挂接分派）。

用法（Docker 编排 / 手动）：
    uv run python -m administrative_agent.worker
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import structlog

from administrative_agent import lifecycle
from administrative_agent.agent.checkpoint import (
    CheckpointService,
    CheckpointUnconfiguredError,
    checkpointer_factory,
)
from administrative_agent.agent.graph import build_graph
from administrative_agent.config import Settings, get_settings
from administrative_agent.domains.attachments import (
    AttachmentStore,
    cleanup_expired_files,
)
from administrative_agent.domains.reconcile_reports import ReconcileReportStore
from administrative_agent.main import APP_NAME, APP_VERSION, _configure_logging
from administrative_agent.persistence.database import SessionLocal
from administrative_agent.persistence.repositories import SqlAlchemyTaskRepository

#: 健康端点监听地址与端口（编排内部网络专用，不对外发布）
WORKER_HEALTH_HOST = "0.0.0.0"
WORKER_HEALTH_PORT = 18002

#: 心跳间隔（秒）：日志心跳证明进程存活（不含业务数据，11.4）
_HEARTBEAT_SECONDS = 30.0

#: 停机循环轮询间隔（秒）
_STOP_POLL_SECONDS = 1.0


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("agent.worker")


class WorkerHealthHandler(BaseHTTPRequestHandler):
    """最小健康端点（/healthz）。非健康路径 404；不打印访问日志。"""

    def do_GET(self) -> None:  # noqa: N802（http.server 命名）
        if self.path != "/healthz":
            self.send_error(404)
            return
        body = json.dumps(
            {"status": "ok", "app": APP_NAME, "version": APP_VERSION},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        # 健康探活不打访问日志（避免刷屏；真实日志走 structlog）
        pass


def make_health_server(host: str = WORKER_HEALTH_HOST, port: int = 0) -> ThreadingHTTPServer:
    """构造健康服务（port=0 由内核分配，供测试/演练使用）。"""
    return ThreadingHTTPServer((host, port), WorkerHealthHandler)


def _startup_checks(settings: Settings) -> None:
    """运行时就绪验证（fail closed）：checkpoint 基础设施 + 图编译。

    返回已就绪的 saver（供停机时关闭连接池）。部署后即验证：
    - checkpoint 连接池可建立（连接串已注入，12.4）；
    - checkpoint 表就绪（依赖 setup，幂等，D7 同口径）；
    - LangGraph 图可构建（图定义一致性，2.6 agent/graph.py）。
    """
    saver = checkpointer_factory(settings)
    service = CheckpointService(saver, SqlAlchemyTaskRepository(SessionLocal))
    service.setup()
    build_graph(checkpointer=saver)
    return saver


def _cleanup_expired_records(settings: Settings) -> None:
    """启动时清理过期附件与对账报告（TTL 机制；best effort）。

    与 main.py lifespan 同口径：附件元数据行 + storage_path 磁盘文件
    一并删（cleanup_expired_files 域内自兜底），对账报告只删元数据行；
    任一失败只 warning 不阻断 worker 启动（15.3 启动检查的 fail closed
    属另一层，本清理是运维动作）。
    """
    log = _logger()
    cleanup_expired_files(AttachmentStore(SessionLocal), settings.attachment_dir)
    try:
        ReconcileReportStore(SessionLocal).delete_expired()
    except Exception:
        log.warning("report_delete_expired_failed", exc_info=True)


def _run_worker(settings: Settings) -> int:
    log = _logger()
    try:
        saver = _startup_checks(settings)
    except CheckpointUnconfiguredError as exc:
        log.error("worker_fail_closed", error=str(exc))
        return 1
    except Exception as exc:  # 其余启动失败（连接失败/图编译失败）：退出
        log.error("worker_startup_failed", error=type(exc).__name__, detail=str(exc))
        return 1

    # [2026-08-31 对话对账] 启动清一次过期附件/对账报告（TTL 兜底；
    # best effort——与 main.py lifespan 同款，失败不阻断）
    _cleanup_expired_records(settings)

    log.info("worker_ready", app=APP_NAME, version=APP_VERSION)

    httpd = make_health_server(WORKER_HEALTH_HOST, WORKER_HEALTH_PORT)
    health_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    health_thread.start()
    log.info("worker_health_listening", host=WORKER_HEALTH_HOST, port=WORKER_HEALTH_PORT)

    stop = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("worker_signal_received", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while not stop.wait(_STOP_POLL_SECONDS):
        pass
    log.info("worker_shutdown_begin")

    # 15.3 步骤 7：先停止接收新写任务 → 等待当前调用落审计或超时 → 停机
    lifecycle.begin_shutdown()
    drained = lifecycle.drain_writes(settings.shutdown_drain_timeout)
    httpd.shutdown()
    try:
        # PostgresSaver.conn 为 psycopg_pool.ConnectionPool（依赖公开属性）
        saver.conn.close()
    except Exception:
        log.warning("worker_pool_close_failed")
    log.info(
        "worker_stopping",
        drained=drained,
        inflight=lifecycle.inflight_count(),
        drain_timeout_seconds=settings.shutdown_drain_timeout,
    )
    return 0


def main() -> int:
    settings = get_settings()
    _configure_logging(settings.log_level)  # structlog JSON（main.py 同配置）
    return _run_worker(settings)


if __name__ == "__main__":
    sys.exit(main())
