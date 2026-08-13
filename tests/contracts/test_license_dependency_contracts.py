"""依赖许可证基础策略、metadata 观察与版本漂移合同。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

import pytest
from tests.contracts.license_contract_test_support import (
    prepared_repository,
    read_report,
    run_check,
    write_lock,
    write_minimal_repository,
    write_observation,
    write_policy,
)


def test_repository_license_is_apache_2_0() -> None:
    """仓库根许可证必须保留 Apache License 2.0 的规范标题与版本。"""

    license_text = (Path(__file__).parents[2] / "LICENSE").read_text(encoding="utf-8")

    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text


def test_allowed_dependency_matches_lock_metadata_and_repository_decision(tmp_path: Path) -> None:
    """允许项只有在 lock、观察和仓库判断一致时才通过。"""

    root, observation = prepared_repository(tmp_path)

    result = run_check(root, observation)

    assert result.returncode == 0, result.stderr
    report = read_report(root)
    assert set(report) == {
        "schema_version",
        "status",
        "input",
        "tools",
        "packages",
        "vendored",
        "service_images",
        "findings",
        "disclaimer",
    }
    assert report["schema_version"] == "license-report/v1"
    assert report["status"] == "pass"
    inputs = cast(dict[str, str], report["input"])
    assert set(inputs) == {
        "uv_lock_sha256",
        "policy_sha256",
        "metadata_snapshot_sha256",
    }
    assert len(inputs["uv_lock_sha256"]) == 64
    assert len(inputs["policy_sha256"]) == 64
    assert inputs["metadata_snapshot_sha256"] == ""
    assert cast(dict[str, str], report["tools"]) == {"licensecheck": "2026.0.8"}
    assert report["packages"] == [
        {
            "basis": "https://pypi.org/project/fixture-package/1.0.0/",
            "decision": "allow",
            "direct": True,
            "license_expression": "MIT",
            "metadata_observation": "MIT",
            "name": "fixture-package",
            "source": "registry:https://pypi.org/simple",
            "status": "pass",
            "version": "1.0.0",
        }
    ]
    assert report["vendored"] == []
    service_images = cast(list[dict[str, object]], report["service_images"])
    assert {item["name"] for item in service_images} == {"postgres", "redis"}
    assert all(
        {
            "name",
            "reference",
            "tag",
            "index_digest",
            "server_version",
            "license_expression",
            "license_basis",
            "smoke_evidence",
            "smoke_evidence_status",
        }
        <= set(item)
        for item in service_images
    )
    assert report["findings"] == []
    assert report["disclaimer"] == "自动检查结果不构成法律意见；组织仍需完成必要的人工复核。"


def test_denied_dependency_returns_nonzero_with_exact_repository_basis(tmp_path: Path) -> None:
    """明确拒绝项必须报告包、版本、license 和判断依据。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="GNU General Public License v3",
        license_expression="GPL-3.0-only",
        decision="deny",
    )
    observation = write_observation(tmp_path, license_name="GNU General Public License v3")

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "fixture-package 1.0.0" in result.stderr
    assert "GPL-3.0-only" in result.stderr
    assert "https://pypi.org/project/fixture-package/1.0.0/" in result.stderr
    assert read_report(tmp_path)["status"] == "fail"


