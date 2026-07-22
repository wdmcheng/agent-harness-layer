"""可发布 runtime closure、workspace 同名包与失败报告合同。"""

from __future__ import annotations

import json
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
