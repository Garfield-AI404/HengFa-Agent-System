"""独立前端来源白名单（规格 2.6 web/cors.py、15.2 FRONTEND_ORIGIN）。

- 多来源用英文逗号分隔配置（settings.frontend_origins）。
- 会话走 Cookie，必须 allow_credentials=True 且 allow_origins 为显式白名单
  （不允许 *），否则浏览器拒绝携带 Cookie。
- 同一白名单同时用于 CSRF 的 Origin/Referer 校验（routes_auth.py）。
"""

from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from administrative_agent.config import settings


def install_cors(app: ASGIApp) -> None:
    """安装 CORS 中间件（调用方为 FastAPI app）。"""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        # Last-Event-ID：SSE 断线重连头（2.7.4 L531）必须允许跨域携带
        allow_headers=["Content-Type", "Accept", "Last-Event-ID"],
        expose_headers=[],
        max_age=600,
    )
