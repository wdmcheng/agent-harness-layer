"""受信 secret root 内的 `_FILE` 收集与安全读取边界。"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from agent_harness.config.errors import SettingsLoadError
from agent_harness.contracts.errors import ErrorDetail

ENV_PREFIX = "AGENT_HARNESS_"
TEST_ENV_PREFIX = "AGENT_HARNESS_TEST_"
DEFAULT_SECRET_ROOT = Path("/run/secrets")
MAX_SECRET_FILE_BYTES = 64 * 1024


def load_secret_file_env(
    process_env: Mapping[str, str],
    *,
    secret_root: Path,
) -> dict[str, str]:
    """把进程 `_FILE` 输入安全解析为对应 direct env 值。"""

    file_inputs = {
        key: value
        for key, value in process_env.items()
        if key.endswith("_FILE")
        and key.startswith(ENV_PREFIX)
        and not key.startswith(TEST_ENV_PREFIX)
        and key.removesuffix("_FILE") != "AGENT_HARNESS_CONFIG"
    }
    conflict_error: SettingsLoadError | None = None
    for file_key in file_inputs:
        direct_key = file_key.removesuffix("_FILE")
        if direct_key in process_env:
            conflict_error = SettingsLoadError(
                [
                    ErrorDetail(
                        code="config.secret_file_conflict",
                        message="direct env 与对应的 _FILE 不能同时设置",
                        field_path=_env_field_path(direct_key),
                        hint="只设置 direct env 或对应的 _FILE，移除另一个输入",
                    )
                ]
            )
            break
    if conflict_error is not None:
        # helper 也可能被独立调用；抛错前释放持有 direct secret 的 mapping，
        # 避免 traceback locals capture 绕过上层结构化错误脱敏。
        file_inputs.clear()
        del process_env
        raise conflict_error

    # 后续读取失败时，traceback 会保留本 frame；先释放包含 direct secret 的
    # 原始环境 mapping，再确保已成功读取的前序值在抛错前原地清空。
    del process_env
    resolved: dict[str, str] = {}
    read_error: SettingsLoadError | None = None
    for file_key, raw_path in file_inputs.items():
        direct_key = file_key.removesuffix("_FILE")
        field_path = _env_field_path(direct_key)
        try:
            resolved[direct_key] = _read_secret_file(
                raw_path,
                secret_root=secret_root,
                field_path=field_path,
            )
        except SettingsLoadError as exc:
            read_error = SettingsLoadError(list(exc.errors))
            resolved.clear()
            file_inputs.clear()
            break
    if read_error is not None:
        raise read_error
    return resolved


def _env_field_path(env_key: str) -> str:
    """把双下划线分层的环境变量名映射为设置错误的点分字段路径。"""

    key = env_key.removeprefix(ENV_PREFIX)
    return ".".join(part.lower() for part in key.split("__") if part)


def _read_secret_file(
    raw_path: str,
    *,
    secret_root: Path,
    field_path: str,
) -> str:
    """以防符号链接与 TOCTOU 的方式读取受信根目录内的单个 UTF-8 secret 文件。

    路径解析、无跟随打开、打开前后 inode/内容状态比较共同阻断替换攻击；读取失败
    只返回结构化配置错误，绝不在异常中包含路径内容或 secret 字节。
    """

    candidate = Path(raw_path)
    try:
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError
        root = secret_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError
        candidate_resolved = candidate.resolve(strict=True)
        if not candidate_resolved.is_relative_to(root):
            raise ValueError

        path_stat = candidate.stat(follow_symlinks=False)
        if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
            raise ValueError
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ValueError
        flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(candidate, flags)
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                opened_before = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened_before.st_mode) or not _same_file(
                    path_stat, opened_before
                ):
                    raise ValueError
                payload = handle.read(MAX_SECRET_FILE_BYTES + 1)
                opened_after = os.fstat(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        final_stat = candidate.stat(follow_symlinks=False)
        if (
            len(payload) > MAX_SECRET_FILE_BYTES
            or not _same_file(opened_before, opened_after, include_content_state=True)
            or not _same_file(opened_after, final_stat, include_content_state=True)
        ):
            raise ValueError
        value = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        raise _secret_file_error(field_path) from None

    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if value == "":
        raise _secret_file_error(field_path)
    return value


def _same_file(
    left: os.stat_result,
    right: os.stat_result,
    *,
    include_content_state: bool = False,
) -> bool:
    """比较 inode 身份；需要时再比较大小与纳秒时间戳以发现读取过程中的替换。"""

    if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
        return False
    if not include_content_state:
        return True
    return (left.st_size, left.st_mtime_ns) == (right.st_size, right.st_mtime_ns)


def _secret_file_error(field_path: str) -> SettingsLoadError:
    """构造不泄露原始路径和内容的 secret 文件配置错误。"""

    return SettingsLoadError(
        [
            ErrorDetail(
                code="config.secret_file_invalid",
                message="secret file 配置无效",
                field_path=field_path,
                hint=("使用受信 root 内绝对、可读、非空且不超过 64 KiB 的普通 UTF-8 文件"),
            )
        ]
    )


__all__ = ["DEFAULT_SECRET_ROOT", "load_secret_file_env"]
