FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 复制项目必须显式提供已构建核心 wheel；镜像不访问仓库源码或同名公共包。
COPY .agent-harness/agent_harness-*.whl /tmp/

# 核心依赖层只受 wheel 影响，普通 app/scripts 改动不应触发完整依赖树重装。
RUN python -m pip install --no-cache-dir /tmp/agent_harness-*.whl \
    fastapi==0.139.0 uvicorn==0.50.2 typer==0.26.8 \
    && rm /tmp/agent_harness-*.whl

COPY pyproject.toml README.md /app/
COPY app /app/app
COPY agents /app/agents
COPY configs /app/configs
COPY eval-cases /app/eval-cases
COPY scripts /app/scripts

RUN python -m pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 harness \
    && mkdir -p /smoke/workspace /smoke/artifacts \
    && chown -R harness:harness /app /smoke

USER harness

CMD ["agent-harness-service", "serve", "--profile", "service", "--profiles-dir", "/app/configs/profiles", "--host", "0.0.0.0", "--port", "8000"]
