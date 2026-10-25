# 本地工程检查：环境搭建与使用

本地入口在 Linux 或 Windows WSL2 运行与公开 push CI 对应的工程检查，使用本机计算资源。标准公开 CI 是默认路线，见[公开工程 CI](public-code-ci.md)；本地结果适合开发反馈，记录的是本地准确提交，不会更新 GitHub checks。

## 准备两个 Python 版本

需要 Git、Python 3.11 和 Python 3.12。项目没有额外运行依赖，检查不需要模型 SDK、模型登录或 API 密钥。脚本核对两个解释器的实际版本，不能用同一个程序冒充两个版本。

从公开仓库完整 clone，明确使用 `dev`；浅 clone、源码 ZIP 和运行代码包不能替代检查所需的完整 Git checkout：

```bash
git clone --branch dev https://github.com/edisonwangshang-web/personal-digital-memory-code.git
cd personal-digital-memory-code
git status --short --branch
```

已有两个解释器且可从 PATH 找到时，在仓库根目录执行：

```bash
python3.12 -B project/scripts/check_local.py
```

如果已安装 uv，可先准备解释器，再显式传入实际路径：

```bash
uv python install --no-bin 3.11 3.12
PDM_PY311="$(uv python find --no-python-downloads 3.11)"
PDM_PY312="$(uv python find --no-python-downloads 3.12)"
"$PDM_PY312" -B project/scripts/check_local.py \
  --python311 "$PDM_PY311" --python312 "$PDM_PY312"
```

安装是单独的环境准备操作；`find --no-python-downloads` 只查找已安装版本，检查脚本也不会下载软件。没有 uv 时可直接用 `--python311 /path/to/python3.11 --python312 /path/to/python3.12` 指定。补丁版本按每次报告记录，不依赖某台开发机的绝对路径。

本地入口只支持 Linux／WSL2。程序依赖 `fcntl`、Linux 文件权限和事务接口；原生 Windows 与原生 macOS 不作为已验证运行环境。Mac 可选容器路线的边界见 [Mac CI](mac-ci.md)。

## 固定提交，在临时副本执行

运行前妥善保存自己的修改，保持工作区干净。脚本只检查当前已提交的准确 HEAD，不自动保存、丢弃、提交、推送或切换原分支。

脚本记录原提交、工作区和已有 `memory/` 字节摘要，用 `git clone --no-local` 建立仓库外完整副本，固定同一提交，并核对无浅历史或外部对象共享。各测试分片使用独立副本。正常结束、失败或可处理的中断后清理副本并保留报告；清理或原仓库不变性核验失败也不能报告通过。

临时目录使用 0700，报告与日志使用 0600。若操作者在私库运行此入口，完整临时副本和日志也属于私有材料，不得上传到公库；公开工程开发使用公开 checkout 即可。

两个 Python 版本分别执行：

1. 复用 [run_test_shard.py](../../.github/scripts/run_test_shard.py) 的 `decomposed`、`other-a`、`other-b` 分片，核对发现结果一致、完整覆盖且不重复。
2. 执行 `pdm doctor`，检查环境和格式兼容性。
3. 生成 Git 源码归档，确认排除 `memory/`、`project/validation/`、`fixtures/` 和 `archive/`。
4. 生成代码包，再核对准确提交与该提交声明的程序版本。

测试使用临时人工资料、模拟模型和一次性伪 CLI 进程，不调用真实模型。测试总数以实际发现为准。默认同时运行两个任务；机器负载高时使用 `--jobs 1`。每步默认超时 3600 秒，可用 `--timeout` 调整。失败、中断或超时返回非零退出码，不能把部分分片或单一版本算作整套通过。

## 查看报告与修复失败

最后输出 `report.json` 路径，默认位于仓库同级 `.cache/<仓库名>/local-checks/<运行标识>/`。可用 `--output /仓库外/新的报告目录` 指定尚不存在的位置；报告不得位于原仓库或其 Git 目录内。

报告包含准确受测提交、程序版本、实际解释器版本、每步结果和耗时、发现的测试数，以及原仓库不变性结果。`running` 表示未完成，只有最终 `passed` 才证明整套本地检查通过。目录还保留步骤日志、分片清单、过滤归档和代码包，均不自动上传 GitHub。

失败时读取 `error`、失败步骤及相应日志：缺解释器就准备正确版本，工作区不干净就先妥善处理修改，测试失败就定位并修复。提交修复后对新提交重新检查，保留失败记录，不跳过失败分片。重新运行离线测试不会恢复或重放任何真实模型任务。

## 与远程 CI、发布和业务验收的关系

入口见 [check_local.py](../scripts/check_local.py)。本地通过只证明报告中的准确提交，不能写成 GitHub checks 成功。它没有真实 PR 合并上下文，不能代替生产资料隔离、PR 合并树检查、分支保护或 `master` 交付门禁；发布按[发布与回滚](release-and-rollback.md)执行。

工程测试、真实模型质量、资料发布、目标云端触发与 M1 业务验收分别记录。本地通过本身不完成这些验收。
