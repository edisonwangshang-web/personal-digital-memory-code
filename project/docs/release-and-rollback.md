# 工程迭代、发布与回滚

本批为dev29开发版，沿用Skill11/9、普通quality0.15，新增超时重试证明／门禁0.16，**仅dev开发验证，不合入或推送master**。实现与验证状态见Roadmap；下文稳定交付流程为历史及后续规范，本批不执行。程序、规则、schema及资料版本分别固定，旧合同原证明保持；代码交付不等于部署或完整M1验收。

[执行方案](m0-m1-execution-plan.md) · [数据规范](data-conventions.md) · [场景验收](scenarios-and-acceptance.md) · [Roadmap](roadmap.md) · [双仓操作](dual-repository-operation.md)

## 双仓边界与实际运行

- 公库是项目方案、产品设计、技术文档、工程代码、提示词、通用 Skill、脚本、人工测试、CI、工作流定义及定时／恢复工程的唯一来源；私库仅保存真实原文、候选、知识、审核、控制、运行数据、历史、导航和私有验证。公库使用独立 Git 历史，不导入私库分支、tag 或祖先。
- 公开 CI 证明准确公库代码 HEAD；实际程序直接从公库 checkout 加载，资料读写和环境绑定指向私库。无需给私库资料提交复制一套工程或补公库绿色状态，真实资料审核与生产发布门禁仍单独验证。
- `check_dual_repo.py` 只读检查可见性、同名分支、准确公库 CI、公开工程完整性和私库无活动工程；不复制代码、迁移资料或合并历史。报告留私有位置，受测公库 `code_commit`、实际核心工程 `program_commit` 与私库 `data_head` 分别记录。
- 规则与Skill资产随发布固定。已发布版本内容不可原地修改；规则修复发布新版本。旧受审资料仍只证明其原规则，不能通过换默认Skill冒称已重新加工。

## 分支与合并

`dev`用于所有功能、修复和配套文档开发，`master`保存生产工程基线。每批固定目标、范围、准确提交和验收标准，完成适当回归与独立审查后经PR交付。创建PR明确指定base为master；默认分支、保护规则和合并选项以实际配置为准，不能从文档或旧查询推定已启用。

所有合并使用保留祖先及父边的merge commit。来源回执、候选、Review、proof及发布清单会引用历史SHA，squash／rebase可能破坏引用链；不得以删除历史替代核对。清理分支先核完整祖先关系和独有结果，已发布tag不移动。

PR说明具体行为、当前实际可用能力、测试与阻塞，关联实际CI运行、受测完整提交及结果。代码变化后重跑受影响检查。草稿PR、本地成功、工作流文件存在或产物版本号均不能冒称远程CI通过。合入master后核该实际提交及适用CI，不能把dev或合并前结果自动转给不同SHA。

工作流`Engineering checks`覆盖dev／master推送和base为master的PR。每版Python的完整测试按整模块分三组，分解审核的大型集成模块单独一组，其余排序交替分组；各组先完整发现测试并验证分组互斥且覆盖全部用例，发现错误、重复或空组均失败。Python3.11／3.12共六个测试任务全部成功后，两个`foundation`门禁才继续环境、源码归档及代码包检查。取消、缺失或失败任务不能计为通过；分组不缩小用例范围。完整准确CI另由实际平台记录证明。

## 环境绑定与历史工程提升工具

当前工程只在公库按 dev → master 的 PR 流程交付；私库不再同步工程，dev 与 master 的资料和运行绑定分别保留。以下 promotion prepare／check 是历史混合仓库的兼容工具，不是纯资料私库的日常工程更新步骤，也不授权将开发资料自动发布到生产。

历史混合仓库的 dev 可能含开发资料，不能直接整分支合入生产 master。`promotion prepare --repo --base --dev-commit`固定生产base与开发提交，在本机对象库创建未挂ref的双父candidate，父顺序为`[生产base, dev提交]`：工程树取dev，`memory/`、`fixtures/`、`archive/`、`.pdm/`及任意层的`pdm-environment.json`精确保留base。prepare不改工作区、dev／master引用或push，创建对象不等于交付。

