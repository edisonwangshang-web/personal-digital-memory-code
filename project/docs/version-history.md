# 工程版本记录

本页记录可公开的能力沿革。代码版本、规则、schema、准确提交的 CI、资料发布和业务验收分别管理；包版本号存在不能证明已打标签、合并、部署或通过整个阶段。私人资料版本与实跑历史保留在私库，工程文档以本公库为统一维护入口。

## 当前：0.2.0.dev26

本批修复 M1 架构审阅发现的多批推进与反馈接续、读取重复核验、调度退出码及控制容量边界；具体实现和兼容范围以[数据规范](data-conventions.md)、[实现明细](implementation-details.md)和[人工验收](scenarios-and-acceptance.md)为准。准确提交CI、稳定分支交付及真实业务结果分别核对，不把本条版本记录当作已经通过。

合并原文超容量时，`verbatim-draft-finalization-v1`在现有final阶段原样联合完整普通草稿，不新增正文或extract调用，再真正独立分解Review。候选／started／最终Review专用0.14，普通10/8候选及queue／attempt／unit保持0.12，来源0.1、formal0.2和group0.6保持。当前回退不接related、答复或重建上下文；对象或质量口径冲突、必要审核超容量仍阻塞。原样候选需实质修订且仍不能承载时保存意见、零调用返回`assembly_revision_required`，不循环相同联合。

固定10/8草稿及答复续接支持实际反馈返工，精确保留phase、答复、来源片段及相关范围；旧链和人工operator恢复边界保持。读取只复用一次证据构建中完整SHA绑定的Git结果，不缓存当前有效性或proof通过结果，不跨模型前后两次构建，原锁与完整校验保留。调度`run`与`drain`统一需处理退出码；超数量控制写前拒绝且幂等已有控制仍可读。

新生成非assembly质量候选固定`review_request_contract=current-draft-v1`，普通Review仅去掉指定返工／恢复历史，保留当前稿、质量检查及对应证据；持久完整历史仍核验。被退回稿带同marker时，返工extract沿原接口取最新稿、未解决意见和必要证据，保留真实verdict／reasons及未解决checks，完整Review和proof不改；已有分解Review白名单不变。无marker旧投影和请求按原字节，未知marker拒绝；不增加合同、Skill版本、阶段或schema。

默认加工／审核Skill仍为10/8，历史Skill字节保持。answer v8沿用原JSON字段，补通用的逐句陈述与引用行自查；程序结构检查仍不证明语义支持，历史读取包按自带规则和摘要核验。规则升级不重跑已完成提取，不重置有限次数或重发unknown。

工程开发版可按[M1交付核对](m1-delivery-readiness.md)交付具体修复；完整M1仍须真实增量、纠正与待确认、目标定时两轮、目标环境状态恢复及第二模型证据，不建立已经完成的结论。

## 历史：0.2.0.dev25

修复通用 `source_time` 日期核对：接受 ISO 与中文年月日；中文月日只在实际来源引文中可唯一确定同一完整日期时成立。不从当前时间或采集时间补年份，歧义、无真实日期依据及冒充当前系统状态仍拒绝。模型原始返回、正文和旧 Skill 字节保持，不替模型改写时间声明。

增加显式 `quality-completed-extraction-revalidation-v2`，只对同一明确重建 generation 的原生已完成提取、`quality_temporal_mismatch` 且尚无候选／Review 的情况做零 extract 重验。操作者核定固定原始缓存和调用绑定，使用原回执、原来源、完整同轮反馈及当前校验版本，合格候选仍须真实独立 Review。旧 V1 继续仅接 `candidate_too_large`，默认恢复合同不改；候选0.12、guard0.13与调用计数边界保持。原 failure 只有调用摘要时不能倒称已有输出 SHA，原始失败、unknown和模型输出均保留。

V2形成候选后，若真实Review要求修订，production仅对原 `failed`／`quality_temporal_mismatch` 且完整核验V2候选与该Review的同一重建任务采用最新反馈，继续正常返工。旧失败与counter不改，后续预检和有限尝试不跳过；不会把任意failed或unknown当作可续接审核。

