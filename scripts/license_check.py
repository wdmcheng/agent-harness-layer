"""Check the Phase 1 license and NOTICE baseline."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORED_DIR_NAMES = {"third_party", "third-party", "vendor", "vendored"}


def _issue(message: str) -> str:
    return f"license-check: {message}"


def check_license_file() -> list[str]:
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
    notice_path = ROOT / "NOTICE"
    if not notice_path.exists():
        return [_issue("NOTICE is missing.")]
    text = notice_path.read_text(encoding="utf-8").strip()
    return [_issue("NOTICE is empty.")] if not text else []


def check_vendored_source() -> list[str]:
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
