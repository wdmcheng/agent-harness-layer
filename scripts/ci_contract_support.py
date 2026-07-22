"""提供 CI 合同校验共享的严格类型转换与配置读取边界。"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml


class ContractError(RuntimeError):
    """表示 pipeline 配置扩大权限、漂移入口或缺失失败证据。"""


def mapping(value: object, label: str) -> dict[str, Any]:
    """要求输入为 mapping，并用调用方标签形成稳定诊断。"""

    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a mapping")
    return cast(dict[str, Any], value)


def sequence(value: object, label: str) -> list[Any]:
    """要求输入为 list，避免宽松转换掩盖 pipeline 结构漂移。"""

    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    return cast(list[Any], value)


def strings(value: object, label: str) -> list[str]:
    """读取字符串或 GitHub needs job 列表，不接受其他宽松形状。"""

    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    result: list[str] = []
    for item in sequence(value, label):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            itemmapping = cast(dict[str, Any], item)
            job_value = itemmapping.get("job")
            if isinstance(job_value, str):
                result.append(job_value)
                continue
            raise ContractError(f"{label} contains a non-job value")
        else:
            raise ContractError(f"{label} contains a non-job value")
    return result


def yaml_document(path: Path) -> dict[str, Any]:
    """读取必需的 pipeline YAML，并拒绝缺失或非 mapping 根节点。"""

    if not path.is_file():
        raise ContractError(f"pipeline file is missing: {path}")
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return mapping(loaded, str(path))


def toml_document(path: Path) -> dict[str, Any]:
    """读取版本化 job 合同，缺失时保持 fail-closed。"""

    if not path.is_file():
        raise ContractError(f"job contract is missing: {path}")
    with path.open("rb") as stream:
        return tomllib.load(stream)


def workflow_triggers(workflow: Mapping[str, Any]) -> set[str]:
    """读取 GitHub trigger，同时兼容 PyYAML 1.1 对 on 的布尔解释。"""

    # 仓库显式引用 on，避免 PyYAML 1.1 将它解释为布尔值后掩盖 trigger 漂移。
    raw = workflow.get("on")
    if isinstance(raw, str):
        return {raw}
    return set(mapping(raw, "GitHub on"))


def permission(job: Mapping[str, Any], workflow: Mapping[str, Any]) -> dict[str, Any]:
    """读取 job 级权限；缺省时继承 workflow 权限合同。"""

    raw = job.get("permissions", workflow.get("permissions"))
    return mapping(raw, "GitHub permissions")


def target_block(makefile: str, target: str) -> str:
    """提取一个 Make target 的 recipe，供跨平台入口合同逐项比对。"""

    match = re.search(
        rf"(?ms)^{re.escape(target)}\s*:[^\n]*\n(?P<body>(?:\t[^\n]*\n)+)",
        makefile,
    )
    if match is None:
        raise ContractError(f"Make target is missing: {target}")
    return match.group("body")


def path_values(value: object) -> set[str]:
    """规范化 artifact 路径列表，保留隐藏目录的仓库相对语义。"""

    if isinstance(value, str):
        return {line.strip().rstrip("/") for line in value.splitlines() if line.strip()}
    return {item.rstrip("/") for item in strings(value, "artifact paths")}
