# 数据规范

本文是公开工程的数据契约；`memory/`、控制、账本和运行证据的路径均指独立私有资料工作区，公库不保存真实个人资料或私有验收结果。当前程序 `0.2.0.dev25`，新任务默认 `knowledge-extract@10`／`knowledge-review@8`；基础提示词、不可变 Skill 资产、程序版本与资料版本分别绑定。M1 尚未整体验收，实际定时 runner、两轮真实触发和第二模型读取仍须独立证据。

来源及采集 `0.1`、正式对象 `0.2`、新10/8质量链 `0.12`，旧9/7为 `0.11`、更早质量链 `0.10`；已支持格式 `0.1`—`0.13` 按各自 kind 契约读取，不批量迁移；`0.13` 仅用于下述专用已完成提取恢复门禁，普通10/8候选与质量链仍为 `0.12`。分节中的 dev 标记说明契约引入时间，不表示切回旧默认。字段以本页为准，执行见[实现明细](implementation-details.md)，检查见[场景验收](scenarios-and-acceptance.md)，交付与兼容回退见[发布与回滚](release-and-rollback.md)。

current 证据可显式指定正式 `object_paths`；主题自动包含 `metadata.records` 中精确版本的依赖。coverage 分开报告请求对象、实际提供对象、依赖对象及未选无关来源；所选对象齐全不代表读遍授权原文或全库。原文片段保留全局行号与未读范围，压缩视图不重复发送 `source.body`；旧证据和限定读取包形状保持。当前10/8沿用固定引用 `knowledge-context-v1`，检查来源、时间、范围及已提供关系的自足性，旧审核只证明其原版本。

<a id="explicit-knowledge-rebuild"></a>

## 显式清理后重建回执

只在用户明确清理后重建时创建。路径为 `memory/runs/processes/rebuilds/<intent_id的SHA256>.md`，封套严格为 `schema_version: 0.4`、`kind: knowledge_rebuild`，正文契约 `explicit-knowledge-rebuild-v1`。同一提交新增回执并删除列出的现有正式目标，不能混入其他写入；本版不迁移旧数据或变更默认 Skill。

| 字段 | 绑定 |
| --- | --- |
| `intent_id` | 操作者给定的稳定清理重建意图；同 ID 不得换来源或目标，也不再次删除 |
| `base_head`、`branch` | 清理前精确资料父提交和实际分支 |
| `sources` | 完整获准来源及其版本、原始保存和采集回执绑定 |
| `controls_digest` | 原清理与本次使用所需的控制摘要；重建不覆盖纠正／删除权限 |
| `targets` | 被清理的正式路径、原始文件 SHA、对象 ID 和内容版本；全部引用须在选定来源内，主题依赖须齐全 |
| `created_at` | 实际清理记录时间，不作为个人事件时间 |

返回并传入加工的 `rebuild_context` 严格只有 `path`、`commit_sha`、`sha256`，分别固定该回执、原子清理提交和完整字节。读写均核首次新增、原子删除、完整父链、回执未改写及来源／控制范围；路径在清理后又被写入或删除时，不能继续以此回执豁免 create 检查。普通无回执的历史删除仍拒绝重建。历史候选按其固定 `base_head` 核原授权，不把当前已发布文件误认为旧清理失败。

`rebuild_context` 进入本次候选、请求和处理身份。生产工作及分解审核身份绑定有效回执；同一回执换队列、日期或工程版本不重置最多四次尝试。清理不会删除旧失败、unknown 或旧计数，也不让旧输出冒充本次结果。当前重建限单批完整来源，输入容量不足在新预约前阻塞。

模型仅从本次来源重新生成正文、统计声明和分类，首次不提供旧 related／revision／merge／复用输入；新候选退回后，`revision_from_run`仅可引用同回执、同来源范围与批次的本轮候选及实际反馈，不能借此引入其他generation。原始返回仍原样保存并接受完整独立 Review。程序没有替模型补写统计声明的新例外。清理回执及工程检查不代表真实业务或 M1 验收通过。

<a id="quality-completed-recovery"></a>

## dev22：已完成返工提取的受控恢复

仅对固定10/8的原生返工提取适用：真实账本在原 pool／授权／operation 下仅有 `extract=completed`，原 failure 为 `candidate_too_large`，尚无 stage、Review 或正式发布。核对原 started、revision_attempt、完整输入、冻结 Skill／规则、原生请求字节、invocation、来源／related及控制；缺失、变化、已启动 Review 或 unknown 均不能恢复，更不能重新提取。

专用证明契约为 `quality-completed-extraction-revalidation-v1`，原预约表示为 `quality-known-completed-reservation-v1`。`response_provenance` 必须明确为 `operator_attested_local_cache_no_prior_git_output_digest`：这是操作者核定并冻结的本地原始返回，不是旧 Git 已保存的输出 SHA，也不是服务端签名或服务端输出摘要。`original_response_sha256` 等新摘要只绑定本次核定材料，不能追认为过去已有的远端证明。

证明固定原 payload／response、原生 request binding、四份缓存文件的独立核定摘要、原调用及账本身份／完整字节摘要、原 failure／started／revision 指针和提取版本。先准备目标新 plan，再生成证明固定 `validation_head`；到候选落盘前若 HEAD、账本或缓存变化即停止。账本和凭据仍在库外，不因恢复复制到公库或假装随 Git 恢复。

恢复沿用原 run／operation，零新 extract，不清旧 failure、queue、counter或 unknown；候选分别记录原提取版本和当前校验版本。候选 stage 仍为 `0.12`，同时写入 `memory/runs/processes/<run_id>/quality-revalidation-guard.md`，kind 为 `quality_revalidation_guard`、schema 为 `0.13`。候选与门禁必须同一原子提交，候选保存门禁路径及摘要；门禁缺失、换版本或不同提交均拒绝。旧程序不认识0.13时须在写入前拒绝。

预约前还检查已知当前核心与历史材料的存储下界；若下界已经超限则零调用停止。该检查不能预测未知模型输出，实际候选落盘时仍核完整核心与总量。仅此恢复契约的Review视图去掉candidate内重复的revision_context、revision_revalidation、revision_resume和quality_revalidation_guard_ref；请求根处的完整语义反馈、当前对象、目标、统计和required_checks保留，原始payload／response及完整历史继续在私库Git中受guard绑定，库外缓存原字节按独立摘要核验。超出模型容量仍按既有分解审核规则处理，不借存储例外提高模型输入上限。

恢复后的实际内容仍须完整独立 Review；只有完整证明通过才可按目标 plan 发布。已有真实 group 的 marker，只有在 plan 完整值、固定 Skill、来源与候选证明均匹配后，新10/8队列才能持久保存 `published`、publication、revision及 adopted 记录。已有 `final_run` 不得换成另一证明；重复采用零写／零模型调用，原失败队列与计数保持。旧9/7仅返回 effective published 的历史行为不变。

<a id="quality-completed-recovery-v2"></a>

### dev25：同一重建内的日期校验重验

`quality-completed-extraction-revalidation-v2` 是独立、显式选择的恢复合同，只接同一 `rebuild_context` 的原生 `extract=completed`、原失败为 `quality_temporal_mismatch` 且未保存候选／Review／发布的调用。原来源、批次、同轮真实反馈、原请求及 Skill、重建回执、控制、operation／pool与调用记录必须完整匹配；unknown、已启动审核、换 generation 或其他错误均拒绝。旧 V1 仍只接 `candidate_too_large`，默认调用不升级为 V2。

V2预留契约为 `quality-known-completed-reservation-v2`；调用端显式用 `prepare_attestation(..., contract=CONTRACT_V2)` 准备。证明在原字段上固定精确 `rebuild_context` 引用，原 payload／response和完整同轮反馈不改写。`response_provenance` 仍为 `operator_attested_local_cache_no_prior_git_output_digest`：操作者核定并冻结原始缓存及摘要，不把原 failure 的 invocation／model／elapsed 摘要追认为原输出正文 SHA 或服务端签名。

恢复沿原 run／operation，零新 extract，以当前程序重新核原输出；不补填模型字段、不清除原失败／unknown或计数。同轮反馈只有在原调用中实际提供且同回执绑定时才保留，不能替换原文或变造原响应。有效候选仍为 schema0.12，使用原 `quality_revalidation_guard`、schema0.13，guard 的合同标记为 V2；stage 与 guard 仍原子保存，字段及历史证明按合同分支校验，不新增格式迁移。

合格只表示通过当前程序检查，随后仍须真实独立 Review、完整发布及读取验证。日期歧义或其他质量问题再次出现时按失败处理，不能覆盖原失败、放宽普通质量检查或直接发布。

若原尝试仍为 `failed`／`quality_temporal_mismatch`，但同一run已通过V2形成完整可核候选、guard及真正 `needs_revision` Review，production可在核准同一重建回执、范围、批次与精确证明后，采用这份最新审核意见进入后续正常返工。原failure和failed counter保持，后续仍受原有四次上限及预约前完整预检约束；非V2、证明缺失、没有真实退回审核或unknown不能借此接续。

<a id="candidate-storage-limits"></a>

### 候选历史存储与模型输入分开限额

仅固定10/8、schema0.12且路径／run一致的 candidate，可使用最多 **4 MiB** 的完整存储包；剔除 `revision_context`、`revision_revalidation`、`revision_resume` 三类明确历史字段后的当前计算核心仍须不超过 **1 MiB**。该例外只承载可核验历史，不扩大事实正文或赋予发布权限；完整绑定及历史证明照常核验。

其他受管Git文件仍最多1 MiB，来源输入最多1 MiB，完整模型请求仍最多256000字节，单次多文件事务仍最多8 MiB。不能把大正文移进历史字段、改路径／kind／Skill或只改版本号来获得例外；总量、核心大小和实际读写均检查，超限保留原记录并停止。

## dev21：质量完整性与反馈接续（schema `0.12`）

10/8候选额外固定 `reuse_quality_requirements`，每项为 `{record_id, content_version, stat_ids}`，由已核验旧发布的精确记录及同定义统计重算。模型显式输出原格式statistic_claims，程序对同路径逐字未变正文核必需集合（仅改标题／标签也不豁免）；不存在可继承义务的资料允许空集合。raw输出不修补，伪造来源、版本、证明或遗漏须拒绝。

渲染选择 `quality-model-projection-v1`，模型视图顶层 `quality_projection` 绑定完整原材料摘要，能重建的profile行表变为 `line_integrity`（契约、完整映射摘要、行数和重建规则）。持久 `quality_context` 不变；解投影用同包原始全局行恢复并核全部绑定，缺必要行的合法单元保留原表，未知版本或篡改拒绝。语义事实、目标、统计及完整反馈不减少。

新queue／attempt、候选／Review及其他质量族header为0.12，首次started和revision_attempt也为0.12；来源0.1、formal0.2、group0.6、audit2不迁移。production的新attempt可固定 `revision_from_run`、原context摘要、原candidate绑定及旧／新planID，只允许同来源／范围／related／controls／merge兼容的真实退回；新proof绑定新plan，不消费旧passed替代。四次稳定尝试上限、unknown先对账和CAS保留。只有完整final候选自动接续，draft／continuation明确blocked_context。

安全调用错误增加固定 `cli_auth_required`、`cli_permission_denied`、`cli_rate_limited`、`cli_service_unavailable`、`cli_network_failure`、`cli_request_rejected`；未知／冲突仍为 `cli_turn_failed`。不保存原message、stderr、URL或凭据，启动后失败仍保守记unknown，不自动重放。

## dev20：显式原样复用（schema `0.11`）

显式 `reuse_refs` 输出由9/7引入，当前默认10/8沿用该结构并增加完整性检查。引用项恰含 `path`、`content_version`；路径只允许本次实际提供且已验证发布依据的related record／topic，版本须与该固定对象完全一致。引用本身不携带可覆盖旧事实的正文，也不能增加来源权限。

新建／变化项继续输出全文；objects与wikis的原始数组可因明确引用而为空，普通wiki允许为空值，但物化后的完整输出仍须满足原记录数量、Wiki数量及精确成员版本。旧合同不接受新增键，也不能将缺省或空输出推断为reuse。重复引用、未知路径、错误版本、全文冲突及变化记录配旧Wiki均拒绝。

原始模型JSON和调用输出摘要保持，物化对象及其关联解析进入固定候选并可从Git历史重新验证。quality的目标去向、统计和时态仍核完整物化正文；Review读取完整候选，不因reuse减少审核义务。该9/7合同运行族使用 `0.11`，后继10/8使用 `0.12`，旧8/6质量链保持 `0.10`；来源0.1、正式对象0.2、group0.6及audit账本保持原格式。

## 无损模型传输契约

显式 extract@5／review@4 起使用版本固定的字段字典 v2；显式 extract@6／review@5 起使用完整可逆列式 v3，相同字段对象数组保存列名及全部行值，先还原列式再解 v2／v1。后继引用按其冻结编码配置执行；全部旧 role refs 保留原请求字节。v2仅替换可还原的对象键名，原字符串值、共享值和转义语义完整保留；解析先核别名、转义、唯一性和界限，再恢复键名并执行原v1解引用，最后逐canonical值核原JSON。冲突、未知别名、重复原名、未用定义或不匹配均拒绝。实际请求binding含编码及全部解码说明。业务输出及持久化source0.1／formal0.2／quality0.10／group0.6不迁移，输出不得使用别名或共享表ID。

<a id="skill-quality-contract"></a>

## M1 Skill 引用与知识质量契约（10/8为 `0.12`，旧9/7为 `0.11`，更早为 `0.10`）

显式bundle启用`quality_contract="skill-quality-v1"`，新10/8合同使用0.12，旧9/7使用0.11，更早质量运行使用0.10；来源／采集0.1、正式知识0.2及历史运行不迁移。

### Skill 引用、展开与运行绑定

CLI 分别提供 `--extract-skill`、`--review-skill`，加载器接口为 `resolve_skill_refs(extract_skill_ref=None, review_skill_ref=None)`；新任务默认 extract@10／review@8，历史 extract@1—@9 与 review@1—@7 保留原字节。底层 API 的 `None` 继续旧契约，不能借此给新质量链降级。引用解析为包含 `SKILL.md`、共同质量规范、输出契约和冻结基础提示词的完整 `skill_bundle`。两个角色共享 `knowledge-quality-v1`，来源正文不能改变本次引用或权限；未知版本、外部路径或未声明依赖不自动发现。

`skill_bundle` 顶层为 `schema_version="skill-bundle-v1"`、`quality_contract`、`skills`、`shared_policy` 和 `sha256`。`skills.extract`／`skills.review` 各保存 `ref`、`role`、`version`、`manifest`、`files`、`base_prompt`；每个内容快照包含 `path`、`content`、`sha256`、`bytes`。manifest 和完整依赖按注册摘要核验，bundle摘要覆盖除其自身 `sha256` 外的规范JSON；角色和共享规范必须一致。设计阶段的 `skill_binding` 在实际格式中由完整bundle与请求绑定共同承担，不另存同名字段。

候选固定 `quality_contract`、完整 `skill_bundle`、`quality_context` 和 `extraction_request_binding`。最后一项为实际展开请求的 `{sha256, bytes}`；普通Review保存对应 `request_binding`。分解审核的 plan、reservation、result 各绑定其实际单元材料与请求，root结果是最后完成或首个拒绝节点，不能要求它冒充全部单元检查。完整规则随候选快照恢复，读历史不依赖当前安装的 Skill 或提示词；缺内容或摘要不符即拒绝，不以最新版替代。原生请求、认证和缓存答案无需复制入库，请求按冻结规则和输入确定性重建。

