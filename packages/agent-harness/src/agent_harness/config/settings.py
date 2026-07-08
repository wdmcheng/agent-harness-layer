"""从 profile YAML、agent YAML、dotenv 和环境变量加载配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError
from yaml import YAMLError

from agent_harness.config.schemas import HarnessSettings
from agent_harness.contracts.errors import ApiErrorEnvelope, ErrorDetail, HarnessError

ENV_PREFIX = "AGENT_HARNESS_"


class SettingsLoadError(HarnessError):
    """配置加载失败，携带可展示给 CLI/API 的诊断。"""

    def __init__(self, errors: Sequence[ErrorDetail]) -> None:
        self.errors = list(errors)
        super().__init__(self.errors)

    def to_envelope(self) -> ApiErrorEnvelope:
        return ApiErrorEnvelope(error=self.errors[0])


def load_settings(
    *,
    profile: str = "local",
    profiles_dir: Path | None = None,
    profile_path: Path | None = None,
    agent_config_path: Path | None = None,
    env_file: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> HarnessSettings:
    """按公开优先级契约加载配置。

    合并顺序是 profile YAML -> agent YAML -> `.env` 文件 -> 进程环境变量
    -> 显式 overrides。后面的来源覆盖前面的标量值，mapping 递归合并，
    list 作为完整值替换。
    """

    resolved_profile_path = _resolve_profile_path(profile, profiles_dir, profile_path)
    data = _read_yaml_mapping(resolved_profile_path, field_prefix="")
    if "profile" not in data:
        data["profile"] = profile

    # agent YAML 只归一化进 agent 子树，避免 agent 级配置覆盖 profile 的部署边界。
    if agent_config_path is not None:
        agent_data = _read_yaml_mapping(agent_config_path, field_prefix="agent")
        data = _deep_merge(data, _normalize_agent_data(agent_data))

    # `.env` 是模板使用者的本机覆盖层，优先级高于 profile 默认值。
    resolved_env_file = _resolve_env_file(resolved_profile_path, env_file)
    env_values = _load_env_values(resolved_env_file)
    data = _deep_merge(data, _env_values_to_nested(env_values))

    # 进程环境变量覆盖 `.env`，用于 CI、容器和调用方临时注入。
    process_env = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(ENV_PREFIX) and key not in {"AGENT_HARNESS_CONFIG"}
    }
    data = _deep_merge(data, _env_values_to_nested(process_env))

    # explicit overrides 只给测试和受控调用使用，优先级最高。
    if overrides:
        data = _deep_merge(data, dict(overrides))

    try:
        return HarnessSettings.model_validate(data)
    except ValidationError as exc:
        raise SettingsLoadError(_validation_errors(exc)) from exc


def _resolve_profile_path(
    profile: str,
    profiles_dir: Path | None,
    profile_path: Path | None,
) -> Path:
    if profile_path is not None:
        return profile_path
    base = profiles_dir if profiles_dir is not None else _default_profiles_dir()
    return base / f"{profile}.yaml"


def _default_profiles_dir() -> Path:
    cwd = Path.cwd()
    repo_template_dir = cwd / "templates" / "service-app" / "configs" / "profiles"
    if repo_template_dir.exists():
        return repo_template_dir
    return cwd / "configs" / "profiles"


def _resolve_env_file(profile_path: Path, env_file: Path | None) -> Path | None:
    if env_file is not None:
        return env_file
    service_root = profile_path.parent.parent.parent
    candidate = service_root / ".env"
    return candidate if candidate.exists() else None


def _read_yaml_mapping(path: Path, *, field_prefix: str) -> dict[str, Any]:
    if not path.exists():
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.missing",
                    message=f"配置文件不存在：{path}",
                    field_path=field_prefix or str(path),
                    hint=f"创建 profile YAML 或检查路径：{path}",
                )
            ]
        )
    try:
        # 配置来自本地文件也仍是输入边界；只允许 safe YAML 数据结构进 Pydantic。
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.invalid_yaml",
                    message=f"YAML 解析失败：{exc}",
                    field_path=field_prefix or str(path),
                    hint=f"检查 YAML 语法：{path}",
                )
            ]
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SettingsLoadError(
            [
                ErrorDetail(
                    code="config.invalid",
                    message="配置文件必须是 mapping",
                    field_path=field_prefix or str(path),
                    hint=f"把 profile YAML 改成 key/value mapping：{path}",
                )
            ]
        )
    return cast(dict[str, Any], raw)


def _normalize_agent_data(agent_data: Mapping[str, Any]) -> dict[str, Any]:
    nested = agent_data.get("agent")
    if isinstance(nested, dict):
        return {"agent": cast(dict[str, Any], nested)}
    return {"agent": dict(agent_data)}


def _load_env_values(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
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
        if not raw_key.startswith(ENV_PREFIX):
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


def _assign_nested(target: dict[str, Any], parts: list[str], value: Any) -> None:
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
    details: list[ErrorDetail] = []
    for item in exc.errors():
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
