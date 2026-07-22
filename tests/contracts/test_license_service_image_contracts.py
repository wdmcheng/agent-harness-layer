"""Service image identity 与许可证边界合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.license_contract_test_support import (
    NOTICE_RUNTIME_BOUNDARY,
    POSTGRES_IMAGE,
    REDIS_IMAGE,
    REDIS_LICENSE_BASIS,
    REDIS_SECURITY_BASIS,
    prepared_repository,
    read_report,
    run_check,
)

ROOT = Path(__file__).resolve().parents[2]


def test_service_compose_defaults_pin_approved_tag_and_oci_index_digest() -> None:
    """仓库模板必须精确固定批准的 PostgreSQL 与 Redis identity。"""

    compose = (ROOT / "templates/service-app/docker-compose.yml").read_text(encoding="utf-8")

    assert f"${{SERVICE_APP_POSTGRES_IMAGE:-{POSTGRES_IMAGE}}}" in compose
    assert f"${{SERVICE_APP_REDIS_IMAGE:-{REDIS_IMAGE}}}" in compose
    assert "postgres:18}" not in compose
    assert "redis:8.0.1" not in compose


def test_floating_redis_or_postgresql_18_3_fails_closed(tmp_path: Path) -> None:
    """浮动 Redis 7.2 和被 18.4 修复线覆盖的 PostgreSQL 18.3 都不能回退。"""

    root, observation = prepared_repository(tmp_path)
    compose_path = root / "templates/service-app/docker-compose.yml"
    baseline = compose_path.read_text(encoding="utf-8")
    compose_path.write_text(baseline.replace(REDIS_IMAGE, "redis:7.2"), encoding="utf-8")
    floating = run_check(root, observation)
    compose_path.write_text(
        baseline.replace(
            POSTGRES_IMAGE,
            "postgres:18.3@sha256:" + "c" * 64,
        ),
        encoding="utf-8",
    )
    vulnerable = run_check(root, observation)

    assert floating.returncode != 0
    assert "redis" in floating.stderr.lower()
    assert "image drift" in floating.stderr
    assert vulnerable.returncode != 0
    assert "PostgreSQL 18.4 security fix line" in vulnerable.stderr


def test_report_keeps_server_identity_and_redis_client_boundary_separate(
    tmp_path: Path,
) -> None:
    """Redis 7.2 server 的 BSD 许可不得覆盖 redis-py client 的 MIT 判断。"""

    root, observation = prepared_repository(tmp_path)

    result = run_check(root, observation)

    assert result.returncode == 0, result.stderr
    report = cast(dict[str, Any], read_report(root))
    images = {item["name"]: item for item in cast(list[dict[str, Any]], report["service_images"])}
    assert images["postgres"]["server_version"] == "18.4"
    assert images["postgres"]["index_digest"].startswith("sha256:")
    assert images["redis"]["server_version"] == "7.2.14"
    assert images["redis"]["license_basis"] == REDIS_LICENSE_BASIS
    assert images["redis"]["security_basis"] == REDIS_SECURITY_BASIS
    assert images["redis"]["license_expression"] == "BSD-3-Clause"
    assert images["redis"]["client"] == {
        "license_expression": "MIT",
        "package": "redis",
    }
    assert images["redis"]["smoke_evidence"] == ".artifacts/license/smoke-service.log"


@pytest.mark.parametrize(
    "marker",
    [
        "PostgreSQL actual server version: 18.4",
        "PostgreSQL security basis: https://www.postgresql.org/support/security/",
        "Redis actual server version: 7.2.14",
        f"Redis security advisory basis: {REDIS_SECURITY_BASIS}",
        "Redis server license boundary: BSD-3-Clause",
        "redis-py client license boundary: MIT",
    ],
)
def test_notice_requires_each_runtime_security_and_license_boundary(
    tmp_path: Path,
    marker: str,
) -> None:
    """NOTICE 缺任一实际版本、安全依据或 server/client 边界都必须失败。"""

    root, observation = prepared_repository(tmp_path)
    notice = root / "NOTICE"
    text = notice.read_text(encoding="utf-8")
    assert NOTICE_RUNTIME_BOUNDARY.strip() in text
    notice.write_text(text.replace(marker, ""), encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "NOTICE" in result.stderr


def test_service_image_metadata_is_required(tmp_path: Path) -> None:
    """缺少 server version、许可证依据或 smoke evidence 时必须 fail closed。"""

    root, observation = prepared_repository(tmp_path)
    policy = root / "compliance/third-party.toml"
    text = policy.read_text(encoding="utf-8")
    policy.write_text(
        text.replace('server_version = "7.2.14"', 'server_version = ""'), encoding="utf-8"
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "redis" in result.stderr
    assert "server_version" in result.stderr


def test_service_smoke_evidence_must_exist_and_report_pass(tmp_path: Path) -> None:
    """策略引用的 smoke evidence 必须是本次仓库内真实归档的通过结果。"""

    root, observation = prepared_repository(tmp_path)
    evidence = root / ".artifacts/license/smoke-service.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        '{"schema_version":"service-smoke-evidence/v1","status":"fail"}\n',
        encoding="utf-8",
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "smoke_evidence" in result.stderr
    assert read_report(root)["status"] == "fail"


def test_service_smoke_evidence_identity_must_match_policy(tmp_path: Path) -> None:
    """smoke 证据中的镜像与 server version 不能脱离策略清单。"""

    root, observation = prepared_repository(tmp_path)
    evidence = root / ".artifacts/license/smoke-service.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        '{"schema_version":"service-smoke-evidence/v1","status":"pass",'
        '"images":{"postgres":{"reference":"postgres:18.3", "server_version":"18.3"},'
        '"redis":{"reference":"redis:7.2.13", "server_version":"7.2.13"}},'
        '"checks":{"streams":true,"xautoclaim":true,"recovery":true}}\n',
        encoding="utf-8",
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "smoke_evidence" in result.stderr
    assert "identity" in result.stderr


@pytest.mark.parametrize("unsupported", ["redis:7.4.0", "redis:8.2.7"])
def test_redis_source_available_license_lines_fail_closed(
    tmp_path: Path,
    unsupported: str,
) -> None:
    """P0 许可决策只批准 BSD 的 7.2 线，7.4+ 不得混入同一策略。"""

    root, observation = prepared_repository(tmp_path)
    compose = root / "templates/service-app/docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(REDIS_IMAGE, unsupported),
        encoding="utf-8",
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "redis image drift" in result.stderr
