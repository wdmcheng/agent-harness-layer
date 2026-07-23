"""发布计划、tag 后正式构建与 registry 交接的公开 CLI 合同。"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tarfile
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, cast

import pytest
from release_contract_test_support import (
    PROMOTE,
    PUBLISH,
    ROOT,
    RegistryHandler,
    add_seeded_origin,
    dry_run,
    git,
    loopback_server_fixture,
    promotion_execute,
    run,
    write_release_repo,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
RELEASE_BUILD_MODULE = importlib.import_module("release_build")
BUILD_RELEASE = cast(Callable[..., dict[str, Any]], vars(RELEASE_BUILD_MODULE)["build_release"])
VERIFY_SDIST = cast(
    Callable[[Path, str], None],
    vars(RELEASE_BUILD_MODULE)["_verify_sdist_metadata"],
)
RELEASE_CONTRACT_ERROR = cast(
    type[Exception],
    vars(importlib.import_module("release_models"))["ReleaseContractError"],
)
VALIDATE_RELEASE_BUILD = cast(
    Callable[[dict[str, Any]], None],
    vars(importlib.import_module("release_build_contract"))["validate_release_build"],
)


@pytest.fixture
def loopback_server() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """复用无外部副作用的 provider/registry loopback 替身。"""

    yield from loopback_server_fixture()


def _release_preview(repo: Path) -> Path:
    """建立带历史 tag 的可发布 preview，避免首发规则干扰交接断言。"""

    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "feature.txt").write_text("four-stage release\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feat: four-stage release")
    output = repo / ".artifacts" / "release-preview" / "four-stage"
    result = dry_run(repo, output)
    assert result.returncode == 0, result.stderr
    return output / "manifest.json"


def _write_formal_sdist(path: Path, *, version: str, workspace_source: bool) -> None:
    """写入最小正式 sdist，用于隔离验证包内 metadata，而不是文件名。"""

    root = "agent_harness-0.2.0"
    pyproject = '[project]\nname = "agent-harness"\nversion = "0.2.0"\n'
    if workspace_source:
        pyproject += '\n[tool.uv.sources]\nagent-harness = { path = "../agent-harness" }\n'
    members = {
        f"{root}/PKG-INFO": f"Metadata-Version: 2.4\nName: agent-harness\nVersion: {version}\n",
        f"{root}/pyproject.toml": pyproject,
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, value in members.items():
            payload = value.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize(
    ("version", "workspace_source", "expected"),
    [("9.9.9", False, "version"), ("0.2.0", True, "workspace path dependency")],
)
def test_formal_sdist_rejects_version_or_workspace_dependency_drift(
    tmp_path: Path,
    version: str,
    workspace_source: bool,
    expected: str,
) -> None:
    """正式 sdist 必须独立验证内容，不能由正常 wheel 或文件名代替。"""

    sdist = tmp_path / "agent_harness-0.2.0.tar.gz"
    _write_formal_sdist(sdist, version=version, workspace_source=workspace_source)

    with pytest.raises(RELEASE_CONTRACT_ERROR, match=expected):
        VERIFY_SDIST(sdist, "0.2.0")


def test_formal_build_rejects_symlinked_release_root_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式构建不得把仓库内 release-build symlink 当成仓库外可清理目录。"""

    repo = tmp_path / "repo"
    artifacts = repo / ".artifacts"
    artifacts.mkdir(parents=True)
    external = tmp_path / "external-builds"
    output = external / "execute"
    output.mkdir(parents=True)
    sentinel = output / "must-survive.txt"
    sentinel.write_text("outside repository\n", encoding="utf-8")
    (artifacts / "release-build").symlink_to(external, target_is_directory=True)

    def fake_run_git(_repo: Path, *args: str) -> str:
        """只提供到达输出清理边界所需的 tag identity。"""

        return "tag" if args[:2] == ("cat-file", "-t") else "c" * 40

    def fail_build_command(_arguments: list[str], *, cwd: Path) -> None:
        """安全输出检查必须先于任何 clone 或 build 子进程。"""

        del cwd
        pytest.fail("unsafe output reached the build command")

    monkeypatch.setattr(
        RELEASE_BUILD_MODULE,
        "run_git",
        fake_run_git,
    )
    monkeypatch.setattr(
        RELEASE_BUILD_MODULE,
        "required_uv_identity",
        lambda: ("uv", "0.11.29"),
    )
    monkeypatch.setattr(
        RELEASE_BUILD_MODULE,
        "_run",
        fail_build_command,
    )

    with pytest.raises(RELEASE_CONTRACT_ERROR, match="unsafe|symlink"):
        BUILD_RELEASE(
            repo=repo,
            tag="agent-harness-v0.2.0",
            expected_version="0.2.0",
            expected_target="c" * 40,
            output=artifacts / "release-build" / "execute",
        )

    assert sentinel.read_text(encoding="utf-8") == "outside repository\n"


