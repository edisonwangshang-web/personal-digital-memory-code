# 工程入口

在公库根目录运行 `PYTHONPATH="$PWD/project/src" python -P -B -m pdm --help` 查看命令。此开发版本包含来源采集、多来源知识提取、独立审核、领域分类、受控发布与证据读取。提取和审核可引用版本化 Skill；真实模型接入和资料授权须在私有工作区单独配置。

公库是唯一工程来源，不保存个人记忆。真实资料的采集、加工和发布从公库目录及模块路径加载程序，使用 `python -P -B -m pdm ... --repo /path/to/private` 指向私库资料根，不再将代码复制回私库。开始前核对同名分支、准确公库 CI 与资料边界，见[双仓使用](../docs/dual-repository-operation.md)。
