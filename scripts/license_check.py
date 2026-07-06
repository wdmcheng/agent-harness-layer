"""检查 license、NOTICE 和 vendored-source 基线。

本门禁只维护 Apache-2.0 项目的最低合规线：根许可证、NOTICE 入口和显式
vendored-source 目录命名。它不生成 SBOM，也不扫描依赖树；依赖许可证审计属于
后续 release/compliance 阶段。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 这些目录名代表“复制第三方源码进仓库”的高风险入口；出现时必须先记录来源、
# license 和修改说明，不能靠 NOTICE 空壳糊过去。
VENDORED_DIR_NAMES = {"third_party", "third-party", "vendor", "vendored"}


def _issue(message: str) -> str:
    return f"license-check: {message}"


def check_license_file() -> list[str]:
    """确认根 LICENSE 是 Apache-2.0 baseline。"""

    license_path = ROOT / "LICENSE"
    if not license_path.exists():
        return [_issue("LICENSE is missing.")]
    text = license_path.read_text(encoding="utf-8")
    required = ["Apache License", "Version 2.0", "http://www.apache.org/licenses/"]
    return (
        [_issue("LICENSE does not declare Apache-2.0.")]
        if not all(item in text for item in required)
        else []
    )


def check_notice_file() -> list[str]:
    """确认 NOTICE 作为第三方声明入口存在且非空。"""

    notice_path = ROOT / "NOTICE"
    if not notice_path.exists():
        return [_issue("NOTICE is missing.")]
    text = notice_path.read_text(encoding="utf-8").strip()
    return [_issue("NOTICE is empty.")] if not text else []


def check_vendored_source() -> list[str]:
    """按目录名发现未声明的 vendored source。

    这个检查故意保守：只跳过 VCS、虚拟环境、缓存和构建产物，避免真实源码副本
    因路径较深而逃过合规提示。
    """

    ignored_roots = {".git", ".venv", "__pycache__", "dist"}
    issues: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_dir():
            continue
        parts = set(path.relative_to(ROOT).parts)
        if parts & ignored_roots:
            continue
        if path.name in VENDORED_DIR_NAMES:
            issues.append(_issue(f"Undeclared vendored source directory: {path.relative_to(ROOT)}"))
    return issues


def main() -> int:
    issues = [*check_license_file(), *check_notice_file(), *check_vendored_source()]
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("license-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
