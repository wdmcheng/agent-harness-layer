"""EVL-004 CLI 的公共 service 装配与稳定 JSON 输出。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, cast
from uuid import uuid4

import typer
from pydantic import ValidationError

from agent_harness.audit import AuditService
from agent_harness.cli_shared import load_settings_or_exit, policy_engine, require_schema_or_exit
from agent_harness.evals import (
    AcceptanceService,
    EvalExperimentError,
    ExperimentAcceptanceRequest,
    ExperimentCreateBody,
    ExperimentCreateRequest,
    ExperimentService,
    RecordedApprovedCaseEvaluator,
)
from agent_harness.identity import IdentityContext
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage, storage_dsn_from_settings


def register_eval_experiment_commands(experiment_app: typer.Typer) -> None:
    """把 EVL-004 命令组挂到传入 Typer，避免根 CLI 承担子域参数装配。"""

    @experiment_app.command("create")
    def create_command(
        request_file: Annotated[Path, typer.Option("--request-file", exists=True, dir_okay=False)],
        idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
        profile: Annotated[str, typer.Option("--profile")] = "local",
        profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
        storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
        request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    ) -> None:
        """从 approved tagged cases 创建或幂等重放 experiment。"""

        create_experiment(
            request_file=request_file,
            idempotency_key=idempotency_key,
            request_id=request_id or str(uuid4()),
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
        )

    @experiment_app.command("show")
    def show_command(
        experiment_id: str,
        profile: Annotated[str, typer.Option("--profile")] = "local",
        profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
        storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
        request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    ) -> None:
        """读取 tenant 可见的 persisted experiment。"""

        show_experiment(
            experiment_id=experiment_id,
            request_id=request_id or str(uuid4()),
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
        )

    @experiment_app.command("compare")
    def compare_command(
        experiment_id: str,
        profile: Annotated[str, typer.Option("--profile")] = "local",
        profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
        storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
        request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    ) -> None:
        """读取 create 阶段已持久化的 comparison。"""

        compare_experiment(
            experiment_id=experiment_id,
            request_id=request_id or str(uuid4()),
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
        )

    @experiment_app.command("accept")
    def accept_command(
        experiment_id: str,
        decision: Annotated[Literal["accepted", "rejected"], typer.Option("--decision")],
        reason: Annotated[str, typer.Option("--reason")],
        accepted_harness_version: Annotated[
            str | None, typer.Option("--accepted-harness-version")
        ] = None,
        followup_issue_ref: Annotated[str | None, typer.Option("--followup-issue-ref")] = None,
        reviewer: Annotated[str, typer.Option("--reviewer")] = "local-reviewer",
        profile: Annotated[str, typer.Option("--profile")] = "local",
        profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
        storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
        request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    ) -> None:
        """以 local reviewer 身份写唯一 accepted/rejected decision。"""

        accept_experiment(
            experiment_id=experiment_id,
            decision=decision,
            reason=reason,
            accepted_harness_version=accepted_harness_version,
            followup_issue_ref=followup_issue_ref,
            reviewer=reviewer,
            request_id=request_id or str(uuid4()),
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
        )

    # Typer 通过装饰器持有这些本地函数；显式引用也让静态检查确认注册是有意的。
    _ = (create_command, show_command, compare_command, accept_command)


def create_experiment(
    *,
    request_file: Path,
    idempotency_key: str,
    request_id: str,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
) -> None:
    """从 JSON body 创建 experiment；CLI 不复制 split 或 evaluator 规则。"""

    identity, storage, experiments, _acceptance = _services(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
    )
    try:
        _require_permission(identity, "eval.experiment.create")
        body = ExperimentCreateBody.model_validate(_load_json_object(request_file))
        request = ExperimentCreateRequest(
            request_id=request_id,
            tenant_id=identity.tenant_id,
            idempotency_key=idempotency_key.strip(),
            **body.to_payload(),
        )
        outcome = _run(experiments.create(request), storage=storage)
    except Exception as exc:
        _fail(exc, storage=storage, request_id=request_id)
    _write_json(_experiment_payload(outcome.result.to_payload()))


def show_experiment(
    *,
    experiment_id: str,
    request_id: str,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
) -> None:
    """按当前身份读取实验详情，并输出与 HTTP 响应一致的稳定 JSON。"""

    identity, storage, experiments, _acceptance = _services(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
    )
    try:
        _require_permission(identity, "eval.experiment.read")
        result = _run(
            experiments.get(
                tenant_id=identity.tenant_id,
                experiment_id=experiment_id,
                request_id=request_id,
            ),
            storage=storage,
        )
    except Exception as exc:
        _fail(exc, storage=storage, request_id=request_id)
    _write_json(_experiment_payload(result.to_payload()))


def compare_experiment(
    *,
    experiment_id: str,
    request_id: str,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
) -> None:
    """读取已持久化的实验比较结果；读取权限与详情接口保持同一边界。"""

    identity, storage, experiments, _acceptance = _services(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
    )
    try:
        _require_permission(identity, "eval.experiment.read")
        result = _run(
            experiments.compare(
                tenant_id=identity.tenant_id,
                experiment_id=experiment_id,
                request_id=request_id,
            ),
            storage=storage,
        )
    except Exception as exc:
        _fail(exc, storage=storage, request_id=request_id)
    _write_json(result.to_payload())


def accept_experiment(
    *,
    experiment_id: str,
    decision: Literal["accepted", "rejected"],
    reason: str,
    accepted_harness_version: str | None,
    followup_issue_ref: str | None,
    reviewer: str,
    request_id: str,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
) -> None:
    """以指定评审身份写入唯一接受或拒绝决定，并保留服务层的策略校验。"""

    identity, storage, _experiments, acceptance = _services(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
    )
    # CLI 允许显式声明评审者，但租户、权限等安全上下文仍只能继承已加载配置。
    actor = identity.model_copy(update={"user_id": reviewer})
    try:
        request = ExperimentAcceptanceRequest(
            request_id=request_id,
            decision=decision,
            reason=reason,
            accepted_harness_version=accepted_harness_version,
            followup_issue_ref=followup_issue_ref,
        )
        result = _run(
            acceptance.decide(
                actor=actor,
                experiment_id=experiment_id,
                request=request,
            ),
            storage=storage,
        )
    except Exception as exc:
        _fail(exc, storage=storage, request_id=request_id)
    _write_json(result.to_payload())


def _services(
    *,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
) -> tuple[IdentityContext, SQLAlchemyStorage, ExperimentService, AcceptanceService]:
    """按 profile 装配 CLI 所需服务，不在命令层复制领域规则。

    schema 在创建存储前检查，避免命令执行一半才发现数据库未迁移；评审服务与实验
    服务共享同一 storage，保证一次 CLI 调用中的读取和写入使用一致连接配置。
    """

    settings = load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    require_schema_or_exit(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    experiments = ExperimentService(
        storage=storage,
        evaluator=RecordedApprovedCaseEvaluator(storage=storage),
    )
    policy = policy_engine(settings, storage, AuditService(storage), profiles_dir)
    acceptance = AcceptanceService(
        storage=storage,
        experiments=experiments,
        policy=policy,
    )
    return settings.identity.default, storage, experiments, acceptance


def _load_json_object(path: Path) -> dict[str, Any]:
    """读取请求文件并强制顶层为对象，拒绝数组或标量等歧义输入。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("experiment request file must contain a JSON object")
    return cast(dict[str, Any], raw)


