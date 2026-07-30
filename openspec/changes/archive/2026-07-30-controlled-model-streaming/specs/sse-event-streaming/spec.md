## ADDED Requirements

### Requirement: 模型增量只从已提交事件日志续读
SSE `Last-Event-ID` 与 CLI `--after-seq` SHALL 通过既有 run-scoped committed-event reader 读取 `model.output.delta` 和 `model.output.completed`。两种传输 MUST 使用 CanonicalEvent `seq` 作为唯一续读位置，不得保存或接受 provider cursor，不得因重连重新调用 provider。默认公开读取器 MUST 返回 public delta、completed 和 run terminal，并继续隐藏内部 started 与 usage；获得既有内部权限的读取路径可以查看内部事件。

#### Scenario: SSE 在 delta 中途重连
- **WHEN** 客户端已经收到 seq=N 的 delta 后用 `Last-Event-ID: N` 重连
- **THEN** SSE 只返回 seq>N 的已提交事件且保持原顺序
- **AND** provider 调用次数不增加，已收 delta 不重复

#### Scenario: CLI 从 completed 前续读
- **WHEN** CLI 以 `--after-seq N` 读取包含后续 delta、completed 与 terminal 的运行
- **THEN** CLI 与 SSE 使用同一 reader 语义返回 seq>N 的已提交事件
- **AND** 不使用 HTTP 外的 provider-specific 恢复路径

### Requirement: 读取端断开不控制供应商执行
SSE 或 CLI 消费者的取消、超时、慢读取与断开 SHALL 只结束该 reader。传输层 MUST NOT 持有 provider stream 对象、向 invocation 发送取消、释放 provider lease、修改 stream outbox 或触发重新执行。调用执行与事件读取通过持久化 CanonicalEvent 解耦。

#### Scenario: SSE 客户端提前断开
- **WHEN** SSE 客户端在收到首条 delta 后断开
- **THEN** 该 SSE reader 被关闭，provider invocation 继续由原执行 owner 管理
- **AND** 后续安全 delta 和 completed 仍可持久化并由新 reader 续读

#### Scenario: 慢 reader 不形成 provider 内存背压
- **WHEN** 某个 SSE 或 CLI reader 长时间不消费
- **THEN** 只受既有分页和轮询边界限制该 reader 的资源
- **AND** provider invocation 不为该 reader 建立无界队列或保留未提交 SDK 事件