def test_promotion_plan_is_atomically_persisted_with_planned_status(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """只读 plan job 必须产出可交接 artifact，不能只把临时 JSON 打到 stdout。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    manifest = _release_preview(repo)
    add_seeded_origin(repo, tmp_path / "remote.git")
    plan_path = repo / ".artifacts" / "release-promotion" / "plan.json"

    result = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(manifest),
        "--plan-output",
        str(plan_path),
        cwd=ROOT,
        env={
            "RELEASE_PROVIDER_URL": endpoint,
            "RELEASE_PROTECTED_DEFAULT_BRANCH": "main",
            "RELEASE_TEST_MODE": "true",
        },
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan == json.loads(result.stdout)
    assert plan["schema_version"] == "release-promotion-plan/v1"
    assert plan["status"] == "planned"
    assert len(plan["approval_sha256"]) == 64
    assert handler.requests == []
    assert list(plan_path.parent.glob(f".{plan_path.name}.*.tmp")) == []


def test_no_release_promotion_plan_is_persisted_for_ci_noop(tmp_path: Path) -> None:
    """合法 no-release 也必须落 plan artifact，不能被 CI 当成缺失产物。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "docs: no release")
    preview = repo / ".artifacts" / "release-preview" / "none-plan"
    assert dry_run(repo, preview).returncode == 0
    plan_path = repo / ".artifacts" / "release-promotion" / "plan.json"

    result = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(preview / "manifest.json"),
        "--plan-output",
        str(plan_path),
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["schema_version"] == "release-promotion-plan/v1"
    assert plan["status"] == "no-release"
    assert plan["tag"] is None
    assert "approval_sha256" not in plan


