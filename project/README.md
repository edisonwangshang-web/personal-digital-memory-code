# 项目方案与工程文档

产品方案、技术设计和工程使用文档统一在本仓库维护。私人资料、知识正文、模型返回、授权与实际个人验收证据保存在独立私库，不作为公开工程文档。

## 按目的阅读

| 目的 | 文档 | 内容 |
| --- | --- | --- |
| 了解产品 | [总体方案](docs/overall-plan.md) | 目标、用户场景、第一版范围与后续阶段 |
| 查看产品交互 | [第一层：人机交互](docs/technical-plan/01-interaction.md) | 提交资料、问答、确认、状态及异常展示 |
| 了解技术架构 | [技术方案](docs/technical-plan.md) | 四层职责、关键决策、模块交接与可靠性 |
| 理解知识加工 | [第二层：个人知识与决策](docs/technical-plan/02-knowledge.md) | 通用 Skill、提取分类、独立审核、知识组织、证据读取 |
| 理解工程控制 | [第三层：公共工程能力](docs/technical-plan/03-engineering.md) | 编排、校验、发布、重试和恢复 |
| 理解数据保存 | [第四层：数据与存储](docs/technical-plan/04-storage.md) | 原文、候选、正式知识、控制记录与版本 |
| 查看详细执行 | [实现明细](docs/implementation-details.md) | 各环节输入输出、执行顺序、事务和失败恢复 |
| 查询数据契约 | [数据规范](docs/data-conventions.md) | 字段、路径、状态、版本兼容、质量与审核证明 |
| 验证功能 | [场景与验收](docs/scenarios-and-acceptance.md) | 人工样例、检查步骤、预期结果和验证边界 |
| 查看阶段工作 | [Roadmap](docs/roadmap.md)、[M0—M1 执行方案](docs/m0-m1-execution-plan.md) | 阶段目标、工作包、实现与待验收事项 |
| 日常操作 | [日常使用](docs/daily-use.md) | 采集、加工、发布、读取和受控恢复命令 |
| Agent查找与读取知识 | [读取方式](docs/technical-plan.md#agent-cli-index)、[操作顺序](docs/daily-use.md#selected-knowledge-reading) | 模型通过目录与文本查找定位，再用现有CLI核验读取；当前不建索引 |
| 使用双仓库 | [双仓库核对](docs/dual-repository.md)、[操作方式](docs/dual-repository-operation.md) | 公库唯一工程来源、准确 CI 与私库资料边界核对及实际运行 |
| 检查和交付代码 | [本地检查](docs/local-checks.md)、[公开 CI](docs/public-code-ci.md)、[发布与回滚](docs/release-and-rollback.md)、[M1 交付核对](docs/m1-delivery-readiness.md) | 准确受测提交、分支、产物、数据保护与回退，以及工程交付和业务验收的区别 |
| 查看可选 CI 路线 | [自托管说明](docs/local-ci.md)、[Mac 说明](docs/mac-ci.md) | 可选方案及尚未验证的运行条件 |
| 接续开发 | [工程交接](docs/codex-handoff.md)、[版本记录](docs/version-history.md)、[分支说明](docs/branch-reconciliation.md) | 阅读顺序、版本边界和协作约定 |
| 查看架构图 | [图集与图源](docs/diagrams/README.md) | 总体架构、信息流、四层职责图及生成源码 |

## 文档职责

总体方案说明为什么做、服务谁及产品范围；技术方案说明如何组成系统；实现明细说明怎样执行。字段与兼容规则集中在数据规范，测试要求集中在场景与验收，阶段状态集中在 Roadmap。

文档中的人工示例不属于用户事实。设计、实现、代码 CI、真实资料加工、模型质量和云端运行分别取得证据；不能相互替代。架构图是职责说明，带版本日期的图按设计快照理解，现行技术约束以正文和实际代码为准。

工程在 `dev` 开发，验证及审查后经 PR 交付 `master`。文档更新也在 `dev`，不把资料私库历史合并到公开工程库。
