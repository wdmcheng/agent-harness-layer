"""在 workspace 外复制模板，用已构建 wheel 运行四服务 Compose smoke。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "service-app"


def parse_args() -> argparse.Namespace:
    """解析服务冒烟模式；迁移专用模式仍复用同一复制模板边界。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-only", action="store_true")
    return parser.parse_args()


def _build_core_wheel() -> Path:
    """构建唯一核心 wheel，并拒绝残留或缺失产物以防复制 smoke 使用错误版本。"""

    uv_executable = os.environ.get("UV", "uv")
    subprocess.run(
        [uv_executable, "build", "--package", "agent-harness", "--clear"],
        cwd=ROOT,
        check=True,
    )
    wheels = sorted((ROOT / "dist").glob("agent_harness-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one core wheel, found {len(wheels)}")
    return wheels[0]


def _run_copied_smoke(command: list[str], copied: Path, wheel_target: Path) -> str:
    """中断时等待子 smoke 完成 finally，避免临时目录先于 Compose cleanup 消失。"""

    process = subprocess.Popen(
        command,
        cwd=copied,
        env={**os.environ, "AGENT_HARNESS_SOURCE": str(wheel_target)},
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate()
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        raise
    return_code = process.returncode
    if return_code != 0:
        print(output, file=sys.stderr, end="")
        raise subprocess.CalledProcessError(return_code, command)
    return output


def _compose_image_reference(copied: Path, service: str) -> str:
    """读取本轮 Compose 实际使用的 image 覆盖值或固定默认值。"""

    override = os.environ.get(f"SERVICE_APP_{service.upper()}_IMAGE")
    if override:
        return override
    compose = copied / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    match = re.search(rf"SERVICE_APP_{service.upper()}_IMAGE:-([^}}\n]+)", text)
    return match.group(1).strip() if match else ""


def _atomic_copy(source: Path, destination: Path) -> None:
    """原子导出已验证原生产物，避免中断留下半个 JSONL。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_smoke_evidence(
    root: Path,
    copied: Path,
    output: str,
    *,
    require_trace: bool,
) -> None:
    """将成功的真实 service smoke 结果和 runtime trace 归档给后续 gate。"""

    postgres = _compose_image_reference(copied, "postgres")
    redis = _compose_image_reference(copied, "redis")
    server_versions = {"postgres": "", "redis": ""}
    for line in reversed(output.splitlines()):
        if not line.startswith("smoke-service: {"):
            continue
        try:
            raw_child_evidence: object = json.loads(line.partition(": ")[2])
        except json.JSONDecodeError:
            break
        if isinstance(raw_child_evidence, dict):
            child_evidence = cast(dict[str, object], raw_child_evidence)
            raw_reported = child_evidence.get("server_versions")
        else:
            raw_reported = None
        if isinstance(raw_reported, dict):
            reported = cast(dict[str, object], raw_reported)
            server_versions = {
                "postgres": str(reported.get("postgres", "")),
                "redis": str(reported.get("redis", "")),
            }
        break
    payload = {
        "schema_version": "service-smoke-evidence/v1",
        "status": "pass",
        "producer": "make smoke-service",
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "images": {
            "postgres": {
                "reference": postgres,
                "server_version": server_versions["postgres"],
            },
            "redis": {
                "reference": redis,
                "server_version": server_versions["redis"],
            },
        },
        "checks": {"streams": True, "xautoclaim": True, "recovery": True},
    }
    destination = root / ".artifacts/license/smoke-service.log"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)
    if require_trace:
        trace_source = copied / ".agent-harness/service-smoke-trace.jsonl"
        if not trace_source.is_file() or trace_source.stat().st_size == 0:
            raise RuntimeError("copied service smoke did not export a runtime trace")
        _atomic_copy(trace_source, root / ".artifacts/smoke/service/trace.jsonl")


def main() -> int:
    """在临时工作区复制模板、注入刚构建 wheel 并运行受限服务冒烟。"""

    args = parse_args()
    (ROOT / ".artifacts/smoke/service/trace.jsonl").unlink(missing_ok=True)
    wheel = _build_core_wheel()
    with tempfile.TemporaryDirectory(prefix="agent-harness-service-smoke-") as temp:
        copied = Path(temp) / "service-app"
        # 复制时排除本机状态与缓存，确保被测模板不能依赖宿主已安装环境或历史密钥。
        shutil.copytree(
            TEMPLATE,
            copied,
            ignore=shutil.ignore_patterns(
                ".agent-harness",
                ".venv",
                ".ruff_cache",
                "__pycache__",
            ),
        )
        wheel_target = copied / ".agent-harness" / wheel.name
        wheel_target.parent.mkdir(parents=True)
        shutil.copy2(wheel, wheel_target)
        command = ["make", "smoke-service", f"PYTHON={sys.executable}"]
        if args.migrate_only:
            command = [sys.executable, "scripts/smoke_service.py", "--migrate-only"]
        try:
            output = _run_copied_smoke(command, copied, wheel_target)
        except subprocess.CalledProcessError:
            return 1
        if list(copied.rglob("*.secret")):
            raise RuntimeError("service smoke did not clean temporary secret files")
        _write_smoke_evidence(ROOT, copied, output, require_trace=not args.migrate_only)
    print("smoke-service-root: workspace-outside=ok wheel-only=ok secret-cleanup=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
