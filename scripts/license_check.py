"""执行依赖、vendoring 与 service image 的可追踪许可证门禁。"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from license_check_support import (
    LICENSECHECK_VERSION,
    SCHEMA_VERSION,
    LicenseError,
    atomic_json,
    issue,
    load_toml,
    sha256_file,
)
from license_dependency_contract import check_dependencies, check_license_files
from license_dependency_inventory import observe_metadata
from license_runtime_contract import check_service_images
from license_vendoring_contract import check_vendoring


def check(*, root: Path, observation_path: Path | None, report_path: Path) -> int:
    """编排许可证、依赖、vendoring 与 runtime image 四类检查并原子落盘。"""

    policy = load_toml(root / "compliance/third-party.toml")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "input": {
            "uv_lock_sha256": sha256_file(root / "uv.lock"),
            "policy_sha256": sha256_file(root / "compliance/third-party.toml"),
            "metadata_snapshot_sha256": sha256_file(
                root / "compliance/pypi-license-observations.toml"
            )
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
    issues = check_license_files(root)
    observed = observe_metadata(root, observation_path)
    issues.extend(check_dependencies(root, policy, observed, report))
    issues.extend(check_vendoring(root, policy, report))
    issues.extend(check_service_images(root, policy, report))
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
    atomic_json(report_path, report)
    for finding in issues:
        print(finding, file=sys.stderr)
    if issues:
        return 1
    print("license-check: ok")
    return 0


def main() -> int:
    """运行许可证门禁，并保证异常路径仍生成机器可读失败报告。"""

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
            atomic_json(report, payload)
        except OSError:
            pass
        print(issue(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
