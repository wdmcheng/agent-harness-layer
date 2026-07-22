"""按已验证 promotion plan 生成最小 GitLab child pipeline。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from release_models import ReleaseContractError, validate_promotion_plan

NO_RELEASE_PIPELINE = """include:
  - local: .gitlab/release-child.yml

promote-no-release:
  extends: .release-promote-no-release
"""

PLANNED_PIPELINE = """include:
  - local: .gitlab/release-child.yml

promote-execute:
  extends: .release-promote-execute

publish-plan:
  extends: .release-publish-plan

publish-execute:
  extends: .release-publish-execute
"""


def generate(plan_path: Path, output_path: Path) -> None:
    """只让 planned child 实例化受保护 job；no-release child 不含这些节点。"""

    raw: object = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ReleaseContractError("promotion plan must be a JSON object")
    plan = cast(dict[str, Any], raw)
    validate_promotion_plan(plan)
    rendered = NO_RELEASE_PIPELINE if plan["status"] == "no-release" else PLANNED_PIPELINE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        generate(args.plan, args.output)
    except (OSError, json.JSONDecodeError, ReleaseContractError) as exc:
        print(f"release-gitlab-pipeline: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
