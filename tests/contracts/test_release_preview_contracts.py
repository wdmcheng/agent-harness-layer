"""发布预演、版本计算与隔离构建行为合同。"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from release_contract_test_support import (
    ROOT,
    dry_run,
    git,
    psr_first_version,
    run,
    write_release_repo,
)

SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
RELEASE_DRY_RUN_MODULE = importlib.import_module("release_dry_run")
RELEASE_PREVIEW_BUILD_MODULE = importlib.import_module("release_preview_build")


def test_releasable_history_generates_explained_version_and_isolated_artifacts(
    tmp_path: Path,
) -> None:
    """已有 tag 后 feat 必须产生 minor preview、wheel/sdist/checksum，且原 git 身份不变。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feat(core): add feature")
    before = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "show-ref"),
        git(repo, "status", "--porcelain"),
    )
    output = repo / ".artifacts" / "release-preview" / "case"

    result = dry_run(repo, output)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "release-preview/v1"
    assert (manifest["status"], manifest["next_version"], manifest["tag"]) == (
        "release",
        "0.2.0",
        "agent-harness-v0.2.0",
    )
    assert manifest["decision"]["commits"][0]["type"] == "feat"
    assert manifest["build_backend"] == {
        "name": "hatchling",
        "version": "1.30.1",
        "source": {"registry": "https://pypi.org/simple"},
    }
    assert manifest["uv_version"] == run(os.environ["UV"], "--version", cwd=repo).stdout.split()[1]
    kinds = {item["kind"] for item in manifest["artifacts"]}
    assert {"wheel", "sdist", "changelog", "release-notes", "checksums"} <= kinds
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "show-ref"),
        git(repo, "status", "--porcelain"),
    )


def test_failed_isolated_build_preserves_original_tracked_state_and_refs(tmp_path: Path) -> None:
    """构建 backend 失败时临时副本可丢弃，原 repo 的 HEAD、tag、tracked diff 保持不变。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    core = repo / "packages/agent-harness/pyproject.toml"
    core.write_text(
        core.read_text(encoding="utf-8").replace(
            'build-backend = "hatchling.build"',
            'build-backend = "missing_backend.build"',
        ),
        encoding="utf-8",
    )
    git(repo, "add", str(core.relative_to(repo)))
    git(repo, "commit", "-m", "fix: exercise failed build boundary")
    before = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "show-ref"),
        git(repo, "status", "--porcelain"),
    )

    result = dry_run(repo, repo / ".artifacts" / "release-preview" / "failed-build")

    assert result.returncode != 0
    assert "isolated uv build failed" in result.stderr
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "show-ref"),
        git(repo, "status", "--porcelain"),
    )


def test_first_release_uses_current_version_without_double_bump(tmp_path: Path) -> None:
    """无 tag 的完整历史只允许 0.0.0→当前版本首发，不得把 0.1.0 再 bump 到 0.2.0。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    output = repo / ".artifacts" / "release-preview" / "first"

    result = dry_run(repo, output)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["next_version"] == "0.1.0"
    assert manifest["tag"] == "agent-harness-v0.1.0"
    assert manifest["source"]["base_tag"] is None


def test_psr_zero_version_requires_explicit_opt_in_for_first_release(tmp_path: Path) -> None:
    """PSR 默认拒绝 0.x 并给出 1.0.0；受控 opt-in 才允许首个 feat 计算为 0.1.0。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)

    default_version = psr_first_version(repo, tmp_path / "default.toml", allow_zero=None)
    allowed_version = psr_first_version(repo, tmp_path / "allow-zero.toml", allow_zero=True)

    assert default_version == "1.0.0"
    assert allowed_version == "0.1.0"


def test_perf_commit_matches_psr_patch_release_after_existing_tag(tmp_path: Path) -> None:
    """已有 0.1.0 tag 后 perf 必须跟随 PSR 默认 patch tag 生成可解释的 0.1.1。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "performance.txt").write_text("faster\n", encoding="utf-8")
    git(repo, "add", "performance.txt")
    git(repo, "commit", "-m", "perf(core): reduce release latency")
    psr_version = psr_first_version(repo, tmp_path / "perf.toml", allow_zero=True)
    output = repo / ".artifacts" / "release-preview" / "perf"

    result = dry_run(repo, output)

    assert psr_version == "0.1.1"
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "release"
    assert manifest["next_version"] == "0.1.1"
    assert manifest["decision"]["bump"] == "patch"
    assert manifest["decision"]["commits"][0]["type"] == "perf"
    assert "perf" in manifest["decision"]["reason"]


