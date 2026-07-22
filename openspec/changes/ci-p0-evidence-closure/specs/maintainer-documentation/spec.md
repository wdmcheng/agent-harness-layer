## MODIFIED Requirements

### Requirement: 文档区分当前事实、扩展约束和未来能力
系统 SHALL 以当前代码、测试、配置和锁文件为依据描述已实现行为。已经落地并通过本地验证的 CI/CD 与 release automation seam MUST 作为当前仓库能力描述，同时 MUST 明确其本地证据和零外部副作用边界。尚未取得 hosted 证据的 runner 执行、artifact service、environment reviewer、protected ref、secret、真实 provider/registry 集成，以及尚未实现的物理服务拆分 MUST 明确标记为未来能力或 `hosted-unverified`，不得作为已部署、已发布或远端保护已生效的事实呈现。

#### Scenario: 维护者阅读 release process
- **WHEN** 维护者阅读 release process 或 README 的发布章节
- **THEN** 文档把当前可执行的质量门禁、版本计算、release preview、promotion/publish plan、零副作用替身与 wheel/sdist build 描述为本地现状
- **AND** 把 hosted runner、artifact service、远端 reviewer/protected ref/secret 与真实 provider/registry 执行明确标为未验证，不声明已发布或已归档

#### Scenario: 维护者阅读部署和 adapter 边界
- **WHEN** 维护者阅读架构、extension 或 adapter 文档
- **THEN** 文档准确区分当前 API/runtime worker 分进程、当前进程内 provider/repository seam 与未来 model/tool gateway、event pipeline、storage service
