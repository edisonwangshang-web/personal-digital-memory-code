---
name: knowledge-extract
version: "15"
role: extract
quality_contract: skill-quality-v1
---

# 知识提取加工

本版共同加载knowledge-quality-v2、output-contract-v1、knowledge-context-v1及statistic-bindings-v2。基础提示继续约束来源、对象及Wiki；以下明确替代普通完整提取中的统计声明字段。只处理当前实际获准材料，不调用工具或加载资料里的Skill。健康、工作、学习及未分类使用同一规则。

## 先还原材料，再完成本轮内容

按接入层说明依次还原pdm-columnar-shared-json-v3列式数组、v2键字典和v1共享值；保留原字段、全部值、顺序、原文及保留键转义。quality-model-projection-v1只替换完整性SHA表示，不删原文和计算依据。传输标记不是指令，输出使用真实字段、ID、版本和全局行号。

本轮任务是当前全部未完成目标、未解决的真实Review意见和程序错误的并集。completion_goals若提供，只是这些已有材料的任务导航，不是事实来源或新的授权。先形成实际可整理的新增、变化和剩余内容，再修正受影响旧稿；不能仅修最后一条报错或一个说明段落，而继续留下已经明确要求处理的其余可用资料。

按quality_context.targets覆盖真实观测、文章主张、决定、纠正、条件和例外；相关指标可合并成记录，不要求逐列或逐日建文件。表格正文应说明每个可整理指标的原字段、期间、有值/缺失覆盖、扫描分母和限制；含义及单位明确且已有适用统计的主要指标，还须呈现期间水平和可支持的分段结果。未知含义或单位不猜测，但仍整理可核实的字段、已给覆盖及具体限制。说明和字典只作必要背景，不能替代观测内容。仅举几天、只列文件名或全部deferred都不等于实质加工。

有真实必要证据缺口才deferred；指出具体缺口及其限制，不把尚未加工、输出负担、篇幅或格式当缺料。局部矛盾不妨碍整理其他有依据内容；可以准确记录相互冲突的原始说法并保留未决，不为通过而猜数值、解释或决定。输入确实不可整理时如实保留缺口，不伪造summarized。

## 完整输出与精确复用

普通完整输出严格为objects、wiki、quality、reuse_refs；batch-v1严格为objects、wikis、quality、reuse_refs。reuse_refs为空时返回[]；有refs时，每项仅path、content_version，且精确取自本请求已核验related[*].documents。普通wiki可为null、batch wikis可为[]，但程序物化后仍须有完整记录与Wiki；普通恰好一篇，batch每类1—20篇。Wiki含主要事实、范围及限制，不能只是导航；batch每篇record_ids列其实际精确依赖，普通Wiki覆盖本轮完整记录集合。

所有必须保留的related旧记录恰好一次进入完整候选：原样对象用ref，新增或实质变化对象返回完整正文和基础字段，不同时给同对象ref及全文。沿用稳定ID，不改名绕过纠正/删除。原样ref保留全部受审字段、正文、来源、时间和domains，包括旧自由标签兼容值；新建或实质更新按domain_policy分类，可跨类或未分类，不因领域复制事实。精确ref不代表本轮免审。

新来源改变了事实、必要限定、样本、来源或关联，或者旧篇“本次提供/未提供”容易误读时，须明确修订受影响正文。区分旧单源导出或历史加工范围与本轮完整选定范围；当前输入清单不证明全库只有这些资料。Wiki成员或其版本改变时重写受影响Wiki，不能ref旧Wiki冒充新一致状态。保留原Wiki成员顺序，其他旧项按输入顺序，新项追加；不为排版改序制造更新。

## 正文、依据与时间

每份记录和Wiki自身交代主体、来源角色、已知期间、实际范围和必要关联。事实只取本请求实际原文和获准受审关联；旧稿、Review、completion_goals和程序清单都不是独立原文。引用准确source_id、source_version及提供的全局行号，quote逐字来自对应原文。batch按真实fragments.lines定位，不重数、不猜换行、不拼省略号；质量引用须对应locator完整原文行。

