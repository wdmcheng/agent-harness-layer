"""发布合同测试共享的 fixture、CLI 调用与 loopback 替身。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from release_registry_test_support import RegistryHandler as RegistryHandler
from release_registry_test_support import loopback_server_fixture as loopback_server_fixture

ROOT = Path(__file__).resolve().parents[2]
DRY_RUN = ROOT / "scripts" / "release_dry_run.py"
PROMOTE = ROOT / "scripts" / "release_promote.py"
PUBLISH = ROOT / "scripts" / "registry_publish.py"


def build_backend_identity() -> dict[str, object]:
    """构造受审 lock 中的 Hatchling 身份；生产校验会拒绝 fixture 与合同漂移。"""

    return {
        "name": "hatchling",
        "version": "1.30.1",
        "source": {"registry": "https://pypi.org/simple"},
    }


def run(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """在隔离目录执行公开 CLI，并完整保留 stdout/stderr 供失败边界断言。"""

    merged = os.environ.copy()
    merged.update(env or {})
    return subprocess.run(args, cwd=cwd, env=merged, text=True, capture_output=True)


def git(repo: Path, *args: str) -> str:
    """操作一次性 git 仓库；固定测试身份，绝不继承维护者的全局发布配置。"""

    result = run("git", *args, cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write_release_repo(repo: Path, *, initial_message: str = "feat: initial package") -> None:
    """建立可真实构建的最小 workspace，覆盖首发、tag 基线与模板依赖边界。"""

    core = repo / "packages" / "agent-harness"
    package = core / "src" / "agent_harness"
    template = repo / "templates" / "service-app"
    package.mkdir(parents=True)
    template.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        """[project]
name = "release-fixture"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["agent-harness==0.1.0"]

[dependency-groups]
release = ["hatchling>=1.30.1,<2"]
license = []

[tool.uv]
required-version = ">=0.11.29,<0.12"
conflicts = [[{ group = "release" }, { group = "license" }]]
# Fixture 从零创建仓库时用私有 constraint 复现受审 lock 中的 backend；真实仓库
# 已有 lock preference，不应为了测试而切换全局 resolution mode。
constraint-dependencies = ["hatchling==1.30.1"]

[tool.uv.workspace]
members = ["packages/agent-harness", "templates/service-app"]

[tool.uv.sources]
agent-harness = { workspace = true }
""",
        encoding="utf-8",
    )
    (core / "pyproject.toml").write_text(
        """[project]
name = "agent-harness"
version = "0.1.0"
requires-python = ">=3.12"

[build-system]
requires = ["hatchling>=1.30.1,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agent_harness"]
""",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (template / "pyproject.toml").write_text(
        """[project]
name = "agent-harness-service-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["agent-harness==0.1.0"]

