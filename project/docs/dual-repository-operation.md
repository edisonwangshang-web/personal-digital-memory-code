# 双仓库实际使用

公开工程开发与私有资料加工采用人工核对后执行的方式。基本边界见[公开代码与私有资料一起使用](dual-repository.md)；本文给出操作顺序。

## 两个独立 checkout

| 工作区 | 用途 |
| --- | --- |
| `/path/to/code` | 公库 `dev`：程序、提示词、通用 Skill、人工测试、CI 与工程文档 |
| `/path/to/private` | 私库 `dev`：实际加载的工程副本、原文、候选、知识、审核与历史 |

两个仓库都使用完整 Git clone，并明确选择分支。公开初始化 `main`、过滤源码 ZIP 或运行代码包不能代替核对工具所在的 checkout。已有私库不重建，不丢弃 Git 父链、环境绑定或库外状态。

## 加工前核对

需要 Linux、Python 3.11 或 3.12、Git，以及可读取两个仓库和公开 Actions 的现有 `gh` 登录。两边工作区保持干净；代码 checkout 必须位于所选准确公开提交。示例中的公开快照已有 [CI 40163471251](https://github.com/edisonwangshang-web/personal-digital-memory-code/actions/runs/40163471251) 通过：

```bash
python3.12 -B /path/to/code/project/scripts/check_dual_repo.py \
  --code-repo /path/to/code \
  --data-repo /path/to/private \
  --code-ref 8e461422712252ccffefe664466c1ba254fc97cf \
  --ci-run 40163471251
```

该命令只适用于代码 checkout 确实固定到这个快照时。后续提交须提供自己的成功运行，不能把上述 CI 转移给新的文档或工程 HEAD。核对入口见 [check_dual_repo.py](../scripts/check_dual_repo.py)。

入口实时核对公库公开、资料库私有、两个仓库独立、私库处于 `dev`，指定公开 CI 的提交、run／attempt 和八个任务全部成功。核心比对覆盖 `project/src/pdm/`、`project/prompts/`、`project/skills/` 与 `pyproject.toml` 的完整路径集合、Git 模式和内容；上述公开快照为 71 个文件，后续以实际完整集合为准，不能只比交集。

检查失败或过程中发生变化时停止，先处理明确的版本、工作区或权限差异。输出包含私库版本信息，只在仓库外私有位置保存，或明确提交到私库；不要附到公开 issue、提交或 CI 日志。

## 从私库加载程序

核对通过后，将模块路径与资料目录都指向私库，先检查运行条件：

```bash
cd /path/to/private
PYTHONPATH=/path/to/private/project/src python3.12 -B -m pdm doctor \
  --repo /path/to/private
```

再按[日常使用](daily-use.md)执行采集、加工、审核、发布与读取。原资料范围、模型服务、调用记录、未知结果、环境绑定及发布条件继续核对。一次只读核对成功不会发出模型调用，也不启用定时任务。

## 工程更新

先在公库 `dev` 修改、审查并取得准确 CI。需要更新私库工程副本时，按明确版本人工应用核心工程变化，仅提交实际变化，再执行完整核对。保留私库资料、README、运行状态及真实验证历史；共用的产品／技术／工程文档以公库为维护入口。历史 Skill 保持不可变，新规则使用新版本。

入口不复制代码、不搬数据、不合并历史，也不进行双向同步；若已有工程一致即可直接核对使用。差异必须先明确处理，不能靠相同包版本号放行。不要将私库历史 fetch／merge／mirror 到公库，不把整个公库包覆盖私库，也不把含私人 README 的私库发布包当公开导出。

公库提交和私库实际 `program_commit` 分开记录。公开 CI 只证明其准确代码提交，不替代私库不同 SHA 的生产门禁。修改工程、切换版本或运行环境后重新核对；真实模型质量、资料业务验收、定时触发与第二模型各自记录。