模型请求须实际包含所引用的规则、模板及本单元必要依赖内容，完整输入容量包含这些字节。必要脚本由编排按固定版本执行并记录输入／输出依据，脚本名称不能替代执行结果。加载仅覆盖显式引用及声明的必要依赖，不启动全局 Skill 发现或任意工具；原文范围、工具能力和正式写入继续由程序控制。包缺失、角色不匹配、依赖不全、摘要不符、输出契约不兼容或必要材料超容量时，在对应调用前阻塞，不能降级为只发旧提示词后继续发布。

### 覆盖、派生统计和时态的实际材料

extract@7 起的动态输出 Schema 按当前 targets 实际数量约束 `coverage_disposition` 长度；目标 ID 非空且唯一，清单可为空或超过256，不引入额外资料上限。长度不替代逐目标唯一性和实质正文校验，旧提取1—6及审核1—5的 Schema 与请求原样保留。

summarized 的 `record_ids` 为唯一 ID 集合；`body_evidence` 可为同一记录提供多段不同精确正文，必须覆盖全部 ID，额外 ID 或重复 `(record_id,text)` 拒绝。`source_time` 接受明确 ISO 日期和中文年月日；中文月日只在实际来源引文中可唯一确定同一完整日期时成立，状态句也须有对应日期限定。无日期依据、年份歧义及冒充当前系统状态仍拒绝，不从当前时间或采集时间补年份。质量声明修复可构成实质返工，但须绑定真实拒收候选与实际审核，不能手改模型输出。

`quality_context` 由 `prepare_quality(sources, source_views=None)` 确定性生成，顶层包含 `contract`、`policy_version="source-quality-v1"`、`statistics_policy="descriptive-v1"`、`sources`、`targets`、`statistics`、`statistic_definitions`、`system_facts` 和 `scope_note`。候选读回时按固定原文及实际片段重算，不能把模型自报清单当程序事实。共享范围描述避免逐条重复同一元数据，实际请求容量仍以展开后的完整字节判断。

| 材料 | 至少保存什么 | 约束 |
| --- | --- | --- |
| `quality_context.sources`／`targets` | profile以`evidence_id`绑定来源版本、`provided_locators`、实际片段、逐行摘要、`format`与`tables`；表格包含列名、结构列、日期范围、重复／无效日期、部分日和缺失标记；target用`source_scope_id`引用该范围，并保存角色、单位、计数和限制 | 完整实际片段在source profile中保存一次，原坐标与BOM不改；仅统计实际提供范围。结构列作为限定，观测／目标列形成指标target；文本形成需要独立语义核对的target，不宣称程序已找全主张 |
| `extraction_output.quality.coverage_disposition` | 每项 `{target_id, status, record_ids, body_evidence:[{record_id,text}], reason}`，状态为 `summarized` 或 `deferred` | 精确覆盖target集合，记录与正文片段真实存在并引用对应来源；暂缓必须具体。结构完整仍需模型审核内容是否充分 |
| `quality_context.statistics`／`quality.statistic_claims` | 程序统计绑定统计ID、指标、操作、字符串值、单位、有效样本、日期及原行范围、算法和缺失／部分日／重复政策；模型声明 `{stat_id, record_id, value, body_evidence, sources}` | 结果按固定Decimal口径重算，声明值与程序值及正文一致、引文回到原行并属于对应record。禁止将派生值伪装成原报表列；未知单位不生成数值描述统计 |
| `quality.temporal_claims` | 每项 `{record_id, statement, basis, source_refs, system_fact_id}`；basis为 `source_time`、`current_state`、`unknown` | 状态句须映射正文并有时间限定；当前状态须匹配已核定system fact。本版prepare默认`system_facts=[]`，不自动认证当前入库状态，不能凭旧导出说明写成当前事实 |
| `required_checks`／Review `checks` | 程序生成必需项的`check_id`、`scope_sha256`、`object_paths`、`source_scope_ids`、`target_ids`、可用`evidence_ids`及指令；模型返回`{check_id, scope_sha256, result, reason, evidence_ids}` | 来源范围ID指向实际source profile，范围摘要仍绑定完整资料，不反复复制逐行fragment列表。精确覆盖预定检查项，缺项、重复、错范围或无依据拒绝；`verdict`按逐项结果归并，任何needs_revision优先，其次needs_confirmation，否则passed；保留`reasons` |

提取顶层沿用 `{objects,wiki}` 或 batch-v1 的 `{objects,wikis}`，仅新契约增加必需 `quality`，内含上述三个数组。旧调用不能加 `quality` 绕过原严格结构，新质量调用也不能省略后降级。描述统计规则包括有效样本均值／最小／最大及按固定日期中点划分的两段比较；两段各须至少3个有效日，重复日期全排除，未完结或完成情况未知时按声明政策排除，不能把阈值称为统计显著或健康标准。

每条`statistics`实际保存五键`{stat_id,target_id,scope_id,operation,value}`。对应`target.statistic_scopes[scope_id]`保存`unit`、`valid_n`、`date_interval`、`source_locators`和`partial_policy`；来源身份和完整依据范围由target的`source_id`／`source_version`／`source_locators`承担。共享`statistic_definitions`固定`resolution`、`missing_policy`、`duplicate_policy`、`rounding`、`count_denominator`、`segmentation`及`operations`。`resolve_statistic(context, stat)`展开并核对统计ID与来源、完整范围、样本范围、数值和规则的绑定；共享存储不减少实际统计项或删除证据。

分解投影要求单元实际提供整个target的依据原文才可使用该统计；仅看过有效采样行，不足以判断被排除的缺失／部分日／重复行。画像与完整请求的容量检查不等于模型已经读过或正确解释了真实资料，实际数字和语义验收分别记录。

### 分解审核的容量与证书投影

输入容量以固定规则、真实`source_views`、质量上下文、候选、必需检查和证书展开后的完整请求计算。逐行fragment不在每个target和检查项重复展开，而由`source_scope_id`／`source_scope_ids`引用实际范围；范围ID、逐行摘要和质量上下文共同参与绑定，不能用名称替代原文。object、coverage与global保持原有完整语义分工；coverage对照实际批次／draft范围及完整最终候选查漏，不能只检查提取者主动声明的统计。跨来源一致性由global及完整proof汇合核验。

`pdm-shared-json-v1`是共享值的基础无损表示：`{format,root,shared}`保存输入根与完整共享值表，只有`{"$pdm_ref": id}`的对象指向共享表中的完整值。原资料对象含`$pdm_ref`或`$pdm_literal`保留键时，编码器使用`$pdm_literal`转义，避免把资料误解释成引用。只共享能减少字节的重复值，其余完整内联；原文、统计、上下文、候选和检查项均能解引用还原，不靠删除辅助材料或拆小语义范围满足容量。

当前默认 `knowledge-extract@10`／`knowledge-review@8` 按显式角色版本渲染为 `pdm-columnar-shared-json-v3`：先还原列式结构，再还原 v2 字段字典和 v1 共享值。v1 基础及旧角色的历史渲染继续保留，不能把新任务全部视为 v1 请求。

`encode_shared_json`在每次编码时调用`decode_shared_json`核对规范JSON与原输入相等；解码拒绝循环、悬空引用、非法结构及超限展开。模型请求同时提供解引用说明，模型输出仍按原业务schema，不能用共享ID代替真实来源、quote、正文或检查标识。`request_binding`和容量门禁核实际编码后的完整线上字节，包括规则与编码说明；编码前原payload审计和完整候选继续保留。旧契约请求维持原字节渲染，历史输出不追写共享引用。

每个分解请求的`candidate.quality`仅投影该单元对象、target和实际可见统计范围内的声明，并通过`quality_declaration_scope`保留范围与未投影的统计声明ID。声明中的来源引用只给来源版本与locator，重复`quote`不成为绕过原文范围的第二条正文通道；实际原文仍在该单元`sources`和固定候选引文中。global的声明仅作一致性输入，其依据是前序证书，不能据此声称重新读过原始资料。完整提取输出仍保存在候选中，投影缺项不等于原候选没有该项。

global接收`certificate_contract="verified-quality-conclusions-v1"`的程序已核验证书投影：保留每项`check_id`、`scope_sha256`、`result`、`reason`以及总`verdict`／`reasons`，证据ID数组改为`evidence_ids_sha256`，并保留`complete_output_sha256`和`complete_checks_sha256`。完整模型返回仍持久保存在原节点result中，完整proof读回和汇合时逐项重验；投影不能用来省略必需检查或替换原始审核结果。global读取完整最终知识、关系与前序结论，不把证书描述为其亲自读过的原文。

上述共享描述和分工只去除请求中的重复材料；必要规则、完整语义原文、最终正文或必需证据仍超容量时阻塞，不抬高限制、不截断证据。工程测量与真实模型执行分别判定，证据留在相应工作区。

表格画像和派生统计属于程序生成的加工依据，不是新的独立原始来源。每项正式结论仍能回到固定原文及实际计算过程；统计引用保存计算所涉及的原行范围与方法，不把计算值塞进原文 quote。每个模型调用只可使用实际提供给该单元的范围，局部统计不得标成全期结论。全期概览必须有覆盖全期相关依据的校验与审核证明；分解审核按固定候选合并各单元义务，不能把整份画像当作已经读取未提供正文的证明。

对话与文章的主张、限定、决定和纠正清单由模型提出，独立审核须回看原文并补查遗漏；程序可核原片段范围、清单结构和知识映射，不宣称能够确定性证明文本语义毫无遗漏。必需检查清单要求覆盖实际原片段及相应质量维度，不只照抄提取模型主动列出的事项。

M1 的描述性概览可包含有记录天数及符合明确可比条件的分段变化；每项统计都须带区间、有效样本数和计算依据。数据不足时写明无法判断对应变化。健康原因解释及个性化建议仍属后续分析范围。旧规则禁止擅添原报表未给出的平均／总量／变化语义；质量契约允许明确标识且可复算的派生统计，仍禁止擅改原字段口径。

### 发布、恢复与兼容

新质量材料与 Skill 绑定共同进入候选、输入去重和审核证明，正式发布重验实际加载材料、覆盖处置、统计计算、时态依据以及最终候选的精确版本。单次和分解 Review 均消费同一固定规范及相应完整范围；一项必需检查失败或尚未完成，相关依赖组不得发布。能力／加载／容量问题记录为执行阻塞，不能伪装成内容审核通过或要求用户决定的知识问题。

Skill bundle摘要进入processing key；工程版本绑定包含实际Skill资产，规则变更不能借旧Review发布。重试计数和未知调用身份继续保留，不因换Skill、run或队列清零。当前10/8的候选／Review／manifest／receipt／failure、queue／attempt、日程事件和分解proof为0.12，旧9/7为0.11、旧8/6质量链为0.10；source0.1、formal0.2、group0.6及旧started／revision_attempt保持原格式，稳定counter身份与既有条目不清零；当前10/8首次预约使用0.12、旧9/7使用0.11，不能让旧writer在模型调用期间继续写新合同工作区。普通返工继承bundle，分解恢复核固定单元；历史0.9 operator的`resume-unstarted-revision`与`revalidate-completed-extraction`缺少原Skill请求证明，对新质量链明确报`skill_quality_recovery_attestation_required`。旧候选按原schema读回，不追写Skill证明，不自动重跑旧知识；兼容与拒写按公开验收定义检查，实际结果另行记录。

## dev11：auto与分解Review实际消费（schema `0.8`）

### batch精确引用的兼容边界

batch 引文契约是：quote只从实际提供的一份fragments[*].lines[*].text复制单行连续短摘，locator固定全局行号Lx-Lx；结论需要多行时拆成多条sources引用，不能用LF重组换行。投影只给line.text，不提供原文换行编码，CRLF原文仍逐字保留。substring与行号校验不放松，不normalize原文或响应；历史合法原始CRLF多行引用仍按旧契约可读可验，不把新提取输出约束误写为旧格式禁用。

### v11阶段：事件时区与报表口径

extract v11仅增加两条通用约束：事件时区只依原文明示，缺失时保持未知，不从采集时间／采集时区／执行环境或常识推导；钟点保留原字符串与精度，不自行换算。报表沿用原列名、明示单位及统计口径，缺失含义明确保留未知，不擅自扩写为平均、总量、变化等未给出的语义。它们是通用提取规范，不嵌入个人数值、日期、失败对象或预设答案；substring、Review规则、schema与程序校验未放松。

### v12起保留：解释边界也是有依据的知识

原始资料的说明、导出覆盖范围、未取得资料、缺失值及字段／时间解释边界都属于应保留的来源知识。资料范围、字段解释record和相关Wiki须带全部影响结论适用范围与可信度的原始限定，并引用对应实际原文；不能只保存观测数值，也不能以一个对象通过代替完整coverage。没有记录不代表事件未发生，资料未取得或指标缺失不能扩写为事实不存在。

### 策略、schema与容量

`prepare_production(..., review_strategy=None)`为新queue保存review_strategy=auto；既有queue缺省沿用原request及legacy-v1，不因升级静默换策略。CLI `production prepare --review-strategy auto|decomposed-v1|legacy-v1`显式选择；strategy参与request、processing_key、candidate binding和原提取复用条件。底层`process_sources(..., review_strategy="legacy-v1")`继续旧缺省，仅batch支持auto／decomposed-v1。

auto先以实际review.md正文、真实JSON序列化及连接文本计算完整Review请求字节，限MAX_INPUT_BYTES=256000；容量内继续一次独立Review，超限才走分解。decomposed-v1 强制分解；legacy-v1沿原单次Review。没有把限制调大、截原文／候选或用少量字节估算代替传输边界。

新production_queue／production_attempts写0.8，兼容读旧0.6／0.7；新策略auto或decomposed-v1的普通加工链（candidate、review、manifest、publication_receipt及按实际产生的run pending／failure）为0.8。旧legacy无context batch链0.6、带context链0.7保持；来源0.1、正式0.2、registry0.4／0.7、pending_registration marker0.7、所有group wrapper0.6及旧日程事件保留各版本。扩大格式支持不等于重跑或迁移真实旧知识。

### 分解计划和独立审核单元

新module为decomposed_review。memory/runs/processes/<run>/decomposed-plan.md的kind为decomposed_review_plan，schema0.8。plan固定run_id、candidate_binding、stage_path／stage_commit／stage_sha256、versions、controls_digest、payload_sha256、source_bindings、完整最终inventory、operations_sha256、units、relation_graph、review prompt摘要／字节数和plan_id。inventory每项含path、id、content_version、object_sha256；plan_id绑定这些精确值。

| unit kind | 实际请求与必须证明的范围 |
| --- | --- |
| object | 每份最终对象完整元数据与正文；Wiki单元同时含它的全部record完整对象和精确content_version，携带相关获准原文及原批次必要前提，不只给quote行 |
| coverage | 每个原draft语义范围及其prior candidate与完整最终对象集合；没有draft边界的未覆盖原批次作为整体义务，不按字节硬拆后冒称无遗漏 |
| global | 完整最终对象正文、精确Wiki成员关系图、前序审核证书及原始fragment IDs；检查整体一致性，continuation原问题与必要回复继续保留 |

所有单元均独立调用Review，invocation_id不能等同提取或其他已用审核调用；每个请求按真实prompt＋JSON再次检查256000字节。必要语义单元、coverage或global本身仍超容量时blocked_capacity，保留原始计划与已完成证据，不声称任意大组都可分解，不允许只靠摘要证明最终正文。

### journal、有限重试及真实发布门禁

每个unit的尝试计数在memory/runs/processes/decomposed-attempts/<identity>.md，kind=decomposed_review_attempts。逐次预约和结果在processes/<run>/decomposed/<unit>/<nn>-reserved.md／-result.md，kind分别为decomposed_review_reservation／decomposed_review_result，均0.8。counter绑定unit实际工作身份与audit池；reservation绑定plan／unit、完整request_sha256／input_bytes、operation_id、attempt及counter的commit／path／SHA；result保存真实call、output和reservation引用。

