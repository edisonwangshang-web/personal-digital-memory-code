# skill-quality-v1 输出契约

输出仍为唯一 JSON 对象，无 Markdown 外壳。基础对象、Wiki、引用及 Review 字段的类型保持基础契约；新契约仅增加以下必需字段。程序提供的 JSON Schema 是准确结构边界，不要增加其他字段。

## 输入材料的引用解析

- 资料若包装为 pdm-shared-json-v1，先按接入层给定的 root/shared 和保留键转义规则完整解引用，再执行下面的检查。共享引用保留全部原文和对象，不是摘要；输出中的来源 ID、版本、quote、检查标识和正文必须使用还原后的原值，不返回编码引用。
- quality_context.sources 每个来源范围只保存一次 fragments、provided_locators、line_sha256 和 evidence_id。evidence_id 绑定该来源版本及本单元实际提供的完整片段集合；它不是“已读取全文”的证明，也不授权读取未提供的行。实际原文仍须从本请求的 sources/source_views 对照，清单或摘要不能替代原文。
- 每个 targets.source_scope_id 指向上述 sources 中相同 evidence_id 的范围。逐项检查 required_checks.source_scope_ids，并按其 evidence_ids 使用允许的来源范围、目标或对象标识。一个来源范围 ID 可指代其全部已提供片段，无需在每个检查中重复逐个 fragment_id；仍须实际核对相关行，不得仅回显 ID 冒充检查。分解单元只能使用其投影后的范围；只有证书而无原文的 global 单元不能宣称重新读取了原文。
- 统计材料使用共享定义减少重复：每条 statistics 由 stat_id、target_id、scope_id、operation、value 组成。先按 target_id 找到 targets，再按 scope_id 读取该 target.statistic_scopes 中的实际样本、日期和源行；算法、缺失／未完结／重复处理及舍入以 statistic_definitions 为准。必须连同这些依据理解统计，不把数值单独当原报表字段。审核单元未提供完整相关范围时不会携带这项统计，不能从其他对象或证书猜补原文。
- target.source_locators 是完整计算依据，包含被排除的行；statistic_scopes[scope_id].source_locators 是该统计实际使用的行。计数统计的 valid_n 表示扫描行数，value 才是命中数；例如扫描 60 行、有值 44 行，不等于 44 天完整佩戴或其余指标也完整。不得混淆计数分母、有效数值样本和日期覆盖。

## extract 的 quality

普通提取输出 objects、wiki、quality；batch-v1 输出 objects、wikis、quality。

quality 必须含：

- coverage_disposition：数组。每项有 target_id、status（summarized 或 deferred）、record_ids（字符串数组）、body_evidence（每项 record_id、text）、reason。target_id 必须来自 quality_context 给定目标。summarized 对应真实正文证据；deferred 说明缺口且不得虚构正文。
- statistic_claims：数组。每项有 stat_id、record_id、value（字符串）、body_evidence（正文准确子串）、sources（source_id、source_version、locator、quote）。stat_id 和 value 必须与程序给定统计完全一致。没有使用统计时为空数组；不得隐藏正文已使用的统计。
- temporal_claims：数组。每项有 record_id、statement（正文准确子串）、basis（source_time、current_state 或 unknown）、source_refs（同上述引用）、system_fact_id（字符串或 null）。当前系统状态须指向程序给定 system_fact_id；来源当时的陈述明确其时间，未知时间不补造。

quality 新增的引用 quote 必须为 locator 完整行文本，不能只截取关键词；行号保持原文全局行号。

## review 的 checks

输出 verdict、reasons、checks。checks 是数组，每项必须有：

- check_id：required_checks 中的准确 ID。
- scope_sha256：该检查范围的准确摘要，不重新编造或扩大范围。
- result：passed、needs_revision 或 needs_confirmation。
- reason：实际核对的结果与依据。
- evidence_ids：本次检查实际使用且当前 required_checks 对应项允许的材料/对象标识，字符串数组；来源范围 ID 按上面的输入材料规则解析。非 global 检查要报告 passed，必须有实际原文范围，且返回该检查全部 source_scope_ids；不能只回一个来源或仅用候选对象自证。

必须逐项覆盖当前 required_checks；遗漏、重复、额外或范围错配均无效。verdict 必须与逐项结果一致：不能在某项未通过时报告整体 passed。结果必须来自独立对照，而非复述候选自报。