def test_unknown_dependency_requires_review_instead_of_silent_allow(tmp_path: Path) -> None:
    """缺失或自定义条款不得被允许列表静默吞掉。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="UNKNOWN",
        license_expression="LicenseRef-Proprietary-Custom",
        decision="review-required",
    )
    observation = write_observation(tmp_path, license_name="UNKNOWN")

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "review-required" in result.stderr
    assert read_report(tmp_path)["status"] == "review-required"


def _write_pypi_snapshot(
    root: Path,
    *,
    name: str = "fixture-package",
    version: str = "1.0.0",
    license_name: str = "MIT",
    basis: str | None = None,
    field: str = "license_expression",
) -> None:
    """写入版本绑定的官方 PyPI metadata 观察快照。"""

    endpoint = basis or f"https://pypi.org/pypi/{name}/{version}/json"
    (root / "compliance/pypi-license-observations.toml").write_text(
        'schema_version = "pypi-license-observations/v1"\n\n'
        "[[packages]]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        'source = "registry:https://pypi.org/simple"\n'
        f'field = "{field}"\n'
        f'license = "{license_name}"\n'
        f'basis = "{endpoint}"\n',
        encoding="utf-8",
    )


def test_unknown_licensecheck_metadata_uses_version_bound_official_snapshot(
    tmp_path: Path,
) -> None:
    """工具观察缺失时只允许精确版本的官方 PyPI 快照补齐。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(tmp_path)

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    report = read_report(tmp_path)
    inputs = cast(dict[str, object], report["input"])
    assert inputs["metadata_snapshot_sha256"]
    package = cast(list[dict[str, object]], report["packages"])[0]
    assert package["metadata_observation"] == "MIT"


def test_unknown_classifier_snapshot_normalizes_to_policy_metadata(tmp_path: Path) -> None:
    """官方PyPI classifier可补未知观察，但报告仍保留完整原始classifier。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="BSD License",
        license_expression="BSD-3-Clause",
    )
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(
        tmp_path,
        license_name="License :: OSI Approved :: BSD License",
        field="classifier",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    package = cast(list[dict[str, object]], read_report(tmp_path)["packages"])[0]
    assert package["metadata_observation"] == "License :: OSI Approved :: BSD License"


def test_classifier_snapshot_requires_osi_approved_prefix(tmp_path: Path) -> None:
    """classifier快照必须保留官方OSI前缀，不能伪装成普通许可证字段。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(tmp_path, license_name="MIT", field="classifier")

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata snapshot package identity or license is invalid" in result.stderr


def test_classifier_snapshot_rejects_whitespace_normalized_prefix(tmp_path: Path) -> None:
    """classifier前缀必须逐字匹配，不能先修复畸形空白再作为官方观察接受。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(
        tmp_path,
        license_name="License  :: OSI Approved :: MIT",
        field="classifier",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata snapshot package identity or license is invalid" in result.stderr


def test_non_classifier_snapshot_rejects_osi_approved_prefix(tmp_path: Path) -> None:
    """普通license字段不得借classifier前缀触发跨字段归一化。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(
        tmp_path,
        license_name="License :: OSI Approved :: MIT",
        field="license",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata snapshot package identity or license is invalid" in result.stderr


def test_unknown_spdx_conjunction_matches_licensecheck_list_metadata(tmp_path: Path) -> None:
    """同一组SPDX标识的官方AND表达与licensecheck列表不得产生平台漂移。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="Apache-2.0;; BSD-2-Clause",
        license_expression="Apache-2.0",
    )
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(
        tmp_path,
        license_name="Apache-2.0 AND BSD-2-Clause",
        field="license_expression",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr


def test_snapshot_rejects_unsupported_pypi_metadata_field(tmp_path: Path) -> None:
    """快照只能引用受控PyPI字段，不能用任意说明文本替代许可证观察。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(tmp_path, field="project_url")

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata snapshot package identity or license is invalid" in result.stderr


def test_snapshot_does_not_conflate_spdx_or_with_conjunction(tmp_path: Path) -> None:
    """OR与多许可证合取语义不同，归一化不得为了跨平台一致而放行。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="Apache-2.0;; BSD-2-Clause",
        license_expression="Apache-2.0",
    )
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(
        tmp_path,
        license_name="Apache-2.0 OR BSD-2-Clause",
        field="license_expression",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata license drift" in result.stderr


def test_repository_snapshot_covers_hosted_unknown_metadata_identities() -> None:
    """Hosted Linux已观测为UNKNOWN的精确包身份必须有官方版本快照。"""

    root = Path(__file__).resolve().parents[2]
    snapshot = tomllib.loads(
        (root / "compliance/pypi-license-observations.toml").read_text(encoding="utf-8")
    )
    entries = {str(item["name"]): item for item in snapshot["packages"]}
    expected = {
        "arize-phoenix-otel": ("0.16.1", "license", "Apache-2.0"),
        "grpcio": ("1.82.1", "license_expression", "Apache-2.0"),
        "numpy": (
            "2.5.1",
            "license_expression",
            "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        ),
        "openinference-instrumentation": ("0.1.54", "license_expression", "Apache-2.0"),
        "openinference-instrumentation-openai": (
            "0.1.52",
            "license_expression",
            "Apache-2.0",
        ),
        "openinference-semantic-conventions": (
            "0.1.30",
            "license_expression",
            "Apache-2.0",
        ),
        "pandas": (
            "3.0.3",
            "classifier",
            "License :: OSI Approved :: BSD License",
        ),
        "prometheus-client": (
            "0.25.0",
            "license_expression",
            "Apache-2.0 AND BSD-2-Clause",
        ),
    }

    for name, (version, field, license_name) in expected.items():
        entry = entries[name]
        assert (entry["version"], entry["field"], entry["license"]) == (
            version,
            field,
            license_name,
        )
        assert entry["basis"] == f"https://pypi.org/pypi/{name}/{version}/json"


def test_known_licensecheck_metadata_is_not_overridden_by_snapshot(tmp_path: Path) -> None:
    """已有工具观察必须优先，且与快照冲突时不能静默放行。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="BSD-3-Clause",
        license_expression="BSD-3-Clause",
    )
    observation = write_observation(tmp_path, license_name="BSD-3-Clause")
    _write_pypi_snapshot(tmp_path, license_name="MIT")

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata snapshot disagrees with licensecheck observation" in result.stderr