一个unit首次＋最多3次安全失败重试，总attempt最多4，不随queue／run名称或日期清零。已持久完成结果核验后0调用复用；已启动而结果未知的请求返回unknown并先对账，不借子operation重发；确定尚未外发的安全失败才允许续接。`resume_decomposed_processing(repo, run_id, *, allowed_source_paths, model_call)`先核明确授权，再重验固定候选、程序规则、控制和来源；只续缺审核节点，不重提取。needs_confirmation仍先保存已知Review，再按0.7 marker零模型补登记。

最终review.decomposed_proof保存schema_version、plan_id／path／SHA、stage_commit／SHA、nodes、global_unit_id、status。`verify_decomposed_review`重建完整plan和每个实际请求，验证reservation／result／counter精确字节、commit祖先、独立call及按序覆盖；passed必须包含全部对象、coverage和global节点。普通publish、group消费和历史authority核验均经该门禁，缺proof、错版本／关系、候选变化、删节点或未完成时不得发布。draft_only仍禁止直接正式消费。

## dev10：确认登记与每日生产契约（schema `0.7`）

本节定义0.7执行状态；后继普通分解链为0.8、显式返工链为0.9、旧质量链为0.10，9/7合同另用0.11、当前10/8使用0.12、专用恢复门禁为0.13；旧0.1—0.6文件不批量迁移。原文及采集仍0.1、正式record／Wiki仍0.2、无continuation_context的普通batch链仍0.6、所有group wrapper／清单仍0.6，audit账本仍为库外独立schema2。新0.7用于下述扩大registry、登记marker、带continuation_context的普通加工链、新production queue／attempt及production-v1日程事件；不能据程序版本号改写旧schema。

### 扩大registry与持久登记结果

权威路径仍memory/pending.md、kind仍pending_registry，items字段、稳定key／item_id及open／answered／resolved／withdrawn语义不变。旧0.4按每项source_refs／reply_sources各最多32、object_refs最多64校验；新0.7分别最多256个source_refs、256个reply_sources，object_refs总计最多256（record＋Wiki合计），run_ids及MAX_ITEMS仍最多1000、整个registry最多1MiB。超过旧单项容量才在正常写入时选择0.7；一旦为0.7后续写入保持0.7，不自动降级。只读不迁移旧表，未变事项身份与created_at保留，合法状态更新按原规则记updated_at。

registry可保存不代表后继模型请求一定可执行：production完整来源并集仍最多256，原事项256来源加一份新答复时保留全部引用并返回blocked_capacity，不截原问题依据或答复来凑容量。完整payload、对象和读取预算仍独立生效。

所有新incremental needs_confirmation先同次持久保存已校验的Review、运行pending及memory/runs/processes/<run>/pending-registration.md，再尝试登记统一表；原Review引用的stage提交与父边保留。登记marker头为schema0.7／kind=pending_registration，正文固定run_id、status、source_paths、candidate_binding、stage_path／stage_sha256、review_path／review_sha256、controls_digest、versions、item_sha256、item_id。初始status=registration_required且item_id=null；成功登记后同CAS写registry与status=registered／实际item_id。旧“Review与registry同一次事务”的历史记录保持原字节，新流程不为登记失败丢弃已经完成的Review。

登记失败返回status=registration_required、review_verdict=needs_confirmation、registration_status、registration_error和registration_path。这是已知Review结果，不是unknown调用，也不是已经登记或解决。`resume_pending_registration(repo, run_id, *, allowed_source_paths, expected_head=None)`及CLI `pending repair-registration --repo --run --source`在来源授权前不读正文，重验stage／Review、原始marker、独立调用、程序规则、控制与来源的精确绑定，只补登记，返回model_calls=0／attempted_calls=0。重复恢复沿用同事项；篡改、删来源、规则或控制变化失败关闭，不换run重发模型。登记仍失败时保持marker和诊断，CLI退出4；needs_confirmation本身不允许正式发布。

### 原事项解释与每批必要答复

`prepare_production`新增required_context_sources及continuation_context。前者是本次选定来源中的必要完整答复来源；后者严格含job_id、parent_item_id、question、parent_run_ids、source_refs、object_refs、reply_sources，绑定真实answered表快照、原needs_confirmation链、答复精确采集commit及关联祖先。上下文参与candidate binding、processing_key和Review，不是新的事实或新增来源权限。带该字段的普通加工链按实际产生的kind使用0.7：candidate、review、manifest、publication_receipt，以及运行pending／failure。所有batch_group_manifest／batch_group_receipt仍0.6，processing_attempt与processed_inputs沿原0.4契约；不能把group wrapper或全库一并升级。历史复验按原候选的answered快照核验，后续合法resolved不使旧受审链失效。

每批以`project_batch_with_context`合并本批片段和必要完整答复，保持原文全局行号；提取与独立Review均收到同一原问题和答复。最终merge仍须完整覆盖计划、候选及必要关系；超容量blocked_capacity，不只把“对／不是”脱离原问题送模型，不把问题本身当用户新事实。全部有效答复必须被核验正式record引用，Wiki或模型输入引用不算解决。

### production队列与三个执行门禁

新写production_queue／production_attempts为schema0.7，路径仍在memory/runs/processes/queues/与queue-attempts/；读旧0.6，旧已发布队列只读核验不静默改写。queue的request固定source_paths、allowed_source_paths、allowed_run_ids、容量参数，以及存在时的required_context_sources／continuation_context；状态保留completed_batches、final_run、publication、audit_pool、revision，并保存last_result／active_run_id。registration_required对应next_action=repair_registration，needs_confirmation对应await_reply，unknown对应reconcile，不能统称失败后重跑。queue名／日期／规则变化不刷新既有工作attempt上限。

`run_production(..., execution_guard=None)`可接收受信程序门禁，依次在before_call（每次extract或review启动审计之前）、before_publish（正式group CAS之前）、after_checkpoint（持久队列进度后）调用。daily适配器在每次门禁重读暂停／工作／答复身份，required模式同步并从实际远程读回必要状态；任一前置不成立停止下一阶段。确定Review尚未启动时可沿原提取续审；已知通过不重复模型，结果未知先核对。门禁回调不能仅自报成功来替代真实远程SHA和文件读回。

### production-v1每日总编排与新事件

新创建的continuous-v1计划在configure时缺省注入processing_engine=production-v1，可显式指定legacy-v1沿旧执行；normalize_config及既有无engine的0.6计划不注入字段、不静默切换。finite原0.4／0.5、legacy continuous原0.6事件与其调用契约保持；新production-v1计划事件使用0.7，仍按递增sequence／previous_event保存到memory/runs/schedules/<id>/events/，audit池绑定与首次后最多3次持久失败重试继续生效。

| 新事件kind | data关键字段及作用 |
| --- | --- |
| production_discovered | root_tick_key、trigger、source_paths、budget_pool；固定本次实际获准发现范围，后续drain不偷偷增加新版本 |
| production_started | work_id、queue_id、root_tick_key、trigger、source_paths、allowed_source_paths、related_runs、pending、base_head、budget_pool；work_id由来源／旧运行／pending身份确定，pending保存原问题、job与答复绑定 |
| production_progress | work_id、status、active_run_id、publication_run_id、resolved_item_id；保留执行阶段与已知结果，支持重启续接 |
| production_finished | work_id、publication_run_id、resolved_item_id、source_paths、remote_status；核验正式group及必要registry解决后记录，required需真实confirmed |
| production_tick_finished | root_tick_key、status、work_ids、budget_pool；只对已经完成的work结束根触发，空批和未完成分别处理 |

新plan／run／drain委托实际production队列，范围内支持最多256个明确来源／旧运行；超过8来源通过batch规划和最终多Wiki Review处理，不把全量来源塞入旧单Wiki入口。发现真实answered事项后自动形成后继work；已知needs_confirmation原work在后继发布并核验resolve后对账，不将旧原run伪造成已发布。`remote_mode=required`核条件push／fetch、精确提交和文件回读后才完成；独立production命令的本地published／remote_status=not_requested仍不是远程成功。

## 文档职责与共同边界

[总体方案](overall-plan.md) · [技术方案](technical-plan.md) · [实现明细](implementation-details.md) · [场景与验收](scenarios-and-acceptance.md)

本文统一定义对象、字段、来源、时间、状态及文件组织。其他文档引用这里的定义，不再维护第二套字段表。采集、Review、发布、纠正、重跑和删除的执行步骤见实现明细；实际建设顺序和进度见 [Roadmap](roadmap.md)。

**已确认要求：** 使用 Markdown／LLM Wiki；保存原始来源和历史；正式知识经过模型 Review；未加工原文可带状态参与回答；待确认事项集中管理；第一版不建专门索引，附件只保存链接；文本与链接记录由私有 Git 管理。

## 已实现的来源采集契约

采集只接收调用者明确提交的 JSON 对象，不抓取链接或平台历史。规范化后的单份输入上限为 1 MiB；未知字段拒绝，以下可空字段省略时按 `null` 处理。

| 输入字段 | 当前实现约束 |
| --- | --- |
| `source_key` | 必填非空稳定字符串，最多 512 字符；同一来源重试或更新持续使用同一键，建议含平台和原始对象标识 |
| `source_type` | `conversation`、`note`、`article`、`video`、`attachment_link` |
| `platform` | 字符串或 `null`，不推定来源平台 |
| `original_url` | HTTP(S) 链接或 `null`；不允许 URL 用户名/密码。调用者仍须去除查询参数中的令牌和其他凭证 |
| `acquisition_status` | `link_only`、`partial_text`、`full_text`；表示实际取得内容，不是验证结论 |
| `body` | 字符串，逐字保存；`link_only` 时必须为空且有 URL，文本状态必须非空。对话保留说话者和消息顺序 |
| `original_time` | 已知年月、日期或带时区的 ISO 8601 时间戳；未知为 `null`，不以采集时间填补 |

`source_id` 为 `src-` 加稳定键 UTF-8 的 SHA256；`source_version` 为 `sha256-` 加上述七个字段规范 JSON 的 SHA256。规范 JSON 使用排序键、无额外空格、UTF-8、不转义非 ASCII 字符；可空字段补齐为 `null`，不包含实际采集时刻。实质内容变化产生新版本。

来源文件采用 `---` 分隔的 JSON flow YAML 文件头及 Markdown 原文，文件头含除 `body` 外的输入字段，以及 `schema_version: 0.1`、来源 ID/版本和带时区的 `captured_at`。正文原样保留。实际路径：

- 对话：`memory/sources/conversations/<source_id>/<source_version>.md`。
- 其他输入：`memory/sources/inbox/<source_id>/<source_version>.md`。
- 保存回执：`memory/runs/captures/<source_id>/<source_version>.md`。

回执包含 `schema_version`、`source_id`、`source_version`、`path`、原文的 `commit_sha`、`save_result: local_saved` 与 `saved_at`。原文先提交，回执随后提交，避免要求提交保存自己的哈希。回执记录本地保存事实；可选推送后的远程核对结果由命令返回 `remote_status`，不能把 `local_saved` 当远程成功。

采集命令返回 `knowledge_status: unprocessed`；来源并未因此通过 Review。schema `0.1` 仅用于原文与采集回执；单来源加工文件为下述 `0.2`，多来源运行文件为 `0.3`，删除控制、纠正和统一清单已按下文0.4契约实现，真实业务验收另记。使用与失败恢复见[工程入口](../src/README.md)。

### 已实现的写前 schema 兼容检查

Foundation 的实际写入支持来自已安装程序的 `contracts.SUPPORTED_SCHEMA_VERSIONS`（当前dev25支持 `0.1`—`0.13`，0.13仅为专用恢复门禁；旧程序以其固定提交能力为准，本批不批量迁移既有memory格式），不能仅修改工作区 `pyproject.toml` 声明就扩大数据兼容范围。`GitAdapter.check()` 在检查提交基础和干净工作区后、每次实际写入前核对受管元数据；来源与回执两次提交都执行。M0 `0.1.0` 不认识 `0.3`，`0.2.0.dev1` 不认识 `0.4`，均不能恢复对含新格式的数据仓库写入；不能移除文件或改其 schema 来绕过回退门禁。

受管范围为 `memory/sources/`、`staging/`、`records/`、`topics/`、`runs/` 与 `memory/pending.md`。Markdown 仅无缓冲、有界读取 JSON/YAML 文件头，最多64 KiB，不预读正文；受管目录中的 JSON 及其他不支持格式不打开正文，按无法确认处理。说明用 README／`.gitkeep` 跳过，个人概况与领域模板不在探测范围，不能把此检查称为全库事实或数据有效性验证。

固定状态为 `compatible`、`incompatible`、`unknown`；只在全部受检文件可确认支持时 `writable: true`。报告仅含固定范围、受检／未知／跳过计数和已知版本白名单，不回显任意 schema 值或个人内容。已识别 `pdm-1` 仅用于拒绝混用，不代表能读取、迁移或继续写入 runtime 数据。

不兼容、未知、损坏、无法访问或超过扫描预算均拒绝写入；最多扫描10000项、递归32层，达到上限也不猜测剩余数据兼容。符号链接不跟随，返回既有 `path_rejected`；其他拒绝使用 `incompatible_memory_schema` 或 `unknown_memory_schema`。检查不改 Git、原文、回执或删除控制。跨分支背景见[分支核对](branch-reconciliation.md)。

## 已实现的 M0 加工契约（schema `0.2`）

开发版本 `0.1.0.dev3` 仅处理一份明确指定的 Foundation `0.1` 文本来源，不迁移 Runtime 的 `pdm-1`。运行 ID 为 `run-` 加 UUID32；候选在 `memory/staging/<run_id>.md`；审核、失败、待确认、发布清单及发布回执在 `memory/runs/processes/<run_id>/`，均用 `---` 分隔的 JSON 文件头和规范 JSON 正文。正式对象／主题为 `memory/records/<id>.md`、`memory/topics/<id>.md`，正文为 Markdown；ID 为受限 ASCII 稳定标识。

候选保存来源 ID／版本、路径、来源提交和原文／采集回执字节摘要、实际加载程序摘要、规则摘要、工程提交、模型调用 ID／实际身份／耗时／可取得的用量、提取输出、对象及绑定摘要。Review 是独立调用，绑定候选提交、字节摘要、语义绑定与另一调用 ID，返回 `passed`／`needs_revision`／`needs_confirmation` 及具体原因。Review 不等于事实核验，不回填未知原始时间。

正式发布重新验证这些绑定且要求目标从未存在、无删除历史，记录、主题和发布清单一致提交。清单列出正式文件摘要及受审来源／规则／程序版本，回执随后指向真实发布提交。重复 publish 同 run 只核对并补回执，不重复发布。process 新尝试使用新 run，不宣称已实现跨运行语义去重。代码包排除全部 memory 数据。

本节的历史 M0 格式为 create-only 手动闭环，不能用于更新已有事实。该旧流程遇控制文件停止；`needs_confirmation` 保存运行级 pending。统一待确认、修订续接、增量控制及调度使用后续分节契约。来源证据入口只读取指定来源，可选核验发布，不把历史回溯或引用当成当前有效性判断。

## M1 多来源运行契约（程序 `0.2.0.dev1`）

一次 `process` 显式选择 1—8 份已采集来源，CLI 通过重复 `--source` 提供路径。重复路径／来源、同一 `source_id` 的多个版本、超过上限或未取得有效采集回执均拒绝。单来源保持上述 `0.2` 契约与原行为；选择多份时，候选、Review、发布清单、发布回执、失败和运行级 pending 文件采用 `0.3`。来源／采集回执仍为 `0.1`，正式记录与主题仍为 `0.2`，现有数据不迁移。

