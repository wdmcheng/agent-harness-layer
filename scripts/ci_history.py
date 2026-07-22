"""在 release wrapper 前拒绝 shallow checkout，并验证显式预期 tag 可见。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


class HistoryError(RuntimeError):
    """表示当前 Git history/tags 不能作为发布版本计算输入。"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise HistoryError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def validate(repo: Path, expected_tag: str | None) -> None:
    """只验证 clone 完整性；版本决策仍由 release change 的公开 CLI 负责。"""

    repo = repo.resolve()
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise HistoryError("shallow repository is not a valid release input")
    _git(repo, "rev-parse", "HEAD")
    if expected_tag:
        visible = _git(repo, "tag", "--list", expected_tag).splitlines()
        if expected_tag not in visible:
            raise HistoryError(f"expected release tag is not visible: {expected_tag}")


def main() -> int:
    """解析 history guard 参数，并以机器稳定消息报告完整性结果。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--expected-tag")
    args = parser.parse_args()
    try:
        validate(args.repo, args.expected_tag)
    except HistoryError as exc:
        print(f"ci-history: {exc}", file=sys.stderr)
        return 2
    print("ci-history: history=complete tags=visible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