def test_pypi_snapshot_rejects_non_exact_or_non_official_basis(tmp_path: Path) -> None:
    """快照依据不是官方精确版本端点时必须 fail closed。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    observation = write_observation(tmp_path, license_name="UNKNOWN")
    _write_pypi_snapshot(
        tmp_path,
        basis="https://example.invalid/fixture-package/1.0.0.json",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "metadata snapshot basis must be the exact official PyPI endpoint" in result.stderr


@pytest.mark.parametrize(
    "metadata_name",
    ["zlib/libpng", "zlib_libpng", "zlib/libpng License"],
)
def test_equivalent_zlib_metadata_spellings_use_one_policy_identity(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    """PyPI 与 licensecheck 的等价拼写必须归一比较，不能制造策略漂移。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(
        tmp_path,
        metadata_license="Zlib",
        license_expression="Zlib",
    )
    observation = write_observation(tmp_path, license_name=metadata_name)

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    package = cast(list[dict[str, object]], read_report(tmp_path)["packages"])[0]
    assert package["metadata_observation"] == metadata_name
    assert package["license_expression"] == "Zlib"


def test_sqlean_policy_separates_raw_metadata_from_normalized_spdx() -> None:
    """真实清单保留发布物原始 metadata，并用独立字段记录规范 SPDX 判断。"""

    root = Path(__file__).resolve().parents[2]
    policy = tomllib.loads((root / "compliance/third-party.toml").read_text(encoding="utf-8"))
    packages = cast(list[dict[str, object]], policy["packages"])
    sqlean = {
        str(package["version"]): package
        for package in packages
        if package.get("name") == "sqlean-py"
    }

    assert set(sqlean) == {"3.49.1", "3.50.4.5"}
    for package in sqlean.values():
        assert package["metadata_license"] == "zlib/libpng"
        assert package["license_expression"] == "Zlib"
        assert str(package["basis"]).startswith(
            "https://raw.githubusercontent.com/nalgeon/sqlean.py/"
        )


def test_lock_version_drift_requires_review_and_does_not_reuse_old_decision(
    tmp_path: Path,
) -> None:
    """lock 升级后旧版本的仓库判断立即失效。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path, version="1.1.0")
    write_policy(tmp_path, package_version="1.0.0")
    observation = write_observation(tmp_path, version="1.1.0")

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert "version drift" in result.stderr
    assert "fixture-package" in result.stderr
    assert read_report(tmp_path)["status"] == "review-required"