多来源运行保留原 `source` 主绑定，仅在多来源时追加 `additional_sources` 列表；整体按来源路径排序。每份绑定保存来源 ID、版本、路径、原文提交、原文字节摘要和采集回执字节摘要。Review 绑定完整候选摘要，发布回执绑定完整清单摘要；通过这条链核对完整来源集合，不要求回执重复存一份来源列表。加工、独立 Review、发布复核及证据读取均覆盖完整集合，不能丢掉其中任一份，也不能引用未选择的来源。任一来源或回执缺失、被篡改、版本变化时，旧候选和审核不能继续放行。一个知识对象或主题可在 `sources` 中引用多份已选来源，并用 `domains` 关联多个领域，共同事实无需按领域复制。各项引用仍须定位该来源版本中的实际原文片段。

`link_only` 仅提供链接、来源类型、平台及获取状态等元数据，不生成正文行号或正文引文。全部来源均为 `link_only` 时，不调用加工模型；有文本和链接混合时可以加工文本，链接不能充当正文证据，不凭标题或链接编摘要。Review 仍是与加工独立的调用，完整请求（包含所有来源、候选和规则）超预算时拒绝，不静默裁剪。

`evidence`／`ask` 同样接受重复 `--source`，只读取明确选择的集合；提供 `--run` 时必须与该运行绑定的来源集合一致，顺序无关，核验全部来源、候选、Review、正式对象、清单和回执，再纳入清单内正式正文。多来源证据使用 `sources` 数组，每项含 `source`、`lines`、`knowledge_status`；单来源维持原形状。引用只能指向所选来源中实际取得的文本。仅链接状态不能伪装成正文引用。完整证据、问题 payload 和模型传输继续受 M0 的字节上限及只读核对约束。

本节的旧0.3流程仍为 create-only；同一 run 重复发布只核对与补回执，新的 run 不具备跨运行语义去重。全库最新判断、控制、统一 pending 与编排不能从该旧格式推定，须按相应后继契约处理。隔离测试不替代真实多来源与第二模型验收。

<a id="m1-incremental-contract"></a>

## M1 增量与控制契约（`0.2.0.dev2`／schema `0.4`）

以下是本批实现所依照的字段与状态，不能据此预先认定真实多来源、每日 runner 或第二模型验收通过。`0.4` 用于新增量运行、控制、统一待确认、调度与限定读取包，正式记录／主题仍为 `0.2`；不重写既有 `0.1`／`0.2`／`0.3` 资料。

### 增量候选、旧对象和防重复标记

`process_sources(..., related_runs=None)` 保持原流程；显式传 `related_runs=[]`（CLI `process --incremental`）启用 `0.4`。`--related-run` 最多显式提供 8 个已发布运行，并启用相同模式；不会扫描其他运行正文。每份旧运行所引用的来源必须全部列入当前 `--source`，先核对来源集合再读取旧正文。增量模式可以显式提供同一来源 ID 的不同已保存版本以比较变化，重复路径仍拒绝；旧模式继续拒绝同 ID 多版本。

| 新运行字段 | 含义及约束 |
| --- | --- |
| `mode` | 固定 `incremental`，与旧 create-only 契约分开 |
| `related_runs`、`related` | 排序、去重后的显式旧运行及其核验快照；快照含 `run_id`、发布 `commit_sha`、`manifest_path` 和 `documents`；每份文档含路径、字节摘要、`content_version`、元数据及正文 |
| `controls_digest` | 当前合法控制记录的规范摘要；不把 HEAD 纳入摘要，但控制变化使旧候选失效 |
| `processing_key` | 所选来源 ID／版本集合、`rules_version` 和 `controls_digest` 的规范摘要；不按采集日或事件日期判断是否已处理 |
| `operations` | 按正式路径记录 `action: create\|update\|reuse`、`before_sha256`、`before_content_version`；create 的两个 before 值为 `null`，其他操作必须匹配明确旧版本 |
| `execution_context` | 手动为 `null`；每日执行时绑定调度预留的 schedule／tick／attempt／run 及预留文件摘要，不能更换来源或旧运行范围 |
| `attempt_path`、`attempt_sha256` | 模型调用前持久 `started.md` 的路径与摘要，候选和清单均绑定该记录；候选 `base_head` 为 started 的实际提交 |

同 ID、同对象类型的已知路径才能更新，保留最初 `recorded_at`；正文或实质元数据改变形成新 `content_version`。相同语义版本使用 `reuse`，保留原正式字节、`generated_by` 和 `review_ref`，不能为“重用”改造原审核来源；清单的 `object_origins` 另记录重用对象原运行／审核路径。未在显式旧运行中的现存目标、身份变化、版本冲突或删除历史均不得覆盖。

旧运行的已有 record 必须在新输出中保留或明确更新，遗漏返回 `related_records_omitted`。修改 record 前只查主题文件头寻找直接依赖；未显式纳入且不能在本批同步更新的依赖返回 `required_related_context`。本节旧0.4入口限一个Wiki，不能安全同步更新多主题时拒绝该组；后继batch-v1多Wiki仍须完整依赖及group proof，不借旧入口宣称已更新全部主题。

增量候选及 Review 继续绑定完整输入、来源／回执、旧正式字节、程序、规则、控制和输出。发布清单沿用来源及文件摘要，并加入上述增量字段和重用来源。`memory/runs/processes/processed/<processing_key>.md` 的头为 `kind: processed_inputs`／`schema_version: 0.4`，正文严格为 `run_id`、`processing_key`、`manifest_path`；与正式变更和清单同一次提交。发现标记后先核验它对应的已发布链，才能返回 `already_processed` 而不再调用模型；标记丢失、被删除或不匹配不得猜测后重发。

手动未预分配 run 的重复处理不产生新写入；调度已预分配 run 时，可保存该 run 的 `reused.md` 零模型调用回执，供 attempt 中断后核对原成功发布，不能凭一个重复状态字符串跳过历史链校验。

每个新的 `0.4` 尝试在调用模型前提交 `memory/runs/processes/<run_id>/started.md`，头为 `kind: processing_attempt`，正文含 `run_id`、`processing_key`、`execution_context`、`source_paths`、`related_runs`、`reserved_calls: 2`、`started_at`。同预分配 run 已有 started 时返回 `run_already_exists`，不重发调用。复用 processed marker 还要求请求的全部旧知识稳定路径已由原运行 objects／related 比较覆盖；额外未比较上下文返回 `processing_context_changed` 且零模型调用，不把相同来源误报为所有新上下文都已处理。

`verify_publication(..., historical=False)` 核对当前正式字节；`historical=True`／CLI `--historical` 核对固定发布提交，用于追溯已被后续合法版本替代的原发布。历史通过不表示旧结论仍为当前有效，也不绕过现行删除控制。默认读取不能因历史链完整而继续沿用已纠正旧事实。

### 公库唯一工程来源的运行版本绑定

新运行从实际加载模块所在的公库 checkout 取得工程版本，不能根据 `--repo` 的私库 HEAD 推断程序版本。`execution_versions.program_commit` 是公库核心路径 `project/src/pdm/`、`project/prompts/`、`project/skills/` 与 `pyproject.toml` 的最近提交；实际程序、规则和领域策略仍按原契约计算摘要。公库准确 HEAD、核心工程提交和私库资料 HEAD 各有含义，文档提交不会被误认为新的核心工程版本。

新增量尝试在写入 `started.md` 的同一提交原子保存 `memory/runs/processes/<run_id>/engine.md`。它使用既有 `schema_version: 0.4` 封套、`kind: engine_provenance` 和独立 `contract: public-engine-v1`；不改变现有业务 schema、processing key 或重试计数语义。正文严格保存：

| 字段 | 含义 |
| --- | --- |
| `run_id`、`processing_key` | 对应原预约运行与处理身份 |
| `repository` | 固定 `pdm-public-engine`，表示工程来源类别，不保存私人远程地址 |
| `code_commit` | 实际加载公库 checkout 的准确 HEAD；成功 CI 仍由双仓入口另行核验 |
| `versions` | 实际 `program_sha256`、`rules_version`、`domain_policy_version`、`domain_policy_sha256` 及公库核心 `program_commit` |
| `data_base_head` | 预约提交之前的私库资料 HEAD，与公库代码提交独立 |
| `started_sha256` | 同提交 `started.md` 的原始字节摘要 |

恢复检查该证明与原 started 的提交、摘要、处理身份及资料父提交一致，证明发生增删改、损坏或不符时拒绝；不能删除证明后回退到旧逻辑。新质量恢复使用原证明中的工程版本，私库当前工作树无需工程副本。没有该证明的旧运行仅按原契约只读核验私库原 Git 历史中的工程，不补写、重标旧证明或重放 unknown。来源、模型审计、控制、候选、Review、已有发布及完整父链保留原事实。

### 来源纠正、替代和普通删除

控制文件为 `memory/runs/controls/ctl-<SHA256(request_key)>.md`，头严格含 `schema_version: 0.4`、`kind: source_control`、`control_id`；正文是以下输入和实际生成字段。只保存必要引用，不复制被要求删除的正文。

| 字段 | 当前契约 |
| --- | --- |
| `request_key`、`action` | 调用方持续复用的请求键；action 为 `corrects`、`supersedes`、`delete`。同键同内容复用；同键不同内容拒绝 |
| `source_paths`、`object_paths` | 显式目标来源版本路径及正式记录／主题路径，规范排序且无重复；禁止任意路径 |
| `replacement_source_paths` | 纠正／替代必须有实际已采集替代来源，并与旧来源分开；删除时必须为空 |
| `basis_source_path`、`effective_from` | 可空依据来源与实际有效时间；未知保持 `null`，支持已知月份、日期或带时区时间，不能用控制创建时刻填充 |
| `control_id`、`created_at`、`object_versions` | 实际生成的控制身份／创建时刻，以及目标正式路径到旧 `content_version` 的映射 |

`plan_control` 只读输出 `required_object_paths`／`required_source_paths`、`missing_object_paths`／`missing_source_paths` 及 `source_suppression`。普通删除按来源 ID 的**所有版本**抑制原文读取和重新采集；删除某正式对象时，也检查其来源及其他直接引用者和主题依赖是否落入范围。计划只报告缺项，`record_control`／CLI `control apply` 仅在用户明确的输入覆盖全部依赖时执行，不能自动扩大删除范围。控制记录和所列正式文件删除同一次条件提交；原文与 Git 历史保留，但常规读取不再取出已删除正文。

`corrects` 立即使指定旧错误退出当前证据。`supersedes` 按 Asia/Shanghai 解释已知有效月份／日期／时间：未生效时旧来源仍可用，新来源仅为未来上下文；有效时间未知则保留不确定性，`current_complete: false`，不机械选新或选旧。显式历史读取可呈现原文和控制关系。未来已获新审核的同对象新版本不会仅因旧版本曾被纠正而永远排除；`delete` 则持续抑制目标对象和该来源 ID 全部版本。纠正／替代不自动改写正文，正式更新仍须加工、Review 和发布。未知位置、未提交／损坏控制、矛盾或循环关系先拒绝，不根据新旧文件顺序裁决事实。

### 统一待确认

旧0.4权威表为 `memory/pending.md`，头严格为 `schema_version: 0.4`、`kind: pending_registry`；dev10扩大表及登记恢复以上文0.7契约为准。JSON 正文为 `items` 数组。每项含 `item_id`、`key`、`question`、`source_refs`、`object_refs`、`run_ids`、`pending_status`、`reply_sources`、`resolution`、`created_at`、`updated_at`。`source_refs` 为来源 ID／版本／path 三元组，`object_refs` 为 id／content_version／path 三元组；`run_ids` 保存产生问题的运行。

默认 `key` 由问题与排序后的来源／对象引用生成，`item_id` 为 `pending-<SHA256(key)>`，不将 run 或当前时刻作为去重身份。同一事项跨 run 合并原运行引用；显式同 key 异内容拒绝。状态为 `open`、`answered`、`resolved`、`withdrawn`；本批不提供 withdrawn 写入口。

调用者先通过 capture 保存真实答复并核验采集回执，再 `answer` 关联已保存来源，不能使用 `link_only` 或拿问题原路径冒充新回复；`reply_sources` 另存来源三元组、原文 `commit_sha` 和答复关联前的 `associated_head`，仅把事项置为 `answered`。`resolve` 同时沿 registry 实际历史核对答复关联提交，要求新发布在该锚点之后且不同于问题原运行；发布来源限于原来源与回复，全部有效回复的精确 ID／版本必须被已核验正式 **record** 的 `sources` 实际引用，仅列入模型输入或 Wiki 不算处理完成。核验后写 `resolution: {run_id, commit_sha, manifest_path}`，才成为 `resolved`。收到回复和知识完成更新是不同状态。

`object_refs` 也可绑定同运行、完整来源范围和精确路径／ID／版本一致的已提交候选对象，无需先发布。来源引用支持显式同 ID 多版本。任一事项来源、回复或对象被 delete 控制时，list 过滤整项，answer／resolve 拒绝；不能借清单泄露删除正文。registry 历史核验最多 1000 条，超限停止。

历史0.4实现把Review的needs_confirmation与统一表更新同次保存。dev10新执行按页首先持久Review及0.7登记marker、再CAS登记，保留候选→Review祖先，不因清单失败丢已知结果。合法pending不阻断其他已通过运行发布；待确认内容本身仍不得绕过Review发布。

### 当前证据与读取覆盖

`build_current_evidence(repo, source_paths, run_ids=None, history=False, max_bytes=192000)` 和 `ask_current` 只读取明确授权来源／运行。选定范围内同时检查正式知识、未加工新原文和现行控制。`coverage` 保存 `requested_source_paths`、`read_source_paths`、`unread_source_paths`、`examined_not_included_source_paths`、`excluded_sources`、`missing_update_paths`、`requested_runs`、`read_runs`、`unread_runs`、`excluded_documents`、`budget_exhausted`、`current_complete`，另有下述时间／历史证明范围。替代来源在范围之外时只列路径缺口，不为求完整而越权读正文。预算不足时保留实际已读范围与缺口，不截断原文字节、不声称确认最新；回答仍受完整问题与规则传输预算和前后只读核对约束。

同来源 ID 的未选新版本仅通过路径、元数据、采集回执及 Git 祖先关系发现；不读未授权正文，新或分叉版本记入缺口，旧祖先不使已选当前版本永远“不完整”。未来替代只选 B 而遗漏仍有效的 A 时也报告未选 A；未知有效时间披露 `temporal_undetermined_source_paths`，未生效来源披露 `not_yet_effective_source_paths`。核验 `0.4` 新发布可在显式许可历史来源范围内验证旧 Review 证明，但输出过滤旧 raw／旧正式正文并披露 `proof_history_source_paths`；已被更新的旧发布在当前视图列为 `unread_runs`／`historical_publication`，不能冒充当前版本。

### 按正式对象选择与压缩来源（dev19）

`build_current_evidence(..., source_views=None, batch_context=None, object_paths=None, compact_sources=False)`及`ask_current`支持明确对象选择。`object_paths`须来自显式选定并完整核验的发布范围，主题带`metadata.records`中精确ID／content_version的record依赖。只提供所选对象及必要依赖，并投影它们实际引用的完整原文行范围；无效路径、缺来源、错record版本、受控排除或超预算不能以摘要补齐。旧无这些选项的读取形状保持。

