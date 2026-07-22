"""同名、多来源与 Git/path 依赖的许可证身份合同。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from tests.contracts.license_contract_test_support import (
    read_report,
    run_check,
    write_minimal_repository,
    write_observation,
    write_policy,
)


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