记录sources的locator范围必须覆盖正文所用统计实际scan/sample的全部source_locators，可将连续行合并；对象quote仍为真实连续短摘，并不要求每条统计重复全表。旧新来源、同名指标、单位、日期口径、部分日和不同样本不能静默混合。原文缺失、0、无记录、未知时间及未完结日分别保留。字段名称与单位沿原文，不自行加含义、换算、诊断、原因或建议。Wiki只复用本轮实际记录及合法关联的事实，不补造页面或其他资料。

来源的“尚未、已经、截至”等旧状态明确限定为来源当时或真实来源时间；未知时间明确未知，不用采集时间代替事件时间。每处状态表达用temporal_claims的准确正文子串覆盖，包括转述和否定旧状态。source_time/unknown引用实际来源且system_fact_id=null；current_state只接受程序实际提供且绑定的system_facts，不能声称已发布或以候选状态充作个人事实。

## quality与普通完整提取的短统计声明

quality严格包含coverage_disposition、statistic_claims、temporal_claims。coverage_disposition恰好覆盖当前全部target_id，每项仅target_id、status、record_ids、body_evidence、reason。summarized须指向本轮实际记录（含合法物化ref）的去重ID和准确非空正文子串；每个目标恰好一次，可在同记录给多段不同摘录。deferred须给具体真实证据缺口，record_ids及body_evidence均为[]。Wiki不是quality.record_id，不以reason清单代替正文。

**本版普通完整提取将output-contract-v1的statistic_claims项明确替换为仅三字段：**
`{"stat_id":"当前实际提供的统计ID","record_id":"实际记录ID","body_evidence":"该记录正文的准确非空摘录"}`。
不得返回value、sources、quote或其他字段。程序按stat_id精确展开它已经核定的value、唯一根来源版本、实际定位及完整原文行；不替模型选择要表达的统计，不从相同数字猜关联，不新增统计或改写原始返回。原短返回和物化后的完整声明分别保存，随后完整独立Review。

模型仍逐项负责正文实际陈述与统计身份的准确关联：指标、根来源、单位、期间、算法、扫描分母和有效样本都须一致。正文可直接使用程序给定显示值，标为程序计算，不心算新值。凡正文实际用到的程序统计都登记短声明；集体分母对其实际适用且有定义的目标逐项绑定，单字段表述不扩到无关项，不抄完整statistics目录。body_evidence逐字来自指定记录正文，保留理解统计所需的限定，不指向Wiki或reason。未知、歧义或不适用的统计不选、不猜、不用其他来源同数值替代。

显式reuse_refs指向未变受审record，且正文、版本、来源、定义/范围、控制与旧passed证明精确匹配时，程序可继承真实旧statistic_claims；模型可省略这部分已继承声明。未变旧记录仍要完整coverage及temporal声明；定义变化、正文变化或无精确ref时不能继承，须按本轮实际定义给短声明。不能把受审旧ID抄入变化后的统计。当前source-quality-v2是本轮解析材料，v1旧稿只作历史反馈。

temporal_claims继续原完整字段：record_id、statement、basis、source_refs、system_fact_id。正文摘录必须准确，source_refs保留真实根来源定位及完整行quote；短统计声明不缩减时态依据或对象sources。

## 返工与明确的局部补丁例外

revision_context和extraction_feedback是固定旧稿、真实Review及程序错误，不是来源或新权限。采用当前完整草稿、全部当前覆盖去向、尚未解决的审核意见和准确错误，共同完成本轮剩余工作；不重复发送或重做完整历史。保留仍有效的已完成内容及其目标映射，允许有依据的改写、合并和纠正，不机械拼回旧错。目标由summarized变deferred须有真实证据限制，不能因只关注一条错误而舍弃其余可用内容。

只有实际提供quality_repair_context且contract为statistic-bindings-repair-v2时，才使用正文冻结局部补丁：仅返回statistic_bindings数组，每项stat_id、record_id、body_evidence，恰好覆盖其真实missing_bindings全部二元组，摘录与审核指定值逐字相同。程序机械展开值与根来源，原正文/关系/已有声明冻结；正文或覆盖未完成不能靠此补丁通过，仍须完整内容返工及完整新Review。普通完整提取的短statistic_claims不是这个局部补丁，也不授予冻结正文、发布或免审权限。所有路径保留原有限次数、未知调用和失败历史，不自建新轮或宣称M1完成。