对象模式coverage增加`requested_object_paths`、`dependency_object_paths`、`read_object_paths`、`not_selected_object_paths`、`object_selection_complete`、`not_needed_source_paths`及`not_needed_runs`；片段范围仍由`provided_source_ranges`、缺失行及未读范围表达。`object_selection_complete=true`只证明对象与依赖选择齐备，不证明读遍所选来源或全库。`compact_sources`去掉source.body与lines的重复正文，不改原行号、引用含义、授权或完整证据预算。

### 回答中的本库处理状态（answer v7）

内容事实继续回到实际原文。来源已核验采集提交／回执可证明该来源版本已本地保存；`publication`／`publications`中已核验的run_id、commit_sha、files及knowledge_status可证明明确选定版本已审核发布，可说明这是本次evidence.head快照核验的处理状态，不为它伪造原文引文。旧来源“未导入／未提交”等自述仅代表描述当时，不能覆盖后来核验的状态。

未提供发布证据不等于从未发布，历史发布不等于当前有效；本地保存／发布不证明原始附件全部入库、远程已同步、上传其他服务或用户目标已完成。未提供pending时不能编造用户待办或推断全库没有事项；来源缺口与确需用户确认分别表达。只读问答不能自称新保存或发布。

### 问答中的限定待确认上下文（dev6）

`evidence`、`build_evidence_sources`与 `build_current_evidence`都可投影统一pending，因此单来源／多来源／current三个ask入口及限定读取包沿用同一上下文。不相关时不新增pending字段，保持原无pending结果形状；registry在本地解析后筛选，不承诺单体文件中未选事项的字节从未被本地读取。

| 输出字段 | 契约 |
| --- | --- |
| `pending.registry_path`、`registry_head`、`registry_sha256` | 权威表路径、固定证据HEAD与registry字节摘要；registry_head不是该表最后修改commit |
| `pending.state_basis` | 固定 `registry_at_evidence_head`，表示在证据版本看到的事项状态 |
| `pending.items` | 仅完整授权项；保留item_id、question、pending_status、source_refs、object_refs、run_ids、reply_sources、resolution及时间，删除内部去重key，新增resolution_verification |
| `pending.coverage`、`limitations` | 读取缺口与状态限制；有gap或未核验resolution时coverage.complete=false，current的coverage.pending_complete同步并使current_complete=false |

只有事项的全部来源与回复路径均在显式选集、原文实际已计入证据预算且控制允许时，才传完整question与引用。部分范围、受corrects等控制排除或正文未预算提供的事项只列coverage.gaps：item_id、pending_status、reason、missing_source_paths、unread_source_paths、controlled_source_paths、controlled_object_count；不发question、key、object_refs或run_ids。与选集无交集的事项省略；delete控制整项只计excluded_item_count，不带问题正文。

resolution_verification没有解决依据时为 `{status:not_applicable}`；解决run未显式选中时为 `{status:not_selected,missing_run_ids:[run]}`，不自动读该run；已选run任一正式对象被delete时为 `{status:excluded_by_control}`。已显式选择且核验固定历史发布、全部有效答复的正式record引用和真实关联祖先后，为 `{status:verified_history,run_id,commit_sha,manifest_path,reply_record_references_verified:true,reply_association_verified:true}`。已选却结构、固定commit、引用或关联不符时，在模型前安全拒绝；不会降成未核验项继续回答。answered回复也核精确采集commit且不接受link_only。

verified_history只证明登记的历史发布和答复关联链，不独立证明问题语义完全解决或当前仍适用；open、answered、resolved继续分开。pending、缺口和limitations全部计入完整证据／问题的192000字节或调用者更小上限，超限整体返回evidence_limit，不静默裁剪事项。问题和事项状态不是新原始事实，引用仍须回到选定来源正文；该只读投影不写registry、答案或正式知识。

### 回答内联引用编号（answer v6）

加工 `execution_versions.rules_version`的基础提示词摘要只汇总extract.md与review.md；领域策略和显式Skill绑定按各自契约共同固定。answer.md不在该加工摘要中，回答规则自身字节及读取包的answer_rules_sha256分别绑定；程序代码改变则program_sha256改变。这些摘要各有范围，不能将回答规则变化说成加工规则已升级或旧知识已重新加工。代码包Manifest的同名rules_version另汇总包内规则文件，不与加工摘要混用。

answer v6保持原JSON结构，三个ask入口共同检查JSON结构、answer正文编号及来源坐标；编号失败为 `invalid_inline_citation`，原结构错误仍为invalid_answer，来源版本／locator错误仍为invalid_citation。`[n]`对应citations数组从1开始的第n项，不是来源行号。相邻 `[1][2]`、组 `[1,2]`、升序范围 `[1-3]`及混组可用；组分隔符支持逗号、中文逗号、顿号、分号和中文分号，Unicode数字及全角方括号规范化后同检。

0、负数、前导0、超出数组长度或数字开头但不符合支持语法的编号拒绝。没有编号时保持旧行为，不要求每条citation都在正文出现；citations为空仍须按原规则说明limitations，不带方括号的普通数值不受影响。程序先在原ASCII文本识别单行闭合backtick代码、合法闭合fence及完整行内link／image字面区，再对剩余文本做Unicode规范化；显式 `\[literal\]` 保留字面含义。未闭合代码、非法含backtick的fence info、全角backtick、文字引号、blockquote及reference-style标签不豁免；它不是完整Markdown解析器，`[99]（时间未知）`也不是有效行内链接。

此检查只核编号与数组索引的绑定，并沿用来源ID／版本／实际行号检查，不证明所指引用在语义上支持紧邻陈述。人工修改答案也不能作为程序已自动纠正语义的证明。输出已返回后被拒仍保留一次实际调用消费，不自动重试，不写memory或发布知识。

### 每日编排事件与预算

每个计划以 `memory/runs/schedules/<schedule_id>/events/00000000.md` 开始、递增八位序号追加。文件头为 `schema_version: 0.4`／`kind: schedule_event`；JSON 正文严格含 `schedule_id`、`sequence`、`previous_event`、`kind`、`at`、`data`。事件类型为 `configured`、`paused`、`resumed`、`tick_started`、`attempt_reserved`、`attempt_result`、`tick_finished`，前序路径和序号必须连续，未知状态拒绝继续。

配置必需 `schedule_id`、`daily_time`（`HH:MM`）、`allowed_source_ids`、`allowed_source_paths`，两类来源授权范围至少一类非空。可选值的初始默认：`timezone: Asia/Shanghai`、`related_run_ids: []`、`max_items: 8`、`max_calls: 16`、`max_attempts: 3`、`timeout_seconds: 120`、`lease_seconds: 1800`、`grace_seconds: 1800`、`remote_mode: required`、`remote: origin`。`local_only` 仅是显式隔离试跑模式，不表示真实每日验收。配置不保存凭证；同名计划不能静默换范围。

仅窄查获准来源 ID 或显式路径，按来源 ID／版本及规则版本选取新增、变更、补录及允许续跑项，不用 `original_time` 作“昨天”的筛选。plan 的 `related_run_ids` 合并显式配置与本 schedule 已确认成功的运行，核验历史链、当前正式字节及来源授权；旧上下文只有被后继当前运行完整覆盖才退场，否则返回 `needs_context_configuration`。被删除或已纠正来源列 excluded，不占调用预算、不阻止其他项。

dev5对待确认历史作只读对账：plan item可派生 `status: resolved`，仅此状态返回 `confirmation_resolutions` 数组，每项严格表示 `{pending_item_id, original_run_id, publication_run_id, commit_sha, manifest_path}`。仅在原候选、needs_confirmation Review与registry的问题／来源／对象精确绑定，后继发布固定历史commit、真实答复关联祖先及全部有效回复的正式record引用均核验后才成立，后续合法更新不使该固定历史解决结果退回待确认；来源集合先通过schedule授权，再读取正文。open／answered仍等待，不因新发布存在而自动resolve。run／status的remaining排除已核验resolved项，但历史attempt_result仍保留awaiting_confirmation，不为原run伪造manifest或published，也不自动将人工后继加入related上下文。该状态是派生读取结果，不新增或重写事件schema。

每个 attempt 先持久预留固定 run、来源、`input_path`、`rules_version` 与实际旧运行，再调用模型；保守占用 2 个调用槽，即使用量未知或调用中断也不返还，`usage` 未取得保持 `null`。条数和调用预算按事件实际 `at` 的 Asia/Shanghai 自然日累计，manual／test／scheduled 更换 trigger ID 不能重置当天预算；`max_attempts` 另约束来源／规则处理身份。成功结果另存 `publication_run_id`，already_processed 指向原成功运行。超时真实传递给调用入口，达到上限保存剩余后结束。过期 lease 只触发核对，不能据此重新发出结果未知的模型调用。

trigger 仅含 `kind: manual\|test\|scheduled`、`trigger_id`、带时区 `scheduled_for`。它是调用方声明，不证明实际 runner 已触发；结果始终保留 `scheduled_environment: unverified`／`real_daily_verified: false`，真实验证另需平台证据。状态无平台记录为 `trigger_unknown`；仅当平台成功查询覆盖应触发窗口且确认为空时，才能判 `no_trigger`。已有开始无结束是 `incomplete`，正常空批为 `completed_empty`。分项结果列 `published`、`already_processed`、`awaiting_confirmation`、`needs_revision`、`failed`，另列剩余；批次汇总为 `completed_empty`、`completed`、`partial`，不把无回执当成功。

暂停事件在统一正式发布入口生效，约束手动和每日发布；不声称已经关闭外部平台触发。恢复须有 `validation_ref` 且没有未核对的 attempt；提交结果未知先核对远程，再补回执或续未完成步骤。不会在本机新建 cron 或用 GitHub Actions 替换选定 GPT 云端 runner。

暂停前已通过 Review 而未发布的 attempt，只有执行已停止、无正式发布且提供 reason／`validation_ref` 时，才能用 `schedule abandon` 显式终结。预算不退，旧 execution context 拒绝再发布；后续新 attempt 必须重新 Review，不能借终结操作掩盖在途未知结果。required 模式在推送或回调后仍独立 fetch／读回验证，不信任空回调；schedule events 不允许历史修改／删除，不能删除 pause 绕过发布门禁。

未知调用没有完成证据时默认不能终结；仅在该日程已暂停、租约过期、实际远程核对无正式发布、reason／`validation_ref` 均提供，且操作员显式 `execution_stopped: true` 声明旧执行已停止后，才可记为 `abandoned_uncertain`。原调用预留及未知用量保持，不将超时当作已停止。只有链接且正文未取得的来源在规划阶段以有界、无缓冲文件头检查标为 `waiting_text`，不占模型调用和批量预算，不阻塞有正文的无关项。

### 条件修改／删除事务与恢复

`PipelineGit.write_changes(files, expected_files, expected_head, message)` 接受路径到 bytes／`None` 的两张同键映射：expected `None` 要求基础提交、工作区和 index 均无目标；files `None` 表示删除。其余目标逐字节匹配旧 blob、工作区与 index，最终通过一次 HEAD CAS 提交整批新增／修改／删除。允许不变字节作为条件保护，整批均未变化则拒绝。来源及采集回执仍不经此接口改写。

本地恢复 journal v3 顶层仍为 `version`、`base`、`commit`、`reference`、`files`、`progress`，每个 file 保存 `before_blob`／`before_sha256` 与 `after_blob`／`after_sha256`，不存在侧均为 `null`。进度为 `pending`、`moving`、`moved`、`linking`、`linked`、`indexing`、`done`。恢复先核对全部目标，再开始任何物化；`moving`／`moved`／`linking` 结果不确定时保持暂停，不复活外部删除、不覆盖人工改动或 index 变化。提交结果未知先查实际引用，父提交和 A/M/D 差异必须与日志一致；标准 index 锁和私有副本保留无关 staging。旧 create-only `write_files` 与 v2 日志恢复契约保留。

条件修改／删除使用 Linux `renameat2(RENAME_NOREPLACE)` 将当前目标原子捕获到尚不存在的 before 快照，再核对实际 inode／字节；不使用“先 hardlink 旧名再 unlink 目标”的存在覆盖窗口。外部替换撞入该窗口时，保留捕获的外部字节并停在 moving，原目标可能缺失，需人工核对而不自动回填。发布新目标仍使用不覆盖创建。初始执行及 pending 恢复先在相关目录探测此原语，平台或文件系统不支持时返回 `atomic_move_unavailable`，在 Git 副作用或进度变化前停止，无不安全降级。

<a id="m1-shared-budget-contract"></a>

## 共享调用预算契约（`0.2.0.dev4`）

独立预算schema `"1"`的账本放在数据及工程Git仓库之外的私有持久目录，不属于memory来源或知识文件。带预算绑定的新日程事件使用0.5，旧0.4事件不重写；候选／Review0.4、formal0.2、source／capture0.1保持。旧dev3遇0.5必须拒写；回退按实际schema及调用门禁检查。目标云持久位置与恢复须另行验证。

### 独立账本与计数

账本为严格JSON，顶层只含以下持久字段；`budget status`另计算统计字段，不把统计副本作为可编辑事实。

| 字段 | 约束 |
| --- | --- |
| `schema_version` | 字符串 `"1"`，与memory schema分别管理 |
| `pool_id` | `budget-`加32位十六进制随机标识，标识同一个共享预算池 |
| `limit`、`initial_used` | 明确授权总限与创建前已消耗次数，整数且 `0 <= initial_used <= limit <= 1000000`；不能默认遗漏历史失败／诊断消费 |
| `authorization_sha256` | 授权引用的UTF-8 SHA256；账本不保存引用原文，摘要本身不授予资料或模型权限 |
| `revision` | 每次持久变更递增的整数 |
| `operations` | operation ID到 `{reserved_roles, calls}` 的映射；ID为1—128位受限ASCII标识，以字母或数字开头，其余支持字母、数字、点、下划线、冒号及连字符 |

支持角色为 `extract`、`review`、`answer`。每个role的call保存 `payload_sha256`、`status`、`started_at`、`finished_at`、`invocation_id`、`error_code`，不保存来源正文、答案或凭据。状态为 `started`／`completed`／`error`／`unknown`，当前适配器异常保守记unknown，error为合法兼容状态；`started`在操作系统启动模型进程前持久记录，外部是否接受请求仍可能未知。completed只说明调用返回，候选或答案随后未通过业务校验仍计消费；不代表Review通过、知识发布或QA验收。合法完成信息也不允许同role再调用一次。

`snapshot`计算 `reserved_calls` 为全部reserved roles之和，`started_calls`为已记录开始的role数，`used = initial_used + reserved_calls`，`remaining = limit - used`。预约和开始分别计数；提取失败或未执行Review时，两槽预约仍不返还，不把未知用量当零。

路径不得在Git工作区、Git目录或其他可识别仓库内，不接受链接和不安全路径；父目录须当前用户所有且0700，账本及同名`.lock`须0600、单链接普通文件。文件锁串行保护读改写，独占创建、临时文件fsync、原子替换及目录fsync保护持久更新。打开时固定pool、授权摘要、limit及initial_used，存活实例期间被替换为另一授权池时返回 `budget_identity_changed`。损坏、未知字段、不安全权限或I/O不确定时拒绝外发，不删除／重建账本“修复”余额。保证范围限可信同UID及同一可靠共享文件系统；不检测同pool旧快照回滚，也不阻止可信操作者另建池，云跨机器存储仍须实际验证。

## M1持续加工契约（dev8／schema `0.6`）

### 持久加工队列与真实分批消费（schema `0.6`）

本节定义0.6基础契约；队列与加工链的后继0.7—0.10扩展见相应分节。正式对象0.2、来源及采集0.1、group wrapper0.6、独立audit schema2保持各自含义；加工 rules_version 与回答规则摘要分别固定。