def test_promotion_rebuilds_from_tag_and_registry_plans_only_formal_artifacts(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """promotion 必须从 tag target 生成 built manifest，registry 不得复用 preview 包。"""

    endpoint, _handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    manifest = _release_preview(repo)
    add_seeded_origin(repo, tmp_path / "remote.git")
    receipt_dir = repo / ".artifacts" / "release-promotion" / "four-stage"

    promoted = promotion_execute(
        repo,
        manifest,
        receipt_dir,
        endpoint=endpoint,
    )
    assert promoted.returncode == 0, promoted.stderr
    receipt_path = receipt_dir / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    build_path = repo / ".artifacts" / "release-build" / "four-stage" / "manifest.json"
    build = json.loads(build_path.read_text(encoding="utf-8"))
    assert build["schema_version"] == "release-build/v1"
    assert build["status"] == "built"
    assert build["build_backend"] == {
        "name": "hatchling",
        "version": "1.30.1",
        "source": {"registry": "https://pypi.org/simple"},
    }
    assert build["tag_target_sha"] == receipt["release_commit_sha"]
    assert receipt["release_build_manifest_sha256"]
    assert receipt["artifacts"] == [
        item for item in build["artifacts"] if item["kind"] in {"wheel", "sdist"}
    ]

    plan_path = repo / ".artifacts" / "registry-publish" / "plan.json"
    promotion = json.loads(receipt_path.read_text(encoding="utf-8"))
    result = run(
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(manifest),
        "--promotion-receipt",
        str(receipt_path),
        "--build-manifest",
        str(build_path),
        "--artifact-root",
        str(repo),
        "--plan-output",
        str(plan_path),
        cwd=ROOT,
        env={
            "RELEASE_TEST_MODE": "true",
            "UV_PUBLISH_URL": endpoint,
            "UV_PUBLISH_CHECK_URL": f"{endpoint}/simple",
            "RELEASE_PROTECTED_REF_NAME": f"refs/tags/{promotion['tag']}",
            "RELEASE_PROTECTED_REF_SHA": promotion["release_commit_sha"],
        },
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["schema_version"] == "registry-publish-plan/v1"
    assert plan["status"] == "planned"
    actual_uv = run(os.environ["UV"], "--version", cwd=ROOT).stdout.split()[1]
    assert plan["uv_version"] == actual_uv
    assert plan["approval"]["uv_version"] == actual_uv
    assert plan["artifacts"] == receipt["artifacts"]
    preview = json.loads(manifest.read_text(encoding="utf-8"))
    assert plan["artifacts"] != [
        item for item in preview["artifacts"] if item["kind"] in {"wheel", "sdist"}
    ]


@pytest.mark.parametrize(
    "backend",
    [
        None,
        {
            "name": "hatchling",
            "version": "1.31.0",
            "source": {"registry": "https://pypi.org/simple"},
        },
    ],
)
def test_formal_build_manifest_rejects_missing_or_drifted_backend_identity(
    backend: dict[str, object] | None,
) -> None:
    """正式 build consumer 必须在授权产物前核对 lock 内 Hatchling identity。"""

    build: dict[str, Any] = {
        "schema_version": "release-build/v1",
        "status": "built",
        "version": "0.2.0",
        "tag": "agent-harness-v0.2.0",
        "tag_target_sha": "c" * 40,
        "uv_version": "0.11.29",
        "artifacts": [
            {"kind": "wheel"},
            {"kind": "sdist"},
            {"kind": "checksums"},
        ],
    }
    if backend is not None:
        build["build_backend"] = backend

    with pytest.raises(RELEASE_CONTRACT_ERROR, match="build backend"):
        VALIDATE_RELEASE_BUILD(build)


def test_formal_build_consumer_accepts_supported_newer_uv_patch() -> None:
    """正式构建 consumer 接受范围内实际 patch，而不是把 CI 选择当唯一版本。"""

    build: dict[str, Any] = {
        "schema_version": "release-build/v1",
        "status": "built",
        "version": "0.2.0",
        "tag": "agent-harness-v0.2.0",
        "tag_target_sha": "c" * 40,
        "uv_version": "0.11.31",
        "build_backend": {
            "name": "hatchling",
            "version": "1.30.1",
            "source": {"registry": "https://pypi.org/simple"},
        },
        "artifacts": [
            {"kind": "wheel"},
            {"kind": "sdist"},
            {"kind": "checksums"},
        ],
    }

    VALIDATE_RELEASE_BUILD(build)


@pytest.mark.parametrize("status", [None, "planned", "failed"])
def test_registry_rejects_missing_or_non_built_manifest_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    status: str | None,
) -> None:
    """正式 build 状态缺失或非法时，registry 必须在读取 credential 和联网前失败。"""

    endpoint, handler = loopback_server
    build: dict[str, Any] = {
        "schema_version": "release-build/v1",
        "version": "0.2.0",
        "tag": "agent-harness-v0.2.0",
        "tag_target_sha": "c" * 40,
        "uv_version": "0.11.29",
        "artifacts": [],
    }
    if status is not None:
        build["status"] = status
    build_path = tmp_path / "build.json"
    build_path.write_text(json.dumps(build), encoding="utf-8")

    result = run(
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(tmp_path / "missing-preview.json"),
        "--promotion-receipt",
        str(tmp_path / "missing-receipt.json"),
        "--build-manifest",
        str(build_path),
        cwd=ROOT,
        env={
            "UV_PUBLISH_URL": endpoint,
            "UV_PUBLISH_CHECK_URL": f"{endpoint}/simple",
            "UV_PUBLISH_TOKEN": "must-not-be-read",
        },
    )

    assert result.returncode != 0
    assert "status must be built" in result.stderr
    assert "must-not-be-read" not in result.stdout + result.stderr
    assert handler.requests == []
