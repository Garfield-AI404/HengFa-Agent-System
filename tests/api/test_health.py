"""D2 运维探活端点测试（规格 15.3 / 16.6 RV-03）。

/api/v1/health 不鉴权、无业务数据；与 D23 developer/health 无关。
"""

from fastapi.testclient import TestClient

from administrative_agent.main import app


def test_health_returns_200_and_identity() -> None:
    # with 块触发 lifespan（配置加载与 structlog 初始化不得抛错）
    with TestClient(app) as client:
        resp = client.get("/api/v1/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["app"] == "hengfa-administrative-agent"
    assert isinstance(data["version"], str) and data["version"]
