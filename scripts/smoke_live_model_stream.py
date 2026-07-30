"""受控真实普通文本增量 smoke；默认零网络并报告 hosted-unverified。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scripts.live_model_stream_contract import (
    AUTHORIZED_ENV,
    SCHEMA_VERSION,
    STREAM_OPT_IN_ENV,
    classify_incomplete_run,
    make_result,
    validate_result,
)
from scripts.live_model_stream_execution import run
from scripts.live_model_stream_probe import (
    LiveStreamSmokeExecutor,
    StreamTimingRecorder,
    measure_existing_sse_first_frame,
)

from agent_harness.config.secret_files import DEFAULT_SECRET_ROOT


def _timing(payload: dict[str, object], name: str) -> int | None:
    """从已校验 payload 读取时延；CLI 失败收口不复制或暴露其他字段。"""

    value = payload.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _local_cli_failure(payload: dict[str, object] | None = None) -> dict[str, object]:
    """把 runner 或 artifact I/O 失败压缩为安全 JSON，并保留已知调用事实。"""

    current = payload or {}
    return make_result(
        status="failed",
        provider_called=current.get("provider_called") is True,
        existing_event_first_frame_ms=_timing(current, "existing_event_first_frame_ms"),
        provider_first_delta_ms=_timing(current, "provider_first_delta_ms"),
        committed_first_delta_ms=_timing(current, "committed_first_delta_ms"),
        client_first_delta_ms=_timing(current, "client_first_delta_ms"),
        reason_code="contract_failure",
    )


def main() -> int:
    """写入单个去敏 JSON；hosted-unverified 保持成功退出供 CI 映射 skipped。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="service")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=Path("templates/service-app/configs/profiles"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/smoke/live-model-stream/result.json"),
    )
    parser.add_argument("--secret-root", type=Path, default=DEFAULT_SECRET_ROOT)
    args = parser.parse_args()
    try:
        payload, exit_code = asyncio.run(
            run(
                profile=args.profile,
                profiles_dir=args.profiles_dir,
                secret_root=args.secret_root,
            )
        )
    except Exception:
        payload, exit_code = _local_cli_failure(), 1
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    except OSError:
        payload, exit_code = _local_cli_failure(payload), 1
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    print(rendered)
    return exit_code


__all__ = [
    "AUTHORIZED_ENV",
    "LiveStreamSmokeExecutor",
    "SCHEMA_VERSION",
    "STREAM_OPT_IN_ENV",
    "StreamTimingRecorder",
    "classify_incomplete_run",
    "make_result",
    "measure_existing_sse_first_frame",
    "run",
    "validate_result",
]


if __name__ == "__main__":
    raise SystemExit(main())