def test_no_release_history_succeeds_without_uv_build_or_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tag 后只有 docs 提交时明确返回 no-release，不伪造版本、tag 或构建发布物。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "docs: explain usage")
    output = repo / ".artifacts" / "release-preview" / "none"
    uv_started = tmp_path / "uv-started"
    fake_uv = tmp_path / "uv-must-not-start"
    fake_uv.write_text(
        f"#!/bin/sh\nprintf started > {uv_started}\nexit 93\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("UV", str(fake_uv))

    result = dry_run(repo, output)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "no-release"
    assert manifest["next_version"] is None and manifest["tag"] is None
    assert manifest["uv_version"] is None
    assert manifest["artifacts"] == []
    assert not uv_started.exists()
    assert not (output / "dist").exists()


def test_no_release_does_not_call_public_uv_identity_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """no-release 必须在公开 uv identity seam 之前收口，不能只保证不启动子进程。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "docs: no release identity")

    def fail_on_identity_lookup() -> tuple[str, str]:
        pytest.fail("no-release called required_uv_identity")

    monkeypatch.setattr(
        RELEASE_PREVIEW_BUILD_MODULE,
        "required_uv_identity",
        fail_on_identity_lookup,
    )
    manifest = RELEASE_DRY_RUN_MODULE.create_preview(
        repo,
        repo / ".artifacts" / "release-preview" / "no-uv-seam",
    )

    assert manifest["status"] == "no-release"
    assert manifest["uv_version"] is None


def test_psr_release_wrapper_no_release_disagreement_fails_closed(tmp_path: Path) -> None:
    """PSR 已判定发布时，本地解释器不得静默写出 no-release 成功结果。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "squashed.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "squashed.txt")
    git(repo, "commit", "-m", "* feat: squashed feature")
    psr_version = psr_first_version(repo, tmp_path / "squash.toml", allow_zero=True)
    output = repo / ".artifacts" / "release-preview" / "psr-disagreement"

    result = dry_run(repo, output)

    assert psr_version == "0.2.0"
    assert result.returncode != 0
    assert "PSR noop drift" in result.stderr
    assert not (output / "manifest.json").exists()


def test_release_dry_run_rejects_semantic_release_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATH 中伪 PSR 即使输出碰巧匹配本地算法，也必须因不是 10.6.1 在预演前失败。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    drift_bin = tmp_path / "psr-drift-bin"
    drift_bin.mkdir()
    fake = drift_bin / "semantic-release"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--print-tag*) printf 'agent-harness-v0.1.0\\n' ;;\n"
        "  *) printf '0.1.0\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{drift_bin}{os.pathsep}{os.environ['PATH']}")
    output = repo / ".artifacts" / "release-preview" / "psr-version-drift"

    result = dry_run(repo, output)

    assert result.returncode != 0
    assert "required semantic-release version is 10.6.1" in result.stderr
    assert not (output / "manifest.json").exists()


def test_no_release_ignores_predictable_tmp_symlink_without_touching_tracked_target(
    tmp_path: Path,
) -> None:
    """攻击者预置旧式可预测 tmp symlink 时，原子写不得跟随它破坏 tracked 文件。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "docs: no release")
    tracked = repo / "pyproject.toml"
    before = tracked.read_bytes()
    output = repo / ".artifacts" / "release-preview" / "tmp-symlink"
    output.mkdir(parents=True)
    (output / "manifest.json.tmp").symlink_to(tracked)

    result = dry_run(repo, output)

    assert result.returncode == 0, result.stderr
    assert tracked.read_bytes() == before
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["status"] == (
        "no-release"
    )


def test_same_identity_release_then_no_release_removes_stale_release_outputs(
    tmp_path: Path,
) -> None:
    """同一安全 output 由 release 转 no-release 时只保留新 manifest，不遗留可发布文件。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    output = repo / ".artifacts" / "release-preview" / "same-identity"
    first = dry_run(repo, output)
    assert first.returncode == 0, first.stderr
    assert (output / "dist").is_dir()
    assert (output / "CHANGELOG.preview.md").is_file()
    assert (output / "release-notes.md").is_file()
    assert (output / "SHA256SUMS").is_file()
    sentinel = output / "operator-note.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    sibling = output.parent / "unrelated-preview.txt"
    sibling.write_text("preserve\n", encoding="utf-8")
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")

    second = dry_run(repo, output)

    assert second.returncode == 0, second.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "no-release"
    assert sorted(path.name for path in output.iterdir()) == ["manifest.json", "operator-note.txt"]
    assert sibling.read_text(encoding="utf-8") == "preserve\n"


