"""uv patch 支持范围与单次发布身份合同。"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SUPPORT = importlib.import_module("release_contract_support")
RELEASE_CONTRACT_ERROR = cast(type[Exception], vars(SUPPORT)["ReleaseContractError"])
VALIDATE_PREVIEW = cast(
    Callable[[dict[str, object]], None],
    vars(importlib.import_module("release_preview_contract"))["validate_preview"],
)


def _fake_uv(tmp_path: Path, output: str, *, exit_code: int = 0) -> Path:
    """创建只实现 `uv --version` 的公开 CLI fixture。"""

    executable = tmp_path / "uv"
    executable.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize("version", ["0.11.29", "0.11.31"])
def test_required_uv_identity_accepts_supported_patch_and_returns_actual_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    """共享 seam 返回实际 executable/version，不把支持下界写成证据。"""

    executable = _fake_uv(tmp_path, f"uv {version} (fixture metadata)")
    monkeypatch.setenv("UV", str(executable))
    required_uv_identity = cast(
        Callable[[], tuple[str, str]],
        vars(SUPPORT)["required_uv_identity"],
    )

    assert required_uv_identity() == (str(executable), version)


@pytest.mark.parametrize(
    "output",
    ["uv 0.11.28", "uv 0.12.0", "uv 0.11", "not-uv 0.11.31", "uv next"],
)
def test_required_uv_identity_rejects_outside_or_malformed_version_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    """范围外或畸形输出必须在调用任何 build/publish seam 前 fail closed。"""

    executable = _fake_uv(tmp_path, output)
    monkeypatch.setenv("UV", str(executable))
    required_uv_identity = cast(
        Callable[[], tuple[str, str]],
        vars(SUPPORT)["required_uv_identity"],
    )

    with pytest.raises(RELEASE_CONTRACT_ERROR, match="required uv version range"):
        required_uv_identity()


def _preview(status: str, uv_version: object) -> dict[str, object]:
    """构造最小 preview consumer 输入，单独验证状态化 uv identity。"""

    release = status == "release"
    return {
        "schema_version": "release-preview/v1",
        "status": status,
        "source": {
            "commit_sha": "c" * 40,
            "dirty_diff_sha256": "d" * 64,
            "base_tag": "agent-harness-v0.1.0",
        },
        "current_version": "0.1.0",
        "next_version": "0.2.0" if release else None,
        "tag": "agent-harness-v0.2.0" if release else None,
        "uv_version": uv_version,
        "build_backend": {
            "name": "hatchling",
            "version": "1.30.1",
            "source": {"registry": "https://pypi.org/simple"},
        },
        "decision": {
            "bump": "minor" if release else None,
            "reason": "fixture decision",
            "commits": [],
        },
        "artifacts": (
            [
                {"kind": "wheel", "path": "dist/package.whl"},
                {"kind": "sdist", "path": "dist/package.tar.gz"},
                {"kind": "changelog", "path": "CHANGELOG.preview.md"},
                {"kind": "release-notes", "path": "release-notes.md"},
                {"kind": "checksums", "path": "SHA256SUMS"},
            ]
            if release
            else []
        ),
    }


def test_preview_consumer_accepts_supported_actual_patch() -> None:
    """release preview consumer 接受范围内实际版本。"""

    VALIDATE_PREVIEW(_preview("release", "0.11.31"))


def test_no_release_preview_consumer_requires_null_uv_identity() -> None:
    """no-release 没有 uv 执行身份，非空版本不得被 consumer 接受。"""

    with pytest.raises(RELEASE_CONTRACT_ERROR, match="no-release"):
        VALIDATE_PREVIEW(_preview("no-release", "0.11.29"))