`promotion check`只读检查双父、固定base／dev、保护路径逐字不变，以及非保护工程文件的精确mode／blob与dev父一致；第二父须为local dev或origin/dev祖先。clone局部环境绑定不经工程提升传播，也不代替目标环境验证。

可用临时`promotion/<candidate完整SHA>`交付分支承载已prepare对象，经PR进入master；它只用于交付，交付后删除。`promotion_delivery_check`核交付head命名及prepared证明，并核实际外层merge父顺序为`[base, head]`、tree与prepared一致。base或dev依据变化必须重新prepare，不覆盖外部更新。CI通过、GitHub显示已合并与生产资料保持分别核验。

环境绑定在clone实际Git管理目录内，dev／prod分别固定dev／master、真实Git根、远程身份及库外state_root；linked worktree各有独立绑定，不进Git。生产入口要求正确绑定，`--local-only`仅用于明确隔离演练，不能把本地published／remote_status=not_requested当作远程生产成功。

## 版本与发布门槛

下表保留各格式引入时的能力边界，供兼容与恢复选择；历史版本号不是当前验收结果。当前默认以页首为准。

| 程序／格式阶段 | 能力边界与交付要求 |
| --- | --- |
| `0.1.0.dev1` | 基础采集与工程预发布；人工样例、准确提交、CI和产物检查齐备，真实链及定时能力另验 |
| `0.1.0.dev2`／`.dev3` | Foundation0.1来源上的手动提取、独立Review、受控发布与证据读取；CLI回执修复另用准确修复提交复验，保留原失败 |
| `0.1.0` | M0手动闭环；固定真实来源、受审知识、三题独立问答、引用、只读与私有恢复证据，不能只凭代码版本宣称通过 |
| `0.2.0.dev1`／运行0.3 | 显式1—8来源、完整来源／回执绑定、链接不造正文、create-only与限定问答；来源0.1／formal0.2保持，旧M0拒写0.3 |
| `0.2.0.dev2`／0.4 | 条件更新／reuse、processed、来源控制、统一pending、current证据及日程；旧程序拒写未知结构，真实业务另验 |
| `0.2.0.dev3` | 完整旧记录候选要求、增量审核基线及已知失败回执修复；不覆盖旧候选／审核，按反例复验 |
| `0.2.0.dev4`／事件0.5 | 共享有限账本schema1与预算绑定日程；旧0.4事件保留，不能重建账本或重置消费 |
| `0.2.0.dev5`—`.dev7` | resolved只读对账、异常清理、累计启动计数、限定pending／引用编号、三领域分类及动态目录；持久schema不因这些功能自动迁移 |
| `0.2.0.dev8`／0.6 | audit schema2、持久1＋3 retry、分批与group、局部控制、continuous、片段读取、绑定与promotion；来源0.1／formal0.2保持 |
| `0.2.0.dev10`／0.7 | production-v1日程总编排、三个执行门禁、Review先持久和零模型登记修复、扩大registry；group wrapper仍0.6 |
| `0.2.0.dev11`／0.8 | auto与object／coverage／global分解Review实际proof消费及单元恢复；必要完整语义仍超容量则blocked_capacity |
| `0.2.0.dev12`／0.9 | 完整实际Review反馈返工、实质变化、同预约零启动接续与已完成提取显式复验；proof0.8／group0.6保持，旧writer拒写新key／格式 |
| `0.2.0.dev13`—`.dev19`／0.10 | 显式Skill、实际规则快照、覆盖／统计／时态和逐项质量、无损wire及知识自足／按对象读取；来源0.1／formal0.2／group0.6保持，历史请求按原字节重建 |
| `0.2.0.dev26`／原样final0.14 | 保持普通10/8质量链0.12；合并输入超容量时，限定范围内原样联合完整草稿并重新独立分解审核，明确无新extract调用；草稿／答复反馈、读取I/O、退出码和控制容量反例分别核验，不代替真实M1验收 |
| `0.2.0` | M1整体通过：多来源、质量、纠正、pending、两轮真实定时触发、恢复与现有受控读取均有实际证据；第二模型实际问答和后续Agent索引服务不作为门槛，当前不能建立此完成结论 |

