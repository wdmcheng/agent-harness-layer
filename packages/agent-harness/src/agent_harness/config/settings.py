"""从 profile YAML、agent YAML、dotenv 和环境变量加载配置。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError
from yaml import YAMLError

from agent_harness.config.errors import SettingsLoadError
from agent_harness.config.model_endpoints import ModelConfigurationError, validate_model_settings
from agent_harness.config.schemas import HarnessSettings
from agent_harness.config.secret_files import DEFAULT_SECRET_ROOT, load_secret_file_env
from agent_harness.contracts.errors import ErrorDetail

ENV_PREFIX = "AGENT_HARNESS_"
TEST_ENV_PREFIX = "AGENT_HARNESS_TEST_"
NON_SETTINGS_CONTROL_KEYS = frozenset(
    {
        "AGENT_HARNESS_LIVE_MODEL_AUTHORIZED",
        "AGENT_HARNESS_LIVE_MODEL_OPT_IN",
    }
)


def load_settings(
    *,
    profile: str = "local",
    profiles_dir: Path | None = None,
    profile_path: Path | None = None,
    agent_config_path: Path | None = None,
    env_file: Path | None = None,
    secret_root: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> HarnessSettings:
    """按公开优先级契约加载配置。

    合并顺序是 profile YAML -> agent YAML -> `.env` 文件 -> secret file
    -> 进程环境变量 -> 显式 overrides。后面的来源覆盖前面的标量值，
    mapping 递归合并，list 作为完整值替换。
    """

    resolved_profile_path = _resolve_profile_path(profile, profiles_dir, profile_path)
    data = _read_yaml_mapping(resolved_profile_path, field_prefix="")
    if "profile" not in data:
        data["profile"] = profile

    # agent YAML 只归一化进 agent 子树，避免 agent 级配置覆盖 profile 的部署边界。
    agent_data: dict[str, Any] = {}
    if agent_config_path is not None:
        agent_data = _read_yaml_mapping(agent_config_path, field_prefix="agent")
        data = _deep_merge(data, _normalize_agent_data(agent_data))

    # `.env` 是模板使用者的本机覆盖层，优先级高于 profile 默认值。
    resolved_env_file = _resolve_env_file(resolved_profile_path, env_file)
    env_values = _load_env_values(resolved_env_file)
    env_input_error = _env_input_error(tuple(env_values))
    if env_input_error is not None:
        for sensitive_values in (data, agent_data, env_values):
            sensitive_values.clear()
        del overrides
        raise SettingsLoadError([env_input_error])
    data = _deep_merge(data, _env_values_to_nested(env_values))

    # `_FILE` 只来自进程环境。direct/file 冲突必须在读取任何 secret 和应用
    # overrides 前失败，避免部署错误被更高优先级输入静默掩盖。
    process_env = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(ENV_PREFIX)
        and key not in {"AGENT_HARNESS_CONFIG", *NON_SETTINGS_CONTROL_KEYS}
    }
    env_input_error = _env_input_error(tuple(env_values), tuple(process_env))
    if env_input_error is not None:
        for sensitive_values in (data, agent_data, env_values, process_env):
            sensitive_values.clear()
        del overrides
        raise SettingsLoadError([env_input_error])
    secret_env: dict[str, str] = {}
    secret_load_errors: list[ErrorDetail] | None = None
    try:
        secret_env = load_secret_file_env(
            process_env,
            secret_root=secret_root or DEFAULT_SECRET_ROOT,
        )
    except SettingsLoadError as exc:
        # secret loader 的原异常 traceback 会保留调用参数；只提取不含输入值的
        # 结构化错误，并在离开 except 后重新抛出干净异常。
        secret_load_errors = list(exc.errors)
    if secret_load_errors is not None:
        for sensitive_values in (data, agent_data, env_values, process_env, secret_env):
            sensitive_values.clear()
        del overrides
        raise SettingsLoadError(secret_load_errors)
    data = _deep_merge(data, _env_values_to_nested(secret_env))

    # direct 进程环境变量覆盖 secret file，用于非冲突字段和非 secret 配置；
    # `_FILE` 本身不能进入 Pydantic schema。
    direct_env = {key: value for key, value in process_env.items() if not key.endswith("_FILE")}
    data = _deep_merge(data, _env_values_to_nested(direct_env))

    # explicit overrides 只给测试和受控调用使用，优先级最高。
    if overrides:
        data = _deep_merge(data, dict(overrides))

    model_config_error: ModelConfigurationError | None = None
    try:
        settings = HarnessSettings.model_validate(data)
        validate_model_settings(settings.model, profile=settings.profile)
    except ValidationError as exc:
        # 原始 ValidationError 会保留输入值；先复制安全字段，再离开异常处理块。
        validation_errors = _validation_errors(exc)
    except ModelConfigurationError as exc:
        model_config_error = ModelConfigurationError(exc.field_path, str(exc))
        validation_errors = [
            ErrorDetail(
                code="config.invalid",
                message="模型配置校验失败",
                field_path=exc.field_path,
                hint="检查 deployment、endpoint policy、credential 与 model catalog 的逐值关系",
            )
        ]
    else:
        return settings
    # 支持 locals capture 的错误监控也不能取得原始配置值；只清理本地副本，
    # 不修改调用方持有的 overrides mapping。
    for sensitive_values in (
        data,
        agent_data,
        env_values,
        process_env,
        secret_env,
        direct_env,
    ):
        sensitive_values.clear()
    del overrides
    del model_config_error
    # 在 except 外抛出，避免 __cause__/__context__ 和 traceback 绕过脱敏。
    raise SettingsLoadError(validation_errors)


def _resolve_profile_path(
    profile: str,
    profiles_dir: Path | None,
    profile_path: Path | None,
) -> Path:
    """优先使用显式 profile 文件；否则在给定或默认目录下解析 profile 名称。"""

    if profile_path is not None:
        return profile_path
    base = profiles_dir if profiles_dir is not None else _default_profiles_dir()
    return base / f"{profile}.yaml"


def _default_profiles_dir() -> Path:
    """在仓库开发环境优先定位模板配置，安装后回退到当前目录下的标准路径。"""

    cwd = Path.cwd()
    repo_template_dir = cwd / "templates" / "service-app" / "configs" / "profiles"
    if repo_template_dir.exists():
        return repo_template_dir
    return cwd / "configs" / "profiles"


def _resolve_env_file(profile_path: Path, env_file: Path | None) -> Path | None:
    """优先使用显式 `.env`，否则只在 profile 所属服务根目录查找可选覆盖文件。"""

    if env_file is not None:
        return env_file
    service_root = profile_path.parent.parent.parent
    candidate = service_root / ".env"
    return candidate if candidate.exists() else None


def _read_yaml_mapping(path: Path, *, field_prefix: str) -> dict[str, Any]:
    """读取 YAML mapping，并将缺失、解码、语法和顶层类型错误映射为安全配置错误。"""

    safe_field = field_prefix or "profile"
    source_name = "agent YAML" if field_prefix == "agent" else "profile YAML"
    if not path.exists():
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.missing",
                    message="配置文件不存在",
                    field_path=safe_field,
                    hint=f"创建或检查 {source_name}",
                )
            ]
        )
    try:
        # 配置来自本地文件也仍是输入边界；只允许 safe YAML 数据结构进 Pydantic。
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, YAMLError):
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.invalid_yaml",
                    message="YAML 解析失败",
                    field_path=safe_field,
                    hint=f"检查 {source_name} 的 UTF-8 编码和 YAML 语法",
                )
            ]
        ) from None
    except OSError:
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.invalid",
                    message="配置文件不可读",
                    field_path=safe_field,
                    hint=f"检查 {source_name} 的读取权限",
                )
            ]
        ) from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.invalid",
                    message="配置文件必须是 mapping",
                    field_path=safe_field,
                    hint=f"把 {source_name} 改成 key/value mapping",
                )
            ]
        )
    return cast(dict[str, Any], raw)


def _normalize_agent_data(agent_data: Mapping[str, Any]) -> dict[str, Any]:
    """兼容 agent 文件带或不带顶层 ``agent`` 键的两种格式，但只写入 agent 子树。"""

    nested = agent_data.get("agent")
    if isinstance(nested, dict):
        return {"agent": cast(dict[str, Any], nested)}
    return {"agent": dict(agent_data)}


def _load_env_values(path: Path | None) -> dict[str, str]:
    """解析受限的本地 `.env` 格式，不执行 shell 展开、命令替换或复杂引用。"""

    if path is None or not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.invalid_env",
                    message=".env 配置不可读或编码无效",
                    field_path=".env",
                    hint="检查 .env 的读取权限和 UTF-8 编码",
                )
            ]
        ) from None
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # 这里不是 shell parser：只支持 key=value 和一层引号，避免 `.env` 产生隐式执行语义。
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_values_to_nested(values: Mapping[str, str]) -> dict[str, Any]:
    """把 AGENT_HARNESS_* 键转换成嵌套 settings 字段。"""

    nested: dict[str, Any] = {}
    for raw_key, raw_value in values.items():
        # 合同测试的外部服务 DSN 与产品配置共享品牌前缀，但不是 HarnessSettings。
        # 在统一转换 seam 排除它，避免 `.env` 与进程环境两条路径行为分叉。
        if (
            not raw_key.startswith(ENV_PREFIX)
            or raw_key.startswith(TEST_ENV_PREFIX)
            or raw_key in NON_SETTINGS_CONTROL_KEYS
        ):
            continue
        # `_FILE` 只允许来自进程环境并由受控 secret loader 消费；`.env`
        # 中的同名项既不能触发文件读取，也不能作为未知 schema 字段进入 Pydantic。
        if raw_key.endswith("_FILE"):
            continue
        key = raw_key.removeprefix(ENV_PREFIX)
        if key in {"CONFIG"}:
            continue
        if key == "PROFILE":
            nested["profile"] = raw_value
            continue
        parts = [part.lower() for part in key.split("__") if part]
        if not parts:
            continue
        _assign_nested(nested, parts, _parse_env_value(raw_value))
    return nested


def _env_input_error(*key_sets: tuple[str, ...]) -> ErrorDetail | None:
    """在 merge/secret read 前拒绝非法路径、大小写别名和 direct/FILE 冲突。

    调用方只传键名快照，因此本 helper 与异常 traceback 都不会持有环境值；真正抛错前
    由 ``load_settings`` 清空包含 secret 的 mapping。
    """

    seen: dict[tuple[str, ...], dict[str, str]] = {}
    for raw_key in (key for keys in key_sets for key in keys):
        if (
            not raw_key.startswith(ENV_PREFIX)
            or raw_key.startswith(TEST_ENV_PREFIX)
            or raw_key in {"AGENT_HARNESS_CONFIG", *NON_SETTINGS_CONTROL_KEYS}
        ):
            continue
        file_input = raw_key.upper().endswith("_FILE")
        logical_key = raw_key[:-5] if file_input else raw_key
        suffix = logical_key.removeprefix(ENV_PREFIX)
        parts = suffix.split("__")
        if not parts or any(
            not part or re.fullmatch(r"[A-Za-z0-9_]+", part) is None for part in parts
        ):
            return ErrorDetail(
                code="config.invalid",
                message="环境变量配置路径包含空分段或非法字符",
                field_path=".env",
                hint="使用非空的字母、数字或下划线分段，并用双下划线分隔层级",
            )
        canonical = tuple(part.lower() for part in parts)
        kind = "file" if file_input else "direct"
        raw_identity = logical_key
        previous = seen.setdefault(canonical, {}).get(kind)
        if previous is not None and previous != raw_identity:
            return ErrorDetail(
                code="config.invalid",
                message="多个环境变量别名映射到同一配置路径",
                field_path=f".env.{'.'.join(canonical)}",
                hint="每个 canonical 配置路径只保留一种大小写拼写",
            )
        seen[canonical][kind] = raw_identity
        if {"direct", "file"}.issubset(seen[canonical]):
            return ErrorDetail(
                code="config.secret_file_conflict",
                message="direct env 与对应的 _FILE 不能同时设置",
                # direct/_FILE 冲突沿用 CFG-001 已公开的 canonical settings path；
                # `.env` 只是输入来源，不得成为字段身份的一部分。
                field_path=".".join(canonical),
                hint="只设置 direct env 或对应的 _FILE，移除另一个输入",
            )
    return None


def _assign_nested(target: dict[str, Any], parts: list[str], value: Any) -> None:
    """按双下划线路径写入嵌套配置；中间标量冲突由后来源替换为 mapping。"""

    current = target
    for part in parts[:-1]:
        next_value = current.setdefault(part, {})
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = cast(dict[str, Any], next_value)
    current[parts[-1]] = value


def _parse_env_value(value: str) -> Any:
    """只把明确的 bool/int 字面量转换类型，其余保留字符串。"""

    if value == "":
        return None
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并 mapping，冲突时 overlay 获胜。"""

    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            existing_mapping = cast(Mapping[str, Any], existing)
            value_mapping = cast(Mapping[str, Any], value)
            result[key] = _deep_merge(existing_mapping, value_mapping)
        else:
            result[key] = value
    return result


def _validation_errors(exc: ValidationError) -> list[ErrorDetail]:
    """将 Pydantic 校验错误转换为不含原始输入值的结构化配置诊断。"""

    details: list[ErrorDetail] = []
    for item in exc.errors():
        # 仅保留字段位置和通用消息，避免 Pydantic 的 input/context 重新暴露密钥。
        loc = item.get("loc", ())
        path = ".".join(str(part) for part in loc) if loc else None
        details.append(
            ErrorDetail(
                code="config.invalid",
                message=str(item.get("msg", "配置校验失败")),
                field_path=path,
                hint=f"在 profile YAML 或 AGENT_HARNESS_* env 中设置 {path or '相关字段'}",
            )
        )
    return details
