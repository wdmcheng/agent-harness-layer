"""校验 service profile 镜像、许可证依据与 smoke evidence 身份。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from license_check_support import (
    POSTGRES_IMAGE,
    POSTGRES_SECURITY_BASIS,
    REDIS_IMAGE,
    REDIS_LICENSE_BASIS,
    REDIS_SECURITY_BASIS,
    REDIS_SERVER_LICENSE,
    issue,
)


def compose_reference(root: Path, service: str) -> str:
    """从模板 Compose 的环境变量默认值读取实际镜像 identity。"""

    compose = root / "templates/service-app/docker-compose.yml"
    text = compose.read_text(encoding="utf-8") if compose.is_file() else ""
    pattern = rf"SERVICE_APP_{service.upper()}_IMAGE:-([^}}\n]+)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def check_service_images(root: Path, policy: dict[str, Any], report: dict[str, Any]) -> list[str]:
    """闭合 Compose、策略、许可证依据与真实 smoke evidence 的镜像身份。"""

    configured = cast(dict[str, Any], policy.get("service_images", {}))
    issues: list[str] = []
    images: list[dict[str, Any]] = []
    for name, expected in (("postgres", POSTGRES_IMAGE), ("redis", REDIS_IMAGE)):
        entry = cast(dict[str, Any], configured.get(name, {}))
        reference = str(entry.get("reference", ""))
        actual = compose_reference(root, name)
        if actual != reference or reference != expected:
            issues.append(issue(f"{name} image drift: expected approved identity"))
        if "@sha256:" not in reference:
            issues.append(issue(f"{name} image must include OCI index digest"))
        if name == "postgres" and not actual.startswith("postgres:18.4@"):
            issues.append(issue("PostgreSQL 18.4 security fix line is required"))
        if name == "redis" and not actual.startswith("redis:7.2.14@"):
            issues.append(issue("Redis 7.2.14 BSD security line is required"))
        required_fields = ["server_version", "license_expression", "basis", "smoke_evidence"]
        if name == "redis":
            required_fields.append("security_basis")
        for field in required_fields:
            if not str(entry.get(field, "")).strip():
                issues.append(issue(f"{name} {field} is required"))
        if name == "postgres" and entry.get("basis") != POSTGRES_SECURITY_BASIS:
            issues.append(issue("postgres security basis drift"))
        if name == "redis":
            if entry.get("basis") != REDIS_LICENSE_BASIS:
                issues.append(issue("redis versioned license basis drift"))
            if entry.get("security_basis") != REDIS_SECURITY_BASIS:
                issues.append(issue("redis security basis drift"))
            if entry.get("license_expression") != REDIS_SERVER_LICENSE:
                issues.append(issue("redis server license boundary drift"))
        smoke_evidence = str(entry.get("smoke_evidence", ""))
        evidence_payload: dict[str, Any] = {}
        if smoke_evidence:
            evidence_path = Path(smoke_evidence)
            if evidence_path.is_absolute() or ".." in evidence_path.parts:
                issues.append(issue(f"{name} smoke_evidence must stay repository-relative"))
            else:
                resolved_evidence = (root / evidence_path).resolve()
                try:
                    resolved_evidence.relative_to(root.resolve())
                except ValueError:
                    issues.append(issue(f"{name} smoke_evidence escapes repository"))
                else:
                    if not resolved_evidence.is_file() or resolved_evidence.is_symlink():
                        issues.append(issue(f"{name} smoke_evidence does not exist"))
                    else:
                        try:
                            raw_evidence = json.loads(resolved_evidence.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            issues.append(issue(f"{name} smoke_evidence is not valid JSON"))
                        else:
                            if not isinstance(raw_evidence, dict):
                                issues.append(issue(f"{name} smoke_evidence must be an object"))
                            else:
                                evidence_payload = cast(dict[str, Any], raw_evidence)
                                if (
                                    evidence_payload.get("schema_version")
                                    != "service-smoke-evidence/v1"
                                ):
                                    issues.append(issue(f"{name} smoke_evidence schema mismatch"))
                                if evidence_payload.get("status") != "pass":
                                    issues.append(
                                        issue(f"{name} smoke_evidence status is not pass")
                                    )
                                images_payload = evidence_payload.get("images")
                                image_evidence = (
                                    cast(dict[str, object], images_payload).get(name)
                                    if isinstance(images_payload, dict)
                                    else None
                                )
                                if not isinstance(image_evidence, dict):
                                    issues.append(issue(f"{name} smoke_evidence identity missing"))
                                else:
                                    image_record = cast(dict[str, object], image_evidence)
                                    expected_identity = actual or reference
                                    if image_record.get("reference") != expected_identity or str(
                                        image_record.get("server_version", "")
                                    ) != str(entry.get("server_version", "")):
                                        issues.append(
                                            issue(f"{name} smoke_evidence identity mismatch")
                                        )
                                checks = evidence_payload.get("checks")
                                if not isinstance(checks, dict) or not all(
                                    cast(dict[str, object], checks).get(check) is True
                                    for check in ("streams", "xautoclaim", "recovery")
                                ):
                                    issues.append(issue(f"{name} smoke_evidence checks incomplete"))
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
                issues.append(issue("redis client_package is required"))
            if not str(entry.get("client_license_expression", "")).strip():
                issues.append(issue("redis client_license_expression is required"))
            if (
                entry.get("client_package") != "redis"
                or entry.get("client_license_expression") != "MIT"
            ):
                issues.append(issue("redis client license boundary drift"))
            item["security_basis"] = str(entry.get("security_basis", ""))
            item["client"] = {
                "package": str(entry.get("client_package", "")),
                "license_expression": str(entry.get("client_license_expression", "")),
            }
        images.append(item)
    report["service_images"] = images
    return issues
