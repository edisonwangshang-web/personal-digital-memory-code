# 公开工程 CI 与双仓库边界

程序、通用 Skill、人工测试、CI 和可公开的产品／技术／工程文档由[公开工程仓库](https://github.com/edisonwangshang-web/personal-digital-memory-code/tree/dev)管理。个人原文、候选、正式知识、真实审核与验收依据及其完整 Git 历史由私库管理。

## 当前公开检查结果

已核对的公开工程快照为 [`8e461422712252ccffefe664466c1ba254fc97cf`](https://github.com/edisonwangshang-web/personal-digital-memory-code/commit/8e461422712252ccffefe664466c1ba254fc97cf)，对应 [Actions 40163471251](https://github.com/edisonwangshang-web/personal-digital-memory-code/actions/runs/40163471251)：八个任务成功，Python 3.11／3.12 各 1004 项测试，以及运行环境、源码归档排除和代码包核验通过。

这是该准确提交的固定结果。后续文档或工程提交均有自己的 HEAD，不能把这次运行写成新提交已经通过，也不能据此声称 `master` 或 PR 的另一轮检查成功。

## 标准托管 Linux 路线

[engineering.yml](../../.github/workflows/engineering.yml) 对 `dev/master` push 和目标为 `master` 的 PR 触发，使用 GitHub 标准托管 `ubuntu-latest`：

- 两个 Python 版本分别执行 `decomposed`、`other-a`、`other-b` 三个测试分片，共六个任务。
- 两组 foundation 必须等待全部分片成功，再做 doctor、源码归档排除和代码包构建／核验。
- PR 额外核对同仓来源、允许的交付分支、实际合并树和生产资料隔离。

公开 CI 只运行人工资料与模拟模型，不需要私库访问权限或真实模型调用。公开仓库可使用标准托管 Actions 的免费运行规则，不消耗私库包含的分钟额度，具体以 [GitHub 当时的计费规则](https://docs.github.com/en/billing/concepts/product-billing/github-actions)为准；仍有并发、单次时长和平台限制，大型执行器等额外服务另计。工程 CI 的费用规则不能证明私库限制已解除，也不代表真实资料加工或模型服务免费。

日常无需 Mac 常开或注册个人执行器。[本地检查](local-checks.md)可辅助开发；[自托管](local-ci.md)和 [Mac 容器](mac-ci.md)保留为可选、尚未验收的路线。

## 分支与交付

`dev` 用于工程开发，`master` 用于稳定交付。公开 `main` 是初始化分支，默认展示设置尚未切换；查看代码请直接进入 [dev](https://github.com/edisonwangshang-web/personal-digital-memory-code/tree/dev) 或 [master](https://github.com/edisonwangshang-web/personal-digital-memory-code/tree/master)。默认分支设置的管理权限限制不影响已取得的准确代码 CI 结果。

在 `dev` 完成审查和准确提交的实际检查后，通过 PR 交付 `master`；PR、合并提交和最终分支的检查分别核对。配置存在、本地通过或旧提交成功都不代替实际运行。发布按[发布与回滚](release-and-rollback.md)执行。

## 私有加工继续独立运行

私库保留现有工程副本、资料 clone、历史与运行绑定，实际采集、提取、独立审核、发布和读取仍从私库加载程序。按[双仓库实际使用](dual-repository-operation.md)核对两边完整核心工程与准确公开 CI；入口只读，不自动复制文件、迁移资料或双向同步。

公库代码提交与私库 `program_commit` 分别记录。即使核心文件相同，也不能把公开 CI 填成私库不同 SHA 的绿色状态；原私库生产交付、资料审核和实际模型质量门禁继续有效。纯知识提交的检查触发规则由私库独立管理，不能从本公库文档推定其已停用或替换。

公开内容从经审查的允许清单建立独立历史，不复制私库 `.git`、分支、标签或旧发布包。报告、真实模型请求／返回、机器认证、调用账本及私人排错和验收记录不进入公开资料。
