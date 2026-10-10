"""D25/RV-38 LangGraph worker 健康端点测试（worker.py；规格 15.3 步骤 5）。

worker 是部署组件（FastAPI / LangGraph worker / 前端静态服务 / 反向代理
四进程之一）：进程内最小 HTTP 健康服务 /healthz 供编排 healthcheck，
不承载业务路由。
"""

from __future__ import annotations

import json
import threading
import urllib.request

from administrative_agent.worker import make_health_server


def _get(url: str) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310（本机测试服务）
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # noqa: B902（urllib 专用）
        return exc.code, None


def test_healthz_ok():
    server = make_health_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(f"http://127.0.0.1:{port}/healthz")
        assert status == 200
        assert body["status"] == "ok"
        assert body["app"] == "hengfa-administrative-agent"
        assert isinstance(body["version"], str) and body["version"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_healthz_unknown_path_404():
    server = make_health_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = _get(f"http://127.0.0.1:{port}/other")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