[build-system]
requires = ["hatchling>=1.30.1,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = []
""",
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
    # 默认解析策略必须与 promotion 的 refresh 保持一致，避免把策略切换误判为依赖漂移。
    locked = run(os.environ["UV"], "lock", cwd=repo)
    assert locked.returncode == 0, locked.stderr
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Release Contract")
    git(repo, "config", "user.email", "release-contract@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgSign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-m", initial_message)


def add_seeded_origin(repo: Path, bare: Path) -> None:
    """建立真实 main 默认分支与 source OID，供 promotion 在副作用前核验 origin/HEAD。"""

    git(repo.parent, "init", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "push", "origin", "HEAD:refs/heads/main")


def dry_run(repo: Path, output: Path) -> subprocess.CompletedProcess[str]:
    """以拒绝连接代理运行预演，证明版本判定不依赖 origin 或隐式联网。"""

    return run(
        sys.executable,
        str(DRY_RUN),
        "--repo",
        str(repo),
        "--output-dir",
        str(output),
        cwd=ROOT,
        env={
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        },
    )


def psr_first_version(repo: Path, config: Path, *, allow_zero: bool | None) -> str:
    """直接运行固定 PSR noop，锁住 0.x opt-in 语义而不借 wrapper 自己证明自己。"""

    zero_line = "" if allow_zero is None else f"allow_zero_version = {str(allow_zero).lower()}\n"
    config.write_text(
        "[semantic_release]\n"
        'commit_parser = "conventional"\n'
        'tag_format = "agent-harness-v{version}"\n'
        f"{zero_line}"
        'version_toml = ["packages/agent-harness/pyproject.toml:project.version"]\n\n'
        "[semantic_release.remote]\n"
        'name = "origin"\n'
        'type = "github"\n'
        'url = "https://example.invalid/agent-harness/repository.git"\n'
        "ignore_token_for_push = true\n",
        encoding="utf-8",
    )
    executable = shutil.which("semantic-release")
    assert executable is not None
    result = run(
        executable,
        "--config",
        str(config),
        "--noop",
        "version",
        "--print",
        cwd=repo,
        env={
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def artifact(path: Path, kind: str, *, relative_to: Path | None = None) -> dict[str, object]:
    """按 release schema 构造真实文件身份，避免测试用假 checksum 掩盖漂移。"""

    return {
        "path": (path.relative_to(relative_to) if relative_to is not None else path).as_posix(),
        "kind": kind,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def write_publish_inputs(root: Path, *, status: str = "promoted") -> tuple[Path, Path, Path]:
    """生成 preview、正式 build 与 promotion 回执，供 registry 网络前门禁共享。"""

    dist = root / "dist"
    dist.mkdir()
    wheel = dist / "agent_harness-0.2.0-py3-none-any.whl"
    sdist = dist / "agent_harness-0.2.0.tar.gz"
    changelog = root / "CHANGELOG.preview.md"
    notes = root / "release-notes.md"
    checksums = root / "SHA256SUMS"
    metadata = b"Metadata-Version: 2.4\nName: agent-harness\nVersion: 0.2.0\n\n"
    wheel_info = "agent_harness-0.2.0.dist-info"
    # fixture 必须是 uv 能真实解析的 distribution，不能再用任意 bytes 让自定义
    # HTTP 替身自证。固定时间戳也让同一测试输入的 checksum 可复现。
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in (
            (f"{wheel_info}/METADATA", metadata),
            (
                f"{wheel_info}/WHEEL",
                b"Wheel-Version: 1.0\nGenerator: release-contract\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n\n",
            ),
            (f"{wheel_info}/RECORD", b""),
        ):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        info = tarfile.TarInfo("agent_harness-0.2.0/PKG-INFO")
        info.size = len(metadata)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(metadata))
    sdist.write_bytes(gzip.compress(tar_buffer.getvalue(), mtime=0))
    changelog.write_text("# Changelog preview\n", encoding="utf-8")
    notes.write_text("# Release notes\n", encoding="utf-8")
    checksums.write_text(
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
        f"{hashlib.sha256(sdist.read_bytes()).hexdigest()}  {sdist.name}\n",
        encoding="utf-8",
    )
    preview_publishable = [
        artifact(wheel, "wheel", relative_to=root),
        artifact(sdist, "sdist", relative_to=root),
    ]
    all_artifacts = [
        *preview_publishable,
        artifact(changelog, "changelog", relative_to=root),
        artifact(notes, "release-notes", relative_to=root),
        artifact(checksums, "checksums", relative_to=root),
    ]
    preview: dict[str, object] = {
        "schema_version": "release-preview/v1",
        "status": "release",
        "source": {
            "commit_sha": "a" * 40,
            "dirty_diff_sha256": "b" * 64,
            "base_tag": "agent-harness-v0.1.0",
        },
        "current_version": "0.1.0",
        "next_version": "0.2.0",
        "tag": "agent-harness-v0.2.0",
        "uv_version": "0.11.29",
        "build_backend": build_backend_identity(),
        "decision": {"bump": "minor", "reason": "feat", "commits": []},
        "artifacts": all_artifacts,
    }
    preview_path = root / "manifest.json"
    preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
    formal_dist = root / "formal-dist"
    formal_dist.mkdir()
    formal_wheel = formal_dist / wheel.name
    formal_sdist = formal_dist / sdist.name
    shutil.copy2(wheel, formal_wheel)
    shutil.copy2(sdist, formal_sdist)
    formal_checksums = root / "FORMAL-SHA256SUMS"
    formal_checksums.write_text(
        f"{hashlib.sha256(formal_wheel.read_bytes()).hexdigest()}  {formal_wheel.name}\n"
        f"{hashlib.sha256(formal_sdist.read_bytes()).hexdigest()}  {formal_sdist.name}\n",
        encoding="utf-8",
    )
    formal_publishable = [
        artifact(formal_wheel, "wheel", relative_to=root),
        artifact(formal_sdist, "sdist", relative_to=root),
    ]
    build = {
        "schema_version": "release-build/v1",
        "status": "built",
        "version": "0.2.0",
        "tag": "agent-harness-v0.2.0",
        "tag_target_sha": "c" * 40,
        "uv_version": "0.11.29",
        "build_backend": build_backend_identity(),
        "artifacts": [
            *formal_publishable,
            artifact(formal_checksums, "checksums", relative_to=root),
        ],
    }
    build_path = root / "build-manifest.json"
    build_path.write_text(json.dumps(build, sort_keys=True), encoding="utf-8")
    receipt: dict[str, object] = {
        "schema_version": "release-promotion/v1",
        "status": status,
        "preview_manifest_sha256": hashlib.sha256(preview_path.read_bytes()).hexdigest(),
        "source": preview["source"],
        "version": "0.2.0",
        "artifacts": formal_publishable,
        "release_commit_sha": "c" * 40,
        "tag": "agent-harness-v0.2.0",
        "tag_target_sha": "c" * 40,
        "release_build_manifest": build_path.relative_to(root).as_posix(),
        "release_build_manifest_sha256": hashlib.sha256(build_path.read_bytes()).hexdigest(),
        "release_notes_sha256": next(
            item["sha256"] for item in all_artifacts if item["kind"] == "release-notes"
        ),
        "provider": "fixture",
        "provider_release_id": "release-1",
        "provider_release_url": "https://provider.invalid/release-1",
    }
    receipt_path = root / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return preview_path, receipt_path, formal_wheel


def registry_execute(
    preview: Path,
    receipt: Path,
    *,
    cwd: Path,
    endpoint: str,
    token: str = "fixture-token",
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """先取得受审 plan digest，再带齐本地 execute 门禁运行 registry CLI。"""

    arguments = [
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(preview),
        "--promotion-receipt",
        str(receipt),
        "--build-manifest",
        str(cwd / str(json.loads(receipt.read_text(encoding="utf-8"))["release_build_manifest"])),
    ]
    if output is not None:
        arguments.extend(["--output", str(output)])
    promotion = json.loads(receipt.read_text(encoding="utf-8"))
    protected_ref = f"refs/tags/{promotion['tag']}"
    evidence = {
        "REGISTRY_PUBLISH_APPROVED": "true",
        "RELEASE_PROTECTED_REF": "true",
        "RELEASE_PROTECTED_REF_NAME": protected_ref,
        "RELEASE_PROTECTED_REF_SHA": str(promotion["release_commit_sha"]),
        "RELEASE_TEST_MODE": "true",
        "UV_PUBLISH_URL": endpoint,
        "UV_PUBLISH_CHECK_URL": f"{endpoint}/simple",
        "UV_PUBLISH_TOKEN": token,
    }
    plan_path = cwd / ".artifacts/registry-publish/test-plan.json"
    plan = run(*arguments, "--plan-output", str(plan_path), cwd=cwd, env=evidence)
    # 负合同可能在生成可审批计划前就被 schema/checksum 门禁拒绝；这本身就是预期的
    # 网络前失败，因此直接把公开 CLI 结果交给调用方断言。
    if plan.returncode != 0:
        return plan
    approval_sha256 = str(json.loads(plan.stdout)["approval_sha256"])
    return run(
        *arguments,
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=cwd,
        env={
            **evidence,
            "REGISTRY_PUBLISH_APPROVAL_SHA256": approval_sha256,
        },
    )


def promotion_plan(
    repo: Path,
    manifest: Path,
    *,
    endpoint: str,
    protected_branch: str = "main",
) -> subprocess.CompletedProcess[str]:
    """用声明的默认保护分支与 provider endpoint 生成可人工审批的去敏计划。"""

    plan_path = repo / ".artifacts/release-promotion/test-plan.json"
    return run(
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
            "RELEASE_PROTECTED_DEFAULT_BRANCH": protected_branch,
            "RELEASE_TEST_MODE": "true",
        },
    )


def promotion_execute(
    repo: Path,
    manifest: Path,
    output_dir: Path,
    *,
    endpoint: str,
    token: str = "fixture-provider-token",
    protected_branch: str = "main",
    approval_sha256: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """复用 plan 身份执行隔离 promotion，允许负例显式覆盖 approval digest。"""

    plan = promotion_plan(
        repo,
        manifest,
        endpoint=endpoint,
        protected_branch=protected_branch,
    )
    assert plan.returncode == 0, plan.stderr
    reviewed = str(json.loads(plan.stdout)["approval_sha256"])
    plan_path = repo / ".artifacts/release-promotion/test-plan.json"
    return run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(manifest),
        "--output-dir",
        str(output_dir),
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
        env={
            "RELEASE_PROMOTION_APPROVED": "true",
            "RELEASE_PROMOTION_APPROVAL_SHA256": approval_sha256 or reviewed,
            "RELEASE_PROTECTED_REF": "true",
            "RELEASE_PROTECTED_DEFAULT_BRANCH": protected_branch,
            "RELEASE_TEST_MODE": "true",
            "RELEASE_PROVIDER_URL": endpoint,
            "RELEASE_PROVIDER_TOKEN": token,
        },
    )
