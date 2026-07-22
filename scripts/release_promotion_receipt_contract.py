"""构造 promotion 各终态共享的受审输入身份。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from release_models import ReleaseContractError, sha256_file


def base_receipt(preview: dict[str, Any], manifest: Path) -> dict[str, Any]:
    """建立所有终态共享身份，failed/no-release 也不能丢掉受审输入。"""

    artifacts_raw = preview.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise ReleaseContractError("preview artifacts must be a list")
    publishable: list[dict[str, Any]] = []
    for raw in cast(list[object], artifacts_raw):
        if isinstance(raw, dict):
            item = cast(dict[str, Any], raw)
            if item.get("kind") in {"wheel", "sdist"}:
                publishable.append(item)
    return {
        "schema_version": "release-promotion/v1",
        "preview_manifest_sha256": sha256_file(manifest),
        "source": preview["source"],
        "version": preview.get("next_version") or preview["current_version"],
        # preview distribution 只用于审批和可复现性对照，正式回执必须在 tag 后
        # 用 release-build/v1 覆盖该字段，避免下游误发 dry-run 产物。
        "artifacts": publishable,
    }


__all__ = ["base_receipt"]
