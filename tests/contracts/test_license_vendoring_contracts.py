"""Vendoring manifest 与明确 ADR 批准合同测试。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from tests.contracts.license_contract_test_support import (
    prepared_repository,
    read_report,
    run_check,
    sha256_text,
    write_policy,
)

SOURCE_SHA = "a" * 64
REVISION = "b" * 40
SUMMARY = "保持上游文件不变。"
SUMMARY_SHA = sha256_text(SUMMARY)
VENDORED_FIELDS = {
    "path": "vendor/example",
    "source_url": "https://example.invalid/source.git",
    "source_revision": REVISION,
    "source_sha256": SOURCE_SHA,
    "license_expression": "MIT",
    "license_ref": "vendor/example/LICENSE",
    "notice_ref": "NOTICE",
    "modified": False,
    "modification_summary": SUMMARY,
    "modification_summary_sha256": SUMMARY_SHA,
    "adr_ref": "docs/adr/0099-example-vendoring.md",
}
APPROVAL_FIELDS = {
    key: VENDORED_FIELDS[key]
    for key in (
        "path",
        "source_url",
        "source_revision",
        "source_sha256",
        "license_expression",
        "modified",
        "modification_summary_sha256",
    )
}


def toml_value(value: object) -> str:
    """将合同字段编码成足够覆盖本 fixture 的 TOML 标量。"""

    if isinstance(value, bool):
        return str(value).lower()
    return f'"{value}"'


def vendored_table(fields: Mapping[str, object]) -> str:
    """生成单条 vendored manifest，便于逐字段破坏。"""

    lines = ["[[vendored]]"]
    lines.extend(f"{key} = {toml_value(value)}" for key, value in fields.items())
    return "\n".join(lines)


def approval_adr(fields: Mapping[str, object], *, status: str = "Accepted") -> str:
    """生成机器可校验的具体 vendoring approval ADR。"""

    body = [f"# ADR fixture\n\n- 状态：{status}\n", "```toml vendoring_approval"]
    body.extend(f"{key} = {toml_value(value)}" for key, value in fields.items())
    body.append("```\n")
    return "\n".join(body)


def prepare_vendored_repository(tmp_path: Path) -> tuple[Path, Path]:
    """创建一份 manifest、NOTICE 和 ADR 完全一致的 vendoring fixture。"""

    root, observation = prepared_repository(tmp_path)
    vendored = root / "vendor/example"
    vendored.mkdir(parents=True)
    (vendored / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (vendored / "LICENSE").write_text("MIT\n", encoding="utf-8")
    notice = root / "NOTICE"
    notice.write_text(
        notice.read_text(encoding="utf-8") + "Vendored path: vendor/example\n",
        encoding="utf-8",
    )
    write_policy(root, vendored=vendored_table(VENDORED_FIELDS))
    adr_ref = cast(str, VENDORED_FIELDS["adr_ref"])
    (root / adr_ref).write_text(approval_adr(APPROVAL_FIELDS), encoding="utf-8")
    return root, observation


def test_undeclared_vendored_directory_reports_exact_relative_path(tmp_path: Path) -> None:
    """高风险目录没有 manifest 条目时必须精确定位。"""

    root, observation = prepared_repository(tmp_path)
    (root / "vendor/example").mkdir(parents=True)
    (root / "vendor/example/source.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "vendor/example/source.py" in result.stderr


@pytest.mark.parametrize(
    "missing_field",
    [
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
    ],
)
def test_vendored_manifest_missing_required_field_fails_closed(
    tmp_path: Path, missing_field: str
) -> None:
    """任一追踪字段缺失都不得把源码视为已声明。"""

    root, observation = prepare_vendored_repository(tmp_path)
    fields = {key: value for key, value in VENDORED_FIELDS.items() if key != missing_field}
    write_policy(root, vendored=vendored_table(fields))

    result = run_check(root, observation)

    assert result.returncode != 0
    assert missing_field in result.stderr


def test_wildcard_or_dangling_vendored_declaration_cannot_bypass_gate(tmp_path: Path) -> None:
    """通配路径和不存在路径都不是有效的一一对应清单。"""

    root, observation = prepare_vendored_repository(tmp_path)
    fields = {**VENDORED_FIELDS, "path": "vendor/*"}
    write_policy(root, vendored=vendored_table(fields))
    wildcard = run_check(root, observation)
    fields["path"] = "vendor/missing"
    write_policy(root, vendored=vendored_table(fields))
    dangling = run_check(root, observation)

    assert wildcard.returncode != 0
    assert "wildcard" in wildcard.stderr
    assert dangling.returncode != 0
    assert "does not exist" in dangling.stderr
    report_entry = cast(list[dict[str, object]], read_report(root)["vendored"])[0]
    assert set(VENDORED_FIELDS) <= set(report_entry)
    assert report_entry["path"] == "vendor/missing"
    assert "adr_status" in report_entry
    assert "approval_matches" in report_entry


@pytest.mark.parametrize(
    ("adr_ref", "expected"),
    [
        ("README.md", "docs/adr/"),
        ("docs/adr/missing.md", "does not exist"),
    ],
)
def test_adr_reference_must_stay_in_accepted_repository_adr_tree(
    tmp_path: Path, adr_ref: str, expected: str
) -> None:
    """越界或悬空 ADR 引用必须失败。"""

    root, observation = prepare_vendored_repository(tmp_path)
    write_policy(root, vendored=vendored_table({**VENDORED_FIELDS, "adr_ref": adr_ref}))

    result = run_check(root, observation)

    assert result.returncode != 0
    assert expected in result.stderr


def test_non_accepted_or_generic_adr_cannot_approve_vendoring(tmp_path: Path) -> None:
    """状态未接受或没有 approval block 的泛化 ADR 都必须失败。"""

    root, observation = prepare_vendored_repository(tmp_path)
    adr = root / cast(str, VENDORED_FIELDS["adr_ref"])
    adr.write_text(approval_adr(APPROVAL_FIELDS, status="Proposed"), encoding="utf-8")
    proposed = run_check(root, observation)
    adr.write_text("# Generic adapter ADR\n\n- 状态：Accepted\n", encoding="utf-8")
    generic = run_check(root, observation)

    assert proposed.returncode != 0
    assert "Accepted" in proposed.stderr
    assert generic.returncode != 0
    assert "vendoring_approval" in generic.stderr


@pytest.mark.parametrize("field", sorted(APPROVAL_FIELDS))
def test_adr_approval_field_mismatch_reports_exact_field(tmp_path: Path, field: str) -> None:
    """具体批准中的任一错配都要按字段诊断。"""

    root, observation = prepare_vendored_repository(tmp_path)
    mismatched = {**APPROVAL_FIELDS, field: "wrong"}
    if field == "modified":
        mismatched[field] = True
    (root / cast(str, VENDORED_FIELDS["adr_ref"])).write_text(
        approval_adr(mismatched), encoding="utf-8"
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert f"vendoring_approval.{field}" in result.stderr


def test_complete_accepted_adr_preserves_field_match_results(tmp_path: Path) -> None:
    """完整具体批准通过，并在 report 中保留逐字段匹配结果。"""

    root, observation = prepare_vendored_repository(tmp_path)

    result = run_check(root, observation)

    assert result.returncode == 0, result.stderr
    vendored = read_report(root)["vendored"]
    assert isinstance(vendored, list)
    assert vendored[0]["adr_status"] == "Accepted"
    matches = cast(dict[str, bool], vendored[0]["approval_matches"])
    assert all(matches.values())
    assert vendored[0] == {
        **VENDORED_FIELDS,
        "adr_status": "Accepted",
        "approval_matches": {field: True for field in APPROVAL_FIELDS},
    }


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [("source_url", "", "source_url"), ("modified", "false", "modified")],
)
def test_vendoring_rejects_invalid_values_even_when_adr_matches(
    tmp_path: Path,
    field: str,
    value: object,
    expected: str,
) -> None:
    """Manifest 与 ADR 一致不能掩盖空来源或伪布尔 modified。"""

    root, observation = prepare_vendored_repository(tmp_path)
    fields = {**VENDORED_FIELDS, field: value}
    approval = {key: fields[key] for key in APPROVAL_FIELDS}
    write_policy(root, vendored=vendored_table(fields))
    (root / cast(str, fields["adr_ref"])).write_text(approval_adr(approval), encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    assert expected in result.stderr


def test_vendoring_rejects_and_redacts_source_url_credentials(tmp_path: Path) -> None:
    """来源 URL 不得携带 userinfo，失败报告也不能归档其中的 credential。"""

    root, observation = prepare_vendored_repository(tmp_path)
    secret = "source-token-must-not-leak"
    source_url = f"https://maintainer:{secret}@example.invalid/source.git"
    fields = {**VENDORED_FIELDS, "source_url": source_url}
    approval = {key: fields[key] for key in APPROVAL_FIELDS}
    write_policy(root, vendored=vendored_table(fields))
    (root / cast(str, fields["adr_ref"])).write_text(approval_adr(approval), encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "source_url" in result.stderr
    report_text = (root / ".artifacts/license/license-report.json").read_text(encoding="utf-8")
    assert secret not in report_text
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "query_key",
    ["token", "api_key", "X-Amz-Credential", "X-Amz-Signature", "sig"],
)
def test_vendoring_rejects_and_redacts_source_url_query_credentials(
    tmp_path: Path,
    query_key: str,
) -> None:
    """来源 URL 的 query 同样不得携带 credential，报告与诊断都不得泄漏值。"""

    root, observation = prepare_vendored_repository(tmp_path)
    secret = "query-secret-must-not-leak"
    source_url = f"https://example.invalid/source.git?{query_key}={secret}"
    fields = {**VENDORED_FIELDS, "source_url": source_url}
    approval = {key: fields[key] for key in APPROVAL_FIELDS}
    write_policy(root, vendored=vendored_table(fields))
    (root / cast(str, fields["adr_ref"])).write_text(approval_adr(approval), encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "source_url" in result.stderr
    report_text = (root / ".artifacts/license/license-report.json").read_text(encoding="utf-8")
    assert secret not in report_text
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("field", ["path", "license_ref", "notice_ref", "adr_ref"])
def test_vendoring_rejects_and_redacts_absolute_repository_paths(
    tmp_path: Path,
    field: str,
) -> None:
    """失败记录仍会归档，但绝对路径不得进入报告或诊断。"""

    root, observation = prepare_vendored_repository(tmp_path)
    absolute_path = str(tmp_path / "private-fixture" / f"{field}.txt")
    fields = {**VENDORED_FIELDS, field: absolute_path}
    approval = {key: fields[key] for key in APPROVAL_FIELDS}
    write_policy(root, vendored=vendored_table(fields))
    if field != "adr_ref":
        (root / cast(str, fields["adr_ref"])).write_text(approval_adr(approval), encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    report_text = (root / ".artifacts/license/license-report.json").read_text(encoding="utf-8")
    assert absolute_path not in report_text
    assert absolute_path not in result.stdout + result.stderr
    assert "[INVALID REPOSITORY PATH]" in report_text


@pytest.mark.parametrize("license_expression", ["SSPL-1.0", "LicenseRef-Unknown-Custom"])
def test_vendored_license_must_follow_repository_allow_and_deny_policy(
    tmp_path: Path,
    license_expression: str,
) -> None:
    """Accepted ADR 不能放行仓库明确拒绝或未允许的 vendored license。"""

    root, observation = prepare_vendored_repository(tmp_path)
    fields = {**VENDORED_FIELDS, "license_expression": license_expression}
    approval = {key: fields[key] for key in APPROVAL_FIELDS}
    write_policy(root, vendored=vendored_table(fields))
    (root / cast(str, fields["adr_ref"])).write_text(approval_adr(approval), encoding="utf-8")

    result = run_check(root, observation)

    assert result.returncode != 0
    assert license_expression in result.stderr


def test_notice_reference_must_resolve_to_declared_notice_entry(tmp_path: Path) -> None:
    """notice_ref 必须指向仓库内存在且在 NOTICE 中可追踪的声明入口。"""

    root, observation = prepare_vendored_repository(tmp_path)
    write_policy(
        root,
        vendored=vendored_table({**VENDORED_FIELDS, "notice_ref": "NOTICE/missing.txt"}),
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "notice_ref" in result.stderr


def test_vendored_paths_must_be_unique_and_non_overlapping(tmp_path: Path) -> None:
    """同一源码不能由重复或父子路径条目重复声明。"""

    root, observation = prepare_vendored_repository(tmp_path)
    write_policy(
        root,
        vendored="\n".join([vendored_table(VENDORED_FIELDS), vendored_table(VENDORED_FIELDS)]),
    )

    result = run_check(root, observation)

    assert result.returncode != 0
    assert "overlap" in result.stderr or "duplicate" in result.stderr
