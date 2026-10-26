# 架构与信息流程图

## 图示范围与当前状态

本目录保存四层架构、信息流程和逐层职责图，以及可编辑 Python 图源。现有 PNG、SVG 和绘图源均为经检查的通用工程设计资料；图像沿用原始字节。本目录不包含个人原文、正式知识、模型返回或真实运行证明。

图只解释职责、数据交接和控制路径，模块不要求独立部署。总图右上角的开发批次与日期是工程设计快照标签；小图中的“V1 实施草案”、目录待落实或完整链路待试跑等文字也保留该快照含义。旧实现细节与本文以外的现行说明冲突时，以[技术方案](../technical-plan.md)、[四层技术子文档](../technical-plan/02-knowledge.md)和[数据规范](../data-conventions.md)为准。

当前程序为 `0.2.0.dev24`，新任务采用 `knowledge-extract@10`／`knowledge-review@8`，三领域共用规则。显式 Skill 装载、资料清单、质量检查、无损传输以及按 record／topic 选择读取和 `compact_sources` 路径未全部画入旧图。来源／采集 schema `0.1`、正式知识 `0.2`、10/8质量链 `0.12`、旧9/7为 `0.11`、更早质量链 `0.10`，专用已完成提取恢复另有 `0.13` 门禁，历史格式保留原契约；不能从图推断当前 schema。

人工触发后的受控加工、程序校验、独立 Review 与完整 proof 发布已有工程实现。Review 可为容量内的一次独立审核，或 object／coverage／global 组成的完整证明；发布控制须实际核齐全部必需 proof。恢复箭头不表示可以重发 `unknown` 调用，已完成阶段须核验后复用。M1 整体、目标定时 runner、两轮真实触发、云端持久恢复和第二模型读取仍待验收；图不证明定时任务已启用，状态见 [Roadmap](../roadmap.md)。

| 图 | 阅读目的 | 文档入口 |
| --- | --- | --- |
| [技术架构图](technical-architecture.png) | 四层架构、层内模块、关键调用与工程控制 | [技术方案](../technical-plan.md) |
| [四层信息流程泳道图](information-flow-swimlane.png) | 采集、加工、审核发布与消费在四层间的数据交接 | [总体方案](../overall-plan.md) |

## 信息流程图

泳道代表架构层，横向表示采集、加工、审核发布和消费阶段。图配合[总体方案](../overall-plan.md)与[技术方案](../technical-plan.md)阅读。

- `information-flow-swimlane.png`：嵌入文档的预览。
- `information-flow-swimlane.svg`：可缩放矢量图，中文字形已转轮廓。
- `render_information_flow.py`：绘图源，维护节点、文字、泳道与连线。

实线是主流程，蓝色虚线是原文参与消费，橙色虚线是退回加工、待确认与用户反馈。跨层箭头为逻辑交接，调度和读写由第三层执行。模型 Review 通过不等于事实已独立核验；用户确认回到采集，回答不自动成为独立证据。

AI 小角标表示需要模型调用的环节：提取与领域加工、独立 Review、组织证据与回答。人工启动后由受控编排推进加工和审核，回答由提问按需触发。接入、校验、读写及 Git 发布由工程流程承担。

“知识加工任务编排”大框横跨第二、第三、第四层，覆盖读取增量、提取分类、保存候选、程序校验、独立 Review、返工／待确认、发布及回执。泳道表示职责，大框表示执行范围；第三层推进。候选、正式记录和待确认状态持久保存，在任务运行之外也可受控读取。采集和即时问答在框外，用户答复作为新证据进入后续加工。

## 技术架构图

- `technical-architecture.png`：技术方案预览。
- `technical-architecture.svg`：可缩放矢量图，中文字形已转轮廓。
- `render_technical_architecture.py`：绘图源。

| 层 | 模块与数据区 |
| --- | --- |
| 人机交互 | 多端采集、问答与确认 |
| 个人知识与决策 | 提取与领域加工、独立 Review、组织证据与回答 |
| 公共工程能力 | 采集适配、流程编排、程序校验、发布控制、证据读取、Git 读写适配器 |
| 数据与存储 | 原始资料、候选变更、正式记录与 Wiki、运行与控制、长期本地副本 |

