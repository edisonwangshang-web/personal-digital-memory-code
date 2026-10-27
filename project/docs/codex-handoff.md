# 公开工程开发交接

本文是公开工程的接手入口，只维护目标、架构、分支、文档入口、约束与阶段待办。产品／技术／工程计划以本仓库为统一维护位置；个人资料、真实实跑／恢复证据和私人接续记录由私库管理，不从本页恢复任何私人执行状态。

## 目标与当前范围

项目保存明确提交的原文，经模型提取组织、自动分类、独立 Review 后形成可追溯知识，供限定范围问答和后续模型读取。长期分为知识库、分析、计划与复盘四个阶段，先完成可持续维护的知识库。三个稳定领域为 `health`、`career`、`learning`，同一事实不为分类复制多份。

本批开发版为`0.2.0.dev29`，沿用Skill11/9、普通quality0.15、assembly0.14及基础提示规则extract v14／review v10／answer v8，新增超时重试sidecar／门禁0.16。**本批只在dev开发验证，不合入或推送master。** 本批针对性人工回归已检查，准确dev CI待核，不用上一批成功代替新结果，真实全链与M1整体另行验收。

本批最小目标是“超时后自动最多再重试3次”，不要求逐次人工确认。新prepare冻结`bounded-timeout-v1`，首版仅对独立人工队列的单完整批、已结束普通root extract（含真实反馈返工）timeout、该run从未有候选／Review／发布、原来源／控制／Skill／rebuild及audit身份可核验的情况自动继续。旧无marker队列不静默升级；当前目标计划沿同work和原次数推进，不是新业务意图。原调用仍unknown，0.16自动回执链接唯一后继，`timeout_retry_ref`参与处理身份但不送模型；第4次仍失败停止，绝不第5次。契约与反例见[数据规范](data-conventions.md#bounded-timeout-retry)及[场景验收](scenarios-and-acceptance.md#bounded-timeout-retry-acceptance)。

仍在途、非timeout unknown、`timeout_cleanup_unknown`、预算／权限或Review调用的超时（含分解审核）不适用，停止并报告原因。后继0调用预约按原run／operation幂等，迟到原run不能再发布、被group采用或成为当前正式结果。原消费与历史保留；本地串行不代表服务端取消或无重叠。其他已确认失败的3次安全重试、审核子单元耗尽父工作停止、第4次通过和待确认原语义继续，见[有限重试](data-conventions.md#bounded-processing-retries)。

dev28纯程序组装保存候选前的恢复仍只接原零调用预约：严格核原started／engine、原pool／operation与全部子单元无状态，用原run／attempt继续原样组装再完整Review。它不取回已启动模型的原结果，不能与dev29另行记录的有限timeout后继混用，见[组装恢复合同](data-conventions.md#assembly-zero-call-resume)。

dev27沿用的等待时间与纯统计绑定返工仍有效：普通CLI新调用默认900秒、可设1—1800秒，旧冻结日程不迁移且实际传参；Review9确认纯缺失后，原revision extract11仅补statistic_bindings，程序以固定统计与根引用物化，旧正文／关系／quality条目保持，完整新Review照常。内容或混合问题走原全稿。旧10/8及assembly0.14原证明不改，字段及边界见[数据规范](data-conventions.md#metadata-only-statistic-repair)。可配置等待本身不保证完成或允许重发，dev29有限timeout策略以外的unknown仍停止。

本批修复多批容量回退、10/8草稿／答复反馈、读取重复Git I/O、调度退出码与控制写前容量。受限原样finalization使用0.14且没有新extract，最终仍独立分解审核；related、答复或重建大组及跨批实质修订不因此获得任意容量支持。普通10/8质量链0.12和历史Skill不迁移，现有成功业务链不全量重加工。准确工程CI与真实业务出口分别检查，见[M1交付核对](m1-delivery-readiness.md)。

## 架构与入口

| 组成 | 工程位置与职责 |
| --- | --- |
| Python 程序与 CLI | `project/src/pdm/`；来源采集、加工、控制、审核证明消费、发布、读取与事务恢复 |
| 基础提示规则 | `project/prompts/`；实际字节摘要参与运行版本绑定 |
| 版本化通用 Skill | `project/skills/`；提取、审核及共享质量／输出／上下文规则，历史版本保持不可变 |
| 人工工程测试与 CI | `project/tests/`、`.github/`；临时样例、模拟模型、双 Python 分片及基础检查 |
| 产品与工程文档 | `project/docs/`；总体方案、技术职责、实现流程、字段规范、验收口径、阶段进度与使用说明 |

私有资料的逻辑路径为 `memory/sources/` 原文、`memory/staging/` 候选、`memory/records/` 与 `memory/topics/` 正式知识。公库是唯一工程来源，真实运行从公库 checkout 加载，以 `--repo` 指向私库并保留既有环境绑定；私库只保留资料、审核、控制、私人验证、完整历史和导航。不得将活动代码、规则、测试或工作流复制回私库。

接手先读根 [AGENTS.md](../../AGENTS.md)、[README](../../README.md)、[Roadmap](roadmap.md)，再按任务读取[总体方案](overall-plan.md)、[技术方案](technical-plan.md)、[实现明细](implementation-details.md)、[数据规范](data-conventions.md)、[场景验收](scenarios-and-acceptance.md)及[发布与回滚](release-and-rollback.md)。[日常使用](daily-use.md)描述 CLI 操作，[双仓库实际使用](dual-repository-operation.md)描述版本核对。

## 分支与准确证据

工程在公开 `dev` 开发，通过审查和准确提交的 CI 后，经 PR 交付 `master`。`master` 是稳定交付分支；初始化 `main` 不作为开发入口。远端默认展示分支设置存在管理权限限制，访问代码可直接使用 [dev](https://github.com/edisonwangshang-web/personal-digital-memory-code/tree/dev)／[master](https://github.com/edisonwangshang-web/personal-digital-memory-code/tree/master)。

上一批dev28快照[`d9116dd1f8c2f70624dde96cc01ea827ff2cb7e3`](https://github.com/edisonwangshang-web/personal-digital-memory-code/commit/d9116dd1f8c2f70624dde96cc01ea827ff2cb7e3)的[CI 40449641483](https://github.com/edisonwangshang-web/personal-digital-memory-code/actions/runs/40449641483)八任务成功，Python3.11／3.12各1288项及基础检查通过。它不证明本批dev29或后续HEAD通过，不替代真实业务，也不授权本批更新master。

标准公开 Linux CI 是主路线，不需要 Mac 常开。离线工程检查使用 [check_local.py](../scripts/check_local.py)，见[本地检查](local-checks.md)；自托管／Mac 容器仅保留为可选未验收方案，公库没有专用注册或 Mac 启停工具。

## 开发约束

- 公开内容只包含通用工程、人工样例与脱离个人背景的文档；真实来源、知识、审核、模型请求／返回、私人验证历史和机器认证不进入公库。
- 不推送私库 Git 历史、标签或分支，不从私有发布包直接导出公开材料。
- 模型负责提取组织和独立语义审核，程序提供可复算材料并核对引用、版本与发布条件；结构或 CI 成功不能代替语义质量。
- 旧任务用原规则快照恢复，历史 Skill 不改写；新规则使用新版本。未知 schema 拒写，不通过降版本或整库 reset 绕过兼容检查。
- 公开工程测试使用临时目录与人工资料，不调用真实模型；根据改动运行适当检查，记录准确受测提交、结果与剩余限制。
- 双仓只读核对同名分支、准确公库 HEAD 的 CI、公库工程完整性及私库无活动工程。运行以公库为当前目录，绝对 `PYTHONPATH` 指向公库，使用 `python -P -B`，`--repo` 指私库；`program_commit` 记录实际公库核心工程提交，`data_head` 独立记录资料提交。旧运行版本、权限、审计与未知结果不改写。
- 当前先人工触发。日程 CLI、runner probe、本机 worker 或模拟触发均不能证明真实 GPT 平台已创建任务或按时执行。

## 阶段与待办

M0 手动基础闭环已有历史验收。M1 主要工程能力包括多来源增量、三领域、质量 Skill、纠正与待确认、持久生产队列、分解 Review、原子发布、限定读取和恢复；整体仍未验收，具体状态统一见 [Roadmap](roadmap.md)。

后续按实际任务推进：

1. 本批仅在公开`dev`完成修改、审查、标准CLI人工全链与准确CI，更新规范和进度，不合入或推送master。
2. 真实业务验证仅在私有运行上下文进行，补齐真实增量／纠正、待确认及相应代表性业务出口；不因工程升级自动重跑已完成模型任务。
3. 接入目标 GPT runner，验证五项实际能力、持久状态与跨窗口恢复，再取得北京时间 22:00 两轮真实触发证据。
4. 获准第二模型实际读取同一限定包并独立回答；准备／核验读取包本身不完成该项验收。
5. 工程修复按[交付核对](m1-delivery-readiness.md)完成准确CI及稳定分支交付；M1所有出口通过后再宣布阶段完成及建立正式完成版本。M2 原因分析与建议、M3 计划、M4 复盘按对应阶段继续细化。

新会话先核对本仓库分支、工作区和远端事实，简要复述目标、当前范围与本批验收，再执行具体任务。需要接续真实资料任务时，从私有工作区读取相应接续材料，程序仍从公库加载；公开交接不能替代它们。
