"""从 uv lock 与 metadata 观察构建可发布 runtime 依赖清单。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any, cast

from license_check_support import (
    PUBLISHED_RUNTIME_ROOT_SOURCES,
    LicenseError,
    PackageIdentity,
    normalize_metadata_license,
    relative_path,
)


def source_value(item: dict[str, Any]) -> str:
    """把 uv lock 的不同 source 形状归一为稳定 identity 字符串。"""

    source_value = item.get("source")
    source = cast(dict[str, Any], source_value) if isinstance(source_value, dict) else {}
    for kind in ("registry", "git", "url", "path", "editable", "virtual"):
        value = source.get(kind)
        if isinstance(value, str):
            return f"{kind}:{value}"
    return "unknown"


def package_identity(item: dict[str, Any]) -> PackageIdentity:
    """返回名称、版本和来源组成的不可混淆依赖身份。"""

    return str(item.get("name", "")), str(item.get("version", "")), source_value(item)


def published_root_identities(
    packages: dict[PackageIdentity, dict[str, Any]],
) -> set[PackageIdentity]:
    """只识别仓库实际 workspace source，不能按名称吞掉同名第三方包。"""

    return {
        identity
        for identity in packages
        if PUBLISHED_RUNTIME_ROOT_SOURCES.get(identity[0]) == identity[2]
    }


def dependency_identities(
    dependency: dict[str, Any], packages: dict[PackageIdentity, dict[str, Any]]
) -> set[PackageIdentity]:
    """按 lock 依赖携带的限定字段解析 identity；未限定的同名项全部保留。"""

    name = dependency.get("name")
    if not isinstance(name, str):
        return set()
    matches = {identity for identity in packages if identity[0] == name}
    version = dependency.get("version")
    if isinstance(version, str):
        matches = {identity for identity in matches if identity[1] == version}
    if isinstance(dependency.get("source"), dict):
        source = source_value(dependency)
        matches = {identity for identity in matches if identity[2] == source}
    return matches


def dependency_values(package: dict[str, Any], *, include_optional: bool) -> list[dict[str, Any]]:
    """返回 lock package 的依赖记录；可发布根同时展开全部 runtime extras。"""

    dependencies: list[dict[str, Any]] = []
    for raw_value in package.get("dependencies", []):
        if isinstance(raw_value, dict):
            dependencies.append(cast(dict[str, Any], raw_value))
    if not include_optional:
        return dependencies
    optional_value = package.get("optional-dependencies")
    if not isinstance(optional_value, dict):
        return dependencies
    optional = cast(dict[str, Any], optional_value)
    for group_value in optional.values():
        if not isinstance(group_value, list):
            continue
        for raw_value in cast(list[object], group_value):
            if isinstance(raw_value, dict):
                dependencies.append(cast(dict[str, Any], raw_value))
    return dependencies


def lock_packages(
    root: Path,
) -> tuple[dict[PackageIdentity, dict[str, Any]], set[PackageIdentity]]:
    """读取 lock 并返回全部包及可发布根的直接依赖身份。"""

    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        raise LicenseError("uv.lock is missing")
    with lock_path.open("rb") as stream:
        lock = tomllib.load(stream)
    packages: dict[PackageIdentity, dict[str, Any]] = {}
    for raw_value in lock.get("package", []):
        if not isinstance(raw_value, dict):
            continue
        raw = cast(dict[str, Any], raw_value)
        if not isinstance(raw.get("name"), str):
            continue
        item = dict(raw)
        item["source_value"] = source_value(item)
        packages[package_identity(item)] = item
    roots = published_root_identities(packages)
    direct: set[PackageIdentity] = set()
    for root_identity in roots:
        for dependency in dependency_values(packages[root_identity], include_optional=True):
            direct.update(dependency_identities(dependency, packages))
    return packages, direct


def runtime_identities(
    packages: dict[PackageIdentity, dict[str, Any]],
) -> set[PackageIdentity]:
    """只把两个可发布 workspace package 的运行时闭包纳入合规清单。"""

    roots = published_root_identities(packages)
    identities = set(roots)
    pending = list(identities)
    while pending:
        identity = pending.pop()
        for dependency in dependency_values(packages[identity], include_optional=identity in roots):
            for dependency_identity in dependency_identities(dependency, packages):
                if dependency_identity not in identities:
                    identities.add(dependency_identity)
                    pending.append(dependency_identity)
    return identities


def pypi_metadata_snapshot(
    root: Path,
    runtime: set[PackageIdentity],
) -> dict[PackageIdentity, dict[str, str]]:
    """读取经版本控制的官方 PyPI 精确版本观察，只填补工具的 metadata 缺口。"""

    path = root / "compliance/pypi-license-observations.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as stream:
        snapshot = tomllib.load(stream)
    if snapshot.get("schema_version") != "pypi-license-observations/v1":
        raise LicenseError("metadata snapshot schema version is invalid")
    entries: dict[PackageIdentity, dict[str, str]] = {}
    for raw_value in snapshot.get("packages", []):
        if not isinstance(raw_value, dict):
            raise LicenseError("metadata snapshot package entry is invalid")
        raw = cast(dict[str, Any], raw_value)
        identity = (
            str(raw.get("name", "")),
            str(raw.get("version", "")),
            str(raw.get("source", "")),
        )
        name, version, source = identity
        expected_basis = (
            "https://pypi.org/pypi/"
            f"{urllib.parse.quote(name, safe='')}/{urllib.parse.quote(version, safe='')}/json"
        )
        if raw.get("basis") != expected_basis:
            raise LicenseError("metadata snapshot basis must be the exact official PyPI endpoint")
        license_name = str(raw.get("license", "")).strip()
        field = str(raw.get("field", ""))
        if (
            not name
            or not version
            or source != "registry:https://pypi.org/simple"
            or field not in {"license", "license_expression"}
            or not license_name
            or "UNKNOWN" in license_name.upper()
        ):
            raise LicenseError("metadata snapshot package identity or license is invalid")
        if identity not in runtime:
            raise LicenseError(f"metadata snapshot contains stale identity: {name} {version}")
        if identity in entries:
            raise LicenseError(f"metadata snapshot contains duplicate identity: {name} {version}")
        entries[identity] = {"version": version, "license": license_name}
    return entries


def observe_metadata(root: Path, supplied: Path | None) -> dict[PackageIdentity, dict[str, str]]:
    """合并 licensecheck 观察与受版本约束的官方 PyPI 快照。"""

    if supplied is not None:
        if not supplied.is_file():
            raise LicenseError(f"metadata observation is missing: {relative_path(root, supplied)}")
        data = json.loads(supplied.read_text(encoding="utf-8"))
    else:
        packages, _direct = lock_packages(root)
        runtime = runtime_identities(packages)
        with tempfile.TemporaryDirectory(prefix="agent-harness-license-observation-") as temporary:
            requirements = Path(temporary) / "requirements.txt"
            requirements.write_text(
                "\n".join(
                    f"{identity[0]}=={identity[1]}"
                    for identity in sorted(runtime)
                    if isinstance(packages[identity].get("source"), dict)
                    and "registry" in cast(dict[str, Any], packages[identity]["source"])
                )
                + "\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                "-m",
                "licensecheck",
                "--format",
                "json",
                "--requirements-paths",
                str(requirements),
            ]
            completed = subprocess.run(
                command, cwd=root, text=True, capture_output=True, check=False
            )
            if completed.returncode != 0:
                raise LicenseError("licensecheck metadata observation failed")
            try:
                data = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise LicenseError("licensecheck metadata observation is not JSON") from exc
    observed: dict[PackageIdentity, dict[str, str]] = {}
    packages, _direct = lock_packages(root)
    runtime = runtime_identities(packages)
    for raw_value in data.get("packages", []):
        if isinstance(raw_value, dict):
            raw = cast(dict[str, Any], raw_value)
            if not isinstance(raw.get("name"), str):
                continue
            name = str(raw["name"])
            version = str(raw.get("version", ""))
            matches = {
                identity
                for identity in runtime
                if identity[0] == name and (not version or identity[1] == version)
            }
            raw_source = raw.get("source")
            if isinstance(raw_source, str):
                matches = {identity for identity in matches if identity[2] == raw_source}
            elif len(matches) > 1:
                continue
            for identity in matches:
                observed[identity] = {
                    "version": version,
                    "license": str(raw.get("license", "UNKNOWN")),
                }
    snapshot = pypi_metadata_snapshot(root, runtime)
    for identity, snapshot_observation in snapshot.items():
        observation = observed.get(identity)
        if (
            observation is not None
            and observation["license"].strip()
            and "UNKNOWN" not in observation["license"].upper()
            and normalize_metadata_license(observation["license"])
            != normalize_metadata_license(snapshot_observation["license"])
        ):
            raise LicenseError(
                "metadata snapshot disagrees with licensecheck observation: "
                f"{identity[0]} {identity[1]}"
            )
        if (
            observation is None
            or not observation["license"].strip()
            or "UNKNOWN" in observation["license"].upper()
        ):
            observed[identity] = snapshot_observation
    return observed
