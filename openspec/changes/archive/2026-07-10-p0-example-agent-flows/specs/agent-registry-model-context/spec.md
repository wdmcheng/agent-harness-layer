## ADDED Requirements

### Requirement: Agent executor reference 受控加载且不公开
每个 agent config SHALL 显式声明相对 Python module/callable executor reference；`AgentRegistry` MUST 只解析位于该 config 所属 agent package 内、实现 `AgentExecutor` protocol 的入口，并 MUST NOT 在 public `AgentDescriptor`、API response、CLI list 或序列化 payload 中暴露 callable、module object 或本机绝对路径。Executor contract 生效时 MUST 同步迁移现有 basic/fake agent 与测试 fixture；缺少 executor 的 config MUST 形成结构化 validation error，不得隐式回退到固定 `fake-ok`。

#### Scenario: 合法 executor 被内部 resolver 加载
- **WHEN** registry 加载一个 executor reference 指向该 agent package 内的 callable
- **THEN** internal resolver 返回符合 `AgentExecutor` protocol 的执行入口，public descriptor 字段保持不变

#### Scenario: 越界或无效 executor 整体拒绝 registry
- **WHEN** executor reference 使用绝对路径、越过所属 agent package、引用缺失 module/callable 或对象不符合 protocol
- **THEN** registry 返回结构化 validation error，不加载部分可运行 registry，也不执行引用目标

#### Scenario: 缺少 executor 不走 legacy fallback
- **WHEN** registry 加载现有或新增的 agent config 而该 config 没有显式 executor reference
- **THEN** registry 返回结构化 validation error，不注册该 agent，也不通过 `RunOrchestrator` 生成固定 `fake-ok` output

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