发布前固定以下四类依据，并通过准确提交CI、代码包检查、独立审查及适用场景。满足对应门槛后才创建不可变tag和发布记录；修复使用新版本。

| 版本依据 | 必须固定什么 |
| --- | --- |
| 程序 | 公库包版本、发布 commit／tag、准确 HEAD 的 CI 与产物摘要；实际核心工程 program_commit 来自公库，与私库资料 HEAD 分别核对 |
| 规则 | 实际提取、审核、回答规则、领域策略与不可变Skill及全部必要依赖 |
| schema | 实际可读取／写入的结构版本与恢复语义，是否需迁移及拒写边界 |
| 数据 | 实际验收输入／输出提交、来源及对象版本；真实证据只存私库 |

代码产物采用白名单，只含必要程序、规则和运行说明；不打包`memory/`、`archive/`、`fixtures/`、私有验收、凭据或真实个人资料，也不以整库归档作为代码包。GitHub源码归档和白名单代码包是两个入口，须各自检查排除边界。`.gitattributes`的export-ignore只影响归档，不删除Git历史；私有资料恢复使用私有clone，过滤源码归档不能替代。

## 暂停、回退与恢复

1. 发布门禁失效、非预期写入或关键回归时，暂停后续自动触发及正式发布，固定程序、规则、schema和数据提交。在途模型或Git结果未知先核实际账本和远程，不假设失败或撤销。
2. 核上一已验证程序／规则对当前格式和恢复语义的实际兼容性。兼容时在公库的独立工程 checkout 选择该版本，以 `--repo` 继续使用当前获准私库资料；不兼容就保持暂停，修复或设计可验证迁移。
3. 回退程序不回退整库个人资料。保留新增原文、回执、纠正／删除控制、pending、registry、started、revision指针、queue／work／job、日程事件、全部unit plan／reservation／result／counter及祖先、audit和旧有限账本；不删proof、改schema、重置counter或整库reset绕门禁。
4. 已完成模型节点只读重验后零调用续接。Review已完成但登记失败只补登记；已published而远程未确认只补同步／读回；unknown先对账，不换operation、run、queue、规则或池重发。
5. 在隔离样例复验，再对限定未完成工作检查读回、版本门禁和幂等发布；通过后恢复触发，记录暂停原因、恢复版本与实际结果，保留原失败和发布记录。

从已核对的公库目录和模块路径执行`doctor`的schema／writable探测，并在每次实际写入前执行格式门禁；本批新增普通quality0.15，实际支持以`contracts.SUPPORTED_SCHEMA_VERSIONS`为准。探测不证明语义或迁移正确，未知、不支持、损坏或不可访问均拒写；不改header或删除证明绕门禁。

格式兼容不代表恢复语义等价。例如旧程序可能认识0.4却缺共享预算门禁，或格式不变却重新暴露resolved对账及进程清理缺陷；恢复自动发布前须评估并复验。条件修改／删除要求Linux及文件系统支持`renameat2(RENAME_NOREPLACE)`；不可用时在副作用前拒绝，不用覆盖写入降级。

新质量链绑定完整Skill与实际请求；历史0.9的两个operator恢复入口缺少新证明，对0.10须报`skill_quality_recovery_attestation_required`，不能借回退降级或重发unknown。完整必要规则、原文、候选或证据超256000字节仍阻塞，不能截断、抬限或借局部Review发布。

紧急修复仍在dev，固定受影响原版本和范围，完成相同审查、准确CI及兼容检查后经PR以新补丁版本交付master。分支历史与数据格式差异见[分支核对](branch-reconciliation.md)。