`prepare_production(repo, queue_id, source_paths, *, allowed_source_paths=None, allowed_run_ids=(), max_input_bytes=128000, payload_overhead_bytes=16000)`固定明确来源与授权上下文，持久plan和授权discovery元数据。队列在memory/runs/processes/queues/下，kind=production_queue；全局尝试表在processes/queue-attempts/下，kind=production_attempts，均schema0.6。相同queue_id不能用于不同request；缺上下文许可返回blocked_context，不越权读；容量不完整保存blocked_capacity，不声称已加工。

`run_production(repo, queue_id, *, model_call, max_steps=8, require_binding=False)`推进有界执行slice，`production_status(repo, queue_id)`读状态。CLI run默认要求environment binding及账本位于绑定的库外state_root，仅显式--local-only作隔离fixture；实际模型入口要求audit账本并绑定pool／授权身份。单批直接完成提取和完整独立Review；多批先分别draft提取／Review，再merge全部必要片段及prior_candidates，最终batch-v1重新提取／独立Review，group一次条件提交正式对象及多Wiki。candidate／processing_key绑定batch_context、source_views、batch_material和merge_inputs；draft_only候选禁止独立publish，不当已发布事实。

旧0.6全组输入超限即blocked_capacity，后继分解能力按0.8及0.10契约核验；必要完整语义仍不能截断。production独立命令为本地正式提交，命令名不代表GPT任务启用、工程提升master或远程已完成，远程确认须有独立实际依据。

普通全局attempt身份由source fragment IDs＋phase＋related identity组成，不含queue名、规则或日期；显式清理重建另绑定已核验的rebuild_context，同回执仍跨队列保持同一身份。首次＋最多3次失败重试，达到4次总attempt停止，跨队列／跨日／规则更新不清零。passed仅在完整新context相同时复用，否则保留次数重审；unknown先对账。`reuse_extraction_run`仅在原候选完整、Review已确定启动前失败且账本没有review started时复用已验证extract元数据，不允许needs_revision原样换Review。有限retry与逐调用计数分别保存。worker持checkout级OS flock，与短时Git事务锁分开；崩溃可由OS释放worker锁，但持久attempt仍待核，不通过删除锁文件清除调用状态，也不保证跨clone分布式互斥。

### current读端消费片段与限定读取包

build_current_evidence、ask_current、build_domain_catalog及prepare_reading_package新增成对的keyword-only source_views／batch_context，来自project／merge的可重验结果。current显式source／run最多各256，旧noncurrent入口仍8，完整序列化192000字节预算继续生效，不能通过片段模式绕过问题／规则／pending／limitations预算。

片段source.body为空，content_scope="fragment"；实际provided文本只在evidence.sources[].lines，使用原文全局行号。entry.source_view严格含kind:"fragment"、source_line_count、fragments（每项fragment_id／locator），不重复正文；result.batch_context固定原context。coverage新增provided_source_ranges、fragment_incomplete_source_paths、fragment_missing_ranges；缺view的整path列unread，partial影响current_complete，partial pending整事项不发送问题正文。正式知识引文缺实际provided范围支持时排除并列excluded_documents，ask按实际line集合校验，未提供的原文不能经scope或摘要补进模型。

片段或expanded>8读取包为schema0.6，scope为source_paths、run_ids、batch_context；context带可重验source_ranges、plan_id、batch_id、parent_batch_ids，不能把包含预算未提供文本的完整source_views放scope。expanded但非fragment的batch_context为null。旧<=8且无片段读取包0.4沿原scope（仅source_paths／run_ids）与验证形状。仍须可信外部expected SHA；包验证不代替来源真实性核验或第二模型实际执行。

### 分批计划、片段投影与合并（schema `0.6`）

`plan_batches(repo, source_paths, *, max_input_bytes, payload_overhead_bytes=0, max_sources=8, expected_head=None, purpose="current")`接收最多256个显式来源，返回kind=batch_plan、schema_version="0.6"。plan_id绑定snapshot的head／versions／controls、来源及采集回执、全局fragment边界和容量参数；plan不复制原文。普通执行batch最多8来源，max_sources只能1—8；声明的payload_overhead计入预算，实际提取／Review完整请求仍须通过传输容量校验。

结构切片保留原文全局Lx-Ly：CSV／Markdown表按原行分片并保留表头、单位和preamble，Markdown同层section保留共享preamble；conversation、普通prose、JSON和代码保持原子，容量不足则blocked。任意位置代码围栏内的假heading不拆；显式“同上／上文／previous”等跨单元指代使整源保持原子，不猜依赖，也不宣称已理解全部隐含语义关系。重叠context沿用同fragment_id，重复表头／前提不增加独立佐证。

`project_batch_sources(repo, plan, batch_id, *, source_paths, max_bytes=None, fragment_ids=None)`返回source_views数组；每项为path、metadata及fragments，片段含fragment_id、locator、lines数组（每行line／text）。batch_context严格为plan_id、batch_id、source_ranges、parent_batch_ids；source_ranges逐项含path、source_id、source_version、locator、fragment_id。先核完整scope许可，缺parent不提供body；超预算保留不完整coverage，不截断原始行。source_policies另列控制，current排除受控片段，history保持原历史限制。

coverage区分本地完整核验plan明示来源的read_source_paths与实际返回片段的included_source_paths，另列omitted_fragment_ids、missing_dependency_fragment_ids、controlled_fragment_ids、budget_exhausted、global_locators。exceeds_legacy_source_limit披露超过旧8来源限制，不据此声称旧reader支持expanded scope；本地核验不等于模型已收到全部原文，也不承诺物理只读了返回片段字节。

`merge_batch_sources(repo, plan, batch_ids, *, source_paths, max_bytes)`合并父batch并去重fragment；synthetic batch带parent_batch_ids，最多256来源，必要输入容量不足或控制阻塞则整体blocked且不返回部分正文。规划／投影本身0模型，不自动写Git或发布知识；跨批最终内容仍须真实提取、独立Review及下节消费门禁。

### 依赖组最终审核与正式消费（schema `0.6`）

`publish_final_group(repo, plan, final_objects, reviewed_runs, *, source_paths, expected_head=None, group_id=None)`接收最终对象和已受审运行，正式对象仍schema0.2。独立group manifest位于memory/runs/processes/<group-run>/manifest.md，kind=batch_group_manifest、schema0.6，固定完整plan／摘要、各证书run的stage／Review精确SHA／commit／binding、object_proofs／origins、operations、父HEAD和正式文件摘要。group_id为run-加32位十六进制，缺省从plan_id及精确formal字节摘要派生，plan_id为plan-加64位十六进制。稳定输入标记在processes/grouped/<plan-id>.md，kind=batch_group_inputs、schema0.6。

消费检查每个plan batch的完整ranges覆盖，不能仅信parent名称；每个Wiki的全部record精确版本须在同一证书中共同受审。不同证明对同一路径给出不同字节拒绝，证书逐项保留execution_guard及旧值条件；局部Review不能借给实质改变后未受审的合并内容，无法容量安全合并仍blocked。正式变更＋manifest＋marker同一次CAS提交，另以kind=batch_group_receipt、schema0.6补回执；结果为published_local、remote_status=not_requested，不能据此称远程已同步。

`verify_group_publication(repo, group_id, *, source_paths=None, historical=False)`核固定发布及受审证明；历史按stored版本核验，发布时另硬核当前程序／规则；delete仍阻止历史正文恢复。重复调用和提交后缺receipt恢复按稳定marker核对已有结果，不能重发模型或复制正式对象；实际幂等与恢复结果以本轮回归为准。

### 对象级局部纠正（控制schema `0.6`）

新scope="objects"仅用于corrects／supersedes，kind=object_control、schema_version=0.6；无scope的旧0.4源级控制及全版本delete保留原契约。输入必须明确old source_paths、object_paths、object_versions、replacement_source_paths及basis_source_path。object_versions是object_path→精确sha256-content_version的map，keys须精确等于object_paths；basis须为replacement之一且有真实非空正文。

只读plan从文件头列出依赖topic，缺授权不写。新控制固化source_locators为old_source_path→`[{object_path, object_version, locator:"Lx-Ly"}]`，无quote副本；范围来自所选record refs，依赖topic不扩大受控raw范围。source_policy不整源排除，附object_restrictions；每项含上述object_path／object_version／locator及control_id、action、active、not_yet_effective、temporal_undetermined。object_policy仅排旧对象精确version，同ID新version不沿用旧排除，正式更新仍须补证据、独立Review和发布。

局部supersedes沿用已知时序：未来active=false，不排旧object／locator，替代来源仅作未来上下文；未知有效时间active=false且temporal_undetermined=true，current_complete=false；到期才控制精确旧version和locator。corrects立即控制所选旧错；在没有其他有效排除时，未受影响的同来源事实继续可用。

current evidence携带限制；旧QA入口因无法表达局部控制，在模型前返回current_evidence_required，要求current入口。current QA若引用受控旧行返回invalid_citation；该检查发生在模型已返回后，保留实际调用审计，不自动重试。history可读旧纠正资料并说明关系，delete仍禁止。异常目标／授权／绑定失败关闭，不自动扩大纠正或删除范围。

### continuous-v1日程与持久1＋3重试（schema `0.6`）

新策略须在配置显式指定`execution_policy: continuous-v1`，max_calls只允许null、max_attempts只允许4，max_items仅限制一个slice的attempt数，不是每日处理总量上限。缺省配置不追加execution_policy，沿用原有限语义；旧0.4／0.5事件不重写。continuous-v1新日程事件统一使用0.6，强制独立schema2 audit账本；其pool绑定仍为pool_id／authorization_sha256／grant_sha256三字段，audit grant材料为pool_id、authorization_sha256、limit:null、initial_used、schema_version:"2"、mode:"audit"，旧有限grant摘要口径不变。

continuous item_key是规范JSON中source_id／source_version／execution_policy的SHA256，不随rules、日期、trigger、operation或子slice改变；每attempt仍固定本次rules快照。首次1次＋失败后最多3次重试跨日与恢复保留，不能靠换身份再得次数。started／unknown仍先对账，不因无次数上限自动重发；waiting或需用户回复分别表达。

`drain_schedule(repo, schedule_id, trigger, expected_head=None, *, model_call=None, remote_confirm=None, clock=None, max_slices=8, window_seconds=300, continuation=None)`按slice及执行窗口推进，返回next_action、remaining及continuation，不创建平台唤醒。首次发现固定来源路径，后续slice只排空该持久根队列，不顺手发现新版本。tick_started.data.queue严格含root_tick_key、parent_tick_key、slice_index、source_paths；根slice_index=0且parent为空，后继parent固定上一slice的tick key，来源选集继承根。

continuation仅含schedule_id、root_tick_key、parent_tick_key、slice_index，不携带可自行改写的来源scope；恢复时从持久根事件取原source_paths。max_slices／window_seconds控制一次drain粒度，窗口结束保存队列，不能当作每日调用额度耗尽；下一轮是否真实被GPT平台唤醒仍需平台能力和两轮触发证据。

### 授权context发现与pending续接（派生计划）

`discover_context(repo, source_paths, *, allowed_source_paths, allowed_run_ids, object_ids=(), topic_ids=(), max_sources=8, max_runs=8)`只在显式授权运行的manifest与正式frontmatter中发现上下文，先核完整来源范围许可再读正式元数据；核当前Git blob精确匹配，排除delete对象。显式ID按精确身份选取，无ID时只将授权范围内当前非竞争的候选交给模型，不以标题相同判定同一事实。

输出含head、source_paths、related_runs、candidate_objects、controls_digest及coverage.gaps，明确metadata_only与publication_verification_required=true。该结果只是可供加工选择的派生计划，不替代完整发布／Review核验，0模型、0写入；必要依赖超授权或容量只列缺口，不顺手读取正文。

`plan_answered_pending`使用同一授权范围，产生稳定continuation-job。只对answered排队，固定原needs_confirmation候选／Review／事项的精确绑定，并核全部active reply的真实采集commit；缺授权只给opaque item及gap，不泄漏question。job固定当前head，HEAD变化须重新规划；job_id由父事项、来源／对象及active replies精确版本决定，换HEAD不重置该身份。job为派生结果，不新增持久schema或迁移现有资料。

`execute_continuation(repo, job, *, process_call, publish_call=None, resolve_call=None)`由调用者注入预算及真实process；只有passed才推进真实publish→verify→resolve→registry读回。非passed不发布，回复未被正式record实际引用不能resolve。收到答复、规划出job、加工已通过、已发布和事项已解决分别记录，不能把计划或注入回调自报成功当完整闭环。真实resolve中断后可重新规划相同job_id，核验已有process已处理结果后零新模型续接；未知调用仍先对账，不借新plan重发。

### 环境绑定与runner配置（独立schema `1`）

环境绑定API为 `bind_environment(repo, config)`和`require_environment(repo, *, require_binding=False, remote=None)`。绑定严格包含schema_version、environment、repo_root、remote_name、remote_identity、branch、ref、state_root、credential_refs；environment为dev或prod，对应dev或master，repo_root是实际绝对Git根。remote_identity是无凭据的规范仓库身份，核对实际全部fetch／push URL；HTTPS与SSH传输可指同一身份，不能借不同URL隐藏另一目的地。

绑定写入clone实际Git管理目录内的`pdm-environment.json`（普通clone为`.git/pdm-environment.json`），linked worktree也各有独立绑定，0600、不进Git；state_root须绝对、库外、当前UID且0700。credential_refs只保存git／model引用名，不保存凭据值。旧无binding入口保持兼容，新runner入口要求绑定；不能把一个clone的本地绑定文件当成另一clone已经验证。

runner配置严格包含schema_version、environment、platform、platform_task_id、schedule_id、code_commit、transport、allowed_source_ids、allowed_source_paths、trigger_observer、state_root、daily_time、timezone。时刻固定Asia/Shanghai 22:00，授权来源至少一类非空，environment及state_root须与绑定相符。`validate_runner_config(config)`只校验配置；`probe_runner(repo, config, *, receipt_refs=None)`只检测本机Git／Python／Codex可执行文件是否存在、绑定与state目录权限，不证明工具已成功执行模型或跨窗口持久恢复，也不调用模型。当前无平台adapter，返回blocked／platform_execution_unverified，platform_task_status为unverified、task_created_by_probe与task_enabled_by_probe均false，五项平台能力仍unverified；caller填写true或提供回执引用名均不能升级为真实通过，也不创建／启用GPT任务。

以下 promotion 接口保留历史混合仓库的兼容行为；纯资料私库不再通过它复制工程，公库工程按 dev → master 的 PR 流程交付。`promotion_check(repo, base, candidate, *, dev_commit=None)`只读核验双父候选：第一父精确为base，显式dev_commit时第二父须匹配。base至candidate对memory／fixtures／archive／.pdm或任意层名为pdm-environment.json的文件有变化即blocked，并列出需从base保留的路径。该函数只读核验，另有`prepare_promotion(repo, base, dev_commit)`及promotion prepare --repo --base --dev-commit在本机对象库构造未挂ref的双父candidate：工程取dev，保护根及子路径／绑定文件精确保留生产base，check还核非保护文件的精确mode／blob与dev父一致，第二父须为local dev或origin/dev祖先。prepare返回prepared、candidate_commit／tree和report，git_objects_created=true、refs_changed=false、worktree_changed=false、pushed=false。临时promotion交付分支及实际merge按发布门禁核对，不能把创建对象当生产交付。

`promotion_delivery_check(repo, base, actual_merge, head_commit, head_ref=...)`核GitHub外层merge：交付head必须命名promotion/<head完整SHA>，prepared检查通过，outer父顺序为[base, head]且tree与head一致才允许交付。base改变须重新准备，不把GitHub合并成功当生产资料保持证明。

