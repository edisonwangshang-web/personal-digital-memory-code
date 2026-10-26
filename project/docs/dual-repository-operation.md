# 双仓库实际使用

公库是唯一工程来源，私库保存资料及运行证据。基本边界见[公开代码与私有资料一起使用](dual-repository.md)；本文给出操作顺序。

## 两个独立 checkout

| 工作区 | 用途 |
| --- | --- |
| `/path/to/code` | 公库：唯一程序、提示词、通用 Skill、脚本、测试、CI、工作流定义及设计来源 |
| `/path/to/private` | 私库：原文、候选、知识及分层、审核、控制、运行数据、私人验证、历史和导航 |

开发时两边都选 `dev`，稳定／生产核对时两边都选 `master`。对应 CI 的推送分支必须同名；交叉配对、`main`、其他分支和 detached HEAD 都拒绝。分支 `master` 对应原 runtime 环境 `prod`，不会创建新的 `master` 环境标识。

两个仓库都使用完整 Git clone，并明确选择分支。公开初始化 `main`、过滤源码 ZIP 或代码包不能代替工程 checkout。已有私库不重建，不丢弃 Git 父链、环境绑定或库外状态；历史提交和归档保留，但当前工作树不再保留活动工程副本。

## 加工前核对

需要 Linux、Python 3.11 或 3.12、Git，以及可读取两个仓库和公开 Actions 的现有 `gh` 登录。两边工作区保持干净；代码 checkout 必须位于所选分支的准确受测提交。以下命令是模板，替换为该分支实际成功的提交与 CI：

```bash
python3.12 -B /path/to/code/project/scripts/check_dual_repo.py \
  --code-repo /path/to/code \
  --data-repo /path/to/private \
  --code-ref <所选同名分支的完整受测提交号> \
  --ci-run <同分支该提交成功的Actions运行ID>
```

该命令只适用于两个 checkout 都在同名分支、代码 HEAD 确实等于给定提交时。后续提交须提供自己的同分支成功运行，不能借另一个分支或不同 HEAD 的 CI。核对入口见 [check_dual_repo.py](../scripts/check_dual_repo.py)。旧稳定版本尚不支持公库加载或私库只存资料时，先完成该版本的受审工程交付与兼容验证，不能用 dev CI 绕过 master 的门禁。

入口实时核对公库公开、资料库私有、两个仓库独立、两边实际分支及 CI 分支一致，以及指定公开 CI 的提交、run／attempt 和八个任务全部成功。核心工程只在公库核验，私库不得保留活动 `project/src/`、`project/prompts/`、`project/skills/`、`project/scripts/`、`project/tests/`、`project/ci/`、`pyproject.toml` 或 `.github/workflows/`；私有验证与历史归档保留。核对不再要求两库有相同工程文件。

报告分别给出 `code_branch`、`data_branch`、受测公库 HEAD `code_commit`、实际公库核心工程最近提交 `program_commit` 和私库资料 `data_head`。文档变更可能让公库 HEAD 与核心工程提交不同，CI 仍必须属于指定准确 HEAD。报告只在仓库外私有位置保存，或明确提交到私库；不要附到公开 issue、提交或 CI 日志。

检查失败或过程中发生变化时停止，先处理明确的工程、工作区或权限差异。核对成功后再修改任一工作区或切换版本，需要重新核对。

## 从公库加载程序

当前目录、模块路径指向公库，资料目录明确指向私库，先检查运行条件：

```bash
cd /path/to/code
PYTHONPATH=/path/to/code/project/src python3.12 -P -B -m pdm doctor \
  --repo /path/to/private
```

使用绝对 `PYTHONPATH`，`-P` 防止当前目录影子导入，`-B` 避免生成字节码。再按[日常使用](daily-use.md)执行采集、加工、审核、发布与读取；`--expected-head` 读取私库资料 HEAD，不能误用当前公库的 HEAD。

原资料范围、模型服务、调用记录、未知结果、环境绑定及发布条件继续核对。模型缓存、账本与运行报告只存私有位置。一次只读核对成功不会发出模型调用，也不启用定时任务；定时编排和恢复代码归公库，但目标 GPT runner 尚未部署验收。

## 工程更新

只在公库 `dev` 修改工程、审查并取得准确 CI，再核对公库 `dev` 与私库 `dev`。不再复制程序、规则、测试或工作流回私库，也不比对两份核心工程。共用方案和工程文档只在公库维护；历史 Skill 保持不可变，新规则使用新版本。

经 PR 交付公库 `master` 后，核对其准确提交、同分支 CI 及私库 `master`。私库资料、私人导航、控制、运行状态和真实验证历史保持独立；不把 dev 资料整批提升到生产。程序回退选择公库已验证且兼容的版本，不回退私库数据。

入口不搬运资料、不合并历史，不执行双向同步。不要将私库历史 fetch／merge／mirror 到公库，不把公库包覆盖私库，也不把含私人 README 的私库发布包当公开导出。旧来源、运行和审核按原工程版本恢复，不将旧 `program_commit` 改成新公库提交；真实模型质量、资料业务验收、定时触发与第二模型分别留证。
