"""模型 endpoint、credential forwarding 与目录逐值解析边界。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import posixpath
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import SecretStr

from agent_harness.config.model_catalog import model_catalog_digest
from agent_harness.config.schemas import ModelCatalogEntrySettings, ModelSettings

DEFAULT_ENDPOINT_CATALOG: dict[tuple[str, str], str] = {
    ("openai_official", "v1"): "https://api.openai.com/v1",
}


class ModelConfigurationError(ValueError):
    """携带安全字段路径的模型配置错误，不保留原始 secret。"""

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(message)
        self.field_path = field_path


@dataclass(frozen=True)
class ResolvedModelDeployment:
    """composition 使用的私有冻结 blueprint；secret 不进入公开序列化。"""

    deployment_id: str
    provider_kind: str
    allowed_models: tuple[str, ...]
    default_model: str
    fallback_models: tuple[str, ...]
    canonical_base_url: str | None
    endpoint_origin: str | None
    endpoint_policy_ref: str | None
    endpoint_policy_version: str | None
    endpoint_policy_digest: str | None
    credential_ref: str | None
    credential: SecretStr | None
    model_catalogs: dict[str, ModelCatalogEntrySettings]
    max_per_attempt_token_bound: int
    max_per_attempt_cost_bound: Decimal | None


def _canonical_ref(value: str, *, field_path: str) -> str:
    """验证 committed mapping key 已是 canonical lower-snake。"""

    import re

    if len(value) > 64 or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is None:
        raise ModelConfigurationError(field_path, "ref must be canonical lower-snake")
    return value


def normalize_model_endpoint(raw_url: str, *, allow_local_http: bool) -> tuple[str, str]:
    """返回 canonical base URL 与 exact origin，并拒绝可扩大出站面的组件。"""

    try:
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname.lower()
        if parsed.scheme == "http":
            if not allow_local_http:
                raise ValueError
            address = ipaddress.ip_address(host)
            if not address.is_loopback:
                raise ValueError
        path = parsed.path or ""
        lower_path = path.lower()
        # Base URL path 是 credential forwarding 与 transport 前缀校验的一部分。
        # 结构字符若经 percent-encoding 隐藏，SDK、代理或上游可能在不同阶段解码，
        # 使 startup 看到的前缀与实际请求路径不一致；%25 还能形成二次编码。
        if any(token in lower_path for token in ("%2e", "%2f", "%5c", "%25")):
            raise ValueError
        if any(segment in {".", ".."} for segment in path.split("/")):
            raise ValueError
        normalized_path = posixpath.normpath(path)
        if "/../" in f"{path}/" or normalized_path.startswith("../"):
            raise ValueError
        if normalized_path == ".":
            normalized_path = ""
        if normalized_path != "/":
            normalized_path = normalized_path.rstrip("/")
        default_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port
        authority = host if port in {None, default_port} else f"{host}:{port}"
        if ":" in host and not host.startswith("["):
            authority = f"[{host}]" if port in {None, default_port} else f"[{host}]:{port}"
        canonical = urlunsplit(SplitResult(parsed.scheme, authority, normalized_path, "", ""))
        origin = f"{parsed.scheme}://{authority}"
        return canonical, origin
    except (ValueError, TypeError):
        raise ModelConfigurationError(
            "model.deployments.base_url", "endpoint URL is invalid"
        ) from None


def _endpoint_policy_digest(
    *,
    deployment_id: str,
    canonical_base_url: str,
    origin: str,
    model: ModelSettings,
) -> str:
    deployment = model.deployments[deployment_id]
    policy_ref = deployment.endpoint_policy_ref
    credential_ref = deployment.credential_ref
    assert policy_ref is not None and credential_ref is not None
    policy = model.endpoint_policies[policy_ref]
    allowed = sorted(
        {
            normalize_model_endpoint(item, allow_local_http=deployment.allow_local_http)[1]
            for item in policy.allowed_origins
        }
    )
    payload = {
        "schema_version": "endpoint-policy/v1",
        "endpoint_policy_ref": policy_ref,
        "endpoint_policy_version": policy.version,
        "provider_kind": deployment.provider_kind,
        "canonical_base_url": canonical_base_url,
        "allowed_origins": allowed,
        "credential_ref": credential_ref,
        "completion_classifier_ref": deployment.completion_classifier_ref,
        "completion_classifier_version": deployment.completion_classifier_version,
        "default_endpoint_catalog_ref": policy.default_endpoint_catalog_ref,
        "default_endpoint_catalog_version": policy.default_endpoint_catalog_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def resolve_model_deployment(model: ModelSettings, deployment_id: str) -> ResolvedModelDeployment:
    """逐值解析一个 deployment；任一身份或公式不匹配立即 fail closed。"""

    _canonical_ref(deployment_id, field_path="model.default_deployment_id")
    try:
        deployment = model.deployments[deployment_id]
    except KeyError:
        raise ModelConfigurationError("model.default_deployment_id", "unknown deployment") from None
    if deployment.provider_kind == "fake":
        bound = deployment.max_per_attempt_token_bound
        if bound is None:
            bound = deployment.max_prompt_utf8_bytes + deployment.max_output_tokens
        return ResolvedModelDeployment(
            deployment_id=deployment_id,
            provider_kind="fake",
            allowed_models=tuple(deployment.allowed_models),
            default_model=deployment.default_model,
            fallback_models=tuple(deployment.fallback_models),
            canonical_base_url=None,
            endpoint_origin=None,
            endpoint_policy_ref=None,
            endpoint_policy_version=None,
            endpoint_policy_digest=None,
            credential_ref=None,
            credential=None,
            model_catalogs={},
            max_per_attempt_token_bound=bound,
            max_per_attempt_cost_bound=None,
        )

    for ref in (
        *model.deployments,
        *model.credentials,
        *model.endpoint_policies,
        *model.model_catalogs,
    ):
        _canonical_ref(ref, field_path="model.mapping_key")
    if deployment.endpoint_policy_ref is None or deployment.endpoint_policy_version is None:
        raise ModelConfigurationError(
            "model.deployments.endpoint_policy_ref", "endpoint policy is required"
        )
    if deployment.credential_ref is None:
        raise ModelConfigurationError(
            "model.deployments.credential_ref", "credential ref is required"
        )
    try:
        policy = model.endpoint_policies[deployment.endpoint_policy_ref]
    except KeyError:
        raise ModelConfigurationError(
            "model.deployments.endpoint_policy_ref", "unknown endpoint policy"
        ) from None
    if (
        policy.version != deployment.endpoint_policy_version
        or policy.provider_kind != deployment.provider_kind
    ):
        raise ModelConfigurationError(
            "model.deployments.endpoint_policy_version", "endpoint policy identity mismatch"
        )
    if deployment.base_url is None:
        identity = (policy.default_endpoint_catalog_ref, policy.default_endpoint_catalog_version)
        try:
            raw_base_url = DEFAULT_ENDPOINT_CATALOG[(str(identity[0]), str(identity[1]))]
        except KeyError:
            raise ModelConfigurationError(
                "model.endpoint_policies.default_endpoint_catalog_ref",
                "unknown default endpoint catalog",
            ) from None
    else:
        raw_base_url = deployment.base_url
    canonical_base_url, origin = normalize_model_endpoint(
        raw_base_url,
        allow_local_http=deployment.allow_local_http,
    )
    allowed_origins = {
        normalize_model_endpoint(item, allow_local_http=deployment.allow_local_http)[1]
        for item in policy.allowed_origins
    }
    if origin not in allowed_origins:
        raise ModelConfigurationError(
            "model.endpoint_policies.allowed_origins", "endpoint origin is not allowed"
        )
    try:
        credential = model.credentials[deployment.credential_ref]
    except KeyError:
        raise ModelConfigurationError(
            "model.deployments.credential_ref", "unknown credential ref"
        ) from None
    credential_origins = {
        normalize_model_endpoint(item, allow_local_http=deployment.allow_local_http)[1]
        for item in credential.allowed_origins
    }
    if origin not in credential_origins:
        raise ModelConfigurationError(
            "model.credentials.allowed_origins", "credential origin mismatch"
        )
    classifiers = {(item.ref, item.version) for item in policy.completion_classifiers}
    classifier = (deployment.completion_classifier_ref, deployment.completion_classifier_version)
    if classifier != (None, None) and classifier not in classifiers:
        raise ModelConfigurationError(
            "model.endpoint_policies.completion_classifiers", "classifier is not allowed"
        )
    catalogs: dict[str, ModelCatalogEntrySettings] = {}
    for allowed_model in deployment.allowed_models:
        ref = deployment.model_catalog_refs.get(allowed_model)
        version = deployment.model_catalog_versions.get(allowed_model)
        if ref is None or version is None:
            raise ModelConfigurationError(
                "model.deployments.model_catalog_refs", "catalog identity is required"
            )
        try:
            entry = model.model_catalogs[ref]
        except KeyError:
            raise ModelConfigurationError(
                "model.deployments.model_catalog_refs", "unknown model catalog"
            ) from None
        if (
            entry.version != version
            or entry.provider_kind != deployment.provider_kind
            or entry.model != allowed_model
        ):
            raise ModelConfigurationError("model.model_catalogs", "model catalog identity mismatch")
        payload = entry.model_dump(mode="python", exclude={"digest"})
        if model_catalog_digest(ref, payload) != entry.digest:
            raise ModelConfigurationError(
                "model.model_catalogs.digest", "model catalog digest mismatch"
            )
        catalogs[allowed_model] = entry
    selected = catalogs[deployment.default_model]
    token_ceiling = (
        deployment.max_prompt_utf8_bytes
        + selected.input_envelope_token_bound
        + deployment.max_output_tokens
    )
    if deployment.max_per_attempt_token_bound != token_ceiling:
        raise ModelConfigurationError(
            "model.deployments.max_per_attempt_token_bound", "token ceiling mismatch"
        )
    cost_ceiling: Decimal | None = None
    if selected.cost_enabled:
        assert selected.input_token_price_usd is not None
        assert selected.output_token_price_usd is not None
        cost_ceiling = (
            Decimal(deployment.max_prompt_utf8_bytes + selected.input_envelope_token_bound)
            * selected.input_token_price_usd
            + Decimal(deployment.max_output_tokens) * selected.output_token_price_usd
        )
        if deployment.max_per_attempt_cost_bound != cost_ceiling:
            raise ModelConfigurationError(
                "model.deployments.max_per_attempt_cost_bound", "cost ceiling mismatch"
            )
    elif deployment.max_per_attempt_cost_bound is not None:
        raise ModelConfigurationError(
            "model.deployments.max_per_attempt_cost_bound", "cost-disabled ceiling must be null"
        )
    # deployment 的 ceiling 由默认 route 精确锚定；fallback 可以更小，但不能借另一份
    # catalog 放大输入/成本上界，也不能在同一 deployment 内切换 cost 语义。
    for model_id, candidate in catalogs.items():
        candidate_token_ceiling = (
            deployment.max_prompt_utf8_bytes
            + candidate.input_envelope_token_bound
            + deployment.max_output_tokens
        )
        if candidate_token_ceiling > token_ceiling:
            raise ModelConfigurationError(
                "model.deployments.max_per_attempt_token_bound",
                f"model catalog exceeds deployment ceiling: {model_id}",
            )
        if candidate.cost_enabled != selected.cost_enabled:
            raise ModelConfigurationError(
                "model.model_catalogs.cost_enabled",
                "all deployment routes must share cost-enabled semantics",
            )
        if not candidate.cost_enabled:
            continue
        assert candidate.input_token_price_usd is not None
        assert candidate.output_token_price_usd is not None
        candidate_cost_ceiling = (
            Decimal(deployment.max_prompt_utf8_bytes + candidate.input_envelope_token_bound)
            * candidate.input_token_price_usd
            + Decimal(deployment.max_output_tokens) * candidate.output_token_price_usd
        )
        if cost_ceiling is None or candidate_cost_ceiling > cost_ceiling:
            raise ModelConfigurationError(
                "model.deployments.max_per_attempt_cost_bound",
                f"model catalog exceeds deployment cost ceiling: {model_id}",
            )
    return ResolvedModelDeployment(
        deployment_id=deployment_id,
        provider_kind=deployment.provider_kind,
        allowed_models=tuple(deployment.allowed_models),
        default_model=deployment.default_model,
        fallback_models=tuple(deployment.fallback_models),
        canonical_base_url=canonical_base_url,
        endpoint_origin=origin,
        endpoint_policy_ref=deployment.endpoint_policy_ref,
        endpoint_policy_version=deployment.endpoint_policy_version,
        endpoint_policy_digest=_endpoint_policy_digest(
            deployment_id=deployment_id,
            canonical_base_url=canonical_base_url,
            origin=origin,
            model=model,
        ),
        credential_ref=deployment.credential_ref,
        credential=credential.value,
        model_catalogs=catalogs,
        max_per_attempt_token_bound=token_ceiling,
        max_per_attempt_cost_bound=cost_ceiling,
    )


def validate_model_settings(model: ModelSettings, *, profile: str) -> None:
    """在任何 composition 副作用前验证全部 deployment 和 profile 出站边界。"""

    if model.default_deployment_id not in model.deployments:
        raise ModelConfigurationError("model.default_deployment_id", "unknown default deployment")
    for deployment_id, deployment in model.deployments.items():
        # `allow_local_http` 只是 local 开发 profile 的显式例外，不能让 deployment
        # 在 service 等正式 profile 中自行扩大 credential forwarding 边界。
        if deployment.allow_local_http and profile != "local":
            raise ModelConfigurationError(
                "model.deployments.allow_local_http",
                "local HTTP is restricted to the local profile",
            )
        resolve_model_deployment(model, deployment_id)


__all__ = [
    "DEFAULT_ENDPOINT_CATALOG",
    "ModelConfigurationError",
    "ResolvedModelDeployment",
    "normalize_model_endpoint",
    "resolve_model_deployment",
    "validate_model_settings",
]
