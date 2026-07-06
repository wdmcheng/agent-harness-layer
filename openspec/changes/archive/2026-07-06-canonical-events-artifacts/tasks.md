## 1. OpenSpec 与测试基线

- [x] 1.1 运行 `openspec validate canonical-events-artifacts --type change --strict`，确认本 change artifact 可解析。
- [x] 1.2 新增 public seam tests，覆盖 terminal event 唯一性、seq 单调与续读、jsonl fallback、artifact payload_ref、redaction 和 OTel mapping facade。

## 2. Event model 与 bus

- [x] 2.1 实现 `agent_harness.events.types` 中的 `CanonicalEvent`、event type 和 terminal status 规则。
- [x] 2.2 实现 `EventBus` / `EventSink` interface 与 local jsonl sink，保证 per-run seq 和 terminal uniqueness。

## 3. Local evidence 与 artifact

- [x] 3.1 实现 local jsonl sink，可写入、按 `run_id` 过滤、按 `seq` 续读。
- [x] 3.2 实现 filesystem artifact store、payload_ref、checksum 和 inline threshold 策略。

## 4. Security、OTel 和 SSE

- [x] 4.1 实现 secret redaction 与 guardrail/context assembly 摘要 payload helper。
- [x] 4.2 实现 provider-neutral OTel mapping facade，不引入 vendor SDK。
- [x] 4.3 新增 `templates/service-app/app/api/sse.py` SSE formatting seam，并运行 `make quality`、`make test`、`make smoke-local`。

## 5. 验证证据

- [x] 5.1 Contract tests 覆盖完整 CanonicalEvent envelope、P0 event type catalog、guardrail blocked 命名、reasoning 默认隐藏、seq 续读和 terminal uniqueness。
- [x] 5.2 Contract tests 覆盖 payload_ref/checksum、secret redaction、OTel mapping 和 SSE formatting。
- [x] 5.3 `scripts/import_boundary_check.py` 和 `make quality` 确认 event/observability seam 不引入 provider SDK。
