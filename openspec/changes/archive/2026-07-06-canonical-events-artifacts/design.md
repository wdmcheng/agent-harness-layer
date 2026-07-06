## Context

Phase 2 已有 trust/source/context/ref DTO 与 guardrail decision；Phase 3 提供 storage/repository seam。当前还没有统一的 run event envelope、本地证据文件或 artifact ref 策略。Phase 4 需要先建立内部事件模型，外部观测 provider 只做后续转换。

## Goals / Non-Goals

**Goals:**
- 统一 CanonicalEvent envelope、event type、terminal event 和 seq 规则。
- 本地无外部 provider 时仍写出 jsonl trace/eval/audit 证据。
- 大 payload、tool output 和 eval evidence 通过 artifact store 存储，事件正文只保留 ref/checksum。
- guardrail/context assembly 事件只包含摘要、source_ref、trust_level 和 truncation metadata。

**Non-Goals:**
- 不引入真实 observability SaaS provider。
- 不实现 runtime worker 或模型/tool 调用。

## Decisions

- `CanonicalEvent` 使用 Pydantic DTO，字段固定为 ids、type、seq、payload/payload_ref、visibility、timestamp 和 terminal marker。原因：跨 API、SSE、eval、audit 都要稳定 JSON。
- `EventBus` 负责分配 per-run seq，sink 只持久化事件。原因：sink 可以是 jsonl、DB、OTel adapter 或测试内存实现。
- Artifact store 使用 filesystem sha256 checksum。原因：local profile 必须离线可用，后续 service profile 可替换为 object storage adapter。
- OTel facade 只产生映射 DTO，不 import provider SDK。原因：vendor boundary 已要求 provider SDK 留在 adapters。

## Affected Surfaces

- `agent_harness.events`：types、bus、sinks。
- `agent_harness.artifacts`：filesystem store。
- `agent_harness.security`：redaction 和 guardrail/context event payload helpers。
- `agent_harness.observability`：OTel mapping facade。
- `templates/service-app/app/api/sse.py`：SSE formatting seam。

## Testing Seams

- Module seam：`EventBus.publish` / `read_after`。
- Persistence seam：local jsonl sink 写入和续读。
- Artifact seam：store large payload returns payload_ref/checksum。
- Security seam：redaction removes secret-like values before event persistence。
- API formatting seam：SSE adapter formats event id/type/data without exposing internal objects。

## Risks / Trade-offs

- [Risk] 事件类型过早膨胀 → Mitigation：只覆盖 P0 run、guardrail、context、artifact、runtime lifecycle types。
- [Risk] jsonl sink 并发写入顺序问题 → Mitigation：Phase 4 使用单进程 async lock；Phase 13 再做多进程 queue。
- [Risk] redaction 规则误伤 → Mitigation：先覆盖常见 key/token/password patterns，保留测试证明。

## Migration Plan

新增本地证据目录和 artifact 文件，无存量迁移。删除本地 evidence directory 可回滚到无事件证据状态。

## Open Questions

- 无。真实 OTel provider 和 eval trace 转换在 Phase 10/11 处理。