### 日常调用审计账本（schema `2`）

本轮冻结的账本API为 `BudgetLedger.create(path, limit, initial_used, authorization_ref, *, repo=None, mode="budget")`。默认budget模式继续生成旧schema `1`，不追加mode字段或改写旧有限专项行为；日常审计须显式 `mode="audit"` 且 `limit=None`，创建独立schema `2`，不把旧有限账本原地转换。CLI以--mode audit创建且必须省略--limit；日程audit身份绑定见continuous-v1契约。

schema `2`严格包含 `schema_version: "2"`、`mode: "audit"`、`pool_id`、`limit: null`、`initial_used`、`authorization_sha256`、`revision`、`operations`。pool ID沿用budget-UUID32；initial_used为非负整数，operation及role的预约／实际启动／完成／unknown字段沿用同一审计语义。snapshot另派生reserved_calls、started_calls、used，remaining固定null，表示没有日常调用总次数门槛，不表示调用未发生或服务无外部限制。

审计模式仍有外发前持久边界、同操作／角色不可重放、未知先对账、锁和fsync以及容量／路径／权限门禁；服务授权引用摘要本身不授予资料或模型权限。取消次数限制不允许清空旧失败／未知或重置持久retry。日程pool／grant如何绑定audit身份由同一最终日程契约固定，不凭空将旧0.5字段解释为新模式。

### 调用适配与入口

以下适配器的持久启动／不可重放语义通用于budget和audit；带--limit及budget_exhausted的CLI描述仅为旧有限模式，日常audit用上一节显式模式且不传limit。production／continuous入口另按本轮契约执行。

`BudgetLedger.create(path, limit, initial_used, authorization_ref, repo=...)`只创建新账本；`BudgetLedger(path, repo=...)`只打开既有账本。`BudgetedModelCall(ledger, operation_id, transport=None)`绑定调用身份，支持 `reserve_calls(2)`原子预约extract／review、`reserve_calls(1)`预约answer、`for_operation(id)`派生同池子操作，以及只读operation与计数查询。public `call_model(..., ledger=..., operation_id=...)`缺少绑定时拒绝；隔离测试可显式注入mock，不声称能阻止恶意调用者绕过程序直接调用底层transport或其他工具。

process在精确已处理输入预检之后、Git started之前预约两槽；已成功处理且精确匹配时零预约／零调用复用。手动操作ID沿用显式CLI参数，不从随机run派生；每日操作由持久run派生。只预约且未started的相同roles预约可幂等读回；已有started的操作不能重跑process，同一role不能重放。ask按单槽调用，evidence、publish及reading-package prepare／verify为零模型入口。

旧有限CLI `budget create --mode budget --repo --ledger --limit --initial-used --authorization-ref`和 `budget status --repo --ledger`管理账本。`process`、`ask`、`schedule run`必须带 `--budget-ledger`和 `--operation-id`；缺失分别为 `budget_ledger_required`／`budget_operation_required`，默认Python调用缺绑定为 `budget_binding_required`。其他门禁含 `budget_exhausted`、`budget_call_already_started`、`budget_operation_conflict`、`budget_pool_mismatch`及路径／状态／I/O安全错误。拒绝返回非0及安全错误计数；调用后落盘失败仍报告已尝试调用，不能一律写 `attempted_calls: 0`。

`attempted_calls`表示本次失败调用是否已越过启动边界，`operation_started_calls`表示该operation累计已观测启动数，两者不能混用。提取已启动后，Review启动前因账本失败被拒绝时，前者为0而后者仍为1；适配器保留已经观测到的累计数，不因后续账本不可读而报告归零。展示修复不改变已预约槽、退款或同操作重放规则；它也不能替代账本核对或证明服务端收到／计费。

### 预算绑定日程事件（schema `0.5`）

本节仅保留旧finite策略；continuous-v1统一使用上节0.6事件与audit身份，不再受以下每日／累计次数门禁。

带以下预算字段的 `tick_started`、`attempt_reserved`、`attempt_result`新事件使用0.5，其余事件仍按原0.4契约，旧历史不重写；0.4事件夹带预算字段时拒绝。首次预算tick即保存 `budget_pool`，即使余额不足、尚未产生attempt也固定预算池，不能通过换池重置原约束。

| 字段 | 内容 |
| --- | --- |
| `tick_started.data.budget_pool` | `{pool_id, authorization_sha256, grant_sha256}` |
| `grant_sha256` | 规范JSON `{pool_id, authorization_sha256, limit, initial_used}` 的SHA256，固定池、授权引用摘要、原总限及原初始消耗；不替代实际授权 |
| `attempt_reserved.data.budget_binding` | budget_pool三个字段，再加 `operation_id`、`reserved_calls`、`reuse_run_id` |
| `reserved_calls`、`reuse_run_id` | 实际需加工时为2及null；精确复用已核发布时为0及原run ID，共享账本不新增预约 |
| `attempt_result.data.budget_binding` | 保留对应预约绑定，不改为另一池／授权／grant／operation |
| `attempt_result.data.budget_started_calls` | 已记录开始数0—2；显式放弃未知调用时可为null，不推断为0 |

调度operation ID为 `schedule-`加规范JSON `{operation_id: CLI显式base, run_id: 持久run}` 的SHA256；`for_operation(id)`使用该明确ID，不自动拼接旧base。恢复核对pool／授权／grant及每个已记录operation的实际账本状态；缺失、换池或绑定不一致则阻断，不重新发出未知模型调用。共享账本的0／2槽与原schedule每日每attempt的保守2槽是不同统计口径，共同限制执行。

加工两槽不足时，scheduler在 `attempt_reserved` 前结束为partial／budget_exhausted，不伪造模型failure；`BudgetBlocked`独立报告。启动前校验失败按实际边界记录0／1次，不仅按phase猜计数。云账本位置与恢复仍需实际证据；来源组及QA结果持久恢复按所选任务需要补齐。

## 对象边界与拆分粒度

| `type` | 表达什么 | 必须保留的区别 |
| --- | --- | --- |
| `knowledge` | 有出处的概念、方法、经验、观点与综合结论 | 适用范围、外部观点和模型推断分别说明 |
| `state` | 在一定时期成立的个人情况、偏好或条件 | 一次行为不能泛化为长期习惯；过去成立不等于现在成立 |
| `event` | 已发生的事情、参与者、时间与结果 | 计划中的事项不能记为已完成事件 |
| `goal` | 希望达到的结果 | 提出者、用户是否采纳、实际进展分别表达 |
| `decision` | 有证据表明已经作出的选择 | 决定主体、选择内容、依据和范围；讨论或建议不能升级成决定 |

按**能独立修改、纠正、审核或判断有效期**的单位拆分记录。一段对话可以形成多个对象，分别引用对应原话，并保留共同上下文；无需把每句话都拆成文件。不同人物、不同有效时期或不同证据支持的结论不能因为主题相同而合并。

领域不是对象类型。同一对象可以涉及零个、一个或多个领域，不复制三份事实。Wiki 组织和综合正式记录；Wiki 中新增的归纳也属于需审核的内容。

M2—M4 的分析、计划和复盘可复用上述来源与版本约定，但其专用结构在进入对应阶段时补充。第一版不将整份行动计划硬塞成一条决定，也不将模型生成计划当成实际行动。

## 知识领域与分类入口

知识按以下三个稳定领域组织；下表是目录规范，不表示已经取得任何个人资料。

| 知识领域 | 现有目录与导航入口 | 主要内容 |
| --- | --- | --- |
| 健康&运动 | `health`：`memory/health/README.md` | 身体监控、睡眠、日常活动与运动 |
| 工作事业 | `career`：`memory/career/README.md` | 工作项目、职业发展、协作与相关决策 |
| 学习&能力提升 | `learning`：`memory/learning/README.md` | 学习资料、方法、练习、技能与能力成长 |

三个README是领域导航；正式内容仍由 `memory/records/`、`memory/topics/` 中同一份受审对象提供，原文仍在 `memory/sources/`（ODS来源层）。跨领域内容可以从多个入口链接同一对象。领域目录中的 `current.md` 继续作为未填写的目标与计划模板，分类归属不会把监控记录变成已确认目标或计划。

### 新信息的自动归类

分类由已有提取调用在理解所选原文时完成，独立Review同时检查内容与分类依据，仍为原有两次调用。新建或实质更新的record使用 `domains` 保存稳定键：`health`、`career`、`learning`，可选择多个，不能重复或使用其他键；不确定或不属于三领域时允许 `[]`，在目录中明确显示未归类，需要用户澄清意图时由Review进入待确认流程。程序检查键值与版本，语义是否合理由独立Review核对，不能用文件名或指标关键词直接认定用户事实。

精确复用的旧对象保留全部受审字段、正文、标签顺序和内容版本。旧record的自由字符串domains仍可读取；旧“健康”标签可映射到health，其他细粒度标签保留原样，“数据质量”不会单独映射到健康。wiki没有新增domains字段，其领域在读取时由所引用record的精确ID与content_version聚合。旧标签没有明确映射时保留未归类状态，不改写旧对象来制造新分类。

策略 `three-domains-v1` 的定义同时送给提取与Review；版本及摘要写入新候选的versions，并纳入rules_version与增量processing_key。读取带新策略的候选时重验分类约束，未知策略拒绝；旧无策略字段的受审链继续按原契约核验。分类策略或规则改变后，旧processed标记不能代表已按新规则加工；重新加工仍需明确来源、旧运行范围和可用额度，不自动重跑真实数据。

### 发布后的领域目录

`pdm knowledge --repo . --source <已保存来源路径> --run <已发布运行> --domain health --format markdown` 根据已核验的当前证据自动列出领域目录；source和run可重复，domain可省略以查看三类，默认输出JSON。目录只包含所选范围内正式发布的record与topic链接、版本等元数据，保留来源／运行覆盖缺口和未归类事项。topic只按精确匹配的record版本聚合；不能因有一项归类成功就隐去其他依赖缺失。

命令复用current evidence的纠正、删除、版本和预算检查，读取分类不调用模型、不写Git、不隐式纳入全库或其他来源。三个README保留固定导航；新增知识在动态目录中按受审标签显示，不在每次发布后重写三个README。capture仍先保存原文，分类发生于实际执行process；工程能力不等于云端定时任务已创建。

## 文件角色与实际目录

下表区分已存在的仓库结构与拟新增位置。`raw`、`staging`、`records`、`wiki` 是逻辑角色，不表示仓库中已有同名目录。

| 逻辑角色 | 当前实际路径／状态 | 文件粒度及后续安排 |
| --- | --- | --- |
| 项目规则与工程验证 | 公库 `project/` | 方案、规则、模板、人工样例和通用检查；私有验收证据留在私库 |
| 原始来源 `raw` | `memory/sources/` 已有，含 `inbox/`、`conversations/` 初始结构 | 一次采集或一份来源一文件；继续使用现有路径，不另建 `memory/raw/` 形成副本 |
| 候选 `staging` | `memory/staging/` 已实现 | 以运行ID保存候选；M1显式增量可提交新增、更新或复用，正式写入仍须独立Review |
| 正式记录 `records` | `memory/records/` 已实现 | 一个可独立维护对象一文件；旧入口create-only，M1增量按固定旧版本条件更新／复用 |
| 主题综合 `wiki` | `memory/topics/` 已有初始结构 | 一主题或实体一页，关联记录与证据；继续使用现有路径，不另建 `memory/wiki/` 形成副本 |
| 待确认事项 | `memory/pending.md` 已实现 | 每项稳定 `item_id`，关联问题、范围和用户回复；answered与resolved分别核验 |
| 运行与控制记录 | `memory/runs/captures/`、`processes/`、`controls/`、`schedules/` 已实现 | 采集、审核、发布、失败、控制和日程事件；新增预算绑定日程事件0.5，旧0.4不重写 |
| 共享调用审计／历史预算 | Git仓库外私有目录的独立JSON账本：日常schema2 audit；历史schema1有限模式 | 日常不设每日／累计调用次数硬门槛，保留逐调用身份与unknown；历史有限授权及消费保持，不静默转换。目标云位置和恢复仍需实证 |
| 私有历史档案 | 私库 `archive/`、`fixtures/` | 保留原树与祖先，不公开历史资料；前缀子树不是独立Git根，不自动转换pdm-1 |
| 领域导航 | `memory/health/README.md`、`memory/career/README.md`、`memory/learning/README.md` | 按三个领域链接已有正式记录与主题，共享一份受审内容 |
| 既有个人模板 | `memory/profile.md`、三个领域目录中的 `current.md` | 保留未填写状态，不自动填入目标、计划或迁移事实 |

主要载体为 YAML 文件头加 Markdown 正文。标题和文件名便于阅读，稳定 ID 用于关联；移动文件或改标题不改变对象身份，文件链接随之维护。记录正文保留内容、适用范围、证据说明及必要关系，不要求把所有自然语言强行压进 YAML。

原始来源保留实际取得的内容。只有链接时，存链接与状态；已取得正文或转录时，保存文本并说明与原件的关系。链接记录不证明已读到全文，也不代表附件本体已经备份。

### 历史档案的原SHA与原路径

私有历史前缀不改写文件内原source commit、Review、manifest或路径绑定。核验从固定原commit建立私有隔离checkout，在其真实Git根按原路径读取；当前前缀不是旧回执的新绑定。根 `memory/` 与现行程序保持自己的树和契约，schema探测及知识入口不自动纳入 `archive/`／`fixtures/`。不能把归档子目录作为 `--repo` 输入，也不能将 Runtime 的 `pdm-1` 资料交给当前writer。公库独立历史不包含这些资料树或私库祖先。

## 标识、版本与引用

| 字段 | 统一含义 |
| --- | --- |
| `schema_version` | 本文件使用的数据结构版本；与模型版本、内容版本分开 |
| `source_id` | 一份来源的稳定标识，不因重复提交或文件移动随意新建 |
| `source_version` | 原始来源某次内容快照的版本；记录引用必须绑定它，不能只指向不断变化的“最新版” |
| `id` | 候选、正式知识对象或 Wiki 页的稳定标识；候选发布后沿用对象 ID |
| `content_version` | 该对象正文及实质性元数据的具体版本；内容、证据或有效时间变化会形成新版本 |
| `rules_version` | 本次加工或审核采用的规则版本；规则升级不等于对象已经被重新加工 |
| `run_id` | 一次实际处理批次的标识，不用它代替来源或对象身份 |
| `review_id`、`item_id` | 分别标识一次审核记录与一个待确认事项 |
| `review_ref` | 指向适用于本条内容版本的审核记录；待审核时可为 `null`，正式发布时必须可追溯 |
| `commit_sha` | 实际写入成功后获得的 Git 提交标识，记在成功回执中；不要求提交包含自身哈希 |

ID 的具体格式及版本生成算法在试跑中确定。文件 Git blob SHA 用于核对保存内容，不能与业务上的 `content_version`、`source_version` 或 `schema_version` 混用。`review_status`、`review_ref` 等审核结果关联信息与实质性内容版本分开，避免仅回填审核结果就产生循环审核；受审字段范围和一致性检查由实现明细规定。

来源引用采用 `sources`，每项至少包含 `source_id`、`source_version`、`locator`；必要时增加原文短摘。`locator` 必须能定位实际版本中的消息、段落或片段，不能只有整页链接。其他记录的引用使用对象 `id` 和 `content_version`，可附当前相对文件链接。

来源保存回执将 `source_id`、`source_version` 映射到实际 `commit_sha` 和该提交内的文件路径。历史回溯先解析回执，再读取指定提交的原文并定位片段；不能沿最新版路径冒充旧版本。文件移动后仍保留旧映射，新版本新增对应回执；提交成功而回执中断时，先核对远程原文再补齐。

