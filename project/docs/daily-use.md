# 日常使用：保存、加工、审核与读取

本文补充`0.2.0.dev27`开发批次的操作接口，默认Skill11/9、普通quality0.15；实现、准确CI、人工全链和真实运行状态以Roadmap为准。本批只在dev开发验证，不合入或推送master。真实资料仍在获准私库操作；参数按实际范围填写，先按[双仓方式](dual-repository-operation.md)核对版本，从公库加载程序、`--repo`指私库，不复制代码回私库。

当前先人工触发。`capture` 只保存原文；加工、独立 Review、正式发布和问答是不同步骤。日程命令不会创建 GPT 云端任务，M1 整体仍未验收，见 [Roadmap](roadmap.md)。

## 准备运行环境

使用完整、干净的 Git checkout，Linux、Python 3.11 或 3.12 和 Git。Windows 使用 WSL2；原生 Windows 与原生 macOS 的生产运行尚未验证。先检查环境，不执行模型调用：

```bash
cd /path/to/code
export PYTHONPATH="$PWD/project/src"
export PDM_DATA_REPO="/path/to/private"
python -P -B -m pdm --version
python -P -B -m pdm doctor --repo "$PDM_DATA_REPO"
python -P -B -m pdm environment verify --repo "$PDM_DATA_REPO"
```

新环境由操作者准备仓库外的配置，再用 `environment bind --repo "$PDM_DATA_REPO" --input "$environment_config"` 绑定实际资料根、分支、remote 与库外 `state_root`。已有绑定不因换设备或换版本自动覆盖；`--local-only` 仅供明确隔离的演练。配置字段见[数据规范](data-conventions.md)，环境与发布约束见[发布与回滚](release-and-rollback.md)。

`-P` 禁止将当前目录插入模块搜索路径，避免同名目录覆盖已核对的公库程序；`-B` 避免生成字节码。后文命令沿用以上绝对 `PYTHONPATH` 和资料根变量，`--expected-head` 始终读取私库 HEAD。

模型调用还需要实际可用的服务与资料范围。工程仓库可读、GitHub 登录成功或命令存在，都不能证明具备模型服务访问权限。