def test_real_depth_one_clone_fails_before_version_or_network(tmp_path: Path) -> None:
    """真实 depth-1 clone 必须在历史/tag 判断和 PSR 前 fail closed，wrapper 不得自行联网。"""

    origin = tmp_path / "origin"
    write_release_repo(origin)
    git(origin, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (origin / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(origin, "add", "feature.txt")
    git(origin, "commit", "-m", "feat: hidden history")
    shallow = tmp_path / "shallow"
    clone = run("git", "clone", "--depth=1", origin.as_uri(), str(shallow), cwd=tmp_path)
    assert clone.returncode == 0, clone.stderr
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"

    result = dry_run(shallow, shallow / ".artifacts" / "release-preview" / "shallow")

    assert result.returncode != 0
    assert "shallow" in (result.stdout + result.stderr).lower()
    assert not (tmp_path / "preview" / "manifest.json").exists()


def test_template_dependency_matches_project_version_while_root_keeps_workspace_override() -> None:
    """模板精确匹配项目版本，根 workspace source 仅服务 checkout 内开发解析。"""

    with (ROOT / "templates/service-app/pyproject.toml").open("rb") as stream:
        template = tomllib.load(stream)
    with (ROOT / "pyproject.toml").open("rb") as stream:
        root = tomllib.load(stream)
    assert "agent-harness==0.1.0" in template["project"]["dependencies"]
    assert root["tool"]["uv"]["sources"]["agent-harness"] == {"workspace": True}


def test_release_artifacts_have_publishable_metadata_and_install_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wheel/sdist 不携带 workspace/path source，且 wheel 可在 workspace 外无依赖安装。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    output = repo / ".artifacts" / "release-preview" / "packaging"
    result = dry_run(repo, output)
    assert result.returncode == 0, result.stderr
    wheel = next((output / "dist").glob("*.whl"))
    sdist = next((output / "dist").glob("*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    with tarfile.open(sdist, "r:gz") as archive:
        pyproject_name = next(
            name for name in archive.getnames() if name.endswith("/pyproject.toml")
        )
        packaged_pyproject = archive.extractfile(pyproject_name)
        assert packaged_pyproject is not None
        sdist_metadata = packaged_pyproject.read().decode("utf-8")
        extracted = tmp_path / "outside-source"
        archive.extractall(extracted, filter="data")
    forbidden = ("workspace = true", "file://", str(repo), "../")
    assert all(value not in metadata for value in forbidden)
    assert all(value not in sdist_metadata for value in forbidden)
    assert 'requires = ["hatchling>=1.30.1,<2"]' in sdist_metadata

    package_source = next(path for path in extracted.iterdir() if path.is_dir())
    rebuilt = run(
        os.environ["UV"],
        "build",
        str(package_source),
        "--out-dir",
        str(tmp_path / "outside-dist"),
        "--no-create-gitignore",
        cwd=tmp_path,
        env={"UV_OFFLINE": "true"},
    )
    assert rebuilt.returncode == 0, rebuilt.stderr

    # PATH 中的同名旧 uv 代表维护者机器漂移；workspace 外安装也必须继续消费
    # 已审查的 UV 绝对路径，不能因离开项目目录而绕过精确版本门禁。
    drift_bin = tmp_path / "drift-bin"
    drift_bin.mkdir()
    drift_uv = drift_bin / "uv"
    drift_uv.write_text(
        "#!/bin/sh\nprintf 'unexpected PATH uv 0.11.19\\n' >&2\nexit 91\n",
        encoding="utf-8",
    )
    drift_uv.chmod(0o700)
    monkeypatch.setenv("PATH", f"{drift_bin}{os.pathsep}{os.environ['PATH']}")

    environment = tmp_path / "outside-venv"
    created = run(
        os.environ["UV"],
        "venv",
        str(environment),
        "--python",
        sys.executable,
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stderr
    installed = run(
        os.environ["UV"],
        "pip",
        "install",
        "--python",
        str(environment / "bin/python"),
        "--no-index",
        "--no-deps",
        str(wheel),
        cwd=tmp_path,
    )
    assert installed.returncode == 0, installed.stderr
    imported = run(
        str(environment / "bin/python"),
        "-c",
        "import agent_harness; print(agent_harness.__version__)",
        cwd=tmp_path,
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "0.1.0"


def test_release_record_remains_ci_artifact_without_runtime_storage_seam() -> None:
    """ReleaseRecord 只能由 JSON manifest 表达，禁止悄悄增加 migration/model/repository/UoW。"""

    production = list((ROOT / "packages/agent-harness/src").rglob("*.py"))
    migrations = list((ROOT / "templates/service-app/migrations").rglob("*.py"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in production + migrations)
    assert "ReleaseRecord" not in text
    assert "release_records" not in text
