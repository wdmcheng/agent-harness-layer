## ADDED Requirements

### Requirement: 模型工具loop全部turn共用root owner与冻结总上限
模型工具loop SHALL在同一root execution-tree shared budget owner下，为每个model turn建立独立reservation/actual，并同时受Agent/deployment单轮上限、owner当前余额和loop冻结剩余token/cost上限约束。Loop累计值 SHALL只由owner已耐久settlement投影，不能由caller或model自报。Tool invocation本身若无model token/cost影响 MUST仍保留独立tool evidence，不能用零model impact掩盖未知外部工具影响。

#### Scenario: 后续turn不能超出loop总额
- **WHEN**前序turn实际usage使loop剩余额小于下一turn最坏reservation
- **THEN**下一model turn在provider前以budget/loop limit稳定拒绝

#### Scenario: Approval恢复使用当前更小余额
- **WHEN**waiting期间同root其他operation消耗余额
- **THEN**approved resume重检owner余额并可在provider/tool副作用前拒绝
- **AND**不得提高原loop hard limit

#### Scenario: Tool unknown不伪装模型零成本即放行
- **WHEN**tool外部影响未知但model usage已结算
- **THEN**loop保持needs-review，新的model reservation与terminal仍被拒绝

### Requirement: Loop exact replay与unknown保持预算幂等围栏
Exact replay SHALL复用每turn既有reservation/actual且不重复扣减；identity conflict SHALL在新reservation前拒绝。Provider/tool/result/commit outcome未知时，相关usage claim、loop state和owner ledger SHALL保持reserved/needs-review，nullable actual不得伪造为零。只有全部owner facts确定并交叉验证后才释放差额或允许terminal。

#### Scenario: Restart不重复reservation
- **WHEN**worker在reservation或actual提交后重启并恢复同turn
- **THEN**repository返回existing claim并保持owner总额不变

#### Scenario: Unknown保留余额围栏
- **WHEN**turn或tool outcome未知
- **THEN**reserved amount不释放且同loop/owner新claim按existing needs-review规则拒绝
