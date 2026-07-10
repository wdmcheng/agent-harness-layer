# Approved Eval Cases

本目录只接收人工审核流程确认后的 eval case。应用代码、自动 detector 和普通测试不得直接写入；所有记录必须保留 reviewer、reason、dataset 与 audit evidence。

`make eval` 只读取 approved cases。没有 approved case 时应返回稳定空态，不得退回执行 drafts 或伪造 score。
