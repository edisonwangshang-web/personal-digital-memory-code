# 公开工程开发交接

本文是公开工程的接手入口，只维护目标、架构、分支、文档入口、约束与阶段待办。产品／技术／工程计划以本仓库为统一维护位置；个人资料、真实实跑／恢复证据和私人接续记录由私库管理，不从本页恢复任何私人执行状态。

## 目标与当前范围

项目保存明确提交的原文，经模型提取组织、自动分类、独立 Review 后形成可追溯知识，供限定范围问答和后续模型读取。长期分为知识库、分析、计划与复盘四个阶段，先完成可持续维护的知识库。三个稳定领域为 `health`、`career`、`learning`，同一事实不为分类复制多份。

当前程序 `0.2.0.dev25`，新任务默认 `knowledge-extract@10`／`knowledge-review@8`，基础提示规则 extract v14／review v10／answer v7。当前重点是通用提取／审核质量、精确证据和选择性读取，沿用已有架构，按具体任务解决确定的问题。

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

已核对公开快照 [`8e461422712252ccffefe664466c1ba254fc97cf`](https://github.com/edisonwangshang-web/personal-digital-memory-code/commit/8e461422712252ccffefe664466c1ba254fc97cf) 的 [CI 40163471251](https://github.com/edisonwangshang-web/personal-digital-memory-code/actions/runs/40163471251) 八任务成功，Python 3.11／3.12 各 1004 项及基础检查通过。该结果不代表后续文档 HEAD、PR 或 `master` 的另一次运行已经通过；每次交付核对自己的实际提交与结果。

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

1. 在公开 `dev` 完成受影响工程修改、审查与准确 CI，更新对应规范、使用说明和进度。
2. 真实业务验证仅在私有运行上下文进行，补齐真实增量／纠正、待确认及相应代表性业务出口；不因工程升级自动重跑已完成模型任务。
3. 接入目标 GPT runner，验证五项实际能力、持久状态与跨窗口恢复，再取得北京时间 22:00 两轮真实触发证据。
4. 获准第二模型实际读取同一限定包并独立回答；准备／核验读取包本身不完成该项验收。
5. M1 所有出口通过后再进入正式完成与发布流程。M2 原因分析与建议、M3 计划、M4 复盘按对应阶段继续细化。

新会话先核对本仓库分支、工作区和远端事实，简要复述目标、当前范围与本批验收，再执行具体任务。需要接续真实资料任务时，从私有工作区读取相应接续材料，程序仍从公库加载；公开交接不能替代它们。
