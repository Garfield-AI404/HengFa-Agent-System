# 恒发内勤 Agent 一期后端镜像（D25/RV-38）。
#
# 单一镜像三种命令（15.3 启动步骤）：
#   migrate  alembic upgrade head（一次性任务，15.3 步骤 2）
#   api      uvicorn 运行 FastAPI（18000，/api/v1 与 SSE 业务端点）
#   worker   python -m administrative_agent.worker（LangGraph worker，18002 健康端点）
#
# 凭据纪律（12.4/15.2）：镜像内不包含任何密钥——全部经运行时环境变量注入
# （docker-compose.yml 的 environment 段），构建上下文亦不含 .env（.gitignore）。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先复制依赖声明（利用层缓存；L62：不升级任何依赖，uv.lock 不动）
COPY pyproject.toml README.md alembic.ini ./
# 包源码 + Alembic 迁移链 + 运维脚本（迁移/演示/演练）
COPY src ./src
COPY alembic ./alembic
COPY scripts ./scripts

# 安装项目（hatchling 构建；运行时依赖来自 pyproject [project].dependencies）
RUN pip install --no-cache-dir .

EXPOSE 18000 18002

# 默认命令 = api（编排内由 compose command 覆盖为 migrate/worker）
CMD ["uvicorn", "administrative_agent.main:app", "--host", "0.0.0.0", "--port", "18000"]
