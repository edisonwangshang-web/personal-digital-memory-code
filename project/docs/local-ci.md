# 自托管 CI：可选路线与验收要求

当前默认使用公开仓库的 GitHub 标准托管 Linux CI，见[公开工程 CI](public-code-ci.md)。无需为了日常工程检查保持个人电脑在线。本页保留自托管方案的通用设计，尚未作为本公库的已验收执行路线。

## 适用场景

只有确有网络、硬件或独立计算资源需求时，才考虑让 GitHub 把任务分配给自有 Linux 执行器。本地检查已经提供 [check_local.py](../scripts/check_local.py)，但运行本地检查与把执行器注册到 GitHub 是两件事；本地报告不会自动成为 Actions 结果。

自托管使用自己的机器资源。GitHub 的计费和可用性以当时[官方 Actions 说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)为准，不能据此承诺无限资源或所有附加服务免费。

## 公开工程当前提供什么

现有 [engineering.yml](../../.github/workflows/engineering.yml) 的执行位置固定为 `ubuntu-latest`，没有自托管变量开关。公库未提供专用注册助手、常驻服务或自托管启停脚本。本页不把其他工作区的工具当作本仓库已提供能力。

若后续正式接入，应作为新的工程变更开发和审查：

1. 选择独立、受管理的 Linux 执行环境，准备 Git、受支持的 Python 和 Actions 网络访问。
2. 按 [GitHub 官方自托管执行器文档](https://docs.github.com/en/actions/hosting-your-own-runners)完成注册与运行，实际标签必须匹配真实操作系统和架构。
3. 通过版本化工作流明确路由，保留双 Python、全部六个测试分片、两组 foundation，以及 PR 的实际合并树和资料隔离门禁。
4. 在 Actions 核对准确受测提交、实际执行器与全部任务结果，再记录运行链接；连接 Online 只证明接入成功。
5. 明确离线排队、重启、升级、停止与撤回路线；一个执行器同一时间只运行一个任务。

注册信息只在执行机本地交互输入，机器认证文件留在专用私有目录。公开工程测试不需要私库访问权限；不要让公开任务取得个人资料或模型登录环境。

## 验收边界

下载完成、版本探测成功、容器运行或本地测试通过都不能证明真实 GitHub CI 成功。接入异常需保留状态并定位，不跳过用例、不伪造同名 status，也不把另一提交的结果移作当前提交通过。

未来切回托管路线时，先提交并核对执行位置，防止任务继续排到离线机器。可选 Mac 容器路线另见 [Mac CI](mac-ci.md)；当前主线继续采用标准公开 Linux CI。
