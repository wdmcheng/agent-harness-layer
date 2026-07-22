"""执行依赖、vendoring 与 service image 的可追踪许可证门禁。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = "license-report/v1"
LICENSECHECK_VERSION = "2026.0.8"
POSTGRES_IMAGE = (
    "postgres:18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a"
)
REDIS_IMAGE = "redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7"
POSTGRES_SECURITY_BASIS = "https://www.postgresql.org/support/security/"
REDIS_LICENSE_BASIS = "https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING"
REDIS_SECURITY_BASIS = "https://github.com/redis/redis/releases/tag/7.2.14"
REDIS_SERVER_LICENSE = "BSD-3-Clause"
NOTICE_RUNTIME_MARKERS = (
    "PostgreSQL actual server version: 18.4",
    f"PostgreSQL security basis: {POSTGRES_SECURITY_BASIS}",
    "Redis actual server version: 7.2.14",
    f"Redis security advisory basis: {REDIS_SECURITY_BASIS}",
    f"Redis server license boundary: {REDIS_SERVER_LICENSE}",
    "redis-py client license boundary: MIT",
)
VENDORED_DIR_NAMES = {"third_party", "third-party", "vendor", "vendored"}
REQUIRED_VENDORED_FIELDS = {
    "path",
    "source_url",
    "source_revision",
    "source_sha256",
    "license_expression",
    "license_ref",
    "notice_ref",
    "modified",
    "modification_summary",
    "modification_summary_sha256",
    "adr_ref",
}
APPROVAL_FIELDS = {
    "path",
    "source_url",
    "source_revision",
    "source_sha256",
    "license_expression",
    "modified",
    "modification_summary_sha256",
}

PackageIdentity = tuple[str, str, str]
PUBLISHED_RUNTIME_ROOT_SOURCES = {
    "agent-harness": "editable:packages/agent-harness",
    "agent-harness-service-app": "editable:templates/service-app",
}
METADATA_LICENSE_ALIASES = {
    "zlib": "Zlib",
    "zlib/libpng": "Zlib",
    "zlib_libpng": "Zlib",
    "zlib/libpng license": "Zlib",
}


class LicenseError(RuntimeError):
    """表示许可证输入不完整、漂移或需要人工复核。"""


def _normalized_metadata_license(value: str) -> str:
    """归一同一许可证的工具拼写，同时保留报告中的原始观察值。"""

    compact = " ".join(value.strip().split())
    return METADATA_LICENSE_ALIASES.get(compact.casefold(), compact)


def _issue(message: str) -> str:
    return f"license-check: {message}"


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _source_url_has_credentials(parsed: urllib.parse.SplitResult) -> bool:
    """识别 userinfo 及常见签名查询字段，避免凭据进入合规归档。"""

    if parsed.username is not None or parsed.password is not None:
        return True
    credential_keys = {
        "apikey",
        "authorization",
        "auth",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
    for component in (parsed.query, parsed.fragment):
        for raw_key, _ in urllib.parse.parse_qsl(component, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", raw_key.casefold())
            if normalized in credential_keys or any(
                marker in normalized
                for marker in ("credential", "password", "secret", "signature", "token")
            ):
                return True
    return False


def _valid_source_url(value: object) -> bool:
    """只接受带主机且不携带凭据的明确网络来源。"""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        return (
            parsed.scheme in {"https", "ssh", "git", "git+https", "git+ssh"}
            and bool(parsed.hostname)
            and not _source_url_has_credentials(parsed)
        )
    except ValueError:
        return False


def _report_source_url(value: object) -> str:
    """报告只保留不含凭据的合法来源；其他输入统一脱敏。"""

    if not isinstance(value, str):
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
        if _source_url_has_credentials(parsed):
            return "[REDACTED SOURCE URL]"
        return value if _valid_source_url(value) else "[INVALID SOURCE URL]"
    except ValueError:
        return "[INVALID SOURCE URL]"


def _report_repository_path(value: object) -> str:
    """报告只保留仓库相对路径，拒绝泄漏本机绝对目录。"""

    if not isinstance(value, str) or not value.strip():
        return ""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return "[INVALID REPOSITORY PATH]"
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LicenseError("policy is missing: compliance/third-party.toml")
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _source_value(item: dict[str, Any]) -> str:
    source_value = item.get("source")
    source = cast(dict[str, Any], source_value) if isinstance(source_value, dict) else {}
    for kind in ("registry", "git", "url", "path", "editable", "virtual"):
        value = source.get(kind)
        if isinstance(value, str):
            return f"{kind}:{value}"
    return "unknown"


def _package_identity(item: dict[str, Any]) -> PackageIdentity:
    return str(item.get("name", "")), str(item.get("version", "")), _source_value(item)


def _published_root_identities(
    packages: dict[PackageIdentity, dict[str, Any]],
) -> set[PackageIdentity]:
    """只识别仓库实际 workspace source，不能按名称吞掉同名第三方包。"""

    return {
        identity
        for identity in packages
        if PUBLISHED_RUNTIME_ROOT_SOURCES.get(identity[0]) == identity[2]
    }


def _dependency_identities(
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
        source = _source_value(dependency)
        matches = {identity for identity in matches if identity[2] == source}
    return matches


def _dependency_values(package: dict[str, Any], *, include_optional: bool) -> list[dict[str, Any]]:
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


def _lock_packages(
    root: Path,
) -> tuple[dict[PackageIdentity, dict[str, Any]], set[PackageIdentity]]:
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
        item["source_value"] = _source_value(item)
        packages[_package_identity(item)] = item
    roots = _published_root_identities(packages)
    direct: set[PackageIdentity] = set()
    for root_identity in roots:
        for dependency in _dependency_values(packages[root_identity], include_optional=True):
            direct.update(_dependency_identities(dependency, packages))
    return packages, direct


def _runtime_identities(
    packages: dict[PackageIdentity, dict[str, Any]],
) -> set[PackageIdentity]:
    """只把两个可发布 workspace package 的运行时闭包纳入合规清单。"""

    roots = _published_root_identities(packages)
    identities = set(roots)
    pending = list(identities)
    while pending:
        identity = pending.pop()
        for dependency in _dependency_values(
            packages[identity], include_optional=identity in roots
        ):
            for dependency_identity in _dependency_identities(dependency, packages):
                if dependency_identity not in identities:
                    identities.add(dependency_identity)
                    pending.append(dependency_identity)
    return identities


def _pypi_metadata_snapshot(
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


def _observation(root: Path, supplied: Path | None) -> dict[PackageIdentity, dict[str, str]]:
    if supplied is not None:
        if not supplied.is_file():
            raise LicenseError(f"metadata observation is missing: {_relative(root, supplied)}")
        data = json.loads(supplied.read_text(encoding="utf-8"))
    else:
        packages, _direct = _lock_packages(root)
        runtime = _runtime_identities(packages)
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
    packages, _direct = _lock_packages(root)
    runtime = _runtime_identities(packages)
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
    snapshot = _pypi_metadata_snapshot(root, runtime)
    for identity, snapshot_observation in snapshot.items():
        observation = observed.get(identity)
        if (
            observation is not None
            and observation["license"].strip()
            and "UNKNOWN" not in observation["license"].upper()
            and _normalized_metadata_license(observation["license"])
            != _normalized_metadata_license(snapshot_observation["license"])
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


def _check_license_files(root: Path) -> list[str]:
    issues: list[str] = []
    license_path = root / "LICENSE"
    notice_path = root / "NOTICE"
    if not license_path.is_file():
        issues.append(_issue("LICENSE is missing"))
    elif not all(
        token in license_path.read_text(encoding="utf-8")
        for token in ("Apache License", "Version 2.0", "http://www.apache.org/licenses/")
    ):
        issues.append(_issue("LICENSE does not declare Apache-2.0"))
    if not notice_path.is_file() or not notice_path.read_text(encoding="utf-8").strip():
        issues.append(_issue("NOTICE is missing or empty"))
    else:
        notice = notice_path.read_text(encoding="utf-8")
        for marker in NOTICE_RUNTIME_MARKERS:
            if marker not in notice:
                issues.append(_issue(f"NOTICE missing runtime boundary: {marker}"))
    return issues


def _check_dependencies(
    root: Path,
    policy: dict[str, Any],
    observed: dict[PackageIdentity, dict[str, str]],
    report: dict[str, Any],
) -> list[str]:
    packages, direct = _lock_packages(root)
    entries: dict[PackageIdentity, dict[str, Any]] = {}
    for raw_item in policy.get("packages", []):
        if isinstance(raw_item, dict):
            item = cast(dict[str, Any], raw_item)
            if isinstance(item.get("name"), str):
                identity = (
                    str(item["name"]),
                    str(item.get("version", "")),
                    str(item.get("source", "unknown")),
                )
                entries[identity] = item
    project = cast(dict[str, Any], policy.get("project", {}))
    allowed = {str(item) for item in project.get("allowed_expressions", [])}
    denied = {str(item) for item in project.get("denied_expressions", [])}
    results: list[dict[str, Any]] = []
    issues: list[str] = []
    published_roots = _published_root_identities(packages)
    for identity in sorted(_runtime_identities(packages)):
        name, version, expected_source = identity
        locked = packages[identity]
        # 可发布 workspace 根不是第三方依赖；其余 editable/virtual 项仍必须逐项过策略。
        if identity in published_roots:
            continue
        entry = entries.get(identity)
        if entry is None:
            same_name = [
                candidate
                for candidate_identity, candidate in entries.items()
                if candidate_identity[0] == name
            ]
            same_source = [
                candidate
                for candidate in same_name
                if str(candidate.get("source", "")) == expected_source
            ]
            same_version = [
                candidate for candidate in same_name if str(candidate.get("version", "")) == version
            ]
            for candidates in (same_source, same_version, same_name):
                if len(candidates) == 1:
                    entry = candidates[0]
                    break
        observation = observed.get(identity)
        if entry is None:
            issues.append(_issue(f"{name} {version} has no policy entry"))
            results.append(
                {
                    "basis": "",
                    "name": name,
                    "version": version,
                    "source": locked.get("source_value", "unknown"),
                    "status": "review-required",
                    "decision": "review-required",
                    "direct": identity in direct,
                    "license_expression": "UNKNOWN",
                    "metadata_observation": observation.get("license", "UNKNOWN")
                    if observation
                    else "UNKNOWN",
                }
            )
            continue
        expression = str(entry.get("license_expression", ""))
        decision = str(entry.get("decision", ""))
        basis = str(entry.get("basis", "")).strip()
        metadata_license = observation.get("license", "UNKNOWN") if observation else "UNKNOWN"
        status = "pass"
        if str(entry.get("version", "")) != version:
            status = "review-required"
            issues.append(
                _issue(f"{name} version drift: policy {entry.get('version')} != lock {version}")
            )
        if str(entry.get("source", "")) != expected_source:
            status = "review-required"
            issues.append(_issue(f"{name} source drift"))
        if observation is None or _normalized_metadata_license(
            str(entry.get("metadata_license", ""))
        ) != _normalized_metadata_license(metadata_license):
            status = "review-required"
            issues.append(_issue(f"{name} metadata license drift"))
        if decision not in {"allow", "deny", "review-required"}:
            status = "fail"
            issues.append(_issue(f"{name} has invalid decision"))
        if not basis:
            status = "review-required"
            issues.append(_issue(f"{name} {version} policy basis is required"))
        if not expression or expression in denied or decision == "deny":
            status = "fail"
            issues.append(
                _issue(
                    f"{name} {version} license {expression} denied; basis {entry.get('basis', '')}"
                )
            )
        elif (
            decision == "review-required"
            or expression not in allowed
            or "UNKNOWN" in expression.upper()
        ):
            status = "review-required"
            issues.append(_issue(f"{name} {version} requires review: {decision or expression}"))
        results.append(
            {
                "basis": basis,
                "decision": decision,
                "direct": identity in direct,
                "license_expression": expression,
                "metadata_observation": metadata_license,
                "name": name,
                "source": expected_source,
                "status": status,
                "version": version,
            }
        )
    report["packages"] = results
    return issues


def _compose_reference(root: Path, service: str) -> str:
    compose = root / "templates/service-app/docker-compose.yml"
    text = compose.read_text(encoding="utf-8") if compose.is_file() else ""
    pattern = rf"SERVICE_APP_{service.upper()}_IMAGE:-([^}}\n]+)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _check_service_images(root: Path, policy: dict[str, Any], report: dict[str, Any]) -> list[str]:
    configured = cast(dict[str, Any], policy.get("service_images", {}))
    issues: list[str] = []
    images: list[dict[str, Any]] = []
    for name, expected in (("postgres", POSTGRES_IMAGE), ("redis", REDIS_IMAGE)):
        entry = cast(dict[str, Any], configured.get(name, {}))
        reference = str(entry.get("reference", ""))
        actual = _compose_reference(root, name)
        if actual != reference or reference != expected:
            issues.append(_issue(f"{name} image drift: expected approved identity"))
        if "@sha256:" not in reference:
            issues.append(_issue(f"{name} image must include OCI index digest"))
        if name == "postgres" and not actual.startswith("postgres:18.4@"):
            issues.append(_issue("PostgreSQL 18.4 security fix line is required"))
        if name == "redis" and not actual.startswith("redis:7.2.14@"):
            issues.append(_issue("Redis 7.2.14 BSD security line is required"))
        required_fields = ["server_version", "license_expression", "basis", "smoke_evidence"]
        if name == "redis":
            required_fields.append("security_basis")
        for field in required_fields:
            if not str(entry.get(field, "")).strip():
                issues.append(_issue(f"{name} {field} is required"))
        if name == "postgres" and entry.get("basis") != POSTGRES_SECURITY_BASIS:
            issues.append(_issue("postgres security basis drift"))
        if name == "redis":
            if entry.get("basis") != REDIS_LICENSE_BASIS:
                issues.append(_issue("redis versioned license basis drift"))
            if entry.get("security_basis") != REDIS_SECURITY_BASIS:
                issues.append(_issue("redis security basis drift"))
            if entry.get("license_expression") != REDIS_SERVER_LICENSE:
                issues.append(_issue("redis server license boundary drift"))
        smoke_evidence = str(entry.get("smoke_evidence", ""))
        evidence_payload: dict[str, Any] = {}
        if smoke_evidence:
            evidence_path = Path(smoke_evidence)
            if evidence_path.is_absolute() or ".." in evidence_path.parts:
                issues.append(_issue(f"{name} smoke_evidence must stay repository-relative"))
            else:
                resolved_evidence = (root / evidence_path).resolve()
                try:
                    resolved_evidence.relative_to(root.resolve())
                except ValueError:
                    issues.append(_issue(f"{name} smoke_evidence escapes repository"))
                else:
                    if not resolved_evidence.is_file() or resolved_evidence.is_symlink():
                        issues.append(_issue(f"{name} smoke_evidence does not exist"))
                    else:
                        try:
                            raw_evidence = json.loads(resolved_evidence.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            issues.append(_issue(f"{name} smoke_evidence is not valid JSON"))
                        else:
                            if not isinstance(raw_evidence, dict):
                                issues.append(_issue(f"{name} smoke_evidence must be an object"))
                            else:
                                evidence_payload = cast(dict[str, Any], raw_evidence)
                                if (
                                    evidence_payload.get("schema_version")
                                    != "service-smoke-evidence/v1"
                                ):
                                    issues.append(_issue(f"{name} smoke_evidence schema mismatch"))
                                if evidence_payload.get("status") != "pass":
                                    issues.append(
                                        _issue(f"{name} smoke_evidence status is not pass")
                                    )
                                images_payload = evidence_payload.get("images")
                                image_evidence = (
                                    cast(dict[str, object], images_payload).get(name)
                                    if isinstance(images_payload, dict)
                                    else None
                                )
                                if not isinstance(image_evidence, dict):
                                    issues.append(_issue(f"{name} smoke_evidence identity missing"))
                                else:
                                    image_record = cast(dict[str, object], image_evidence)
                                    expected_identity = actual or reference
                                    if image_record.get("reference") != expected_identity or str(
                                        image_record.get("server_version", "")
                                    ) != str(entry.get("server_version", "")):
                                        issues.append(
                                            _issue(f"{name} smoke_evidence identity mismatch")
                                        )
                                checks = evidence_payload.get("checks")
                                if not isinstance(checks, dict) or not all(
                                    cast(dict[str, object], checks).get(check) is True
                                    for check in ("streams", "xautoclaim", "recovery")
                                ):
                                    issues.append(
                                        _issue(f"{name} smoke_evidence checks incomplete")
                                    )
        item: dict[str, Any] = {
            "name": name,
            "reference": reference,
            "tag": reference.split("@", 1)[0].split(":", 1)[-1] if ":" in reference else "",
            "index_digest": reference.split("@", 1)[1] if "@" in reference else "",
            "server_version": str(entry.get("server_version", "")),
            "license_expression": str(entry.get("license_expression", "")),
            "license_basis": str(entry.get("basis", "")),
            "smoke_evidence": str(entry.get("smoke_evidence", "")),
            "smoke_evidence_status": evidence_payload.get("status", "") if evidence_payload else "",
        }
        if name == "redis":
            if not str(entry.get("client_package", "")).strip():
                issues.append(_issue("redis client_package is required"))
            if not str(entry.get("client_license_expression", "")).strip():
                issues.append(_issue("redis client_license_expression is required"))
            if (
                entry.get("client_package") != "redis"
                or entry.get("client_license_expression") != "MIT"
            ):
                issues.append(_issue("redis client license boundary drift"))
            item["security_basis"] = str(entry.get("security_basis", ""))
            item["client"] = {
                "package": str(entry.get("client_package", "")),
                "license_expression": str(entry.get("client_license_expression", "")),
            }
        images.append(item)
    report["service_images"] = images
    return issues


def _parse_approval(text: str) -> tuple[str, dict[str, Any]]:
    status_match = re.search(r"(?m)^- 状态：([^\n]+)", text)
    status = status_match.group(1).strip() if status_match else ""
    block_match = re.search(r"```toml vendoring_approval\s*\n(.*?)\n```", text, re.S)
    if not block_match:
        return status, {}
    try:
        return status, tomllib.loads(block_match.group(1))
    except tomllib.TOMLDecodeError:
        return status, {}


def _check_vendoring(root: Path, policy: dict[str, Any], report: dict[str, Any]) -> list[str]:
    entries = [
        cast(dict[str, Any], item) for item in policy.get("vendored", []) if isinstance(item, dict)
    ]
    issues: list[str] = []
    project = cast(dict[str, Any], policy.get("project", {}))
    allowed = {str(item) for item in project.get("allowed_expressions", [])}
    denied = {str(item) for item in project.get("denied_expressions", [])}
    found_files: set[str] = set()
    declared_paths: list[tuple[Path, str]] = []
    for directory in root.rglob("*"):
        if not directory.is_dir() or directory.name not in VENDORED_DIR_NAMES:
            continue
        if any(part in {".git", ".venv", ".artifacts", "__pycache__"} for part in directory.parts):
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                found_files.add(_relative(root, path))
    report_entries: list[dict[str, Any]] = []
    for entry in entries:
        report_entry: dict[str, Any] = {
            **{field: entry.get(field) for field in sorted(REQUIRED_VENDORED_FIELDS)},
            "adr_status": "unverified",
            "approval_matches": {field: False for field in sorted(APPROVAL_FIELDS)},
        }
        report_entry["source_url"] = _report_source_url(entry.get("source_url"))
        for field in ("path", "license_ref", "notice_ref", "adr_ref"):
            report_entry[field] = _report_repository_path(entry.get(field))
        report_entries.append(report_entry)
        missing = sorted(REQUIRED_VENDORED_FIELDS - set(entry))
        raw_path = entry.get("path")
        path_value = raw_path if isinstance(raw_path, str) else ""
        report_path = _report_repository_path(raw_path)
        path_label = (
            report_path
            if report_path and report_path != "[INVALID REPOSITORY PATH]"
            else "(invalid path)"
        )
        if missing:
            issues.extend(_issue(f"vendored {path_label} missing {field}") for field in missing)
            continue
        if (
            not path_value.strip()
            or Path(path_value).is_absolute()
            or ".." in Path(path_value).parts
        ):
            issues.append(_issue("vendored path must be a non-empty repository-relative path"))
            continue
        if any(char in path_value for char in "*?[]"):
            issues.append(_issue(f"vendored path wildcard is forbidden: {path_value}"))
            continue
        candidate = (root / path_value).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            issues.append(_issue(f"vendored path escapes repository: {path_value}"))
            continue
        if not candidate.exists():
            issues.append(_issue(f"vendored path does not exist: {path_value}"))
            continue
        for previous, previous_value in declared_paths:
            if (
                candidate == previous
                or candidate in previous.parents
                or previous in candidate.parents
            ):
                issues.append(_issue(f"vendored paths overlap: {previous_value} and {path_value}"))
        declared_paths.append((candidate, path_value))
        source_url = entry["source_url"]
        if not _valid_source_url(source_url):
            issues.append(_issue(f"source_url must be an absolute network URL: {path_value}"))
        source_revision = entry["source_revision"]
        source_sha256 = entry["source_sha256"]
        summary = entry["modification_summary"]
        summary_sha256 = entry["modification_summary_sha256"]
        if not isinstance(source_revision, str):
            source_revision = ""
        if not isinstance(source_sha256, str):
            source_sha256 = ""
        if not isinstance(summary, str) or not summary.strip():
            summary = ""
            issues.append(_issue(f"modification_summary is required: {path_value}"))
        if not isinstance(summary_sha256, str):
            summary_sha256 = ""
        if not isinstance(entry["modified"], bool):
            issues.append(_issue(f"modified must be a boolean: {path_value}"))
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_revision):
            issues.append(
                _issue(f"source_revision must be an immutable hex revision: {path_value}")
            )
        if not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
            issues.append(_issue(f"source_sha256 must be a SHA-256: {path_value}"))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", summary_sha256):
            issues.append(_issue(f"modification_summary_sha256 must be a SHA-256: {path_value}"))
        elif hashlib.sha256(summary.encode("utf-8")).hexdigest() != summary_sha256:
            issues.append(_issue(f"modification_summary_sha256 mismatch: {path_value}"))
        expression = entry["license_expression"]
        if not isinstance(expression, str) or not expression.strip():
            issues.append(_issue(f"license_expression is required: {path_value}"))
        elif expression in denied:
            issues.append(_issue(f"vendored license_expression denied: {expression}"))
        elif expression not in allowed or "UNKNOWN" in expression.upper():
            issues.append(_issue(f"vendored license_expression is not allowed: {expression}"))
        for field in ("license_ref", "notice_ref"):
            raw_reference = entry[field]
            reference = raw_reference if isinstance(raw_reference, str) else ""
            reference_path = Path(reference)
            if reference_path.is_absolute() or ".." in reference_path.parts or not reference:
                issues.append(_issue(f"{field} must be a repository-relative path: {path_value}"))
                continue
            resolved_reference = (root / reference).resolve()
            try:
                resolved_reference.relative_to(root.resolve())
            except ValueError:
                issues.append(_issue(f"{field} escapes repository: {reference}"))
                continue
            if not resolved_reference.is_file():
                issues.append(_issue(f"{field} does not exist: {reference}"))
                continue
            if field == "notice_ref" and path_value not in resolved_reference.read_text(
                encoding="utf-8"
            ):
                issues.append(_issue(f"notice_ref does not mention vendored path: {path_value}"))
        raw_adr_ref = entry["adr_ref"]
        adr_ref = raw_adr_ref if isinstance(raw_adr_ref, str) else ""
        if (
            not adr_ref.startswith("docs/adr/")
            or Path(adr_ref).is_absolute()
            or ".." in Path(adr_ref).parts
        ):
            report_adr_ref = _report_repository_path(raw_adr_ref)
            adr_label = (
                report_adr_ref
                if report_adr_ref and report_adr_ref != "[INVALID REPOSITORY PATH]"
                else "(invalid path)"
            )
            issues.append(_issue(f"adr_ref must stay in docs/adr/: {adr_label}"))
            continue
        adr_path = root / adr_ref
        if not adr_path.is_file():
            issues.append(_issue(f"adr_ref does not exist: {adr_ref}"))
            continue
        adr_status, approval = _parse_approval(adr_path.read_text(encoding="utf-8"))
        matches = {field: approval.get(field) == entry.get(field) for field in APPROVAL_FIELDS}
        if adr_status != "Accepted":
            issues.append(_issue(f"adr_ref {adr_ref} must have status Accepted"))
        if not approval:
            issues.append(_issue(f"adr_ref {adr_ref} lacks vendoring_approval"))
        for field, matched in matches.items():
            if not matched:
                issues.append(_issue(f"vendoring_approval.{field} mismatch"))
        for file in candidate.rglob("*") if candidate.is_dir() else [candidate]:
            if file.is_file():
                found_files.discard(_relative(root, file))
        report_entry["adr_status"] = adr_status
        report_entry["approval_matches"] = matches
    for path in sorted(found_files):
        issues.append(_issue(f"undeclared vendored source: {path}"))
    report["vendored"] = report_entries
    return issues


def check(*, root: Path, observation_path: Path | None, report_path: Path) -> int:
    policy = _load_toml(root / "compliance/third-party.toml")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "input": {
            "uv_lock_sha256": _sha256(root / "uv.lock"),
            "policy_sha256": _sha256(root / "compliance/third-party.toml"),
            "metadata_snapshot_sha256": _sha256(root / "compliance/pypi-license-observations.toml")
            if (root / "compliance/pypi-license-observations.toml").is_file()
            else "",
        },
        "tools": {"licensecheck": LICENSECHECK_VERSION},
        "packages": [],
        "vendored": [],
        "service_images": [],
        "findings": [],
        "disclaimer": "自动检查结果不构成法律意见；组织仍需完成必要的人工复核。",
    }
    issues = _check_license_files(root)
    observed = _observation(root, observation_path)
    issues.extend(_check_dependencies(root, policy, observed, report))
    issues.extend(_check_vendoring(root, policy, report))
    issues.extend(_check_service_images(root, policy, report))
    report["findings"] = [{"message": issue, "severity": "error"} for issue in issues]
    if issues:
        review_markers = (
            "requires review",
            "version drift",
            "source drift",
            "metadata license drift",
            "no policy entry",
        )
        report["status"] = (
            "review-required"
            if all(any(marker in issue for marker in review_markers) for issue in issues)
            else "fail"
        )
    _atomic_json(report_path, report)
    for issue in issues:
        print(issue, file=sys.stderr)
    if issues:
        return 1
    print("license-check: ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--metadata-observation", type=Path)
    parser.add_argument(
        "--report", type=Path, default=Path(".artifacts/license/license-report.json")
    )
    args = parser.parse_args()
    root = args.root.resolve()
    report = args.report if args.report.is_absolute() else root / args.report
    try:
        return check(root=root, observation_path=args.metadata_observation, report_path=report)
    except (LicenseError, OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "input": {},
            "tools": {"licensecheck": LICENSECHECK_VERSION},
            "packages": [],
            "vendored": [],
            "service_images": [],
            "findings": [{"message": str(exc), "severity": "error"}],
            "disclaimer": "自动检查结果不构成法律意见；组织仍需完成必要的人工复核。",
        }
        try:
            _atomic_json(report, payload)
        except OSError:
            pass
        print(_issue(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
