# 功能分支整合与历史契约核对

本页保留分支核对的方法和仍有约束力的格式差异，不公布私库提交、PR、CI、资料链或恢复验收。当前程序`0.2.0.dev22`，默认extract@10／review@8；来源0.1、正式对象0.2、新10/8质量链0.12、旧9/7为0.11、更早质量链0.10；支持0.1—0.13各自契约，0.13仅用于专用已完成提取恢复门禁。M1未整体验收，工程或历史资料链通过不能代替定时runner、两轮真实触发及第二模型证据。

## 当前双仓接续

公库保存方案、产品设计、技术文档、工程代码、通用Skill、人工测试及CI；私库保存真实个人资料、原文、知识、审核、Git历史、运行绑定及私有验收。两库历史独立，公库不导入私库的分支、tag、资料子树或祖先。私库现有工程及实际加载入口保留，公开文档搬迁不等于已切换运行程序。

`check_dual_repo.py`只读核对两库可见性、准确公库CI以及71个核心文件的完整集合、模式与内容。公库代码提交和私库实际program_commit分别记录；即使核心文件相同，也不能把公库绿色CI赋给另一仓库或不同SHA。工具不自动sync、迁移资料、合并历史或改变实际加载入口。操作见[双仓运行](dual-repository-operation.md)。

所有功能、修复与配套文档在dev开发，固定目标、准确提交、适当回归和独立审查后经PR交付master。合并保留全部祖先及父边，不squash／rebase；候选、Review、proof、清单和回执可能引用历史SHA。新批次不沿用旧提交CI，最终master仍核实际提交和CI。生产隔离与回退见[发布与回滚](release-and-rollback.md)。

## Foundation 与 Runtime 的历史契约差异

Foundation与Runtime最初从共同基线分叉，存在重叠实现。Runtime的代码或真实运行结果不等于Foundation后续版本，不可直接套用对方的授权、CI或数据链。以下为技术差异；历史固定版本及整合证据仅留私库。

| 项目 | Foundation | Runtime |
| --- | --- | --- |
| 来源schema | `0.1`；正式知识`0.2`及后继运行格式另管 | `pdm-1` |
| 来源ID | `src-`＋稳定键完整SHA-256 | `src-`＋身份摘要前24位 |
| 来源版本 | `sha256-`＋完整摘要 | 裸SHA-256 |
| 来源路径 | `<source_id>/<source_version>.md`不可变快照 | `<source_id>.md`当前文件，历史经Git／回执读取 |
| 采集入口 | JSON契约，`capture(repo,payload,expected_head,remote=...)` | 文本入口及另一套`capture(root,body,...)` |
| 回执 | Markdown元数据，`save_result: local_saved` | JSON，`status: local_verified` |
| 工程入口 | Python3.11+、`pdm-memory`、pyproject、CI及白名单代码包 | 独立`./pdm`与另一套命令组织，不继承Foundation版本包或CI |

不能仅把`pdm-1`加入支持列表就宣称兼容：身份、版本、路径、字段和引用链均不同。当前schema探测识别它仅为拒绝混写，不代表能够读取、迁移或恢复其writer。选择性移植代码须在Foundation契约上补适配和隔离回归，再产生实际新证据；不复制个人资料链来制造“已验证”。

## 私有历史子树的核验

私库可保留完整历史树、提交和父边；`archive/`与`fixtures/`是前缀子目录，不是独立Git根。归档不替换当前`project/src/pdm/`、规则或根`memory/`，不自动转换旧schema。当前知识入口和schema探测仅覆盖既定受管范围，不因为档案存在而宣称它们已兼容。

前缀导入不能改写文件里的原source commit、Review、manifest或原路径绑定。核验从固定原commit建立私有隔离checkout，在真实Git根按原路径读取；当前前缀不是回执的新绑定。不能把资料子目录直接传给`--repo`，也不能运行旧writer改写新资料。源码归档及白名单代码包排除私有档案，完整私有clone才保留资料与历史。

新的受审资料交付保留来源、候选、Review、proof、发布提交及父链，不从历史候选或旧答案冒充新模型输出。重新加工、复用旧结果、零调用兼容核验分别记录真实加工版本和后继validator版本；不能把后继门禁修复重标为原模型已使用新程序或新Skill。

## 接续与冲突处理

1. 分别核两库真实Git根、远程身份、dev／master、实际加载代码及工作区；不把相同目录名、版本号或核心文件相同当作同一提交历史。
2. 保留Foundation来源身份、不可变快照、采集回执、CI和代码包契约；`pdm-1`仅作历史资料，迁移需显式设计、映射和验收，不能靠增加schema白名单。
3. 对每个输入固定来源／采集版本、程序／规则／Skill、控制、候选、Review和proof，按其实际格式复验。旧0.4／0.7 registry、0.6 group、0.8分解、0.9返工和0.10质量不可混升。
4. 冲突时保留真实原文、已有回执、控制、未知调用、持久retry及全部已知失败，不通过reset、删除proof、换queue或账本刷新次数。已完成且有效的节点核验后复用，unknown先对账。
5. 私库dev有开发资料时，先用promotion prepare构造`[生产base, dev]`双父候选，工程取dev、保护资料取base；核实际PR合并树和准确CI后交付，不直接把dev资料并入master。
6. 清理分支前核祖先与独有树，保留可追溯结果；不重写已发布tag。私库历史核验留私有证据，公库只保存通用方法与人工测试。

阶段进度由[Roadmap](roadmap.md)维护；字段见[数据规范](data-conventions.md)，实际验收按[场景定义](scenarios-and-acceptance.md)判断。文档、代码包、历史链或人工transport成功均不单独证明真实语义、云端部署或M1完成。
