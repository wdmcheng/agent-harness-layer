# Draft Eval Cases

自动 detector、失败 trace 或人工输入只能先写入本目录。写入前必须完成 secret 与隐私脱敏；draft 不参与正式 eval，也不能自动移动到 approved dataset。

人工审核应通过 `agent-harness eval approve <case_id>` 进入核心 review seam，禁止脚本直接复制文件绕过审核、policy 和 audit。
