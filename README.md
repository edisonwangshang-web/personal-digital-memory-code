# Personal Digital Memory — 工程代码

此仓库统一提供产品方案、技术设计，以及来源采集、知识提取、独立审核、分类发布和带引用读取的工程代码、通用 Skill、人工测试与 CI。当前版本为开发版；代码检查通过不代表真实资料加工或完整业务验收已经完成。

## 项目方案与产品设计

产品目标、用户场景、技术方案、实现明细、数据契约及阶段计划统一在本工程仓库维护，入口见[项目文档导航](project/README.md)。

- [总体方案](project/docs/overall-plan.md)：产品目标、用户场景与范围。
- [技术方案](project/docs/technical-plan.md)：四层架构与实现原理。
- [实现明细](project/docs/implementation-details.md)、[数据规范](project/docs/data-conventions.md)、[场景与验收](project/docs/scenarios-and-acceptance.md)：具体执行与检查要求。
- [Roadmap](project/docs/roadmap.md)：已实现、待验证和后续阶段。

## 代码与资料分开

真实原文、知识、运行记录、授权、凭据和模型返回必须保存在另一个私有工作区。此仓库没有个人资料，也不包含原私库的提交历史。不要在此公开仓库运行会提交真实资料的采集或加工任务。`.gitignore` 只是防误加提示，不能阻止程序或手动强制提交。

## 分支与双仓库使用

所有工程在 `dev` 开发和验证，经审查与实际 CI 后通过 PR 交付 `master`。个人原文、知识及审核历史保存在另一个私有仓库，实际加工使用私库保留的工程副本。

运行加工前，用 `project/scripts/check_dual_repo.py` 核对公库的成功 CI 与私库实际工程文件；核对通过后，继续使用私库的原有命令。操作见[双仓库使用说明](project/docs/dual-repository.md)。此入口只核对版本与边界，不合并两边历史或搬运个人资料。

## 检查代码

需要 Linux、Git 及 Python 3.11 或 3.12，无第三方运行依赖。

```bash
PYTHONPATH=project/src python -m pdm doctor --repo .
PYTHONPATH=project/src python -m unittest discover -s project/tests
```

GitHub Actions 在推送 dev、master 或提交到 master 的 PR 时运行：两版 Python 各分三组测试，再检查运行条件、源码归档和代码包。PR 保留同仓库分支及生产资料保护检查。测试只在临时目录创建人工资料，不调用真实模型。

## 目录

- `project/src/pdm/`：工程实现。
- `project/docs/`：产品方案、技术设计、使用说明及架构图。
- `project/prompts/`：模型提示词。
- `project/skills/`：提取与审核的版本化通用规则，适用于健康、工作与学习。
- `project/tests/`：人工测试及临时 Git 仓库辅助工具。
- `project/scripts/check_local.py`：本地按双 Python 分片检查；通过 `--help` 查看用法。

保留全部历史 Skill 版本是为了核对规则绑定；它们不包含用户的加工结果。此代码仓库的提交号独立于私有资料仓库，CI 结果只适用于实际受测提交。