auto审核遇到共享编码器明确的容量上限时，转入已有分解审核；每个单元仍须完整证据、正常容量预检及独立审核。其他Skill或编码错误继续阻断，历史请求形态和绑定不变。

本批沿用人工触发与原加工阶段，不新增模型环节。准确 CI、真实重验／审核／发布／读取和私有恢复分别留证；本页不宣称业务通过或 M1 完成。字段与执行见[日期规范](data-conventions.md#skill-quality-contract)、[V2重验](data-conventions.md#quality-completed-recovery-v2)和[实现明细](implementation-details.md#quality-completed-recovery-v2-execution)。

## 历史：0.2.0.dev24

新增显式清理后从原文重建入口。操作者给出稳定意图、来源范围和正式目标，`rebuild-prepare` 在同一提交删除选定正式文件并保存不可变回执；原文、旧候选、审核、控制、调用记录和 Git 历史保留。相同意图重放不再次删除，普通删除保护继续生效。

已有加工、审核与发布接收可选 `rebuild_context`，只有有效清理回执才授权重新创建该范围中的已清路径。首次新输入不含旧 related、revision、merge 或复用结果；退回后仅允许同一回执绑定的本轮新候选及审核反馈返工。继续使用不变的 extract@10／review@8 提取、自动分类和完整独立审核。当前重建限定为一批完整来源，容量不足在预约前阻塞，不拆成缺少必要上下文的发布。

显式回执绑定本次重建工作与审核计数；同一回执跨队列、日期或工程版本仍沿用有限次数，旧 unknown 和旧计数不重置。回执使用既有0.4封套及独立 `explicit-knowledge-rebuild-v1` 契约，普通质量链0.12、Skill字节及无重建参数的历史请求保持。准确 CI、实际原文加工和私有恢复分别取得证据，不据此宣布 M1 完成。操作与字段见[日常使用](daily-use.md#explicit-knowledge-rebuild)及[数据规范](data-conventions.md#explicit-knowledge-rebuild)。

## 历史：0.2.0.dev23

公库成为程序、提示词、Skill、脚本、测试、CI、工作流定义和设计的唯一工程来源；私库仅保存来源、候选、知识及分层、审核、控制、运行数据、私人验证、历史和导航。启动时当前目录及绝对模块路径指向公库，使用 `python -P -B`，`--repo` 指向私库；不再复制或比对第二份核心工程。同名 dev／master 配对、准确公库 HEAD 的 CI、可见性、干净工作区和运行前状态复核继续生效。

`program_commit` 改从实际加载公库的核心工程最近提交取得，私库资料 HEAD 独立绑定。新增量预约将 `engine.md` 与 `started.md` 同提交保存，使用既有0.4封套及独立 `public-engine-v1` 契约，固定公库准确 HEAD、实际程序／规则版本、原预约摘要和私库资料父提交；新质量恢复使用原工程来源证明，旧无证明的运行只读核验原私库历史，不重写旧运行或重放 unknown。

默认 extract@10／review@8、Skill 字节、业务 schema、processing key 和有限重试语义保持；本批只调整工程来源与其审计绑定。旧资料、审核、控制、调用账本及历史不随工程切换迁移。准确 CI、真实加工与恢复结果仍各取实际证据；本记录不预写通过，目标 GPT runner 和两轮真实触发仍待验收。契约见[数据规范](data-conventions.md#公库唯一工程来源的运行版本绑定)，操作见[双仓实际使用](dual-repository-operation.md)。

## 历史：0.2.0.dev22

默认extract@10／review@8、普通质量链schema0.12、原文0.1、正式知识0.2、group0.6及既有提取字段保持。专用`quality-completed-extraction-revalidation-v1`只恢复原生返工提取completed、因candidate_too_large且无stage／Review的已知输出；新证明明确是操作者核定本地缓存，不虚构旧Git或服务端输出摘要。零新extract，原失败和调用历史保留，完整独立Review仍必需。

候选当前计算核心仍≤1 MiB，仅revision_context／revision_revalidation／revision_resume历史使完整包可至4 MiB；其他受管文件1 MiB、模型请求256000字节、事务8 MiB不变。预约前核已知保存下界，最终输出仍完整检查。专用恢复stage0.12与schema0.13 guard原子提交，旧writer不支持guard即拒写；不把普通候选升级为0.13。

先准备新plan，再固定validation_head与原始材料证明。恢复Review只去candidate内四键重复历史／程序provenance，根处完整语义反馈、当前对象、统计、目标及required_checks保留；原始payload／response及完整历史在私库Git受guard绑定，库外缓存按独立摘要核验。真实完整group匹配plan／bundle／候选后，新10/8队列持久采用published，重复零写／零模型，旧失败queue／counter不改。旧9/7的effective-only采用语义保留。

本版不修改任何Skill字节或新增加工阶段；准确CI、真实质量和私库恢复仍各取实际证据，不据包版本宣称通过或M1完成。字段、执行及反例见[数据契约](data-conventions.md#quality-completed-recovery)、[执行步骤](implementation-details.md#quality-completed-recovery-execution)和[验收](scenarios-and-acceptance.md#quality-completed-recovery-acceptance)。

## 历史：0.2.0.dev21

新任务默认 extract@10／review@8，加工族使用 schema0.12。精确复用旧知识时，从已核验发布证明提取仍适用的统计义务，模型显式提交，程序在独立Review前核完整性；不补造声明、不强制使用全部统计。

新质量模型视图仅替换能从同包精确原文重建的逐行完整性SHA，持久完整质量材料和所有原文、目标、统计、候选、实际反馈保留。完整容量预检在新调用预约前完成，最终原生请求仍核256000上限。新生产队列的完整final候选退回后，接现有显式返工接口，保留原审核意见、稳定尝试身份和未知调用；draft／continuation返工仍显式阻断。调用错误仅保存结构化安全分类。

旧9/7请求与schema0.11、旧质量0.10、来源0.1、正式对象0.2和group0.6保持原契约，不自动迁移或重加工。通用规则用于健康、工作、学习；不新增加工阶段。准确CI、真实质量、私库恢复和M1整体验收分别留证，不能由版本号推定完成。

## 历史：0.2.0.dev20

新任务默认 `knowledge-extract@9`／`knowledge-review@7`，支持显式 `reuse_refs`：仅引用本次已提供并验证发布依据的旧对象路径及精确内容版本，程序原样物化后组成完整候选。新建或实质变化仍返回全文；覆盖、统计、时间、分类、依赖和独立审核要求保持，不增加加工阶段。

新合同的运行格式为 `0.11`，旧8/6质量链仍按 `0.10` 读取和恢复；来源 `0.1`、正式知识 `0.2`、group `0.6` 和audit账本不迁移。原始模型JSON与调用摘要保留，不能把旧空输出解释为复用；旧writer须在新格式写入前拒绝。工程CI、真实模型质量和私有资料验收各按准确版本留证，M1整体仍未验收。

## 0.2.0.dev19：知识语境与选择读取

该版默认 `knowledge-extract@8`／`knowledge-review@6`，共享 `knowledge-context-v1`；基础提示规则为 extract v14／review v10／answer v7。新规则强化单篇记录与主题的来源、时间、范围自足表达，关联仅限实际提供的已审核内容，不把同名指标静默合并。

current evidence／ask 新增 `--object` 与 `--compact-sources`：选择正式 record／topic，自动携带精确 record 依赖，只提供必要来源行，避免重复正文。对象选择完整与实际读遍资料分别报告；旧读取和 `reading-package` 默认行为保持。

已核对的公开工程提交 [`8e461422712252ccffefe664466c1ba254fc97cf`](https://github.com/edisonwangshang-web/personal-digital-memory-code/commit/8e461422712252ccffefe664466c1ba254fc97cf) 对应 [CI 40163471251](https://github.com/edisonwangshang-web/personal-digital-memory-code/actions/runs/40163471251)：八任务成功，Python 3.11／3.12 各 1004 项，以及 doctor、源码归档排除、代码包核验通过。该记录只证明准确提交，不能转记为后续文档 HEAD 已通过。

## 历史：双仓库工程副本与操作入口

以下记录早期双副本过渡方案，不是现行操作要求。现行公库唯一工程来源及私库仅存资料的规则见[双仓使用](dual-repository.md)；旧提交与旧运行版本保留原事实。

工程、通用 Skill、人工测试、标准 CI 与产品／技术／工程文档归公开仓库；个人原文、知识、审核、真实验收与完整历史留私库。两个仓库采用独立历史，不公开私有旧提交或标签。

新增只读 [check_dual_repo.py](../scripts/check_dual_repo.py)，核对可见性、准确 CI 和完整核心路径集合／模式／内容；上述公开快照为 71 文件。实际运行继续从私库工程副本加载，公库提交与私库程序提交分别记录，不自动复制／同步，不替代私库不同 SHA 的生产门禁。

新增 [check_local.py](../scripts/check_local.py) 复用双 Python 三分片和基础工程检查，在临时完整副本执行并生成绑定准确提交的报告。本地报告与 GitHub checks 分开。默认 CI 为标准托管 Linux，自托管和 Mac 容器仅保留可选未验收方案；公库未提供专用注册和 Mac 启停工具。

这些工程入口不改变程序包的 `0.2.0.dev19` 版本，也不代表 M1 整体验收完成。当前操作见[日常使用](daily-use.md)、[双仓核对](dual-repository-operation.md)和[公开 CI](public-code-ci.md)。

## 0.2.0.dev18：区分当前处理状态与来源历史

answer v7 明确：已核验的发布与 `knowledge_status` 只能证明相应资料在本系统的处理状态；来源历史自述不能覆盖当前发布证明，也不能据此证明其他系统状态或业务事实。pending 缺席只表示本次未提供已登记事项，不能把每个资料缺口变成用户任务或断言全库无事项。提取／审核 Skill、旧请求和数据 schema 保持。

## 0.2.0.dev17：完整处理清单的生成约束

新增不可变 `knowledge-extract@7`，生成 schema 按实际目标数固定 coverage 数组长度，结构检查补 `maxItems`；完整清单之外仍要求每个目标的实际正文、统计关联、来源与时间依据。审核保持 `knowledge-review@5`，结构齐全不等于语义通过。旧 Skill 请求和 schema 保留。

## 0.2.0.dev16：无损列式请求

新增显式无损列式 v3 编码，复用同字段对象数组的字段名，保留全部值与行序，随后恢复原键字典和共享引用。旧 Skill 仍使用各自固定的原编码，256000 字节请求上限不变；压缩方式不允许删除必要内容或降低独立审核要求。

## 0.2.0.dev15：完整审核容量预检

新增版本固定、可逆的字段字典 v2，并在首个 Review 前对全部静态审核单元完整预检，global 单元按实际证明再检。保留完整原文与规则、旧请求可重建性和未知调用检查。容量不足明确阻塞，不截断材料后发布。

## 0.2.0.dev14：期间内容与精确映射

质量规则逐步强化期间覆盖与主要观测水平，要求实际内容、统计声明、目标标识、完整来源范围与时间映射一致。不能以举例、覆盖声明或部分指标代替本批完整目标；候选不符合要求时拒收，模型原输出保持原样。

## 0.2.0.dev13：Skill 绑定与知识质量门禁

新增固定版本的 `knowledge-extract`、`knowledge-review` 与共享 `knowledge-quality-v1`。程序解析并展开必要依赖和基础提示快照，实际请求绑定候选与 Review；准备确定性资料清单、描述统计并校验覆盖、统计关联、时间限定和逐项审核。新 CLI 任务默认启用，旧任务与底层 API 的原契约保留。

质量候选／Review／发布链、queue／attempt、日程事件与分解 proof 使用 schema `0.10`。来源 `0.1`、正式知识 `0.2`、group wrapper `0.6` 和稳定旧计数不迁移。普通质量返工与分解恢复继承 Skill；两个历史 `0.9` 人工恢复入口不接收新质量任务，缺适用证明时返回 `skill_quality_recovery_attestation_required`。

## 0.2.0.dev12：显式返工与安全复验

增加基于完整 Review 反馈的显式返工、实质变化检查、同预约零启动续接、每对象至多 256 引用和已完成返回的原样重校验。恢复核对原调用完整身份，未知结果不得换记录或 operation 重发。原失败、started 和稳定指针保留。

该历史返工／复验链使用 `0.9`，proof `0.8`、group `0.6`、来源 `0.1` 与正式对象 `0.2` 不迁移。普通 auto 恢复还须把原 completed extract 的 invocation 与完整 payload 摘要精确绑定候选，不能把新操作或空记录当作旧 Review 未启动的证明。新质量链的限制见上节。

## 0.2.0.dev11：分解审核

production 默认 `auto`，在完整请求容量边界按需采用 object／coverage／global 独立审核；发布者消费同一候选的完整证明后才发布。节点保存独立持久调用身份，已完成部分可复验恢复，未知先对账，必要语义仍超容量则阻塞。该批新队列、候选与 proof 为 `0.8`，旧来源和正式知识不迁移。

## 0.2.0.dev10：日程接通持久生产队列

新增 `production-v1` 日程编排，将发现／drain 接到持久分批、最终独立 Review 和多主题 group 发布。answered 续接携原问题与必要真实答复，发布实际采用后核验解决；required 远程模式执行真实条件同步与精确读回。

Review 先持久，登记失败可在核验后零模型补登记。registry 的来源和回复引用分别至多 256，对象引用合计至多 256；完整来源并集仍有边界。新上下文链和相关执行文件为 `0.7`，旧兼容路径与 group wrapper `0.6` 保留。

## 0.2.0.dev9：跨模块接续兼容

通过整体人工流程核对生产、读取、pending、纠正、环境和远程确认接口，修复 group 发布在日程关联与 pending 解决中的兼容。组件回归与整体接线分别验证，不能把单模块通过视为 M1 通过。

## 0.2.0.dev8：连续知识生产与环境隔离

引入 audit 计量与 `continuous-v1`，持久首次加三次重试，语义分批与全局坐标，跨批最终合并／独立审核、多主题原子发布、范围内关联与确认续接、对象级局部纠正、环境绑定和保护生产资料的 promotion 候选检查。新运行 `0.6`，旧 writer 拒写未知结构。

## 0.2.0.dev7：通用三领域分类与动态目录

`three-domains-v1` 在提取与独立 Review 中完成分类，不额外增加分类调用。新建／实质更新只允许三个稳定领域键，可跨类或未分类；精确 reuse 保留旧受审对象。只读 `knowledge` 实时生成限定来源／运行的目录，topic 按精确 record 版本推导领域，同时保留分类缺口和覆盖信息，不改 README 或发布写集。

## 0.2.0.dev6：限定待确认上下文与引用检查

evidence／ask 和读取包统一投影选定范围内的 pending；完整范围才提供问题、状态与回复依据，缺失则返回缺口。回答编号按 citations 一基索引与来源坐标检查，畸形或越界拒绝；该检查不保证逐句语义支持，也不改写旧回答。

## 0.2.0.dev1—dev5 与 M0 基础

| 版本 | 通用工程变化 |
| --- | --- |
| `0.2.0.dev5` | 修复已解决事项的日程对账、模型进程初始化／清理异常和已知累计调用计数保留 |
| `0.2.0.dev4` | 共享持久预算，提取与 Review 原子预约，预算绑定日程事件 `0.5`，历史来源和正式对象保持原 schema |
| `0.2.0.dev3` | 增量审核基线、候选旧记录完整性、稳定引用与精确 reuse 语义修复 |
| `0.2.0.dev2` | 条件更新与复用、去重、纠正／普通删除、统一 pending、当前证据、每日核心与限定读取包；新运行／控制 `0.4` |
| `0.2.0.dev1` | 显式多来源联合加工、独立 Review、create-only 发布与限定问答；多来源运行 `0.3` |
| `0.1.0` 与早期开发版 | 来源采集、Git 回执、schema 兼容拒写、手动提取／Review／发布、正式正文读取、限定问答与代码包；来源 `0.1`、正式对象 `0.2` |

本表不将私库的历史标签、CI 或 PR 当作公开仓库发布记录。版本回退须先核对数据兼容性并保留资料与引用祖先，不能靠整库回退覆盖新增资料或删除控制。阶段状态见 [Roadmap](roadmap.md)，交付规则见[发布与回滚](release-and-rollback.md)。
