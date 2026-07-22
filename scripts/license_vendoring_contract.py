"""校验 vendored source 声明、文件引用和 ADR 审批证据。"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any, cast

from license_check_support import (
    APPROVAL_FIELDS,
    REQUIRED_VENDORED_FIELDS,
    VENDORED_DIR_NAMES,
    issue,
    relative_path,
    report_repository_path,
    report_source_url,
    valid_source_url,
)


def parse_approval(text: str) -> tuple[str, dict[str, Any]]:
    """读取 ADR 状态和机器可核验的 vendoring approval 区块。"""

    status_match = re.search(r"(?m)^- 状态：([^\n]+)", text)
    status = status_match.group(1).strip() if status_match else ""
    block_match = re.search(r"```toml vendoring_approval\s*\n(.*?)\n```", text, re.S)
    if not block_match:
        return status, {}
    try:
        return status, tomllib.loads(block_match.group(1))
    except tomllib.TOMLDecodeError:
        return status, {}


def check_vendoring(root: Path, policy: dict[str, Any], report: dict[str, Any]) -> list[str]:
    """校验 vendored source、许可证引用、修改摘要与 ADR 审批一致性。"""

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
                found_files.add(relative_path(root, path))
    report_entries: list[dict[str, Any]] = []
    for entry in entries:
        report_entry: dict[str, Any] = {
            **{field: entry.get(field) for field in sorted(REQUIRED_VENDORED_FIELDS)},
            "adr_status": "unverified",
            "approval_matches": {field: False for field in sorted(APPROVAL_FIELDS)},
        }
        report_entry["source_url"] = report_source_url(entry.get("source_url"))
        for field in ("path", "license_ref", "notice_ref", "adr_ref"):
            report_entry[field] = report_repository_path(entry.get(field))
        report_entries.append(report_entry)
        missing = sorted(REQUIRED_VENDORED_FIELDS - set(entry))
        raw_path = entry.get("path")
        path_value = raw_path if isinstance(raw_path, str) else ""
        report_path = report_repository_path(raw_path)
        path_label = (
            report_path
            if report_path and report_path != "[INVALID REPOSITORY PATH]"
            else "(invalid path)"
        )
        if missing:
            issues.extend(issue(f"vendored {path_label} missing {field}") for field in missing)
            continue
        if (
            not path_value.strip()
            or Path(path_value).is_absolute()
            or ".." in Path(path_value).parts
        ):
            issues.append(issue("vendored path must be a non-empty repository-relative path"))
            continue
        if any(char in path_value for char in "*?[]"):
            issues.append(issue(f"vendored path wildcard is forbidden: {path_value}"))
            continue
        candidate = (root / path_value).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            issues.append(issue(f"vendored path escapes repository: {path_value}"))
            continue
        if not candidate.exists():
            issues.append(issue(f"vendored path does not exist: {path_value}"))
            continue
        for previous, previous_value in declared_paths:
            if (
                candidate == previous
                or candidate in previous.parents
                or previous in candidate.parents
            ):
                issues.append(issue(f"vendored paths overlap: {previous_value} and {path_value}"))
        declared_paths.append((candidate, path_value))
        source_url = entry["source_url"]
        if not valid_source_url(source_url):
            issues.append(issue(f"source_url must be an absolute network URL: {path_value}"))
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
            issues.append(issue(f"modification_summary is required: {path_value}"))
        if not isinstance(summary_sha256, str):
            summary_sha256 = ""
        if not isinstance(entry["modified"], bool):
            issues.append(issue(f"modified must be a boolean: {path_value}"))
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_revision):
            issues.append(issue(f"source_revision must be an immutable hex revision: {path_value}"))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
            issues.append(issue(f"source_sha256 must be a SHA-256: {path_value}"))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", summary_sha256):
            issues.append(issue(f"modification_summary_sha256 must be a SHA-256: {path_value}"))
        elif hashlib.sha256(summary.encode("utf-8")).hexdigest() != summary_sha256:
            issues.append(issue(f"modification_summary_sha256 mismatch: {path_value}"))
        expression = entry["license_expression"]
        if not isinstance(expression, str) or not expression.strip():
            issues.append(issue(f"license_expression is required: {path_value}"))
        elif expression in denied:
            issues.append(issue(f"vendored license_expression denied: {expression}"))
        elif expression not in allowed or "UNKNOWN" in expression.upper():
            issues.append(issue(f"vendored license_expression is not allowed: {expression}"))
        for field in ("license_ref", "notice_ref"):
            raw_reference = entry[field]
            reference = raw_reference if isinstance(raw_reference, str) else ""
            reference_path = Path(reference)
            if reference_path.is_absolute() or ".." in reference_path.parts or not reference:
                issues.append(issue(f"{field} must be a repository-relative path: {path_value}"))
                continue
            resolved_reference = (root / reference).resolve()
            try:
                resolved_reference.relative_to(root.resolve())
            except ValueError:
                issues.append(issue(f"{field} escapes repository: {reference}"))
                continue
            if not resolved_reference.is_file():
                issues.append(issue(f"{field} does not exist: {reference}"))
                continue
            if field == "notice_ref" and path_value not in resolved_reference.read_text(
                encoding="utf-8"
            ):
                issues.append(issue(f"notice_ref does not mention vendored path: {path_value}"))
        raw_adr_ref = entry["adr_ref"]
        adr_ref = raw_adr_ref if isinstance(raw_adr_ref, str) else ""
        if (
            not adr_ref.startswith("docs/adr/")
            or Path(adr_ref).is_absolute()
            or ".." in Path(adr_ref).parts
        ):
            report_adr_ref = report_repository_path(raw_adr_ref)
            adr_label = (
                report_adr_ref
                if report_adr_ref and report_adr_ref != "[INVALID REPOSITORY PATH]"
                else "(invalid path)"
            )
            issues.append(issue(f"adr_ref must stay in docs/adr/: {adr_label}"))
            continue
        adr_path = root / adr_ref
        if not adr_path.is_file():
            issues.append(issue(f"adr_ref does not exist: {adr_ref}"))
            continue
        adr_status, approval = parse_approval(adr_path.read_text(encoding="utf-8"))
        matches = {field: approval.get(field) == entry.get(field) for field in APPROVAL_FIELDS}
        if adr_status != "Accepted":
            issues.append(issue(f"adr_ref {adr_ref} must have status Accepted"))
        if not approval:
            issues.append(issue(f"adr_ref {adr_ref} lacks vendoring_approval"))
        for field, matched in matches.items():
            if not matched:
                issues.append(issue(f"vendoring_approval.{field} mismatch"))
        for file in candidate.rglob("*") if candidate.is_dir() else [candidate]:
            if file.is_file():
                found_files.discard(relative_path(root, file))
        report_entry["adr_status"] = adr_status
        report_entry["approval_matches"] = matches
    for path in sorted(found_files):
        issues.append(issue(f"undeclared vendored source: {path}"))
    report["vendored"] = report_entries
    return issues