深色线表示处理交接，蓝线表示数据读写与回答，AI 角标只用于第二层的模型模块。实际先后为编排、提取、程序校验、独立 Review、发布；模型不直接写正式知识，发布控制经 Git 读写适配器提交。

虚线大框横跨第二至第四层，表示加工和持久化范围，包含候选、审核、待确认、进度和回执。第一层采集与即时问答独立触发；共享文件和适配器也可在加工任务之外访问。此图与信息泳道图分别解释模块关系和数据流。

## 四层实现原理小图

四张图解释内部协作与跨层调用，虚线节点为层外依赖。模块名称和职责与各子文档对应，处理步骤不表示独立部署。

| 图 | 解释重点 |
| --- | --- |
| [第一层：人机交互](layer-1-interaction.png) · [SVG](layer-1-interaction.svg) | 提交与提问分路、回执与答案、确认纠正回到采集 |
| [第二层：个人知识与决策](layer-2-knowledge.png) · [SVG](layer-2-knowledge.svg) | 公共提取、领域加工、候选、程序校验后独立 Review、按证据回答 |
| [第三层：公共工程能力](layer-3-engineering.png) · [SVG](layer-3-engineering.svg) | 编排、校验、发布控制、统一读写和持久恢复 |
| [第四层：数据与存储](layer-4-storage.png) · [SVG](layer-4-storage.svg) | 文件角色、来源引用、版本提交与本地副本 |

图源为 `render_layer_principles.py`。程序校验须先于独立 Review，正式写入须经发布控制，回答不直接发布，来源版本和 Git 提交标识分别核验。图不增加架构选型或第一版范围。

## 四层技术架构图

各技术子文档内嵌对应本层架构图，展示本层模块、层外依赖和关键交接。框为职责或文件区，虚线框为层外依赖。

| 架构图 | 对应子文档 |
| --- | --- |
| [第一层：人机交互](layer-1-architecture.png) | [交互层技术文档](../technical-plan/01-interaction.md) |
| [第二层：个人知识与决策](layer-2-architecture.png) | [知识层技术文档](../technical-plan/02-knowledge.md) |
| [第三层：公共工程能力](layer-3-architecture.png) | [工程层技术文档](../technical-plan/03-engineering.md) |
| [第四层：数据与存储](layer-4-architecture.png) | [存储层技术文档](../technical-plan/04-storage.md) |

图源为 `render_layer_architectures.py`，复用 `render_layer_principles.py` 的绘图组件，生成 PNG。

## 再生成与维护

需要 Python、Matplotlib、Pillow 及支持中文的字体。原图采用 [LXGW WenKai TC Regular](https://raw.githubusercontent.com/google/fonts/main/ofl/lxgwwenkaitc/LXGWWenKaiTC-Regular.ttf)，字体遵循 [SIL Open Font License](https://raw.githubusercontent.com/google/fonts/main/ofl/lxgwwenkaitc/OFL.txt)；仓库不包含字体文件。

在仓库根目录执行；下面都是通用路径占位符，不含私有环境位置：

```bash
python -m pip install matplotlib pillow
python project/docs/diagrams/render_information_flow.py --font /path/to/LXGWWenKaiTC-Regular.ttf
python project/docs/diagrams/render_technical_architecture.py --font /path/to/LXGWWenKaiTC-Regular.ttf
python project/docs/diagrams/render_layer_principles.py --font /path/to/LXGWWenKaiTC-Regular.ttf
python project/docs/diagrams/render_layer_architectures.py --font /path/to/LXGWWenKaiTC-Regular.ttf
```

默认更新脚本所在目录的 PNG／SVG，可用 `--output-dir` 指定输出目录。修改图源后检查中文、箭头、层间职责和现行技术约束，再更新对应文档。绘图生成物与源代码一起维护，不提交 `__pycache__`、本地字体或临时图片。
