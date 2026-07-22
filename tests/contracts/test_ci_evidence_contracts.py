"""CI evidence runner 的公开 CLI 合同。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "ci_evidence.py"


def _git(repo: Path, *args: str) -> str:
    """在隔离仓库建立可复核输入身份，不继承生产发布状态。"""

    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write_repo(repo: Path) -> None:
    """建立只暴露 allowlisted Make target 的最小 CLI fixture。"""

    (repo / "Makefile").write_text(
        "ruff-format:\n"
        "\t@mkdir -p reports\n"
        "\t@printf 'format-ok\\n'\n"
        "\t@printf 'report-body' > reports/format.txt\n"
        "ruff-lint:\n"
        "\t@printf 'lint-failed\\n' >&2\n"
        "\t@false\n"
        "pyright:\n"
        "\t@test ! -e .artifacts/ci/pyright/result.json\n"
        "\t@printf 'stale-result-hidden\\n'\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "CI Evidence Contract")
    _git(repo, "config", "user.email", "ci-evidence@example.invalid")
    # 临时仓库只验证 evidence identity，不应依赖宿主机是否启动 GPG agent。
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgSign", "false")
    _git(repo, "add", "Makefile")
    _git(repo, "commit", "-m", "test: seed ci evidence fixture")


def _run(repo: Path, gate: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), "--repo", str(repo), "--gate", gate, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _result(repo: Path, gate: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((repo / ".artifacts" / "ci" / gate / "result.json").read_text()),
    )


def test_success_writes_ci_result_log_and_relative_artifact_identity(tmp_path: Path) -> None:
    """成功 gate 仍以 commit/diff 和文件摘要绑定结果，不能只留绿色退出码。"""

    _write_repo(tmp_path)
    completed = _run(tmp_path, "ruff-format", "--artifact", "report=reports/format.txt")

    assert completed.returncode == 0, completed.stderr
    result = _result(tmp_path, "ruff-format")
    assert result["schema_version"] == "ci-result/v1"
    assert result["status"] == "pass"
    assert result["exit_code"] == 0
    assert result["command"] == ["make", "--no-print-directory", "ruff-format"]
    identity = cast(dict[str, str], result["input_identity"])
    assert identity["commit_sha"] == _git(tmp_path, "rev-parse", "HEAD")
    assert identity["dirty_diff_sha256"] == hashlib.sha256(b"").hexdigest()
    artifacts = cast(list[dict[str, object]], result["artifacts"])
    assert {item["path"] for item in artifacts} == {
        ".artifacts/ci/ruff-format/command.log",
        "reports/format.txt",
    }
    assert all(item["producer_gate"] == "ruff-format" for item in artifacts)
    assert all(not Path(str(item["path"])).is_absolute() for item in artifacts)
    assert (
        "format-ok" in (tmp_path / ".artifacts" / "ci" / "ruff-format" / "command.log").read_text()
    )


def test_failure_still_writes_result_and_returns_make_exit_code(tmp_path: Path) -> None:
    """失败诊断必须先原子落盘，再把被执行 make 进程的状态原样返回。"""

    _write_repo(tmp_path)
    completed = _run(tmp_path, "ruff-lint")

    assert completed.returncode != 0
    result = _result(tmp_path, "ruff-lint")
    assert result["status"] == "fail"
    assert result["exit_code"] == completed.returncode
    assert (
        "lint-failed" in (tmp_path / ".artifacts" / "ci" / "ruff-lint" / "command.log").read_text()
    )


def test_runner_hides_stale_result_while_same_gate_is_running(tmp_path: Path) -> None:
    """同名 gate 重跑时先撤下旧结果，避免运行中的消费者读取过期身份。"""

    _write_repo(tmp_path)
    stale = tmp_path / ".artifacts" / "ci" / "pyright" / "result.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"status":"pass","input_identity":{"dirty_diff_sha256":"stale"}}\n')

    completed = _run(tmp_path, "pyright")

    assert completed.returncode == 0, completed.stderr
    result = _result(tmp_path, "pyright")
    assert result["status"] == "pass"
    assert (
        "stale-result-hidden"
        in (tmp_path / ".artifacts" / "ci" / "pyright" / "command.log").read_text()
    )


def test_runner_rejects_unknown_gate_path_escape_and_credential_artifact(tmp_path: Path) -> None:
    """gate allowlist 和 artifact 根边界必须早于归档，避免任意命令或凭据泄漏。"""

    _write_repo(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=do-not-archive", encoding="utf-8")

    unknown = _run(tmp_path, "../../shell")
    escaped = _run(tmp_path, "ruff-format", "--artifact", f"report={outside}")
    credential = _run(tmp_path, "ruff-format", "--artifact", "config=.env")

    assert unknown.returncode == 2
    assert escaped.returncode == 2
    assert credential.returncode == 2
    assert "allowlisted" in unknown.stderr
    assert "outside repository" in escaped.stderr
    assert "credential-like" in credential.stderr


def test_runner_records_each_file_from_a_native_artifact_glob(tmp_path: Path) -> None:
    """原生产物通配必须展开为逐文件 checksum，不能只记录目录名。"""

    _write_repo(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "one.whl").write_bytes(b"wheel-one")
    (tmp_path / "dist" / "two.whl").write_bytes(b"wheel-two")
    completed = _run(tmp_path, "ruff-format", "--artifact", "wheel=dist/*.whl")

    assert completed.returncode == 0, completed.stderr
    artifacts = cast(list[dict[str, object]], _result(tmp_path, "ruff-format")["artifacts"])
    records = {str(item["path"]): item for item in artifacts if item["kind"] == "wheel"}
    assert set(records) == {"dist/one.whl", "dist/two.whl"}
    assert records["dist/one.whl"]["sha256"] == hashlib.sha256(b"wheel-one").hexdigest()


def test_runner_rejects_missing_native_artifact(tmp_path: Path) -> None:
    """声明的原生产物缺失时必须让 gate 失败，防止 YAML 路径冒充证据。"""

    _write_repo(tmp_path)
    completed = _run(tmp_path, "ruff-format", "--artifact", "wheel=dist/*.whl")

    assert completed.returncode == 2
    result = _result(tmp_path, "ruff-format")
    assert result["status"] == "fail"
    assert "did not match" in str(result["failure"])


def test_untracked_source_bytes_change_dirty_diff_identity(tmp_path: Path) -> None:
    """未提交的新文件也必须绑定 evidence identity，ignored artifact 则不得污染摘要。"""

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_repo(first)
    _write_repo(second)
    (first / ".gitignore").write_text(".artifacts/\n", encoding="utf-8")
    (second / ".gitignore").write_text(".artifacts/\n", encoding="utf-8")
    (first / "new-source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (second / "new-source.py").write_text("VALUE = 2\n", encoding="utf-8")

    first_run = _run(first, "ruff-format")
    second_run = _run(second, "ruff-format")

    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr
    first_identity = cast(dict[str, str], _result(first, "ruff-format")["input_identity"])
    second_identity = cast(dict[str, str], _result(second, "ruff-format")["input_identity"])
    assert first_identity["dirty_diff_sha256"] != second_identity["dirty_diff_sha256"]