一页有多个主要结论时，在正文中分别绑定证据和适用时间，不能让整页共享一组无法对应到结论的泛化来源。派生关系使用 `derived_from`，指向所用来源或对象的具体版本；总结、回答、模型建议和 Wiki 不能重新被当成独立原始证据，增强同一结论的可信度。

## 各类文件的最小结构

以下为建议最小字段。未知值保留 `null` 或明确说明未知；“必填”不意味着可以编造缺失信息。与某类内容无关的字段可省略，模板应说明必填、可空和可选的范围。

| 文件 | 建议字段与正文 |
| --- | --- |
| 原始来源 | `schema_version`、`source_id`、`source_version`、`source_type`、`platform`、`original_url`、`captured_at`、`acquisition_status`；正文保留实际文本、发言者、消息顺序和必要上下文；已知原始时间单独保留 |
| 来源保存回执 | `schema_version`、`source_id`、`source_version`、实际 `commit_sha`、提交内文件路径、保存结果与实际时间；原文写入成功后记录，拟存于运行记录区域 |
| 候选／正式记录 | `schema_version`、`id`、`content_version`、`type`、`title`、`source_kind`、`sources`、`valid_from`、`valid_to`、`recorded_at`、`generated_by`、`review_status`、`review_ref`、`verification_status`、`lifecycle_status`；正文写结论、主体和适用范围 |
| Wiki 页 | `schema_version`、`id`、`content_version`、`title`、引用记录及版本、`updated_at`、`generated_by`、`review_status`、`review_ref`；主要归纳分别引用依据 |
| 待确认事项 | `schema_version`、`item_id`、问题、来源与对象引用、影响范围、`created_at`、`pending_status`、用户回复引用；无回复时保留空值 |
| 审核记录 | `schema_version`、`review_id`、`run_id`、受审候选及版本、所用来源及版本、相关旧记录及版本、`rules_version`、审核模型信息、`reviewed_at`、结论和原因 |
| 批次记录 | `schema_version`、`run_id`、输入及版本、`rules_version`、模型信息、各项处理结果、失败原因、未完成范围、`run_status`、开始与结束时间 |
| 发布清单与回执 | `schema_version`、`run_id`、发布对象及版本、对应审核引用、输入映射和结果；成功回执另关联实际 `commit_sha`、远程地址及写入时间 |
| 禁止重建控制 | 控制记录 ID、受控对象／来源范围、依据、记录时间和是否仍生效；只保留防止误恢复所必需的信息，普通删除不等于清除 Git 历史 |

`domains`、`derived_from`、对象间关系及用户采纳、执行进展按需要补充。审核模型身份由审核记录保存；`generated_by` 表达内容加工者，不用它替代审核者或原始作者。

### 来源身份与加工者

- `source_type` 表示材料形态，例如对话、个人笔记、文章、视频或附件链接。
- `source_kind` 表示该项结论由谁提出：`user_statement`（用户自述）、`external_source`（外部来源）、`model_inference`（模型新增推断）。混合内容应逐项说明；不能因多个来源共存而抹掉身份区别。
- 原始发言者、作者与平台记录在来源及相应片段中。对话可以有多个发言者，不用一个页面级作者覆盖所有消息。
- `generated_by` 记录处理这条内容的 `run_id`、模型标识 `model`、`rules_version`；人工录入等非模型处理应如实记录实际处理方式。

模型忠实整理用户表达时，`source_kind` 仍为 `user_statement`。模型新增的归纳和建议才标为推断；收藏不等于认同，阅读不等于掌握，讨论不等于决定，决定不等于执行。

### 候选决定示例

以下为完全虚构的结构示例，不描述任何真实个人决定，也不作为用户来源。占位符仅说明字段，`draft-0.1` 表示设计草案，不是可写入的现行schema。

```yaml
---
schema_version: draft-0.1
id: synthetic-decision-001
content_version: "<候选内容版本>"
type: decision
title: 虚构示例：实验笔记采用文本格式
domains: [learning]
source_kind: user_statement
sources:
  - source_id: "<来源ID>"
    source_version: "<来源内容版本>"
    locator: "<消息或段落位置>"
valid_from: null
valid_to: null
recorded_at: "<带时区的实际记录时间>"
generated_by:
  run_id: "<实际加工批次ID>"
  model: "<实际加工模型标识>"
  rules_version: "<实际加工规则版本>"
verification_status: unverified
lifecycle_status: undetermined
review_status: pending
review_ref: null
---
```

正文补充决定内容、决定主体、适用范围与原话依据。示例尚未审核，`lifecycle_status: undetermined` 表示尚未确定现实有效性；未知的决定发生时间不能用文件创建时间替代。加工模型出现在 `generated_by` 中，决定的原始表达者仍由 `sources` 对应的用户发言证明。

## 时间和状态的含义

### 时间

| 字段 | 表示什么 |
| --- | --- |
| `captured_at` | 系统实际采集到原始材料的时间 |
| 来源中的原始时间 | 消息、文档发布或事件发生的原有时间；按材料分别保存，不用采集时间补齐 |
| `valid_from`、`valid_to` | 对象所表达信息在现实中的有效范围；具体事件发生时间可在正文或对应类型字段中表达 |
| `recorded_at` | 系统建立这条知识记录的时间 |
| `updated_at` | 系统更新文件或记录的时间，不自动代表现实状态变化 |
| `reviewed_at` | 本次审核实际发生时间 |

实际时间戳采用带时区的 ISO 8601。原文只提供日期或月份时保留原精度，不凭空补具体时刻；“下个月”等相对时间同时保留原话和可用的参照依据，无法可靠落定就保持未知。时间推断需说明依据和推断身份。

真实变化保留过去在当时成立的记录；错误纠正表达旧内容有误。新旧对象通过明确关系和证据关联，不能只按最后写入时间判断哪条是当前事实。如何查找受影响对象、标记复查及重新审核，见实现明细。

### 独立状态维度

下列英文枚举是试跑草案；不同维度不能压成一个笼统的 `status`。

| 字段 | 建议值 | 含义和边界 |
| --- | --- | --- |
| `acquisition_status` | `link_only`、`partial_text`、`full_text` | 分别表示只有链接、取得部分文本、取得完整文本；不表示附件本体已备份或内容已核实 |
| `review_status` | `pending`、`passed`、`needs_revision`、`needs_confirmation` | 待审核、审核通过、需返工、需澄清；`passed` 适用于指定内容版本 |
| `verification_status` | `unverified`、`supported`、`contested`、`undetermined` | 未独立核验、按记录的方法得到支持、存在反对证据、暂不能判断；同时保存核验方法和支持／反对证据 |
| `lifecycle_status` | `undetermined`、`current`、`historical`、`superseded`、`retracted` | 有效性未定、当前适用、过去成立、被新版本替代、因错误撤回；与是否发布分开 |
| `adoption_status` | `proposed`、`accepted`、`rejected`、`undetermined` | 建议、用户采纳、用户拒绝、尚不清楚；仅在需要表达采纳时使用，变化须有用户证据 |
| `execution_status` | `unknown`、`not_started`、`in_progress`、`completed`、`cancelled` | 执行情况；“未反馈”是未知，不能推定已经开始或完成 |
| `pending_status` | `open`、`answered`、`resolved`、`withdrawn` | 问题待答、收到答复、对应处理已完成、事项撤回；收到答复不等于知识更新完成 |
| `run_status` | `completed`、`partial`、`awaiting_confirmation`、`failed` | 批次结果摘要；同时列各项结果，不能仅凭摘要判断每项已经发布 |

Review 通过表示达到内容整理与发布要求，不等于独立事实核验，也不等于用户采纳。`supported` 只能在已有核验方法和证据时使用，不能以模型 Review 代替。对象当前有效，也不代表候选已经发布；正式发布由审核记录与实际写入回执共同证明。

关联关系建议分别表达：`supersedes` 表示替代，`corrects` 表示纠正，`derived_from` 表示派生。关系指向具体对象／来源及版本，并附依据；没有证据时不能仅凭新文本出现就撤回旧记录。

## 数据边界与维护归属

只保存用户明确提交的内容及获准取得的资料；对话保存可见消息和适当工具结果，不声称保存模型内部推理。凭证、API 密钥和访问令牌不能进入记录或 Git。私有仓库存储权限不等于授权任意模型读取全库。

未加工原文和正式记录都需表达纠正、撤回与删除控制。回答可以使用尚未加工的信息，但不能让已纠正的旧原文绕过控制再次成为当前事实。普通删除保留 Git 历史；彻底清除历史的范围另行明确。删除、重跑、依赖更新及实际读取检查的步骤由[实现明细](implementation-details.md)统一定义。

新增字段、状态或关系先修改本文及对应模板，再更新执行规则和[场景验收](scenarios-and-acceptance.md)。记录数据格式变化的验证依据，避免旧文件仅因缺少新字段而被误认成事实不可信；迁移与兼容策略随实际变更实施。未来索引应能从权威文件重建，不另建可编辑的事实副本。

## M0 独立问答证据（程序 `0.1.0`）

读取范围显式指定一份来源和可选发布run；给定run时必须完整复验来源／采集回执、候选、独立Review、正式对象、清单及回执绑定，再只读取清单中的记录和主题正文。模型输入包含原文、原文行号及对应正式元数据／正文，不能扫描全库或加入其他run。原始引用仅采用来源ID／版本／行号，派生知识不成为新的独立证据。

证据与完整问题payload各受192000字节上限，不截断；传输另受规则在内的256000字节上限。调用前后核对HEAD、控制、工作区与正式字节，拒绝未提交篡改及版本变化。只读回答不写memory；返回耗时／可取得用量、选中来源及正式对象版本、证据和输入payload的SHA256与字节数，实际模型未知保留unknown。真实问答和通过结论仅存私有验收记录，不回写成用户来源。


### dev11选定输入范围的版本化审核契约

引入选定输入范围契约后，新分解计划固定`request_contract=selected-input-scope-v1`和`input_scope_inventory`，每个object／coverage／global请求均携带同一受绑定清单。清单记录选定来源/采集回执绑定、实际提供的metadata、fragment与全局行范围；本批未提供的选定来源明确为metadata=null、metadata_provided=false、空fragment和零提供行数。局部unit实际正文仍只在sources中提供，不把清单当作未读原文、健康事实、全库不存在或外部独立核验的证据。

新plan_id及request hash覆盖该契约和清单；逻辑counter身份保持，换契约不能绕过旧unknown或四次上限。无契约字段的旧计划在current和historical读取时均按原request及原prompt摘要精确重建，不改旧verdict；旧writer拒绝新proof。extract v13收紧为本次提供/选定范围，review v10说明这三个范围的区别。schema0.8保持，旧数据不迁移；人工反例按公开验收定义检查，真实运行证据留私有位置。


### dev12显式返工schema0.9

`revision_context`的contract为`review-feedback-v1`，绑定旧stage/Review的commit/path/hash、原candidate binding、sources、related、controls以及完整旧candidate/actualReview。`model_context`为确定可重建的完整语义投影：全对象/operations/versions和完整Review保留，当前payload已有的旧raw副本省去。其binding、投影及当前完整原文共同进入新加工请求/候选/plan验证，不把反馈quote当source引用。深度上限8，循环拒绝。

新的revision_attempt为schema0.9稳定不可变指针，与started同commit保存；unknown或重复不能换code/rules/run重发。原样输出排除generated_by/recorded_at/review标记后仍相同则revision_unchanged，保存实际提取及失败但不开始Review。新candidate/Review/manifest/receipt/failure按0.9核验；source/capture0.1、正式0.2、普通proof0.8与group0.6不迁移，不支持0.9的旧writer拒写。当前／历史发布核验闭包包含完整旧退回authority snapshots，证据构建与读取包生成前复用此核验；包内提供选定证据、引用和版本摘要，不能仅凭包完整性证明发布真实性。新候选仅在Review passed并正式发布后作为正式派生知识；原文仍按明确范围和状态参与回答。


### dev12零启动返工续接契约

`resume_unstarted_revision(repo, run_id, *, allowed_source_paths, model_call, reservation, expected_reservation_sha256, related_runs=(), expected_head=None, batch_context=None, source_views=None, review_strategy="decomposed-v1")`只处理已经存在且确定未启动的原预约。`reservation`固定`contract=unstarted-revision-resume-v1`、schema0.9、原run／revision_from_run／head、started与revision_attempt的路径/commit/hash、原processing_key／revision_context摘要、精确source_paths／related_runs、budget身份与外部原回执摘要。`expected_reservation_sha256`为操作者独立核定的canonical裸SHA256；不能把任意新空ledger当原预约。

budget包含原schema／pool_id／authorization_sha256／limit／initial_used／mode／operation_id／reserved_roles及实际ledger字节SHA。锁内只读核验真实账本、原operation确有extract/review预约且calls为空；started、unknown、completed或任何非空calls均拒绝。开始模型前不reserve、不写Git，实际payload.controls.head仍是原预约commit。隐藏参数缺revision_from_run或run也早拒。

新candidate.revision_resume持久保存原reservation、其可信摘要及这次completed extract的实际audit事件。新processing_key绑定此显式续接合同与原摘要，原started／revision_attempt及旧key原字节不改。Review／publisher／current／historical核验旧Git authority、新key映射及实际extract payload/调用绑定，不读取外置ledger、不要求调用后ledger仍空。完整私有ledger／凭证／原生prompt及执行器不进入Git；外部回执仅存摘要。该入口不追加调用额度，不用于未知调用恢复。


### dev12已完成提取的显式重校验

每个record／Wiki的原文引用上限为256，与M1生产最多256来源对齐；逐条source/version/locator/quote、完整载荷与必要语义容量门禁保持，超限不删引文。来源／采集0.1与正式对象0.2不迁移，新重校验候选仍走显式0.9返工链；旧代码对新processing key必须拒绝，不仅依赖schema数字判断兼容。

`revalidate_completed_extraction`／`revalidate-completed-extraction`仅复用已知completed的原提取结果，禁止新extract调用。操作者独立核定attestation摘要，程序对账原run、failure／started／revision_attempt、精确来源和feedback、原payload与返回、真实原budget完整身份及completed调用。started／unknown／error、别pool/auth/op、原输入或输出变化均拒绝。原失败和预约字节不改、不退还消费。

`revision_revalidation`明确区分原提取版本／原request head与batch context，及当前validator版本／当前Review plan。原输入和返回按精确结构持久绑定为未受审计算证明，可确定性重建并核完整原native输入摘要；事实仍只来自获准原文，不能凭复验将原返回判为正式知识。旧failure只有invocation/model/elapsed三键，旧ledger不含output hash；新内容authority来自操作者核定的原结构化cache与实际调用匹配，不能倒称旧Git已有返回正文锚。

候选内仅保原获准来源范围的必要结构化输入／输出及通用提取规则，用于当前／历史／远程离线复验；原私有缓存文件、拼装native prompt、认证和QA答案不上传。新独立Review仍完整核每个对象、覆盖与整体，全部passed后才发布。重复同stage复验不再提取，已完成Review零调用；未知Review不能通过换operation／pool或通用恢复入口重发。

### 普通auto审核的原提取归属

普通auto候选在尚无分解计划时恢复为分解Review，传入调用账本须有原extract的completed记录，invocation与候选一致、payload SHA与完整原请求一致；缺少关联则报`review_extraction_operation_mismatch`。原根Review已有started／unknown／completed记录时不转入子调用，先对账。空pool或新operation不能证明旧Review未启动；合法同原提取且Review确未启动才继续。此门禁不改候选／规则／schema，不重新提取，实际模型加工版本与后继工程门禁版本分别记录。
