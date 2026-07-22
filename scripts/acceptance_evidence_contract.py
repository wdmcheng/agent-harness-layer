"""验证需求验收矩阵引用的 CI result、artifact 与当前输入身份。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import cast

from acceptance_matrix_policy import (
    CI_RESULT_SCHEMA,
    CI_RESULT_STATUSES,
    EVIDENCE_GATE_PATH,
    GIT_OBJECT_ID,
    KNOWN_EVIDENCE_GATES,
    SHA256,
)
from ci_evidence import GATE_TARGETS
from ci_identity import InputIdentityError, input_identity


class MatrixError(RuntimeError):
    """表示矩阵缺项、重复或用占位内容冒充证据。"""


def _git(repo: Path, *args: str) -> str | None:
    """读取仓库身份；临时合同夹具不在 Git 仓库时返回 None。"""

    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def repository_root(spec_path: Path, matrix_path: Path) -> Path:
    """以 Git 根为证据基准，非 Git 临时夹具退回 spec 所在目录。"""

    for candidate in (spec_path.resolve().parent, matrix_path.resolve().parent):
        root = _git(candidate, "rev-parse", "--show-toplevel")
        if root:
            return Path(root).resolve()
    return spec_path.resolve().parent


def current_identity(repo: Path) -> dict[str, str] | None:
    """复用 evidence runner 的完整输入身份；非 Git 夹具跳过当前态比对。"""

    try:
        return input_identity(repo)
    except InputIdentityError:
        return None


def _relative_artifact_path(root: Path, raw: object, *, field: str) -> Path:
    """把 evidence 中的相对路径约束在仓库根内。"""

    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise MatrixError(f"evidence artifact {field} must be a relative path")
    candidate = Path(raw)
    if ".." in candidate.parts:
        raise MatrixError(f"evidence artifact {field} has unsafe path: {raw}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MatrixError(f"evidence artifact {field} escapes repository: {raw}") from exc
    return resolved


def load_evidence(
    root: Path,
    row: dict[str, str],
    evidence_path: Path,
    expected_identity: dict[str, str] | None,
    ci_job: str,
) -> None:
    """验证 result、gate、artifact checksum 及当前输入身份，全部失败即拒绝。"""

    identifier = row["ID"]
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise MatrixError(f"{identifier} evidence file is missing or not regular: {evidence_path}")
    try:
        raw_payload: object = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixError(f"{identifier} evidence is not valid JSON: {evidence_path}") from exc
    if not isinstance(raw_payload, dict):
        raise MatrixError(f"{identifier} evidence result must be a JSON object")
    payload = cast(dict[str, object], raw_payload)
    if payload.get("schema_version") != CI_RESULT_SCHEMA:
        raise MatrixError(f"{identifier} evidence schema_version must be {CI_RESULT_SCHEMA}")

    match = EVIDENCE_GATE_PATH.fullmatch(evidence_path.relative_to(root).as_posix())
    if match is None:
        raise MatrixError(f"{identifier} evidence path is not a CI result path")
    path_gate = match.group(1)
    gate = payload.get("gate")
    if gate != path_gate:
        raise MatrixError(f"{identifier} evidence gate does not match path: {gate!r}")
    expected_gate = ci_job if ci_job in KNOWN_EVIDENCE_GATES else ci_job.removeprefix("ci-")
    if gate != expected_gate:
        raise MatrixError(f"{identifier} evidence gate {gate!r} does not match CI job {ci_job!r}")

    status = payload.get("status")
    if status not in CI_RESULT_STATUSES:
        raise MatrixError(f"{identifier} evidence has invalid status: {status!r}")
    raw_command = payload.get("command")
    if (
        not isinstance(raw_command, list)
        or not raw_command
        or not all(isinstance(item, str) and item for item in cast(list[object], raw_command))
    ):
        raise MatrixError(f"{identifier} evidence command is missing or invalid")
    expected_command = ["make", "--no-print-directory", GATE_TARGETS[path_gate]]
    if raw_command != expected_command:
        raise MatrixError(
            f"{identifier} evidence command does not execute producer {path_gate!r}: "
            f"expected {expected_command!r}"
        )
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise MatrixError(f"{identifier} evidence exit_code is invalid")
    if status == "pass" and exit_code != 0:
        raise MatrixError(f"{identifier} passing evidence has non-zero exit_code")
    if status == "fail" and exit_code == 0:
        raise MatrixError(f"{identifier} failing evidence has zero exit_code")
    if row["状态"] == "pass" and status != "pass":
        raise MatrixError(f"{identifier} matrix pass is not backed by passing evidence")

    raw_identity = payload.get("input_identity")
    if not isinstance(raw_identity, dict):
        raise MatrixError(f"{identifier} evidence input_identity is missing")
    identity = cast(dict[str, object], raw_identity)
    commit_sha = identity.get("commit_sha")
    dirty_diff_sha256 = identity.get("dirty_diff_sha256")
    if not isinstance(commit_sha, str) or GIT_OBJECT_ID.fullmatch(commit_sha) is None:
        raise MatrixError(f"{identifier} evidence commit_sha is invalid")
    if not isinstance(dirty_diff_sha256, str) or SHA256.fullmatch(dirty_diff_sha256) is None:
        raise MatrixError(f"{identifier} evidence dirty_diff_sha256 is invalid")
    if expected_identity is not None and identity != expected_identity:
        raise MatrixError(
            f"{identifier} evidence commit/diff identity does not match current input"
        )

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise MatrixError(f"{identifier} evidence artifacts are missing")
    artifacts = cast(list[object], raw_artifacts)
    required_artifact_fields = {"path", "kind", "sha256", "size", "producer_gate"}
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise MatrixError(f"{identifier} evidence artifact {index} is incomplete")
        artifact_record = cast(dict[str, object], artifact)
        if not required_artifact_fields <= artifact_record.keys():
            raise MatrixError(f"{identifier} evidence artifact {index} is incomplete")
        artifact_path = _relative_artifact_path(root, artifact_record.get("path"), field="path")
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise MatrixError(
                f"{identifier} evidence artifact is missing: {artifact_record.get('path')}"
            )
        checksum = artifact_record.get("sha256")
        if not isinstance(checksum, str) or SHA256.fullmatch(checksum) is None:
            raise MatrixError(f"{identifier} evidence artifact checksum is invalid")
        actual_checksum = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if checksum != actual_checksum:
            raise MatrixError(
                f"{identifier} evidence artifact checksum drift: {artifact_record.get('path')}"
            )
        size = artifact_record.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size != artifact_path.stat().st_size
        ):
            raise MatrixError(
                f"{identifier} evidence artifact size drift: {artifact_record.get('path')}"
            )
        if artifact_record.get("producer_gate") != gate:
            raise MatrixError(f"{identifier} evidence artifact producer gate mismatch")