def _require_permission(identity: IdentityContext, action: str) -> None:
    """在进入领域服务前执行 CLI 身份权限门禁，支持显式通配授权。"""

    if "*" not in identity.permissions and action not in identity.permissions:
        raise EvalExperimentError("policy.denied", "permission missing", status_code=403)


def _run[T](awaitable: Awaitable[T], *, storage: SQLAlchemyStorage) -> T:
    """在同步 Typer 命令中执行协程，并保证运行结束后关闭 storage engine。"""

    async def execute() -> T:
        """将资源释放放在 finally，使成功和领域异常走同一关闭路径。"""

        try:
            return await awaitable
        finally:
            await storage.dispose()

    return asyncio.run(execute())


def _fail(exc: Exception, *, storage: SQLAlchemyStorage, request_id: str) -> NoReturn:
    """将 CLI 异常映射为脱敏 JSON 错误，并在协程尚未启动时释放资源。"""

    # 若异常发生在 coroutine 构造前，仍需显式释放 engine。
    try:
        asyncio.run(storage.dispose())
    except RuntimeError:
        pass
    # 只向调用者暴露稳定错误码；未知异常不透传实现细节或潜在敏感文本。
    if isinstance(exc, EvalExperimentError):
        code = exc.code
        message = str(exc)
    elif isinstance(exc, ValidationError):
        code = "validation_error"
        message = "request validation failed"
    elif isinstance(exc, (json.JSONDecodeError, OSError, ValueError)):
        code = "eval.experiment.input_invalid"
        message = str(exc)
    else:
        code = "api.internal_error"
        message = "internal error"
    _write_json(
        {
            "error": {
                "code": code,
                "message": str(redact_secrets(message)),
                "request_id": request_id,
            }
        },
        err=True,
    )
    raise typer.Exit(1) from exc


def _write_json(payload: dict[str, Any], *, err: bool = False) -> None:
    """以可复现键顺序输出 JSON，供脚本与 HTTP 合同测试稳定解析。"""

    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True), err=err)


def _experiment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """与 HTTP EvalExperimentResponse 保持同字段，comparison 走独立命令。"""

    result = dict(payload)
    result.pop("comparison", None)
    return result
