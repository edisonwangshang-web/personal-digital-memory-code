# 个人数字化记忆实现明细

[总体方案](overall-plan.md) → [技术方案](technical-plan.md) → 实现明细

本文说明明确提交的资料如何保存、加工、审核、发布与使用，以及纠正、删除、持续编排和失败恢复。本批开发版为`0.2.0.dev29`，主路径仍为 **source → staging → record／topic**。分类及质量检查嵌入原提取／独立Review，不增加阶段。本批补提取超时自动有限重试，回归与准确dev CI待核，真实全链与M1整体另行验收，状态见Roadmap。本批仅dev开发验证，不合入或推送master。

新默认为`knowledge-extract@11`／`knowledge-review@9`、普通质量链0.15，基础提示词extract v14／review v10／answer v8保持。旧10/8、0.12及assembly0.14按原绑定恢复，不因默认变化升级或重跑。实际字段见[数据规范](data-conventions.md)；`memory/`路径均指私有资料库，公库不含个人原文、知识或运行历史。

文档分工如下：

- [技术方案](technical-plan.md)定义系统组成、运行位置和模块交接。
- [数据规范](data-conventions.md)统一定义对象、字段、状态、版本、文件模板与目录映射；本页不另定义一套。
- [场景与验收](scenarios-and-acceptance.md)保存人工输入要求、操作步骤、预期结果与检查方法。
- [Roadmap](roadmap.md)管理阶段成果、执行顺序、进度与通过证据；本页不重复阶段表。

人工触发后可由受控编排串联采集、production、独立审核、发布和读取；单独 `capture` 只保存，不请求模型。M1 整体尚未验收：目标 GPT 每日北京时间 22:00 任务尚未创建／启用，目标 runner、两轮真实触发、云端持久恢复与第二模型独立读取仍待验证。采集、加工、定时编排、复用和恢复代码统一由公库维护并加载，`--repo` 指向仅存资料的私库；公开工程检查绑定公库准确 HEAD，不代替真实资料验收。双仓入口只读核对同名分支、准确公开 CI、工程完整性和私库无活动工程，不同步代码或迁移资料，见[双仓使用说明](dual-repository.md)。

## dev29：提取超时后自动继续

1. 新`production prepare`默认冻结`bounded-timeout-v1`。旧队列无marker不原地升级；当前工程的新目标计划必须与原工作保持相同source／fragment、控制、Skill及rebuild回执，继续原有限计数，不新建业务意图。
2. 仅独立人工队列、单完整批、无related或答复续接的普通root提取适用，包括依据真实Review意见的返工extract。核原调用unknown＋`error_code=timeout`＋已保存`finished_at`、原extract timeout失败、原started／engine与计划；该超时run无任何候选、Review、审核计划或发布，来源及控制未变，且仍使用同一持久audit池，才自动记录重试回执。仍在途、非timeout、`timeout_cleanup_unknown`、预算／权限或任何Review超时（含分解审核）等保持停止并报告原因。
3. 保存schema0.16 sidecar链接原run／operation、原证明和唯一后继，不修改原unknown或旧attempt条目。后继使用同work的下一attempt，candidate／started／attempt绑定`timeout_retry_ref`；该引用加入处理身份但不作为模型材料；返工仍提供原真实草稿及未解决Review意见，不把超时回执或传输错误历史送模型。后继零调用预约中断时继续原run／operation，不重复预约或计次。
4. 在现有worker步数范围内自动继续，首次加最多3次，总attempt最多4；任务层可按failed继续，底层实际调用仍是unknown。第4次仍超时当即`attempt_limit`停止，不发第5次。已知失败的既有重试上限继续生效，成功后仍须完整独立审核和条件发布。
5. 本地worker保持串行，不能据此声明服务端旧请求已取消或没有重叠。停用的旧run及依赖不得再被普通发布、group采用或当前证据作为正式结果；历史记录保留。Skill11/9、quality0.15、assembly0.14及来源／正式对象不迁移，旧writer遇0.16门禁拒写。

