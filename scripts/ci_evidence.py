"""执行 allowlisted Make gate，并生成可复核的 ``ci-result/v1`` 证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ci_identity import InputIdentityError, input_identity

SCHEMA_VERSION = "ci-result/v1"
GATE_TARGETS = {
    "lock": "lock",
    "install": "install",
    "ruff-format": "ruff-format",
    "ruff-lint": "ruff-lint",
    "pyright": "pyright",
    "import-boundary": "import-boundary",
    "unit-contract": "unit-contract",
    "integration": "integration",
    "quality-aggregate": "quality-aggregate",
    "test-aggregate": "test-aggregate",
    "eval": "eval",
    "smoke-local": "smoke-local",
    "smoke-service": "smoke-service",
    "smoke-live-model": "smoke-live-model",
    "smoke-live-model-stream": "smoke-live-model-stream",
    "smoke-live-model-failover": "smoke-live-model-failover",
    "build": "build",
    "license": "license-check",
    "release-dry-run": "release-dry-run",
    "ci-contract": "ci-contract-check",
    "acceptance-validate": "acceptance-validate-check",
    "release-promote-plan": "release-promote-plan",
    "release-promote-execute": "release-promote-execute",
    "registry-publish-plan": "registry-publish-plan",
    "registry-publish-execute": "registry-publish-execute",
}
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
CREDENTIAL_PARTS = {
    ".env",
    "credential",
    "credentials",
    "secret",
    "secrets",
    "token",
    "tokens",
    "id_rsa",
    "id_ed25519",
}
SECRET_ENV = re.compile(r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY|API_KEY)")


class EvidenceError(RuntimeError):
    """表示证据路径、输入身份或 artifact 合同无法安全闭合。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_text(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise EvidenceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _input_identity(repo: Path) -> dict[str, str]:
    """把共享身份算法的失败转换为 evidence runner 的公开错误。"""

    try:
        return input_identity(repo)
    except InputIdentityError as exc:
        raise EvidenceError(str(exc)) from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """同目录 fsync 后替换，避免 runner 中断留下半个 JSON 或半段日志。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _redact(text: str) -> str:
    """日志只替换当前进程显式 credential 值，不猜业务文本中的普通 key。"""

    redacted = text
    for name, value in os.environ.items():
        if SECRET_ENV.search(name.upper()) and len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _artifact_path(repo: Path, raw: str) -> Path:
    """把产物路径约束在仓库内，并拒绝明显指向凭据的路径形状。"""

    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (repo / candidate).resolve()
    try:
        relative = resolved.relative_to(repo)
    except ValueError as exc:
        raise EvidenceError(f"artifact path is outside repository: {raw}") from exc
    lowered = {part.lower() for part in relative.parts}
    if lowered & CREDENTIAL_PARTS or any(part.endswith(".pem") for part in lowered):
        raise EvidenceError(f"credential-like artifact path is forbidden: {raw}")
    return resolved


def _parse_artifacts(repo: Path, values: list[str]) -> list[tuple[str, Path]]:
    """解析可重复的 ``kind=path`` 参数，保留 glob 到受限展开阶段处理。"""

    parsed: list[tuple[str, Path]] = []
    for value in values:
        kind, separator, raw_path = value.partition("=")
        if not separator or SAFE_NAME.fullmatch(kind) is None or not raw_path:
            raise EvidenceError("--artifact must use kind=repo-relative-path")
        raw = Path(raw_path)
        if raw.is_absolute():
            raise EvidenceError(f"artifact path is outside repository: {raw_path}")
        if ".." in raw.parts:
            raise EvidenceError(f"artifact path must stay repository-relative: {raw_path}")
        literal_parts = {
            part.lower() for part in raw.parts if not any(char in part for char in "*?[")
        }
        if literal_parts & CREDENTIAL_PARTS or any(part.endswith(".pem") for part in literal_parts):
            raise EvidenceError(f"credential-like artifact path is forbidden: {raw_path}")
        if any(char in raw_path for char in "*?["):
            parsed.append((kind, repo / raw_path))
        else:
            parsed.append((kind, _artifact_path(repo, raw_path)))
    return parsed


def _expand_artifact(repo: Path, path: Path) -> list[Path]:
    """在 gate 完成后展开目录/通配，确保记录的是实际生成文件。"""

    raw = path.relative_to(repo).as_posix()
    matches = sorted(repo.glob(raw)) if any(char in raw for char in "*?[") else [path]
    expanded: list[Path] = []
    for match in matches:
        resolved = match.resolve()
        if resolved.is_dir():
            expanded.extend(
                sorted(candidate for candidate in resolved.rglob("*") if candidate.is_file())
            )
        elif resolved.is_file():
            expanded.append(resolved)
    if not expanded:
        raise EvidenceError(f"artifact path did not match a regular file: {raw}")
    return expanded


def _record(repo: Path, gate: str, kind: str, path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise EvidenceError(f"artifact is missing or not a regular file: {path}")
    relative = path.resolve().relative_to(repo)
    return {
        "path": relative.as_posix(),
        "kind": kind,
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "producer_gate": gate,
    }


def run_gate(repo: Path, gate: str, artifact_specs: list[str]) -> int:
    """执行一个稳定 Make target；命令失败时仍先写完整诊断再返回其状态。"""

    if gate not in GATE_TARGETS or SAFE_NAME.fullmatch(gate) is None:
        raise EvidenceError(f"gate is not allowlisted: {gate}")
    repo = repo.resolve()
    root = Path(_git_text(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if not root.samefile(repo):
        raise EvidenceError(f"--repo must be the Git worktree root: {repo}")
    parse_error = ""
    try:
        requested = _parse_artifacts(repo, artifact_specs)
    except EvidenceError as exc:
        # 缺失的原生产物必须留下 gate result 供 CI 归档诊断；越界/凭据路径仍在
        # 执行前拒绝，避免把安全错误混成普通产物缺失。
        if "did not match a regular file" not in str(exc):
            raise
        requested = []
        parse_error = str(exc)
    identity = _input_identity(repo)
    target = GATE_TARGETS[gate]
    command = ["make", "--no-print-directory", target]
    gate_dir = repo / ".artifacts" / "ci" / gate
    log_path = gate_dir / "command.log"
    result_path = gate_dir / "result.json"
    # result.json 代表一次已经终结的 gate。重跑期间先撤下旧结果，避免聚合测试、
    # matrix validator 或 artifact 收集器把上一轮身份误认成当前运行的结论。
    result_path.unlink(missing_ok=True)
    started = datetime.now(UTC)
    command_env = os.environ.copy()
    command_env["CI_EVIDENCE_ACTIVE_GATE"] = gate
    completed = subprocess.run(
        command,
        cwd=repo,
        env=command_env,
        text=True,
        capture_output=True,
        check=False,
    )
    finished = datetime.now(UTC)
    combined = _redact(f"$ {' '.join(command)}\n{completed.stdout}{completed.stderr}").encode()
    _atomic_write(log_path, combined)

    exit_code = completed.returncode
    status = "pass" if exit_code == 0 else "fail"
    command_failure = (
        ""
        if completed.returncode == 0
        else (
            f"gate command failed with exit code {completed.returncode}; "
            f"see {log_path.relative_to(repo).as_posix()}"
        )
    )
    artifact_records = [_record(repo, gate, "log", log_path)]
    artifact_error = parse_error
    try:
        seen: set[Path] = set()
        for kind, path in requested:
            for expanded in _expand_artifact(repo, path):
                if expanded in seen:
                    continue
                seen.add(expanded)
                artifact_records.append(_record(repo, gate, kind, expanded))
    except EvidenceError as exc:
        artifact_error = str(exc)
        status = "fail"
        if exit_code == 0:
            exit_code = 2
    if gate == "smoke-live-model" and not artifact_error:
        live_path = repo / ".artifacts" / "smoke" / "live-model" / "result.json"
        try:
            live = json.loads(live_path.read_text(encoding="utf-8"))
            live_status = live.get("status")
        except (OSError, json.JSONDecodeError):
            live_status = None
        if live_status == "hosted-unverified" and exit_code == 0:
            status = "skipped"
        elif live_status != "pass":
            status = "fail"
            if exit_code == 0:
                exit_code = 2
    if gate == "smoke-live-model-stream" and not artifact_error:
        stream_path = repo / ".artifacts" / "smoke" / "live-model-stream" / "result.json"
        try:
            stream_live = json.loads(stream_path.read_text(encoding="utf-8"))
            stream_status = stream_live.get("status")
            stream_schema = stream_live.get("schema_version")
        except (OSError, json.JSONDecodeError):
            stream_status = None
            stream_schema = None
        if (
            stream_schema == "model-stream-live-smoke/v1"
            and stream_status == "hosted-unverified"
            and exit_code == 0
        ):
            status = "skipped"
        elif stream_status != "passed":
            status = "fail"
            if exit_code == 0:
                exit_code = 2
    if gate == "smoke-live-model-failover" and not artifact_error:
        failover_path = repo / ".artifacts" / "smoke" / "live-model-failover" / "result.json"
        try:
            from live_model_failover_contract import validate_result

            failover_live = validate_result(json.loads(failover_path.read_text(encoding="utf-8")))
            failover_status = failover_live["status"]
        except (OSError, json.JSONDecodeError, ValueError):
            failover_status = None
        if failover_status == "hosted-unverified" and exit_code == 0:
            status = "skipped"
        elif failover_status != "passed":
            status = "fail"
            if exit_code == 0:
                exit_code = 2
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "gate": gate,
        "status": status,
        "command": command,
        "exit_code": exit_code,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "input_identity": identity,
        "artifacts": artifact_records,
    }
    if command_failure:
        # gate命令失败是主错误；随后缺失的原生产物通常只是失败发生在产物发布前，
        # 必须作为附加诊断保留，不能覆盖command.log中的原始失败边界。
        result["failure"] = command_failure
        if artifact_error:
            result["artifact_failure"] = artifact_error
    elif artifact_error:
        result["failure"] = artifact_error
    _atomic_write(
        result_path,
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )
    if artifact_error:
        print(f"ci-evidence: {artifact_error}", file=sys.stderr)
    if command_failure:
        print(f"ci-evidence: {command_failure}", file=sys.stderr)
    print(f"ci-evidence: {gate} {status} -> {result_path.relative_to(repo)}")
    return exit_code


def main() -> int:
    """执行一个 CI gate 并把参数或合同错误收口为稳定退出码。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--gate", required=True)
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="附加原生产物，格式为 kind=repo-relative-path；可重复。",
    )
    args = parser.parse_args()
    try:
        return run_gate(args.repo, args.gate, args.artifact)
    except EvidenceError as exc:
        print(f"ci-evidence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
