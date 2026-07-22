"""依赖许可证策略与报告 CLI 合同测试。"""

from __future__ import annotations

import json
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


def test_same_name_lock_packages_keep_distinct_version_and_source_identities(
    tmp_path: Path,
) -> None:
    """同名 lock 条目必须逐项进入运行时闭包，不能被名称索引覆盖。"""

    write_minimal_repository(tmp_path)
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        "dependencies = [\n"
        '  { name = "shared-package", version = "1.0.0", '
        'source = { registry = "https://pypi.org/simple" } },\n'
        '  { name = "shared-package", version = "2.0.0", '
        'source = { registry = "https://packages.example/simple" } },\n'
        "]\n\n"
        "[[package]]\n"
        'name = "shared-package"\n'
        'version = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        "[[package]]\n"
        'name = "shared-package"\n'
        'version = "2.0.0"\n'
        'source = { registry = "https://packages.example/simple" }\n',
        encoding="utf-8",
    )
    write_policy(tmp_path)
    policy_path = tmp_path / "compliance/third-party.toml"
    policy = (
        policy_path.read_text(encoding="utf-8")
        .replace(
            'name = "fixture-package"\nversion = "1.0.0"\n'
            'source = "registry:https://pypi.org/simple"',
            'name = "shared-package"\nversion = "1.0.0"\n'
            'source = "registry:https://pypi.org/simple"',
        )
        .replace(
            "https://pypi.org/project/fixture-package/1.0.0/",
            "https://pypi.org/project/shared-package/1.0.0/",
        )
    )
    policy += """

[[packages]]
name = "shared-package"
version = "2.0.0"
source = "registry:https://packages.example/simple"
metadata_license = "MIT"
license_expression = "MIT"
decision = "allow"
basis = "https://packages.example/shared-package/2.0.0/"
"""
    policy_path.write_text(policy, encoding="utf-8")
    observation = tmp_path / "licensecheck-observation.json"
    observation.write_text(
        json.dumps(
            {
                "info": {"program": "licensecheck", "version": "2026.0.8"},
                "packages": [
                    {"name": "shared-package", "version": "1.0.0", "license": "MIT"},
                    {"name": "shared-package", "version": "2.0.0", "license": "MIT"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    packages = cast(list[dict[str, object]], read_report(tmp_path)["packages"])
    assert [(item["name"], item["version"], item["source"]) for item in packages] == [
        ("shared-package", "1.0.0", "registry:https://pypi.org/simple"),
        ("shared-package", "2.0.0", "registry:https://packages.example/simple"),
    ]
    assert all(item["direct"] is True for item in packages)


def test_ambiguous_metadata_without_source_does_not_cross_fill_same_version_sources(
    tmp_path: Path,
) -> None:
    """观察缺少 source 时不得把一个结果复制给同版本的多个 registry identity。"""

    write_minimal_repository(tmp_path)
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        "dependencies = [\n"
        '  { name = "shared-package", version = "1.0.0", '
        'source = { registry = "https://pypi.org/simple" } },\n'
        '  { name = "shared-package", version = "1.0.0", '
        'source = { registry = "https://packages.example/simple" } },\n'
        "]\n\n"
        "[[package]]\n"
        'name = "shared-package"\n'
        'version = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        "[[package]]\n"
        'name = "shared-package"\n'
        'version = "1.0.0"\n'
        'source = { registry = "https://packages.example/simple" }\n',
        encoding="utf-8",
    )
    write_policy(tmp_path)
    policy_path = tmp_path / "compliance/third-party.toml"
    policy = (
        policy_path.read_text(encoding="utf-8")
        .replace('name = "fixture-package"', 'name = "shared-package"')
        .replace(
            "https://pypi.org/project/fixture-package/1.0.0/",
            "https://pypi.org/project/shared-package/1.0.0/",
        )
    )
    policy += """

[[packages]]
name = "shared-package"
version = "1.0.0"
source = "registry:https://packages.example/simple"
metadata_license = "MIT"
license_expression = "MIT"
decision = "allow"
basis = "https://packages.example/shared-package/1.0.0/"
"""
    policy_path.write_text(policy, encoding="utf-8")
    observation = write_observation(tmp_path, license_name="MIT")
    observation.write_text(
        observation.read_text(encoding="utf-8").replace('"fixture-package"', '"shared-package"'),
        encoding="utf-8",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    assert result.stderr.count("metadata license drift") == 2


def test_published_runtime_optional_dependencies_enter_inventory(tmp_path: Path) -> None:
    """可发布包声明的 optional runtime 依赖也必须进入直接依赖和完整闭包。"""

    write_minimal_repository(tmp_path)
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        "dependencies = []\n\n"
        "[package.optional-dependencies]\n"
        'observability = [{ name = "fixture-package" }]\n\n'
        "[[package]]\n"
        'name = "fixture-package"\n'
        'version = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    write_policy(tmp_path)
    observation = write_observation(tmp_path)

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    packages = cast(list[dict[str, object]], read_report(tmp_path)["packages"])
    assert [(item["name"], item["direct"]) for item in packages] == [("fixture-package", True)]


def test_git_url_and_path_sources_have_distinct_stable_identities(tmp_path: Path) -> None:
    """非 registry source 也必须稳定区分，不能坍缩成同一个 unknown identity。"""

    write_minimal_repository(tmp_path)
    sources = (
        ("git", "https://example.com/shared.git#1111111"),
        ("url", "https://example.com/shared-1.0.0.tar.gz"),
        ("path", "vendor/shared-package"),
    )
    dependencies = ",\n".join(
        f'  {{ name = "shared-package", version = "1.0.0", source = {{ {kind} = "{value}" }} }}'
        for kind, value in sources
    )
    packages = "\n".join(
        "[[package]]\n"
        'name = "shared-package"\n'
        'version = "1.0.0"\n'
        f'source = {{ {kind} = "{value}" }}\n'
        for kind, value in sources
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        f"dependencies = [\n{dependencies},\n]\n\n"
        f"{packages}",
        encoding="utf-8",
    )
    write_policy(tmp_path)
    policy_path = tmp_path / "compliance/third-party.toml"
    policy = policy_path.read_text(encoding="utf-8").replace(
        'name = "fixture-package"\nversion = "1.0.0"\nsource = "registry:https://pypi.org/simple"',
        f'name = "shared-package"\nversion = "1.0.0"\nsource = "git:{sources[0][1]}"',
    )
    policy = policy.replace(
        "https://pypi.org/project/fixture-package/1.0.0/",
        "https://example.com/shared.git",
    )
    for kind, value in sources[1:]:
        policy += f'''\n\n[[packages]]
name = "shared-package"
version = "1.0.0"
source = "{kind}:{value}"
metadata_license = "MIT"
license_expression = "MIT"
decision = "allow"
basis = "https://example.com/license/shared-package"
'''
    policy_path.write_text(policy, encoding="utf-8")
    observation = tmp_path / "licensecheck-observation.json"
    observation.write_text(
        json.dumps(
            {
                "info": {"program": "licensecheck", "version": "2026.0.8"},
                "packages": [
                    {
                        "name": "shared-package",
                        "version": "1.0.0",
                        "source": f"{kind}:{value}",
                        "license": "MIT",
                    }
                    for kind, value in sources
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    report_packages = cast(list[dict[str, object]], read_report(tmp_path)["packages"])
    assert {item["source"] for item in report_packages} == {
        f"{kind}:{value}" for kind, value in sources
    }
    assert all(item["direct"] is True for item in report_packages)


@pytest.mark.parametrize(
    ("source_kind", "source_location"),
    [
        ("editable", "vendor/editable-package"),
        ("virtual", "vendor/virtual-package"),
    ],
)
def test_editable_and_virtual_runtime_dependencies_enter_policy_inventory(
    tmp_path: Path,
    source_kind: str,
    source_location: str,
) -> None:
    """workspace 根可排除，但其 editable/virtual 第三方依赖不能被静默跳过。"""

    write_minimal_repository(tmp_path)
    source = f"{source_kind}:{source_location}"
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        "dependencies = [\n"
        f'  {{ name = "fixture-package", version = "1.0.0", '
        f'source = {{ {source_kind} = "{source_location}" }} }},\n'
        "]\n\n"
        "[[package]]\n"
        'name = "fixture-package"\n'
        'version = "1.0.0"\n'
        f'source = {{ {source_kind} = "{source_location}" }}\n',
        encoding="utf-8",
    )
    write_policy(tmp_path)
    policy_path = tmp_path / "compliance/third-party.toml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            'source = "registry:https://pypi.org/simple"',
            f'source = "{source}"',
            1,
        ),
        encoding="utf-8",
    )
    observation = tmp_path / "licensecheck-observation.json"
    observation.write_text(
        json.dumps(
            {
                "info": {"program": "licensecheck", "version": "2026.0.8"},
                "packages": [
                    {
                        "name": "fixture-package",
                        "version": "1.0.0",
                        "source": source,
                        "license": "MIT",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    packages = cast(list[dict[str, object]], read_report(tmp_path)["packages"])
    assert [(item["name"], item["source"], item["direct"]) for item in packages] == [
        ("fixture-package", source, True)
    ]


@pytest.mark.parametrize("relationship", ["direct", "transitive"])
@pytest.mark.parametrize(
    ("source_kind", "source_location"),
    [
        ("registry", "https://third-party.example/simple"),
        ("git", "https://third-party.example/agent-harness.git#1111111"),
        ("url", "https://third-party.example/agent-harness-9.9.9.tar.gz"),
        ("path", "vendor/third-party-agent-harness"),
        ("editable", "vendor/editable-agent-harness"),
        ("virtual", "vendor/virtual-agent-harness"),
    ],
)
def test_workspace_root_name_collision_keeps_third_party_identity_in_inventory(
    tmp_path: Path,
    relationship: str,
    source_kind: str,
    source_location: str,
) -> None:
    """只有实际 workspace identity 可排除，同名第三方 direct/transitive 均须检查。"""

    write_minimal_repository(tmp_path)
    source = f"{source_kind}:{source_location}"
    root_dependency = (
        f'{{ name = "agent-harness", version = "9.9.9", '
        f'source = {{ {source_kind} = "{source_location}" }} }}'
        if relationship == "direct"
        else '{ name = "bridge-package" }'
    )
    bridge = ""
    if relationship == "transitive":
        bridge = (
            "\n[[package]]\n"
            'name = "bridge-package"\n'
            'version = "1.0.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            "dependencies = [\n"
            f'  {{ name = "agent-harness", version = "9.9.9", '
            f'source = {{ {source_kind} = "{source_location}" }} }},\n'
            "]\n"
        )
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        f"dependencies = [{root_dependency}]\n\n"
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "9.9.9"\n'
        f'source = {{ {source_kind} = "{source_location}" }}\n'
        f"{bridge}",
        encoding="utf-8",
    )
    write_policy(tmp_path)
    policy_path = tmp_path / "compliance/third-party.toml"
    policy = policy_path.read_text(encoding="utf-8").replace(
        'name = "fixture-package"\nversion = "1.0.0"\nsource = "registry:https://pypi.org/simple"',
        f'name = "agent-harness"\nversion = "9.9.9"\nsource = "{source}"',
    )
    policy = policy.replace(
        "https://pypi.org/project/fixture-package/1.0.0/",
        "https://third-party.example/licenses/agent-harness-9.9.9",
    )
    if relationship == "transitive":
        policy += """

[[packages]]
name = "bridge-package"
version = "1.0.0"
source = "registry:https://pypi.org/simple"
metadata_license = "MIT"
license_expression = "MIT"
decision = "allow"
basis = "https://pypi.org/project/bridge-package/1.0.0/"
"""
    policy_path.write_text(policy, encoding="utf-8")
    observed_packages = [
        {
            "name": "agent-harness",
            "version": "9.9.9",
            "source": source,
            "license": "MIT",
        }
    ]
    if relationship == "transitive":
        observed_packages.append(
            {
                "name": "bridge-package",
                "version": "1.0.0",
                "source": "registry:https://pypi.org/simple",
                "license": "MIT",
            }
        )
    observation = tmp_path / "licensecheck-observation.json"
    observation.write_text(
        json.dumps(
            {
                "info": {"program": "licensecheck", "version": "2026.0.8"},
                "packages": observed_packages,
            }
        ),
        encoding="utf-8",
    )

    result = run_check(tmp_path, observation)

    assert result.returncode == 0, result.stderr
    packages = cast(list[dict[str, object]], read_report(tmp_path)["packages"])
    matching = [
        item
        for item in packages
        if item["name"] == "agent-harness"
        and item["version"] == "9.9.9"
        and item["source"] == source
    ]
    assert len(matching) == 1
    assert matching[0]["direct"] is (relationship == "direct")


def test_failure_atomically_writes_sanitized_report(tmp_path: Path) -> None:
    """失败也必须写完整报告，且不得泄漏凭据或临时仓库绝对路径。"""

    root, observation = prepared_repository(tmp_path)
    write_lock(root, version="9.9.9")

    result = run_check(root, observation)

    assert result.returncode != 0
    report_path = root / ".artifacts/license/license-report.json"
    assert report_path.exists()
    payload = report_path.read_text(encoding="utf-8")
    report = json.loads(payload)
    assert report["schema_version"] == "license-report/v1"
    assert str(root) not in payload
    assert "credential" not in payload.lower()
    assert not list(report_path.parent.glob("*.tmp"))


def test_invalid_policy_decision_cannot_leave_report_status_pass(tmp_path: Path) -> None:
    """任何 findings 都必须让 report 顶层 status 离开 pass。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path, decision="not-a-decision")
    observation = write_observation(tmp_path)

    result = run_check(tmp_path, observation)

    assert result.returncode != 0
    report = read_report(tmp_path)
    assert report["findings"]
    assert report["status"] in {"fail", "review-required"}
    assert report["status"] != "pass"