无需每次失败再人工确认，程序自动保存受控重试回执。具体字段、适用范围和已用次数规则见[有限超时合同](data-conventions.md#bounded-timeout-retry)。工程与人工反例通过后，真实链仍需实际模型输出、Review、发布和读取证据。

## dev28：纯程序组装在保存候选前中断

最终组装已提交`started.md`及同提交engine、但尚未写入staging时，生产队列先核原预约确属`verbatim-draft-finalization-v1`且`reserved_calls=0`，核原run、原attempt、计划、完整草稿、Skill、来源及控制不变。再以原审计pool和operation核root的calls为空、真实审核布局对应的子单元计数器及operation均未存在（包括仅预约），该run目录仅原started／engine且候选从未存在，才以原run重新进入确定性组装。没有原持久账本的调用者不能仅凭`reserved_calls=0`自动恢复。

恢复保留原started／engine和attempt身份，不重新提取或刷新次数；原样候选仍需完整object／coverage／global独立Review及group发布检查，后续Review正常调用并计次。已有staging继续既有候选恢复路径。root已有任意调用记录、子单元已有状态，来源／计划／证明变化，或曾经有候选而当前缺失，都不能通过此零调用路径。当前版本仍须满足原plan绑定，不保证跨工程升级自动恢复。完整契约见[数据规范](data-conventions.md#assembly-zero-call-resume)，人工中断步骤见[场景验收](scenarios-and-acceptance.md#assembly-zero-call-resume-acceptance)。

实际模型超时不能走本节纯程序组装路径。符合[dev29有限超时合同](data-conventions.md#bounded-timeout-retry)时由自动重试回执处理，原unknown照常保留；其他未知结果继续停止对账。

有限处理仍是首次加失败后最多3次重试，总attempt最多4。第4次已知失败在当前切片立即以`attempt_limit`停止；必需分解审核单元耗尽时父工作也停止，重开不会另起extract。仅dev29明确适用的已结束提取timeout可自动继续；其他unknown停止对账。第4次通过或待确认分别保持正常后续与等待语义，不按次数误判。详见[有限重试合同](data-conventions.md#bounded-processing-retries)。

## dev27：等待时间与纯绑定返工

新标准CLI单次模型调用默认900秒，`--model-timeout`可设1—1800秒，并实际传入子调用；专用历史恢复入口默认120秒保持可显式覆盖。production沿本次worker参数推进，不改变queue身份；schedule使用原冻结`timeout_seconds`，旧值不迁移。提高等待时间本身不允许重发unknown，符合dev29有限超时策略时才自动重跑，详见[超时合同](data-conventions.md#model-call-timeout)。

收到Review9的真实`needs_revision`后，先核`repair_scope`及完整旧证明。纯`statistic_bindings`且检查、目标pair和固定范围全部满足时，同一个revision extract11只取`{statistic_bindings:[{stat_id,record_id,body_evidence}]}`；程序从固定统计和原文物化value／根引用，其他正文、关系及旧quality保持。raw patch与完整输出分别绑定，完整新Review后才允许发布。内容或混合问题走原全稿，错误或歧义不能猜，原稳定次数及unknown保持。字段与拒绝条件见[定点返工合同](data-conventions.md#metadata-only-statistic-repair)；该版隔离定向回归、标准CLI人工全链及准确CI的固定证据见Roadmap，不能移用于本批HEAD或真实验收。

## dev26：多批容量回退与反馈接续

各批草稿通过后，若完整原文并集可承载，继续原最终合并提取。只有合并输入容量受阻时，编排才尝试`verbatim-draft-finalization-v1`：完整核验原草稿及真实提取／Review，原样联合最终对象、来源、统计与关系，保存schema0.14候选及无新extract的来源证明，再沿既有object／coverage／global逐项独立审核。全部passed后由group publisher核完整plan和正式字节，原子发布。

该回退限无related、答复续接或重建上下文的普通完整草稿，不支持原样reuse；同路径不同正文、质量范围／统计口径不一致、必要依赖缺失或审核单元超容量时保存明确原因，不截断原文或发布部分结果。原样final真实退回需实质修订而仍超容量时，下一次返回`assembly_revision_required`且0新调用，保留意见和次数等待处理，避免重复相同联合再审。候选、证明与错误边界见[数据规范](data-conventions.md#verbatim-draft-finalization)。

正常10/8草稿与答复续接的`needs_revision`通过同一反馈路径重新提取／审核，原phase、完整答复、来源片段、merge和related范围保持精确绑定。草稿修订后仍不能直接发布；事项只有正式record采用全部有效回复后才resolve。旧9/7及更早phase拒绝、历史operator恢复入口和unknown对账规则保持，不换身份刷新有限次数。

新质量候选固定`review_request_contract=current-draft-v1`：普通Review构建时只去掉指定返工／恢复历史，保留当前稿、质量检查及对应证据；持久candidate binding和完整历史proof不减少。被退回稿带同marker时，返工extract取最新稿、未解决意见和必要证据，完整真实Review仍存储核验，分解Review白名单保持。旧stage／resume不回填marker，旧投影和请求字节保持；未知值在调用前拒绝，具体字段见[当前稿审核合同](data-conventions.md#current-draft-review)。

<a id="explicit-knowledge-rebuild"></a>

## 显式清理后从原文重建

1. 操作者明确本次稳定意图、获准原文与现有正式目标；`rebuild-prepare` 核对来源／控制和主题依赖闭包，在同一提交删除选定 record／topic 并写清理回执。来源、采集回执、旧候选、审核、运行和调用历史保留。重复同一意图只返回原引用，不再次删除。
2. 将返回的 `rebuild_context` 三键引用交给 `process` 或 `production prepare`。程序核原子清理、不可变历史、原来源／控制及精确范围；首次加工输入的旧 related、revision、merge／prior 和复用均为空；新候选被退回时，仅允许同回执绑定的本轮新候选／实际审核反馈返工。新候选绑定该引用及实际公库工程版本，不取旧知识作事实输入。
3. 当前只支持一批完整来源的重建。预约前完整核输入容量，模型重新提取和自动分类，程序校验后执行既有独立 Review；审核、覆盖和统计要求保持。没有通过完整审核就不发布。
4. 只有回执明确清理、仍未被再次写入的路径可通过本次 create 历史检查；新路径沿原 create 检查，控制排除的对象／来源不能借重建恢复。发布及历史读取重建同一固定证据，普通无回执调用的删除保护不变。
5. 生产与分解审核身份仅在该回执存在时加入其绑定，同一意图跨队列、日期和程序升级仍沿用同一有限计数。旧 unknown、失败及计数不改。实际成功后再更新私库导航，并分别核真实读取与远程恢复；清理回执本身不证明加工通过。

字段与命令分别见[数据规范](data-conventions.md#explicit-knowledge-rebuild)和[日常使用](daily-use.md#explicit-knowledge-rebuild)。

<a id="quality-completed-recovery-v2-execution"></a>

## dev25：同轮日期校验失败的零提取重验

仅对同一显式重建回执的原生已完成提取、原 `quality_temporal_mismatch` 且尚无候选／Review 使用[独立V2合同](data-conventions.md#quality-completed-recovery-v2)。操作者固定原始缓存和原调用摘要，核原完整payload／response、请求字节、原来源、同轮真实反馈、重建回执与pool／operation；unknown或变更范围不得进入。默认V1仍仅处理其原存储失败。

使用当前日期校验器重验原响应，原始JSON及失败保留；中文月日必须在实际来源引文中唯一确定同一完整日期，歧义、缺依据与当前状态冒用继续拒绝。零新extract，合格候选和schema0.13的V2 guard原子保存；随后仍执行真正独立Review、完整发布和读取。重验成功不代表审核或M1已通过，不新增模型阶段或修改旧Skill。

<a id="quality-completed-recovery-execution"></a>

## dev22 已完成提取恢复与发布接续

1. 确认是原生10/8返工提取已完成、只因候选存储过大失败，原 Review 尚未启动。原始返回和已消费调用保留，不再 extract，不把 unknown 当作已完成。
2. 先准备目标新 plan，再核定原始缓存和原生请求，生成[专用恢复证明](data-conventions.md#quality-completed-recovery)并固定 `validation_head`。证明如实标为操作者核定的本地返回；不能宣称原 failure 或服务端已保存输出摘要。
3. 核原 pool／授权／operation、completed invocation与payload、账本完整字节、四份缓存独立摘要、原 source／related／controls／Skill／规则和失败历史。冻结后出现 Git、账本或缓存变化时停止。
4. 预约前先核已经确定的当前核心与历史材料存储下界；这不预测未知模型输出，实际落盘仍完整核验。使用当前 validator 校验原始结构化返回及全部质量义务，分别记录原提取版本与当前校验版本。只为合格候选承载完整历史，不更改原 JSON；完整包／当前核心分别核[4 MiB／1 MiB](data-conventions.md#candidate-storage-limits)，其余限额保持。
5. 将 schema0.12 stage 与 schema0.13恢复门禁放在同一原子事务。原失败、预约和计数不改；不能先落新候选再补兼容门禁。
6. 在原调用身份下执行真正独立 Review。旧无marker恢复请求继续去掉candidate内四键重复历史／程序provenance、保留根处完整语义反馈的原视图；新生成并带`current-draft-v1`的恢复候选按当前稿合同取用。当前对象／统计／目标和required_checks保持，完整历史及原始payload／response在私库Git中受guard绑定，库外缓存按独立摘要核验；实际请求仍核256000字节，超限使用既有分解审核。恢复成功、结构校验或本地缓存核验都不能代替 Review 通过。
7. 按目标 plan 发布完整合格 group，再由新10/8生产队列核 marker、完整证明、相同 plan／bundle与已固定候选，CAS持久采用发布。重复采用不调用模型、不写提交，也不把旧失败counter改成passed。尚无真实group则不采用。
8. 私库推送、远程完整恢复与模型读取各核实际提交和证据；本节流程存在不等于已取得真实通过结果。

## dev21 受影响环节

1. 提取前从同版本旧受审链重算统计义务索引，放入本次完整请求。模型显式提交依据，程序在Review前拒遗漏；对复用及全文同字正文执行同一检查。
2. `preflight_processing` 复用实际payload构造，在新attempt及调用预约前计量全部规则、事实、关联和反馈。预检的control快照早于run预约，最终调用按预约后的快照记录实际binding并再次核容量。超限不启动模型，不以截断或提高256000上限放行。
3. 新10/8请求用版本化质量视图处理仅程序使用的逐行SHA；从同包精确行重建，持久完整context保留，旧请求按原渲染器回放。
4. final被实际Review退回后，队列显式传 `revision_from_run`，保留完整反馈并绑定新plan和新证明。预约前检查原指针／unknown，预约后CAS及返回run身份双核；普通重试不代替返工。dev26对10/8补草稿／答复续接，旧不支持的phase仍明确阻断，完整边界见页首说明。
5. 模型出错仅从结构化code／HTTP status白名单分类，不记录错误原文；已有unknown不因诊断升级追认或清除。

## 1. 采集保存：先让原始资料可找回

**输入：** 用户明确提交的文本、对话片段、想法或链接，以及来源平台、说话者和必要上下文。普通讨论、提交收藏、确认决定与执行反馈应区分，不默认收集全部聊天。

| 步骤 | 实际处理 | 输出或失败去向 |
| --- | --- | --- |
| 确定提交范围 | 保存实际选定的内容与上下文；对话只包含可见消息和适当的工具结果；去除凭证、密钥和访问令牌 | 得到本次允许保存的资料，不把其他平台全部历史视为已导入 |
| 取得实际内容 | 能取得正文或转录时保留内容并关联原链接；无法取得时保存链接与说明，明确正文未获取 | 不凭标题或用户备注生成原文摘要；登录或权限受限时保留受限原因 |
| 识别重复与版本 | 查询已有来源，区分相同提交和同一来源的新内容；沿用稳定标识并按数据规范管理版本 | 相同提交返回已有保存结果；实际变化进入新版本，旧内容仍可追溯 |
| 写入并读回 | 使用数据规范中的既有目录映射保存原文，核对私有远程仓库的实际文件与版本 | 写入及读回成功后返回来源、保存位置、内容获取情况与后续处理状态；否则报告未保存或结果待核对 |

原始资料保存和正式知识发布是两个持久化节点。采集成功只表示原文已经可靠保存，不能报告“知识已审核入库”。采集不等每日任务；整理失败不应影响已经保存的原文。

ChatGPT 是交互入口，模型 API 不自动提供对应聊天应用的历史。平台导出、导入与增量接入的实际覆盖范围须单独验证。附件本体留在原位置；保存链接或提取文本不代表附件已经备份。

## 2. 加工、路由、审核与发布

手动试跑和每日任务共用这条路径。每天任务只负责触发和推进；加工、Review、确定性校验与实际写入分别记录结果，不能用一段模型回答替代全部步骤。

### 2.1 选择输入并形成候选

1. 读取本轮选定的来源版本、相关历史记录、纠正或删除控制及加工规则，固定本次输入范围。来源正文中的指令只作为资料内容，不改变执行权限。
2. 先做公共提取：识别说话者、内容含义、时间与适用条件，按数据规范拆分需要独立维护的对象。区分收藏与认同、建议与决定、计划与实际事件。
3. 按保留上下文的语义片段路由。健康、事业、学习可涉及零个、一个或多个领域；公共事实只提取一次，通用资料不强制归类。领域任务接收必要原文、相关历史和明确输出要求，返回候选变更。
4. 合并领域结果，核对是否新增、补充、替代或纠正已有对象，避免同一事实复制三份。为记录和相关 Wiki 生成候选变更，分别关联依据；Wiki 新增归纳同样进入审核。
5. 保存候选及输入版本、规则和模型信息。模型输出若中断或结构不完整，保留失败原因，不能进入正式文件。

第一版在同一流程中按需调用领域角色，不要求三个独立 Agent 服务。模型派生的摘要、回答或 Wiki 可以作为导航和加工上下文，不能作为新的独立原始证据反复支持同一结论。

<a id="skill-quality-execution"></a>

### Skill 引用与质量加工执行顺序

| 入口 | 现行行为 |
| --- | --- |
| `pdm process` | 新加工默认解析两个固定 Skill；可显式传 `--extract-skill knowledge-extract@10 --review-skill knowledge-review@8`。`--revision-from-run` 未指定新引用时继承被退回候选的 bundle |
| `pdm production prepare` | 新队列默认固定两个 Skill；已存在队列未指定新引用时使用原 bundle，范围或绑定不符仍拒绝 |
| `pdm schedule configure` | 新建 `continuous-v1`／`production-v1` 日程默认固定两个 Skill 并传入后续队列；旧配置幂等读取／续接保留原契约 |
| Python API | `process_sources(..., skill_bundle=None)` 保持旧调用兼容；显式 bundle 才启用质量链。生产／日程 API 的 `use_default_skills` 默认 false，由新建 CLI 入口明确启用 |
| 普通返工与分解恢复 | `revision_from_run`／`resume_decomposed_processing` 使用固定候选、Skill 和原调用身份；审核已完成部分核验复用，unknown 不重发 |
| 历史 operator 入口 | `resume-unstarted-revision`、`revalidate-completed-extraction` 只支持原 0.9 证明；对新质量链报 `skill_quality_recovery_attestation_required`，不能静默降级或重建原预约 |

加载器只接受已注册、角色匹配且摘要一致的版本，拒绝未知引用、外部路径和依赖变化。版本化文件位于 `project/skills/knowledge-extract/<version>/`、`knowledge-review/<version>/`，共同依赖位于 `project/skills/shared/`。当前两个默认角色共同引用 `knowledge-context-v1`。`skill_support.py` 核不可变 manifest 与实际内容；`model_calls.py` 展开冻结规则并计算真实字节和摘要；`knowledge_quality.py` 准备并核验质量材料；pipeline、production、scheduler 及分解 Review 消费同一绑定。

新11/9普通质量族使用0.15，旧10/8的0.12、9/7的0.11、更早的0.10及assembly0.14按原合同核验。专用旧恢复仍有0.13门禁，来源0.1、正式record／topic0.2、group0.6及registry既有合同保持；不回填Skill证明，不迁移原计数或预约。

### 加工与审核

1. **固定资料与两项 Skill 引用。** 选择实际获准的原文、相关旧知识、控制和待确认上下文，同时固定 `extract_skill_ref` 与 `review_skill_ref`。程序解析两个引用及共同质量规范，将 `SKILL.md` 和完整必要依赖固定到不可变版本与内容摘要，并保存可恢复内容的固定提交／快照定位，核对角色及输出契约。必要依赖不可取得或不兼容时保留原因，停止相应阶段。
2. **程序生成资料清单与可复算统计。** 在`quality_context.sources`的表格profile中区分日期／时区等结构列、未完结等限定列和观测／目标指标，分别统计有值、已识别缺失、真实零值与解析异常。对于含义、单位及可比条件明确的指标，按固定方法生成`statistics`，通过共享定义和target内的样本范围保留区间、有效样本及原行依据。不同CSV的同源重复不叠加为新样本，未完结日单独处理；资料不足的指标保留缺口。对话和文章保留独立语义审核义务，不套用表格天数规则。
3. **加载加工 Skill 并调用模型。** 将实际展开的 Skill 规则、必要模板、原文及清单组装成完整请求，容量检查包含全部材料；程序保存装载绑定和请求摘要后按现有审计边界启动调用。模型在同一次提取中完成三领域分类、形成有用的主题概览和必要独立记录，输出每项指标的 `coverage_disposition` 与状态陈述的时间依据。覆盖全时段的概览优先交代指标可用情况和有依据的分段变化，单个日期记录服务于具体事实；原文限定仍需保留。
4. **在独立审核前执行程序校验。** 核对结构、分类键、来源引用、固定版本和指标集合；每项 `summarized` 必须指向实际候选记录，每项 `deferred` 必须给出具体原因。程序重算所引用统计、检查有效样本及纳入规则，时态陈述核对相应原文或真实系统回执。通过结构校验不等于语义充分，尤其不能只填满暂缓原因便声称整体完成。发现可修复问题时保存候选和错误，按既有有限返工规则修订。
5. **加载审核 Skill，独立检查实际最终候选。** 程序先按固定质量规范、实际资料和候选范围生成 `required_checks`，再提供给独立审核上下文。审核收到固定原文、清单、统计依据、完整候选及必要旧记录，并实际加载所引用的审核 Skill。既检查每条已写结论，也检查应处理却遗漏的指标、来源限制、重复和时态。分解模式的 object／coverage／global 各自加载相同版本的审核规范及本单元完整材料，逐项返回 `checks`；程序核必需项不缺失、不重复且范围匹配，总结论与逐项结果一致。global 只消费其范围与前序证明，局部通过不能替代全组义务。审核者只返回结论和具体问题，不静默补写候选。
6. **按质量结论返工、确认或发布。** `needs_revision` 携具体检查项与原文依据回到加工，修改后的候选重新校验并独立审核；原始冲突或意图确需用户决定时登记 `needs_confirmation`。只有程序门禁、必要审核单元和全部依赖均通过，才由现有发布入口提交最终记录及 Wiki。发布前重验 Skill／规则、源版本、统计与候选绑定和控制状态，防止审核后换规则或改正文继续放行。
7. **记录完成范围并沿原身份恢复。** 回执区分已发布知识、暂缓指标、待确认事项及执行阻塞，不能把原文已送入模型当成知识已完整组织。中断后按保存的 Skill、清单、候选和调用身份复核；已知完成且未变化的阶段复用，未知结果先对账。Skill 变化不重置尝试次数，旧调用、退回和发布历史保留。

Skill 由接入层受控展开，真实规则和模板参与模型请求；只传名称、要求模型自报已加载或依赖会话记忆均不能满足门禁。确定性清单和统计在程序内执行，模型不因 Skill 引用获得任意读库、联网或发布权限。CLI 继续关闭隐式 Skill 注入，由显式加载器提供固定规则。完整规则与必要依据超容量时保留阻塞，不能删规则、截依据或退回普通提示词后发布。

### 原样复用的解析与核验

extract@9的原始输出显式携带 `reuse_refs`。每项为 `{path, content_version}`，只从本次已验证的related对象选择；未变对象可省去重复全文，新建和实质变化项仍按原字段返回全文。batch中的objects/wikis可在引用足够时为空；普通模式的wiki可为空值，但物化后仍须恰好一篇Wiki。完整候选仍满足原记录／Wiki数量及全部依赖约束，不自动补回未声明对象。

程序先核引用的路径、精确版本、授权范围和控制，再物化旧对象的受审字段与正文。原始 `extraction_output` 和实际调用摘要保持；物化结果参与候选绑定并在当前／历史核验时从固定related重新构建。重复、未知或错误引用、全文与引用冲突、过时Wiki成员和缺失旧记录均失败关闭。

随后在物化的完整候选上核覆盖、数字、时态、分类及必要关联，继续原auto／分解Review和发布流程。物化不生成新的模型调用或新事实，也不能补写quality、代替Review、缩减必要审核证据或修改原始失败返回。发布reuse沿原operations机制保存正式文件原字节及原审核来源。

### 实际容量与分解交接

按真实逐行`source_views`测完整请求容量。source profile通过`evidence_id`保存完整实际片段，target和必需检查用`source_scope_id`／`source_scope_ids`引用；scope摘要仍核完整来源与候选，避免每项指标反复展开同一片段列表。object保持其完整对象与必要证据，coverage保持实际批次／draft的完整语义范围及全部最终对象，global核跨来源关系；不把逐行切割当作独立语义审核，也不通过删掉未被提取者声明的统计来省略查漏义务。

实际渲染按冻结角色选择v1／v2／v3；新11/9沿用`pdm-columnar-shared-json-v3`，依次还原列式、字段字典和共享值，并在程序内逐值核对。阶段内必要材料不被编码裁减，容量与摘要计入规则、共享表及说明的全部线上字节。旧角色请求按原字节；输出仍须实际业务字段，不拿共享引用代替原文或正文。

global使用`verified-quality-conclusions-v1`证书投影，保留前序逐项结论、理由、范围和完整输出／检查摘要，重复证据ID数组以摘要绑定。原始完整Review输出仍在proof节点保存并于汇合时重验；global仍看完整最终知识与关系，证书不声称其读过未提供原文。任何必要单元仍超容量就保留阻塞，实际最终字节和检查结果统一见提测记录，不能以压缩后的请求结构代替真实模型质量验收。

人工验收应检查：概览覆盖整个选定期间，说明实际记录覆盖和缺失；缺失不能写成现实事件没有发生，未完结日不能参与未经说明的全天比较。原文中的“尚未处理”等陈述须限定为写成当时；当前处理状态若需呈现，须有固定 Git／回执依据。预期值与退回反例放在人工验收样例，不写死到通用 Skill 或程序规则。

新提取的生成 Schema 按实际 `targets` 数量固定 `coverage_disposition` 的 `minItems`／`maxItems`，包括空清单和超过 256 项的情况；不额外增加来源上限。后置去重、正文实质、数字／来源／时间和独立审核继续执行；数组长度齐全不等于语义完成。有界标识枚举减少长 ID 抄写错误，模型返回后仍完整核验，不自动修正输出。

当前 v3 传输按列名与全部行值表达相同字段对象数组，先还原列式结构，再还原字段字典与共享值；不改变原 JSON、来源身份、引用、规则或业务 schema。旧 v2／v1 请求按其引用精确重建真实字节。保留键转义，限制深度、节点和展开大小；完整请求超过 256000 字节时停止。新分解计划在首个 Review 前预检全部静态单元，global 在真实前序证书完成后核实际容量。

### 自动领域策略

execution_versions返回domain_policy_version与domain_policy_sha256；策略摘要和extract／review摘要一起形成加工rules_version，并参与processing_key。新create／update的domains只允许health、career、learning且不重复，允许[]或多类；同一对象跨领域共享，不复制事实。归属的语义由提取模型和独立Review核对，程序枚举检查不代替语义判断；原始意图确需用户决定时进入needs_confirmation，普通分类修订仍按Review返工。

精确reuse须保持全部受审实质字段、正文及content_version一致，保留原自由标签与正式字节；伪装reuse返回invalid_domain_reuse。新create／update标签非法或重复返回invalid_domains，保留extract失败回执与实际调用计数。旧候选须同时没有策略版本和摘要两个标记，才按旧契约核验；只缺其一、未知策略或摘要不符均返回invalid_domain_policy。规则／策略变化后不能用旧processed标记冒称新规则已处理，现有旧data也不会因此自动重新调用模型或迁移。

### 2.2 校验与模型 Review

先执行确定性检查：对象与必填信息符合数据规范、引用能够定位、版本一致、文件可解析、变更范围明确。引文从实际 `source_views` 的单行 `line.text` 复制短摘，并使用全局 `Lx-Lx` 定位；多行结论拆成多个引用，不改原文，也不放宽实际子串核验。结构检查不通过时返回加工环节修复。

随后独立调用模型 Review。审核输入须包含**原始证据及版本、候选变更、相关既有记录及版本、审核规则**，不能只审核加工模型自己的摘要。第一版可使用同一模型的独立调用，也可使用另一模型；配置在试跑时确定。

Review 检查语义是否忠实、限定条件是否保留、时间和对象关系是否合理、关键结论和 Wiki 综合是否有依据。详细检查项和质量门槛统一见[场景与验收](scenarios-and-acceptance.md#质量门槛)。审核通过表示符合整理与发布要求，不表示外部事实已经独立核实，也不表示用户采纳或执行。

审核后的执行分支：

- 可以发布：进入版本核对与统一写入。
- 需要修改：保留问题和候选，返工后重新 Review；次数达到本轮上限则留到后续处理。
- 需要用户判断：登记或更新统一待确认事项，并保留受影响内容；无关通过项继续发布，任务不等待用户在线。

审核必须绑定实际审过的候选、来源、相关旧记录与规则版本。候选实质内容变化，或审核所依赖的来源、相关旧记录、规则版本变化时，旧审核失效，需要重新合并、校验和 Review；用户手工改动正式文件也适用这条规则。

受审内容版本按数据规范覆盖正文与实质元数据。发布步骤只可补齐对应已通过审核的关联信息（`review_ref`、`review_status`）及实际发布回执，不得借此改动正文、来源、时间等受审内容。这些关联信息不生成新的受审语义版本；实际落盘文件仍可用 Git blob SHA 等标识核对，避免把审核关联字段的回填误判成需要无限重审。

### 2.3 统一发布与回执

**发布前同时满足：** 规则校验通过、模型 Review 通过、待发布内容与审核版本一致、相关基础记录仍适用、没有纠正或删除控制阻止发布。任一项不满足，都不能进入正式知识。

1. 重新读取目标仓库及相关文件的最新版本。相关记录被其他操作修改时，先合并并重新审核；与本次内容无关的更新也须保留，不能覆盖。
2. 将互相依赖的正式记录、Wiki 变更和发布清单放在同一次一致的 Git 提交中。未通过或待确认的候选留在候选区；依赖它们才能成立的综合内容也暂不发布。
3. 通过单一发布入口执行写入，核对分支基础版本，防止并发覆盖。初期先采用串行发布；领域角色不直接改正式文件。
4. 确认远程提交成功并读回目标内容，才报告正式发布完成。提交标识与发布对象版本关联到运行回执。

发布清单先记录批次、输入和对象版本；提交成功后将实际 commit 标识写入独立回执，不要求一个提交包含自身哈希。回执写入失败时，依照已存在的提交补齐，不能再次发布相同内容。

### 2.4 显式增量路径

1. 调用者用 `process --incremental`，或列出 `--related-run` 启用增量模式；`--source` 继续逐个明确允许读取的来源版本。旧发布包含的全部来源必须在选择范围内，先核对集合再开正文，不能只授权一个新来源就隐式读入旧运行其他来源。需要比较同 ID 的前后版本时逐个显式选择；旧 create-only 模式保持原限制。
2. 对来源 ID／版本、规则和现行控制计算处理身份；存在 processed marker 时，先验证标记和固定发布链，并确认所要求的旧知识稳定路径均已比较，才返回 `already_processed`；额外未比较上下文拒绝。标记不完整或受损不能当成未处理直接重新调用模型。新尝试在模型调用前持久 started.md，并绑定后续候选／清单；同一预分配 run 中断后禁止重发未知调用。
3. 将原文、相关旧知识与实际控制一起交给加工，再由确定性检查分为 create、同 ID／类型 update、完全相同内容版本 reuse。旧对象不在许可快照中、当前字节变化、遗漏旧 record、删除控制或未知状态均阻止覆盖。检查直接依赖只查主题文件头，未显式纳入的依赖或单 Wiki 输出无法同步维护多主题时拒绝整组，不声称完成全部依赖更新。共同事实沿用对象身份、共享来源和领域关系，不靠换 ID 规避重复检查。
4. 加工与 Review 为两次独立调用。Review 固定新候选、每份来源／回执、旧记录和主题版本、规则、程序及控制摘要。`needs_confirmation` 同时保存 run 级记录与去重后的统一事项；只阻止依赖该问题的候选，无关独立通过运行仍可发布。返工不复用已经失效的 Review。
5. 发布入口再次检查所有依据、暂停状态、旧对象条件及期望 HEAD，通过一次条件 Git 提交写正式新增／更新、manifest 和 processed marker。reuse 保留旧正式字节和原 Review，用清单记录原对象来源；不能将本次新审核伪装成旧对象最初审核。回执随后保存真实提交，重复 publish 先核对已有结果。
6. 普通当前核验要求正式对象仍等于受审字节。已被合法后继更新的原发布，用 `verify-publication --historical` 对固定历史提交核对；历史有效只表示曾经审过并发布，不表示当前事实仍成立。采集、审核、原发布和后继提交的祖先关系必须保留。

条件事务将新增／修改／删除分别绑定不存在或旧字节，先核对所有目标再提交；冲突不能部分应用。Linux 条件修改／删除使用 renameat2 的不覆盖原子移动，能力缺失在副作用前拒绝，无降级覆盖。提交成功但工作区物化中断时按 journal v3 恢复；外部替换被捕获时保留外部字节与日志、停在 moving，不自动回填原目标。无法辨认是本次物化还是用户修改时保持暂停，不能回退整个仓库或覆盖新增资料。

### 2.5 日常调用审计

日常调用使用显式audit账本，limit与remaining均为null，逐次保留预约、实际启动和结果；不会因当天或历史累计次数停止。容量、并发／速率、服务条件、持久1＋3 retry、未知对账和真实资料／模型服务授权继续生效。新 audit schema2 与旧有限 schema1 分别创建和核验，不转换历史账本；入口命令和日程绑定见数据规范。旧有限模式的余额与次数门禁不作为新 audit 运行的产品限制。

### 2.6 跨入口预约与有限预算兼容

模型调用适配器在外发前先原子持久预约；下述余额和共享总限只适用于显式有限 `budget` 模式，日常 `audit` 模式仍保留预约和实际启动身份。`process`先完成精确已处理输入预检；可复用旧成功链时直接零调用返回，不占新槽。确需加工时，调用 `BudgetedModelCall.reserve_calls(2)` 一次预约提取与独立Review两个槽，再保存Git started；余额不足时零模型调用、零候选，不能先提取后才发现Review无预算。

预约数与started数分列。提取失败、输出门禁拒绝、Review未调用、超时或结果未知时，原预约仍保留，不自动退额或重放已有started的操作。started在OS启动边界前持久记录，Popen失败仍保守计开始且结果unknown，不证明服务端已收到或计费；参数、提示词及CLI启动前检查失败则started为0。未启动的Review不能伪称已调用，token用量未知也不补零。

手动process沿用调用者固定的operation ID，不从每次随机run派生新预算身份；每日入口从固定run ID派生预算操作，在模型调用前将run及预算绑定持久保存到attempt_reserved，重启后按该事件恢复。若预约后、事件落盘前中断，已预约槽保守保留，不自动返还。ask按实际问题调用预约单槽；证据构建和读取包prepare／verify不调用模型，不占调用槽。CLI有模型入口及API适配方式见[日常入口](daily-use.md)，同一限定任务共用同一本账本；旧有限日程的当日限制仍按原配置生效，continuous-v1 则按第 5 节推进。

账本的持久位置、任务范围与原消费必须明确绑定。复制临时文件、清空账本、换 operation ID 或创建另一账本不能自动扩展范围或重置既有调用身份；本地存储与人工故障演练不能证明跨机器云持久状态或目标 runner 已接通。

模型已启动后，selector创建、管道设为非阻塞、注册及读取都处于统一清理保护中。selector关闭、进程停止／回收和各管道关闭独立尝试，某项清理失败不能跳过其余动作。已有模型错误或取消按原保护保留；普通timeout若清理未能确认成功，返回`timeout_cleanup_unknown`，不能进入有限timeout自动重试。若读取原本成功而清理出现异常，仍返回安全`cli_cleanup_error`而不返回答案，异常链不带原始输入／输出。OS拒绝清理时不能断言进程已停止；本地清理也不证明服务端取消。原消费保留，适配器本身不自动重发，上层仅按[有限timeout合同](data-conventions.md#bounded-timeout-retry)处理符合条件的提取。预算错误同时区分本次attempted_calls与operation累计started；后续Review启动前失败不得抹去已经启动的提取消费。具体故障注入与检查方法见[场景与验收](scenarios-and-acceptance.md)。

### 2.7 人工触发、分解审核与显式返工

1. 明确收集范围并通过 capture 保存原文后，编排调用 `production prepare`／`run`，自动提取分类、独立审核与发布。是否允许使用旧候选／正式知识须在计划中明确，不能隐式带入。
2. 新队列默认 `review_strategy=auto`。固定 candidate 后，计算普通 Review 完整请求的实际 256000 字节边界；容量内走单次审核，超限或显式 `decomposed-v1` 时构造 object → coverage → global 计划。各节点保留完整目标正文与必要原文前提，topic 带精确 record 成员版本，global 带完整最终正文、关系与前序证书。
3. 审核前保存 reservation，调用后保存实际 result；已完成节点核验后零调用复用，安全未启动失败最多 4 次总 attempt，unknown 先对账。重开同 queue 只续缺失阶段；必要语义单元超容量保留 `blocked_capacity`，不截正文、旧关系或跳过全局审核。
4. `verify_decomposed_review` 重建每个请求与 proof 闭包，全部必需节点 passed 才进入发布门禁。依赖组一次写正式 record／topic；随后检查当前目录、引用、只读问答、读取包、重复 worker 零调用及 Git 恢复。本地发布与远程确认分别记录。

显式返工通过 `process --revision-from-run` 传入完整核定的 `needs_revision` 链，固定旧候选、真实 Review、proof 与来源。模型投影保留完整旧对象和退回意见，去除已作为当前输入提供的重复原文。反馈不成为事实，结论仍须引用原文；仅改生成时间不能构成实质修订。质量映射修复本身可以构成实质返工，也须通过正常调用、校验与独立审核，不能手改已保存的模型输出。

历史 0.9 未启动预约的显式恢复由 `resume-unstarted-revision` 核对操作者证明、原预约摘要、原 run／operation 及外置账本实际字节，只有 calls 为空才续接；模型前保持原 Git HEAD，不创建新预约。候选保存 `revision_resume`、原预约与完成的 extract 审计，后续发布及恢复核 Git 证明。未知、已启动或已完成均拒绝该入口，新质量链的限制见上表。

### 2.8 持久 production 协调

`production prepare` 保存不可换义的 queue ID、完整 plan 与 context。`status` 只读阶段、coverage、已完成批次、发布和 `next_action`；`run` 要求 audit 账本，CLI 默认强制环境绑定及账本位于库外 `state_root`，`--local-only` 仅用于隔离。`max_steps` 控制一次推进粒度，不是每日模型次数上限。worker 使用 checkout 级 OS flock，与 Git 事务锁分开；崩溃后 OS 释放锁，持久 attempt 与 unknown 仍须对账，不能删锁文件重置。

单批提取后完整 Review；多批先 draft 提取／Review，汇总全部必要片段和 `prior_candidates`，再对最终对象重新提取并独立 Review，消费 group proof 后一致发布。`draft_only` 禁止单独发布。

另有显式 `publish_final_group` 路径可提交同一完整 plan 的多个已审核 final run：每个均非 draft，来源／版本／控制绑定匹配，合计完整覆盖计划，同路径对象和操作无冲突，每篇 Wiki 的精确 record 依赖已在其所属候选的独立 Review 中共同受审。group 只核验并原子写入这些原样受审对象与 proof，不新增模型调用、合成正文或跨批关系，也不新增一次跨运行模型语义审核。需要改变正文或增加跨批关系时，仍须新候选及独立审核；不能凭组装自动证明全部跨来源事实一致。这不是默认 production 自动选择的优化。

attempt 身份按片段、phase 与 related identity 全局持久；显式清理重建还绑定有效回执。同一身份换队列、规则或日期不重置首次＋3 次上限；相同完整 context 的 passed 核验复用。只有能确定 Review 未启动的错误才可复用 extract 单独续审，`needs_revision` 必须实质修订后重新审核。

日程发现前保存 `production_discovered` 范围快照，核获准旧 manifest 并选择当前依赖；answered 事项固定原问题、原运行、精确来源／对象与有效答复，形成稳定 work／queue。各批携相同 `continuation_context` 与完整必要答复，简短回复必须结合原问题；原问题不成为事实。必要来源并集或完整请求超容量时保存 `blocked_capacity`，不能删上下文硬跑。

每次 extract／Review 的 `before_call` 在实际启动审计前检查暂停、范围、答复身份和 required 远程状态；`before_publish` 在 group CAS 前重验，`after_checkpoint` 在持久队列阶段后同步读回。本地 `published` 或空回调不证明远程完成。

`needs_confirmation` 先保存 Review、run pending 与 `pending_registration` marker，再 CAS 写统一 registry 和 registered marker。容量、I/O 或 CAS 失败返回 `registration_required`，保留已发生的审核与审计；重开 worker 或 `repair-registration` 重验绑定后零模型补登记，重复修复不建新事项。后继 record 真正引用有效答复且发布晚于关联提交时才 resolve；required 同步及独立远程读回后保存 `production_finished` 与 tick 结束。待确认、登记待补、unknown、容量阻塞和空批分别报告。

### 2.9 分片规划与最终发布消费

plan 固定完整显式来源／回执、版本、控制和容量。表格与同层 Markdown sections 保留表头、单位及共享前提，普通 prose／对话／JSON／代码不强行按字节切断。project 返回全局行号 `source_views` 与可核 `batch_context`；coverage 同时说明本地核验和实际提供范围，缺片段不等于原文不存在。merge 合并父 batch 与重叠 context，超容量或缺必要依赖整体 blocked。

最终 group publish 消费精确 stage／Review 证明与完整 plan ranges；同一 topic 与全部精确 record 版本共同受审。同路径不同字节、旧值或控制变化、缺关系、未经审核的新合并均拒绝。formal、manifest 与稳定 marker 同 CAS 提交，receipt 另补；`published_local`／remote `not_requested` 不能充当远程回执，规划／投影成功也不等于已发布。

### 2.10 只读与执行入口

以下参数为现有接口摘要，文件中的来源和 ID 均由调用者在私有环境提供；未指定真实资料时不运行模型。

| 入口 | 主要约束 |
| --- | --- |
| `environment bind --repo --input`；`environment verify --repo [--require-binding] [--remote]` | 固定及核对资料根、分支、远程和持久状态绑定；该绑定本身不核程序SHA，实际程序／规则另由运行记录和双仓代码／CI核对验证 |
| `runner probe --repo --input [--receipts <template.json>]` | 平台未验证时退出 4，配置有效不等于真实触发 |
| `promotion prepare --repo --base --dev-commit`；`promotion check --repo --base --candidate [--dev-commit]` | 历史混合仓库兼容工具：prepare 仅创建未挂 ref 的双父 candidate，工程取 dev、生产保护路径取 base，不改工作区／分支或 push；check 只读核验。纯资料私库不再用它同步工程，见[发布门禁](release-and-rollback.md#环境绑定与历史工程提升工具) |
| `budget create --mode budget\|audit` | 默认 budget；audit 省略 `--limit`，`--initial-used` 和 `--authorization-ref` 仍显式提供。引用和实际账本仅留私有环境 |
| `context discover` | 显式 `--source`、`--allow-source`，可重复；可选 `--allow-run`、`--object-id`／`--topic-id`、`--max-sources`／`--max-runs` |
| `context continuations plan`／`execute --repo --input <job-template.json> --budget-ledger` | 计划按同一 allow／capacity 规划；执行 operation 固定为 job_id，显式不同 ID 拒绝，计划不调用模型 |
| current evidence／ask、knowledge、`reading-package prepare` | `--plan` 与可重复 `--batch` 成对使用，多 batch 经 merge 投影；显式 source 范围和完整读取预算保留 |
| `production prepare --repo --id --source` | source 可重复；可附 `--allowed-source`（默认同 source）、`--allowed-run`、`--max-input-bytes 128000`、`--payload-overhead 16000` |
| `production status --repo --id`；`production run --repo --id --max-steps 8 --budget-ledger --operation-id` | status 只读；run 默认要求 binding，`--local-only` 仅隔离 |
| `batch plan --repo --source --max-input-bytes` | 可附 `--overhead-bytes`／`--max-sources`／`--expected-head`／`--purpose current\|history` |
| `batch project --repo --plan --batch-id --source` | source 表示实际允许读取范围，可重复；可附 `--budget-bytes`／`--fragment`。规划／投影返回 JSON，零模型，不自动写 Git 或发布片段 |

### 2.11 范围内的 context 与答复续接

`discover_context` 在允许来源／运行内固定必要元数据与 coverage，真实 process 再核完整来源、旧发布与 Review；`metadata_only` 不能直接成为受审知识。必要来源未获准时只列缺口，标题近似不等于同一对象。`plan_answered_pending` 只规划已答复事项，`execute_continuation` 沿原事项调用受控 process；passed 后才发布和核验，全部有效答复进入正式 record 后 resolve 并读回权威表。失败保留当前阶段，不自动把 answered 改 resolved，也不接受口头回调代替核验。

## 3. 按需问答：先组织证据，再生成答案

问答由用户问题触发，独立于每日整理。第一版使用目录、元数据、普通引用和文本查找；不用专门索引也必须能检查相关原文与更新。

| 步骤 | 实际处理 | 用户可见结果 |
| --- | --- | --- |
| 确定问题范围 | 识别问题涉及的人、主题、时间，以及问的是当前情况还是历史 | 关键范围不明确且影响答案时，说明需要补充什么 |
| 读取候选证据 | 查主题页、正式记录与其原文；同时查相关尚未加工的新资料 | Wiki 帮助找到主题，主要结论回到具体来源位置和版本 |
| 检查有效性 | 检查适用时间、来源身份、审核状态、未处理纠正、撤回与删除控制 | 不按最后写入时间机械选择“当前”，不让旧错误从原文路径重新成为有效事实 |
| 形成证据集合 | 整理支持与反对依据、当前与历史关系，以及缺失材料 | 资料不足或冲突未解决时保留缺口和分歧，不拼凑确定结论 |
| 组织回答 | 模型依据上述证据回答，逐项给出来源、必要时间和状态 | 使用当天原文时标明尚未加工或审核，并说明它与旧知识的差异 |

回答过程不修改正式知识，也不自动把答案保存成新的独立证据。用户随后明确提交补充、纠正或决定时，再走采集路径。新一轮会话应从获准的持久文件取得依据，不能依靠前一轮对话记忆通过验收。

### 3.1 当前读取与限定迁移包

`evidence --current`／`ask --current` 在显式来源／运行范围内核对现行控制、已审正文和新原文。先排除删除或已纠正的旧当前依据，再读取允许内容；替代来源不在许可范围时，只记录缺失路径，不顺手读取。输出 coverage 中实际已读、排除、未读与缺少更新范围；预算不够时说明当前信息未查全，不能凭部分成功断言“已经最新”。显式历史模式保留变化与纠错关系，也不能恢复被普通删除控制排除的正文。

同 ID 未选新版本只读元数据／回执并按 Git 祖先关系报告缺口，不读正文。未来 supersedes 在生效前保留旧来源；仅选未来新来源时仍列当前旧来源缺口。时间未知不裁决，current_complete 保持 false。历史 Review 可作为新发布的核验依据，但当前 payload 不重新带入已抑制的旧正文。

`reading-package prepare` 只把同一固定当前证据、问题、程序／规则／schema／数据版本与摘要写到仓库外的限定文件包；回答规则独立用 `answer_rules_sha256` 绑定，加工规则与领域策略构成 `rules_version`，程序文件变化反映在 `program_sha256`。包中不含第一模型答案、标准答案或验收预期，不调用第二模型。`reading-package verify` 检查包的结构及可信预期摘要。准备／核验成功不等于第二模型已经读取；仍须获准的另一模型真正读取相同包、独立回答，再核对实际答案和引用。该限定包保持原接口及历史格式。

### 选择正式记录或主题读取

`evidence --current`／`ask --current` 可重复提供 `--object memory/records/<人工记录占位>.md` 或 `--object memory/topics/<人工主题占位>.md`；这些是路径模板。显式对象必须在所选发布链中合法、当前且未受控制抑制。topic 自动带上精确 record 依赖，再纳入必要来源行；不存在、受控、缺来源或依赖不全时拒绝且不调用模型，不能悄悄缩成部分对象。

`--compact-sources` 只删除与 `lines` 重复的原文 `body`，全局行号、实际原文和引用能力保留。完整选择仍计入字节预算，超限明确失败。未指定这些选项保留原接口；选齐依赖与已读全部资料分别报告，选定知识不能冒充全库覆盖。日常命令见[日常使用](daily-use.md#selected-knowledge-reading)。

### 3.2 限定 pending 进入问答

三个证据构建入口在同一证据HEAD解析并筛选统一registry，三个ask及读取包共用投影。只有事项全部source／reply路径已选择、原文实际已纳入证据且控制允许，才提供question与完整状态；部分范围只给不含问题、对象和run内容的缺口，无交集省略，delete只给排除计数。完整registry可能在本地解析，进入模型的上下文仍受显式范围约束，不将去重key发给模型。

resolved只在显式选定解决run后核固定历史发布、全部有效回复的正式record引用及实际关联祖先；未选不自动读取，已选却核验失败则拒绝问答。verified_history只说明登记的历史发布和答复关联链，不独立证明问题语义完全解决或当前仍适用；answered不代表知识已完成，事项文本不作为新事实引用。投影、缺口与limitations全计入完整字节预算，超限整体拒绝；前后证据核对同时覆盖pending，不写registry／正式知识。字段与状态见[数据契约](data-conventions.md#问答中的限定待确认上下文dev6)。

### 3.3 内联编号与语义核验分开（answer v6）

问答共同检查JSON结构、所选来源ID／版本、实际行号，以及按citations数组的一基索引绑定的正文编号。支持相邻编号、组和升序范围，规范化Unicode数字及全角括号；0／负数／前导0／越界或畸形数字编号以invalid_inline_citation拒绝。先在原ASCII文本识别单行闭合代码、合法闭合fence与完整行内link／image，再规范化其余文本；显式转义方括号保留字面含义，未闭合／非法fence、全角backtick、reference-style标签和普通引语不能用于绕过检查。此识别不是完整Markdown解析器。没有正文编号保持兼容，不强制所有citation被使用。

编号可定位不等于每句都有充分语义支持，独立事实与引用核验仍需要。问答输出因编号失败被拒，也保留已发生的一次调用／预算，不自动修正文、不自动重试、不写正式知识；失败回执与原始结果维持历史，不用人工修正文冒充模型原答。完整语法见[数据规范](data-conventions.md#回答内联引用编号answer-v6)。

### 只读领域目录（knowledge）

build_domain_catalog(repo, source_paths, run_ids=None, *, domain=None, max_bytes=192000)要求显式source选集，可显式提供多个已发布run；knowledge CLI以--domain筛选health／career／learning，默认JSON，也支持Markdown。它只复用build_current_evidence的版本、controls、coverage和预算边界；不扫描全库、不自动发现run。目录条目只含id、title、path、content_version、sha256、publication_runs，不复制正文或引文。

record按所选受审标签及明确旧别名归类，不按标题、正文或数值猜测；只有“数据质量”标签不能推成health。topic不新增domains字段，只以精确record ID与content_version引用的领域并集派生。未归类record、topic缺成员／版本不符／成员未归类或无refs均列classification.gaps；已有已知领域兼带旧细标签只列unmapped_labels，不单独形成缺口。unclassified单列选集内未归类项；--domain只过滤显示，不删其他分类缺口或coverage。catalog_complete_within_selected_scope仅在原current_complete且没有分类缺口时为true，不代表全库最新。JSON完整格式化结果及Markdown分别接受max_bytes边界校验，超限拒绝evidence_limit。

固定的三领域README继续充当入口，动态视图在读取时计算，不在publisher中新增写集，不改README或受审对象，也不调用模型。仅在同一明示选集内说明结果及coverage，不将局部列表称为全库最新目录。

## 4. 纠正、待确认与删除

### 4.1 补充与纠正

1. 将用户新表达保存为新来源，关联被补充或纠正的资料，保留原话和已知时间。
2. 区分真实变化与错误纠正：变化保留过去在当时成立的情况；纠错指出旧内容有误。无法判断时进入待确认分支，不按新旧消息顺序擅自裁决。
3. 遍历来源引用和对象关系，找出直接受影响的记录与 Wiki，标记待复查并生成候选修改；后续分析和计划存在时也应保留受影响提示。第一版先覆盖直接依赖，不声称已经支持大规模依赖重算。
4. 按相同校验、Review 和发布流程更新。新审核完成前，即时问答应读取已经保存的纠正依据，说明新旧差异与未审核状态，不继续无条件沿用旧结论。

现实有效时间、系统获知时间及当前信息选择规则以数据规范为准。未知时间保持未知，不能用采集时间代替发生时间。

先以 `control plan`／`control apply` 登记 `corrects` 或 `supersedes`，绑定真实旧来源、替代来源及旧正式版本，避免整理前旧错误继续沿原文路径成为当前事实。该控制不自动改正文；再选择必要的新旧来源和旧发布运行，走 2.4 的增量审核更新。新的受审版本与被纠正旧版本分别核验，不把“最晚文件”直接当现实有效时间。

### 4.2 统一待确认清单

登记问题时保留相关证据、影响对象和具体待判断内容；同一事项继续更新，不因重跑重复生成。缺失信息不影响理解或发布时，可以保留未知，无须为每个空值询问用户。

用户回复先保存为来源，再更新对应事项并重新加工受影响内容。只有问题已得到处理且结果有依据时，才关闭或更新事项；用户确认个人意图不自动等于核实外部事实。等待回复期间，独立通过项继续发布，批次可以结束。

统一表位于 `memory/pending.md`；`pending list` 只读，`register` 按问题和来源／对象引用去重。调用者先 capture 文本答复，再用 `answer` 关联来源与实际 HEAD 锚点，只将状态改为 `answered`；不能把只有链接的回复当实质答案。后续新运行必须让全部有效回复的精确版本进入核验正式 record 的来源引用，仅作为模型输入或 Wiki 引用不够；`resolve` 核验该发布晚于 registry 中真实答复关联提交且不同于问题原运行，才记录 `resolved`。跨运行遇到同一问题合并引用，不能因 run ID 不同复制事项。

调度对账只在来源授权及原candidate／needs_confirmation Review／registry的精确绑定通过后，核对resolved指向的固定历史发布与真实答复关联，派生plan item为resolved并列confirmation_resolutions。原attempt_result仍为awaiting_confirmation，不自动解决事项、不为原run补造发布、不自动扩大后续related范围；run／status只将核验通过项移出remaining，open或answered仍等待。字段以[数据规范](data-conventions.md#每日编排事件与预算)为准。

### 4.3 普通删除与本地修改

普通删除依照用户明确范围移除当前版本中的内容，同时维护受影响 Wiki 和引用，保留必要的禁止自动重建控制。下一轮加工和即时读取都检查这些控制，不能从旧来源重新生成或引用被排除的内容。控制记录只保留实施所需的信息，避免复制已要求删除的正文。

普通删除不清除 Git 历史；连历史清除属于另行确定范围的流程。附件当前只保存链接，删除链接不等于删除附件本体。

删除先运行只读计划，列出被删来源 ID 的所有版本抑制范围、引用它的正式记录以及主题直接依赖。输入未明确覆盖所需来源／对象时，apply 拒绝且不写入；调用者核对实际用户范围后重新准备输入，不由程序自动追加删除。通过后控制与所列当前正式文件删除同一次提交。原文历史未擦除，但采集、加工与只读入口都检查删除控制，不能从旧快照恢复当前事实。

私有远程仓库与本地副本用于文本和链接记录的同步、回溯及恢复。首版建议统一发布入口；是否开放本地作为另一写入端仍待确定。手工变更不能沿用旧审核标记，必须重新进入版本核对与审核流程。

## 5. 每日调度：推进同一条加工路径

已确定使用GPT云端定时任务，按 `Asia/Shanghai` 每日22:00运行；当前任务尚未创建／启用，目标runner能力仍待实测。定时任务编排范围横跨知识处理、公共工程和持久存储三层；采集保存与即时问答在范围之外。

一次任务按以下顺序执行：

1. 读取获准的持久资料、规则、既往批次与待确认/删除控制。不能依赖上一次执行留下的临时目录或聊天上下文。
2. 选取新增、内容变化、此前未完成、允许重试及历史补录的输入；按来源和规则版本判断是否已处理。不能只按“事件发生在昨天”筛选，也不能只靠采集日期忽略补录。
3. 在单批容量、执行窗口及持久返工次数约束内，逐项推进第 2 节的加工、审核与发布步骤；显式有限模式同时核预算。达到当次推进边界保存进度，后续续跑；日常 audit 不以每日或累计调用数停工。
4. 汇总已发布、待确认、失败和剩余范围，保存可追踪回执；有待确认内容不等于整个任务失败，也不阻止本次结束。

具体模型、访问资料范围、执行时刻、批次上限、重试预算和本地同步方式，在接入时配置并留下版本。Git 私有存储不代表任意模型获准读取全部资料。

启用前必须在实际定时执行环境验证：读取指定资料与规则、调用加工和独立 Review、完成确定性校验、写回 Git、读取回执、在后续任务续跑。若某一步无法执行，就记录该限制并调整接入方案；创建了任务或生成了候选都不算每日链路已打通。验收方法见[每日运行与恢复验证](scenarios-and-acceptance.md#每日运行与恢复验证)。

### 5.1 状态查询、暂停与重新启用

以下为 M1 接入时要验证的运行规则，现有程序已实现对应编排接口，真实 runner 和两轮触发尚未验证。首版复用调度记录、Git 回执与状态查询，不增加独立监控服务。

**发现未触发。** 用户查询“昨天整理了吗”时，对照获准调度配置中的预期时间、平台实际触发记录、最近成功完成时间、失败与剩余输入。超过配置的容忍窗口却没有触发记录时，说明未按预期触发；已触发但无结束结果时，说明未完成或结果待核对。若平台记录不可读，只能说无法确认是否触发，不能把没有回执推定为成功或无资料。正常触发但无新增资料时，也保存空批结果。容忍窗口、运行超时和批次预算在实际环境试跑后记录到运行配置。

首版通过用户主动状态查询或下一次成功触发核对缺失轮次；没有独立检查器时，不承诺在调度器失效当刻主动告警。

**暂停与恢复。** 发现审核门禁失效、非预期正式写入或重复发布等关键问题时，关闭后续自动触发并阻止新的正式发布，保存当前进度。暂停动作需在正式提交入口检查；已经发送、结果未知的 Git 请求先核对远程结果，不声称能撤销正在提交的事务。按已成功、未提交和结果待核对区分在途批次。

恢复时修复问题，或使用与当前数据格式兼容的已验证程序及规则版本；先在隔离样例复验，再处理获准的未完成项并重新启用。程序版本恢复不能通过整库回退覆盖期间新增原文，也不能撤销删除控制。若已有错误知识发布，登记影响范围，按纠正流程更新相关记录与 Wiki；复查前问答标明相关结论待核对。原文保存与只读查询可在各自能力正常时继续，手动加工同样遵守发布暂停状态。

M1 验收包含“启用 → 暂停 → 核对在途结果 → 修复或恢复兼容版本 → 复验 → 重新启用”。定时链路的程序、规则或配置改变后，重新记录版本并复验受影响路径；影响两轮每日验收可比性的变更，须在修复版本重新取得两次真实触发证据。

### 5.2 编排入口与有限模式兼容

`schedule configure` 固定时刻、获准来源 ID／路径、相关旧运行和执行策略；旧有限模式同时固定预算。`plan` 只读显示新增、变化、历史补录、未完成与超上限范围。`run` 需要显式 trigger；编排先持久保存 tick 与 attempt 预留，固定 run ID 和来源，预占加工／Review 两个调用槽，再推进同一增量路径。旧有限模式按实际事件 at 的北京时间自然日累计条数／调用预算，manual／test 触发换 ID 不重置预算；达到上限保存剩余。日常 audit 模式保留调用事件且不设累计次数停机门槛，无用量回执保持未知。结果未知的预留即使 lease 到期也先核对已存 started／候选／审核／清单，不自动再调用一次。

状态与进度保存在 append-only schedule events。`status` 无平台观测时只能返回 `trigger_unknown`；平台实际查询成功且覆盖应触发窗口、列表为空才是 `no_trigger`。正常空批仍写结果，已开始但没完成不能当成功。`pause` 通过发布门禁阻止手动及编排的新正式写入，但不谎称关闭外部任务；`resume` 需复验引用并确认没有未核对的在途 attempt。

`remote_mode` 默认 required，事件与发布写入应通过现有认证推送并独立 fetch／读回，空回调不能冒充远程确认；显式 `local_only` 仅供隔离演练。暂停后的已审未发布 attempt 需停止执行、核对无正式提交并提供复验引用，再显式 `schedule abandon`；预算保留，旧 context 不再放行，新尝试重审。外部 trigger 声明为 scheduled 仍只是输入标签，不能证明目标 GPT runner 已执行。实际平台定时能力、限定数据权限和持久恢复需按环境核对独立实测，不能使用本机 cron、CI 或连续手动 `run` 代替。

失去输出的未知调用默认保留在途状态；只有暂停、租约过期、远程核对无发布，并有明确已停止执行的操作员声明和记录时，才能用 `abandon --execution-stopped` 终结为不确定结果。原预算不退、未知用量不补零，不撤销外部请求；新尝试仍受总预算、尝试次数及新 Review 限制。规划先通过限定来源的文件头识别 `link_only`，保留 `waiting_text`，不为已知无正文项预留调用而阻塞其他来源。

### 5.3 连续发现与排空队列

新日程显式使用 `execution_policy=continuous-v1`、`processing_engine=production-v1` 并绑定 audit 账本，`max_calls=null`、`max_attempts=4`。单 slice 的 `max_items` 和一次 drain 的 `max_slices`／`window_seconds` 只控制推进粒度。根触发发现并固定允许的 `source_paths`，后继 slice 与 continuation 从根事件恢复该范围，不在续跑中隐式加入新版本；来源版本的持久 `item_key` 不随规则、日期或 trigger 名称变化。

`schedule drain` 要求 `--repo`、`--id`、`--trigger`、`--budget-ledger`、`--operation-id`，可设 `--max-slices 8`、`--window-seconds 300` 及 `--continuation <continuation-template.json>`。返回 `next_action`、`remaining` 和 continuation；执行窗口结束即持久保存进度。继续令牌不能扩大原来源范围，也不代表平台已安排唤醒。旧有限日程与 0.4／0.5 事件继续按原契约核验，真实 GPT 平台能力与两轮触发单独验收。

`run`与`drain`统一把`reconcile`、`repair_accounting`或`attention_required`归为非零退出码4；结果中的未知、容量阻塞或达到尝试上限因此不能被shell当作成功。正常`continue`、`idle`和等待确认保持各自结果，不能仅以退出码0推断本轮已全部发布。

## 6. 失败恢复：先核对已经发生的结果

处理身份使用数据规范中的来源、内容与规则版本；稳定对象标识用于判断同一知识对象，避免规则升级后复制整份知识。候选、审核、处理进度与失败原因均须持久保存。

| 失败位置 | 恢复前核对 | 恢复动作 |
| --- | --- | --- |
| 原文写入结果不明 | 查远程实际文件、来源版本及已有提交 | 已保存则补回执；未保存才重试，避免重复来源 |
| 加工或校验失败 | 查完整输入、候选是否可用及失败原因 | 从缺失步骤继续或重新加工，不发布半成品 |
| Review 退回或中断 | 查审核结果是否完整且绑定当前候选 | 退回项实质修订后重审；确认尚未启动或完成对账后才沿原身份恢复，unknown先暂停核对，不能把无输出直接当作未调用并重审 |
| 审核后内容或依据变化 | 比较候选、来源、相关旧记录和规则版本 | 重新合并、校验、Review，旧结果不放行新内容 |
| 发布发生并发冲突 | 查最新分支与受影响记录 | 保留其他更新，重新计算本次变更；影响审核依据时重新 Review |
| 提交成功但回执中断 | 查远程提交、发布清单及对象版本 | 关联既有提交，补齐回执，只续跑未完成部分 |
| 等待用户确认 | 查原事项及是否已有新回复 | 无回复则保留待确认；有回复则作为新来源续接，不重复建事项 |
| 达到当次推进边界或显式有限预算 | 查已完成范围与剩余输入 | 保存可恢复进度后结束，下一次按版本和处理记录继续 |

恢复不是重新运行整个批次。只有不能复用的阶段才重做；状态不清时先核实远程结果，不能直接假定写入失败。有限重试仍无法完成时，向用户说明实际保存了什么、哪些尚未完成及原因。

## 附录：调研参考与适用边界

| 参考 | 借鉴内容与边界 |
| --- | --- |
| [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | 原始资料、Markdown Wiki、规则文档及摄取、查询、检查流程；它是组织模式，具体 schema 仍需定义 |
| [Wikidata 陈述](https://www.wikidata.org/wiki/Help:Statements)与[排序规则](https://www.wikidata.org/wiki/Help:Ranking) | 陈述、限定条件、来源和使用优先级；有来源不等于真实，正确历史信息与错误信息应区分 |
| [W3C PROV](https://www.w3.org/TR/prov-o/)与[Web Annotation](https://www.w3.org/TR/annotation-model/) | 资料、加工活动、责任归属、派生关系和原文片段定位；溯源本身不完成事实核验 |
| [SHACL](https://www.w3.org/TR/shacl/)与[JSON Schema](https://json-schema.org/understanding-json-schema/reference/object) | 类型、必填字段和结构约束；SHACL 面向 RDF，Markdown 元数据可考虑解析后用 JSON Schema 校验 |
| [Zep 时间事实](https://help.getzep.com/facts)与[Graphiti](https://github.com/getzep/graphiti) | 区分现实有效时间与系统获知时间、保留来源和历史；时间字段不证明事实为真，也不保证所有派生计划自动更新 |
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval)与[Ragas 召回](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)、[回答忠实性](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) | 借鉴记忆更新、跨会话与时间推理、信息不足时不作答，以及证据召回和回答支持度；需要建立自己的中文个人资料验收集 |
| [Google Cloud Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing) | 借鉴 Markdown、YAML 元数据、概念文件和普通链接；v0.1 是初始开放规范，不替代本项目的对象与发布契约 |
| [Basic Memory 知识格式](https://docs.basicmemory.com/concepts/knowledge-format) | 借鉴笔记元数据、事实条目、稳定标识和关系表达；不要求部署其服务 |
| [ChatGPT Automations](https://learn.chatgpt.com/docs/automations) | 云端定时任务能力参考；具体资料访问和 Git 写回必须在本项目验证 |

这些参考用于规则和文件格式设计，未整体选用任何一套框架或独立记忆服务。本项目的稳定 ID、审核契约和目录是自己的设计，不宣称与所有参考格式完全兼容；第一版不因此引入图数据库、向量库或专门索引。

## 图源与维护

[总体方案](overall-plan.md#信息如何流动)与[技术方案](technical-plan.md#2-技术架构)的 Mermaid 描述通用信息流与架构；逐层图示的编辑方式见[图源与维护说明](diagrams/README.md)。模型环节与任务范围应随实际流程同步维护。

字段、枚举、模板和目录变更统一更新数据规范；处理步骤更新本文；样例和检查方法更新场景验收；阶段完成情况只更新 Roadmap 并附运行证据。后续新增接口、提示词或配置按实际建设结果补充，不能仅因写入文档就标为已实现。

原生模型适配器默认120秒；调用方可显式选择最长1800秒，以容纳较大的结构化期间总结。上限外或非法值在CLI启动前拒绝，适配器实际超时仍记为未知并返回错误，本身不自动重发。上层production仅对[有限timeout合同](data-conventions.md#bounded-timeout-retry)符合条件的提取最多补3次，原unknown与消费保留，不能把延长等候当作免费重试。
