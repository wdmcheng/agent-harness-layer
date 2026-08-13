"""Service smoke 的 Compose project、运行身份与 server version 策略。"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from uuid import uuid4

from service_smoke_support import compose

COMPOSE_PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
ASCII_INTEGER_PATTERN = re.compile(r"[0-9]+\Z")
DOCKER_ID_MAX = 2_147_483_647


def compose_project() -> str:
    """返回 Compose 接受的 ASCII 单段 project 名，拒绝路径和大小写漂移。"""

    override = os.environ.get("SERVICE_APP_COMPOSE_PROJECT")
    if override is None:
        return f"agent-harness-{uuid4().hex[:10]}"
    if COMPOSE_PROJECT_PATTERN.fullmatch(override) is None:
        raise RuntimeError(
            "SERVICE_APP_COMPOSE_PROJECT must start with a lowercase ASCII letter or digit "
            "and contain only lowercase ASCII letters, digits, dashes, or underscores"
        )
    return override


def runtime_uid() -> str:
    """选择 bind mount 可回读且不破坏 Docker Desktop 身份的容器 UID。"""

    override = os.environ.get("SERVICE_APP_RUNTIME_UID")
    if override is not None:
        if ASCII_INTEGER_PATTERN.fullmatch(override) is None:
            raise RuntimeError("SERVICE_APP_RUNTIME_UID must be a positive non-root integer")
        value = int(override)
        if value == 0 or value > DOCKER_ID_MAX:
            raise RuntimeError("SERVICE_APP_RUNTIME_UID must be a positive non-root integer")
        return str(value)
    host_uid = os.getuid()
    if sys.platform == "darwin" or host_uid == 0:
        return "10001"
    if host_uid > DOCKER_ID_MAX:
        raise RuntimeError("host UID exceeds the Docker numeric identity range")
    return str(host_uid)


def runtime_gid() -> str:
    """选择可读取本轮 0640 共享文件的容器 GID。"""

    override = os.environ.get("SERVICE_APP_RUNTIME_GID")
    if override is not None:
        if ASCII_INTEGER_PATTERN.fullmatch(override) is None:
            raise RuntimeError("SERVICE_APP_RUNTIME_GID must be a non-negative integer")
        value = int(override)
        if value > DOCKER_ID_MAX:
            raise RuntimeError("SERVICE_APP_RUNTIME_GID must be a non-negative integer")
        return str(value)
    host_gid = os.getgid()
    if host_gid > DOCKER_ID_MAX:
        raise RuntimeError("host GID exceeds the Docker numeric identity range")
    return str(host_gid)


def runtime_user_override_content(uid: str, gid: str) -> str:
    """渲染受控数值身份与本轮可写eval目录的Compose override。

    原生Linux把容器UID映射为宿主UID后，镜像层中属于``10001``的
    ``/app/eval-cases``不再可写。运行期draft/approved数据必须进入本轮隔离
    bind mount，不能通过放宽整个``/app``或切换doctor身份绕过权限检查。
    """

    eval_volume = (
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ${SERVICE_APP_SMOKE_DIR}/eval-cases\n"
        "        target: /app/eval-cases\n"
    )

    return (
        "services:\n"
        f'  migration:\n    user: "{uid}:{gid}"\n'
        f"{eval_volume}"
        f'  api:\n    user: "{uid}:{gid}"\n'
        f"{eval_volume}"
        f'  worker:\n    user: "{uid}:{gid}"\n'
        f"{eval_volume}"
    )


def server_versions(
    env: dict[str, str],
    *,
    compose_runner: Callable[..., str] = compose,
) -> dict[str, str]:
    """从运行中的容器读取实际 server version，避免用策略版本冒充 smoke 事实。"""

    postgres_output = compose_runner(env, "exec", "-T", "postgres", "postgres", "--version")
    redis_output = compose_runner(env, "exec", "-T", "redis", "redis-server", "--version")
    postgres_match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", postgres_output)
    redis_match = re.search(r"Redis server v=?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", redis_output)
    if postgres_match is None or redis_match is None:
        raise RuntimeError("service smoke did not report database server versions")
    return {"postgres": postgres_match.group(1), "redis": redis_match.group(1)}


__all__ = [
    "compose_project",
    "runtime_gid",
    "runtime_uid",
    "runtime_user_override_content",
    "server_versions",
]