普通`process`（含`--revision-from-run`）、`production run`、`ask`及待确认执行的新模型调用默认等待900秒，可用`--model-timeout 900`显式设置，允许范围为1—1800秒；不是整条任务的总时长。专用resume／revalidate入口未指定时保留120秒兼容值，可显式覆盖。生产队列将worker参数传到实际调用；日程仍使用冻结的`timeout_seconds`，不因升级改旧值。启动后超时先对账，不换operation或加长超时重发unknown，见[超时合同](data-conventions.md#model-call-timeout)。

<a id="explicit-knowledge-rebuild"></a>

## 明确清理后从原文重新加工

只有用户明确要求清理并重建时才使用此入口。先核准原文和现有正式目标，填写本次稳定意图；下列变量均为人工占位，不能把示例当个人资料：

```bash
python -P -B -m pdm rebuild-prepare --repo "$PDM_DATA_REPO" \
  --intent-id "$rebuild_intent" --source "$source_path" \
  --target "$record_path" --target "$topic_path" --expected-head "$data_head"
```

多份来源或目标重复对应参数。程序在同一提交删除明确选定的正式知识并保存清理回执，原文、旧候选、审核、运行、控制和调用记录保留。相同意图重放只返回原引用，发布新知识后也不会再次删除；另一次明确清理须有新的用户意图。更新导航是后续私库资料操作，成功发布前标为待重新加工。

将返回的 `rebuild_context` 三键对象单独保存为仓库外 JSON 文件，再准备队列；不要把整个命令响应当作引用：

```bash
python -P -B -m pdm production prepare --repo "$PDM_DATA_REPO" \
  --id "$queue_id" --source "$source_path" --rebuild-context "$rebuild_reference_file"
python -P -B -m pdm production run --repo "$PDM_DATA_REPO" \
  --id "$queue_id" --budget-ledger "$audit_ledger" --operation-id "$operation_id"
```

小范围也可把同一引用交给 `process --rebuild-context`；该非批次入口沿用普通 Review，必须满足完整普通审核输入上限，发布仍是显式步骤。需要自动分解审核的完整重建使用上述 `production` 入口，不给普通 `process` 强加 `--review-strategy auto`。重建使用完整选定原文，首次不提供旧 related、revision、merge 与复用输入，重新提取和三领域分类后完整独立 Review。当前只支持来源能放入一批的重建，容量不足在新预约前停止；不要删原文或换意图重试。

本轮新候选退回时，可将同一 `--rebuild-context` 与 `--revision-from-run` 一起交给 `process`；核心须核同回执、同来源和批次，仍不读取清理前旧知识。

有效回执只授权重建它实际清理的目标，仍检查来源／控制、当前写入状态及完整证明。普通删除保护、旧 unknown 和计数保留；同一重建回执换队列、日期或工程版本不重置最多四次尝试。具体[字段契约](data-conventions.md#explicit-knowledge-rebuild)和[实现步骤](implementation-details.md#explicit-knowledge-rebuild)统一管理。原文清理、工程 CI、新知识审核、读取问答及私有恢复分别记录；任一步成功都不能单独证明 M1 整体验收完成。

## 配置调用记录

`process`、`ask`、`production run` 和日程执行入口显式接收 `--budget-ledger` 与稳定的 `--operation-id`。调用记录放在仓库外的私有持久目录；同一授权范围共用原记录，恢复时核对原操作身份，不新建空记录或换 ID 重放未知调用。

日常审计模式记录调用，不设置每日或历史累计次数硬上限；容量、有限重试、并发、速率和实际服务范围仍约束执行。创建时按实际已有用量填写，不默认从零开始：

```bash
python -P -B -m pdm budget create --repo "$PDM_DATA_REPO" --ledger "$audit_ledger" \
  --mode audit --initial-used "$actual_initial_used" \
  --authorization-ref "$scope_reference"
python -P -B -m pdm budget status --repo "$PDM_DATA_REPO" --ledger "$audit_ledger"
```

历史有限预算模式仍可使用 `--limit "$authorized_total"`；旧记录按其原契约保留。`reserved` 和 `started` 分开报告，预约不等于已经调用，已发生调用也不因输出校验失败自动返还或重试。本文不包含任何实际调用记录。

## 先保存原文

把符合来源契约的 JSON 放在仓库外。保留原文、角色、顺序和已知时间；未知时间留空。取不到正文时保存链接与获取状态，不编摘要。将采集结果中的实际 `path` 用作后续的 `source_path`：

```bash
python -P -B -m pdm capture --repo "$PDM_DATA_REPO" --input "$source_json" \
  --expected-head "$(git -C "$PDM_DATA_REPO" rev-parse HEAD)" --remote origin
```

`local_saved` 只说明本地保存；指定 `--remote origin` 后，只有实际推送并精确读回成功，才报告 `remote_status: confirmed`。来源位于私库 `memory/sources/`，候选位于 `memory/staging/`，正式知识位于 `memory/records/` 和 `memory/topics/`。

## 人工触发持久生产队列

本批新默认为`knowledge-extract@11`与`knowledge-review@9`、普通quality0.15，旧10/8的0.12及assembly0.14保持原证明。程序准备清单、可复算统计和绑定，模型提取并独立审核；精确`reuse_refs`继续按原条件物化和审核，不用空输出省略旧记录。旧任务沿用原Skill及格式，不靠改队列名清失败或unknown；专用旧恢复合同不因新默认自动扩权。

```bash
python -P -B -m pdm production prepare --repo "$PDM_DATA_REPO" --id "$queue_id" \
  --source "$source_path" --review-strategy auto
python -P -B -m pdm production run --repo "$PDM_DATA_REPO" --id "$queue_id" \
  --budget-ledger "$audit_ledger" --operation-id "$worker_operation_id" --model-timeout 900
python -P -B -m pdm production status --repo "$PDM_DATA_REPO" --id "$queue_id"
```

多来源重复 `--source`；需要旧知识时，准备阶段通过 `--allowed-source` 和 `--allowed-run` 明确全部允许的依赖。程序保存分批计划、各批候选与审核、最终依赖组证明和多主题原子发布结果。普通完整 Review 请求超过 256000 字节时，`auto` 按需转为 object／coverage／global 分解审核；完整必要语义仍超容量时保持 `blocked_capacity`，不能截断材料后宣称完成。

普通多批的完整原文合并超容量时，程序可自动把已核完整草稿原样组成0.14最终候选，再真正独立分解审核；不增加extract调用或新的CLI参数。当前只接无旧关联、答复或重建上下文的普通草稿；对象、统计口径冲突或必要审核仍超容量时保留具体阻塞。若该原样最终候选审核要求实质修订且仍不能承载，后续`assembly_revision_required`表示需要处理，不能反复运行同一联合期待自动修好。完整边界见[数据规范](data-conventions.md#verbatim-draft-finalization)。

所有必要独立审核通过后才正式发布。已完成节点按原身份复验，未知先对账；首次执行后最多三次失败重试，跨日、重启或改队列名不清零。独立生产队列的 `published_local`／`not_requested` 只确认本地发布，远程交付另按既有规则完成。

Review9若明确只缺统计绑定，正常返工会在同一extract中要求精确`statistic_bindings`，程序补固定统计与根引用，原正文和关系保持后重新独立审核；没有新的补绑CLI命令或额外模型阶段。混合／正文问题仍需完整返工，不能手改模型返回或把旧证明补成通过。具体边界见[定点返工合同](data-conventions.md#metadata-only-statistic-repair)。新真实全链另核现有状态和明确意图，不把零调用复验当成新加工。

已完成的10/8返工提取若仅因候选存储失败，不重新extract。先准备目标plan，再由操作者核原生completed账本、原始缓存及完整请求并固定validation_head，按[专用步骤](implementation-details.md#quality-completed-recovery-execution)恢复；unknown不适用。恢复仍需真正独立Review和完整发布证明，不能把候选或缓存核验当作完成。

## 单次加工与显式发布

小范围人工操作也可直接使用 `process`。它完成提取和 Review，随后由操作者明确发布：

```bash
python -P -B -m pdm process --repo "$PDM_DATA_REPO" --incremental --source "$source_path" \
  --expected-head "$(git -C "$PDM_DATA_REPO" rev-parse HEAD)" \
  --budget-ledger "$audit_ledger" --operation-id "$process_operation_id"
python -P -B -m pdm publish --repo "$PDM_DATA_REPO" --run "$run_id" \
  --expected-head "$(git -C "$PDM_DATA_REPO" rev-parse HEAD)"
python -P -B -m pdm verify-publication --repo "$PDM_DATA_REPO" --run "$run_id"
```

使用返回的实际 `run_id`。更新同主题知识时，重复 `--source` 列出全部必要新旧来源，并显式提供 `--related-run "$old_run_id"`。缺少旧记录、主题依赖或来源范围时停止补齐，不自动读全库。`already_processed` 表示核验并复用旧成功链；`passed` 只表示审核通过，尚未正式发布。

合法 `needs_revision` 可通过 `process --revision-from-run "$rejected_run"` 在原范围返工，保留完整候选和真实反馈。反馈是修订依据，原文才是事实来源；新候选须有实质修订并重新独立审核。`needs_confirmation` 需要真实答复，未知调用先对账。

<a id="selected-knowledge-reading"></a>

## 只读取需要的记录或主题

```bash
python -P -B -m pdm evidence --repo "$PDM_DATA_REPO" --current \
  --source "$source_path" --run "$run_id" \
  --object memory/topics/example-topic.md --compact-sources
python -P -B -m pdm ask --repo "$PDM_DATA_REPO" --current \
  --source "$source_path" --run "$run_id" \
  --object memory/topics/example-topic.md --compact-sources \
  --question '在本次选定范围内，哪些结论有依据，还有哪些缺口？' \
  --budget-ledger "$audit_ledger" --operation-id "$question_operation_id"
```

`--object` 可重复，选择已有正式 record／topic 路径；主题自动带上精确版本的 record 依赖。选择正式对象时须明确 `--run`，运行涉及多个来源时仍须逐个列全。`--compact-sources` 保留带原始行号的 `lines`，避免再次发送相同 `source.body`。未读范围会明确返回，不能把选择齐全当成读遍全部资料。

`evidence` 不调用模型，`ask` 执行一次独立模型问答且只读。`--source` 和 `--run` 均可重复；片段读取成对提供 `--plan`／`--batch`，保留全局行号。未选依赖、控制限制或容量不足时返回缺口或停止，不以省略必要上下文继续。未使用对象选择参数的旧读取与 `reading-package` 保持原行为。

追溯旧发布使用 `verify-publication --historical`；读取纠正历史使用 `evidence --current --history`。历史核验通过不能证明旧事实仍然有效，删除控制始终生效。未选同 ID 新版本只报告元数据缺口，不读取其正文；不能按最近保存时间替代事实有效时间。

## 三领域目录与回答引用

稳定领域键为 `health`（健康与运动）、`career`（工作事业）、`learning`（学习与能力提升）。提取自动分类，独立 Review 核对；新建或实质更新的记录可跨类或明确未分类，同一事实不为分类复制多份。精确 `reuse` 保留旧受审对象，不为换标签制造更新。

```bash
python -P -B -m pdm knowledge --repo "$PDM_DATA_REPO" --source "$source_path" --run "$run_id" \
  --domain learning --budget-bytes "$evidence_budget" --format markdown
```

目录只覆盖显式来源与发布运行，不自动发现全库运行，不调用模型，也不改 README 或正式文件。topic 领域来自精确受审 record 的并集；未分类、分类缺口和 coverage 一起报告。`evidence_budget` 是读取／输出字节上限。

当前回答规则区分已核验的系统处理状态与来源历史自述。未提供 pending 只代表本次未提供相关事项，不能把每个资料缺口变成用户任务或宣称全库没有待确认项。正文 `[1]` 对应 citations 第一项；程序拒绝畸形、越界编号和无效来源坐标，但编号有效不能证明逐句语义支持，仍需质量核对。

## 补充、纠正、删除与待确认

先保存新原文，再预览明确范围的控制操作：

```bash
python -P -B -m pdm control plan --repo "$PDM_DATA_REPO" --input "$control_json"
python -P -B -m pdm control apply --repo "$PDM_DATA_REPO" --input "$control_json" \
  --expected-head "$(git -C "$PDM_DATA_REPO" rev-parse HEAD)"
python -P -B -m pdm pending list --repo "$PDM_DATA_REPO" --status open
```

缺少来源或对象依赖时拒绝写入；纠正控制不自动改正文，随后仍走增量加工、审核与发布。普通删除清理当前视图并防止旧来源重建，Git 历史保留；彻底清除历史须另定范围。

Review 需要澄清时登记统一事项。回复先 `capture`，再关联原事项，后续真实新发布实际采用全部有效答复后才能解决：

```bash
python -P -B -m pdm pending answer --repo "$PDM_DATA_REPO" --item "$item_id" --source "$reply_source_path"
python -P -B -m pdm pending resolve --repo "$PDM_DATA_REPO" --item "$item_id" --run "$new_run_id"
```

`answered` 表示已收到回复，`resolved` 需要后续发布核验。限定 evidence／ask 会投影相关 pending；仅在来源、回复和正文都完整选中时显示完整问题，缺范围时只报告缺口。`registration_required` 表示 Review 已保存但登记未完成；核对存储或冲突后，可用 `pending repair-registration --repo "$PDM_DATA_REPO" --run "$run_id" --source "$source_path"` 零模型补登记，重复列全原来源。不能修改候选或规则后沿用旧审核。

## 日程入口与停启

日程是已有工程接口，实际平台接入、北京时间 22:00 两轮真实触发和持久状态恢复仍待验收。先人工触发，不把本机执行或 trigger 文件里的 `scheduled` 当作平台已执行。

新 `continuous-v1` 配置使用 `production-v1` 引擎，明确来源、允许的旧运行、时区、`max_attempts=4`、`max_calls=null` 及远程模式。配置保存在仓库外；旧无 engine 日程不静默升级。字段与完整流程见[数据规范](data-conventions.md)和[实现明细](implementation-details.md)。

```bash
python -P -B -m pdm schedule configure --repo "$PDM_DATA_REPO" --input "$schedule_config"
python -P -B -m pdm schedule plan --repo "$PDM_DATA_REPO" --id "$schedule_id"
python -P -B -m pdm schedule drain --repo "$PDM_DATA_REPO" --id "$schedule_id" --trigger "$trigger_json" \
  --budget-ledger "$audit_ledger" --operation-id "$schedule_operation_id" \
  --max-slices 8 --window-seconds 300
python -P -B -m pdm schedule status --repo "$PDM_DATA_REPO" --id "$schedule_id"
```

`drain` 在有限窗口内推进持久队列，续跑使用返回的 `--continuation "$continuation_json"` 和原身份。答复关联为 `answered` 后，同一日程会携带完整必要回复继续加工。`remote_mode=required` 需要实际条件 push、fetch 和精确读回；`local_only` 仅供隔离演练。完整来源并集超过支持范围或必要语义超容量时停止，不截断。

查看JSON中的`status`、`next_action`及剩余范围。`run`和`drain`需对账、账本修复或人工处理时均返回退出码4，应先核已有结果或修复前置；合法等待确认和可继续进度不会因此变成完成，退出码0也不表示所有资料均已发布。

```bash
python -P -B -m pdm schedule pause --repo "$PDM_DATA_REPO" --id "$schedule_id" --reason "$pause_reason"
python -P -B -m pdm schedule resume --repo "$PDM_DATA_REPO" --id "$schedule_id" --validation-ref "$validation_ref"
```

暂停阻止程序的新正式发布，不等于关闭外部任务。恢复前核对在途执行、本地与远程发布及原调用记录。没有平台观测时保持 `trigger_unknown`；`incomplete`／`reconciliation_required` 先核已有结果。`abandon` 是受控终结入口，不撤销外部在途请求，不能把未知结果改成从未执行；完整条件按实现与数据契约核验。

## 恢复与另一模型读取

遇 `transaction_recovery_required` 先核对 HEAD 和已有提交，再执行 `python -P -B -m pdm recover --repo "$PDM_DATA_REPO"`。冲突或结果不明时保持暂停，不整库 reset，不直接删除锁或 journal。资料恢复使用私库的完整 Git 历史；源码 ZIP 或代码包不能替代。

`resume-unstarted-revision` 和 `revalidate-completed-extraction` 是历史 `0.9` 人工恢复入口。它们核对原预约／已完成返回和固定证明；当前不能用于新的 Skill 质量任务，该模式会要求 `skill_quality_recovery_attestation_required`。不要照抄旧命令绕过质量、原调用身份或未知结果检查。

另一模型读取先准备同一固定证据与问题；问题 JSON 是字符串数组，不含答案和验收预期，输出使用仓库外尚不存在的新文件：

```bash
python -P -B -m pdm reading-package prepare --repo "$PDM_DATA_REPO" --source "$source_path" --run "$run_id" \
  --questions "$questions_json" --output "$reading_package_json"
python -P -B -m pdm reading-package verify "$reading_package_json" \
  --expected-sha256 "$package_sha256"
```

准备和核验不调用另一模型。真正的跨模型验收还需指定模型实际读取同包同版本、独立回答并核对来源，结果保存在私有验证记录中。
