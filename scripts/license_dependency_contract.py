"""对照仓库策略校验许可证文件和 runtime 依赖裁决。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from license_check_support import (
    NOTICE_RUNTIME_MARKERS,
    PackageIdentity,
    issue,
    normalize_metadata_license,
)
from license_dependency_inventory import (
    lock_packages,
    published_root_identities,
    runtime_identities,
)


def check_license_files(root: Path) -> list[str]:
    """校验仓库许可证正文与 NOTICE 中的 runtime 边界。"""

    issues: list[str] = []
    license_path = root / "LICENSE"
    notice_path = root / "NOTICE"
    if not license_path.is_file():
        issues.append(issue("LICENSE is missing"))
    elif not all(
        token in license_path.read_text(encoding="utf-8")
        for token in ("Apache License", "Version 2.0", "http://www.apache.org/licenses/")
    ):
        issues.append(issue("LICENSE does not declare Apache-2.0"))
    if not notice_path.is_file() or not notice_path.read_text(encoding="utf-8").strip():
        issues.append(issue("NOTICE is missing or empty"))
    else:
        notice = notice_path.read_text(encoding="utf-8")
        for marker in NOTICE_RUNTIME_MARKERS:
            if marker not in notice:
                issues.append(issue(f"NOTICE missing runtime boundary: {marker}"))
    return issues


def check_dependencies(
    root: Path,
    policy: dict[str, Any],
    observed: dict[PackageIdentity, dict[str, str]],
    report: dict[str, Any],
) -> list[str]:
    """对照 runtime closure、metadata 观察和策略逐项裁决依赖。"""

    packages, direct = lock_packages(root)
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
    published_roots = published_root_identities(packages)
    for identity in sorted(runtime_identities(packages)):
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
            issues.append(issue(f"{name} {version} has no policy entry"))
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
                issue(f"{name} version drift: policy {entry.get('version')} != lock {version}")
            )
        if str(entry.get("source", "")) != expected_source:
            status = "review-required"
            issues.append(issue(f"{name} source drift"))
        if observation is None or normalize_metadata_license(
            str(entry.get("metadata_license", ""))
        ) != normalize_metadata_license(metadata_license):
            status = "review-required"
            issues.append(issue(f"{name} metadata license drift"))
        if decision not in {"allow", "deny", "review-required"}:
            status = "fail"
            issues.append(issue(f"{name} has invalid decision"))
        if not basis:
            status = "review-required"
            issues.append(issue(f"{name} {version} policy basis is required"))
        if not expression or expression in denied or decision == "deny":
            status = "fail"
            issues.append(
                issue(
                    f"{name} {version} license {expression} denied; basis {entry.get('basis', '')}"
                )
            )
        elif (
            decision == "review-required"
            or expression not in allowed
            or "UNKNOWN" in expression.upper()
        ):
            status = "review-required"
            issues.append(issue(f"{name} {version} requires review: {decision or expression}"))
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
