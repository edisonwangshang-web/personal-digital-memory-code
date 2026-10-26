#!/usr/bin/env python3
"""Render per-layer module architecture PNGs for the four technical subdocuments.

Uses the same drawing helpers, font and dependencies as render_layer_principles.py.
"""
from pathlib import Path
from io import BytesIO
import argparse
from PIL import Image
import matplotlib.pyplot as plt
from render_layer_principles import Diagram, PAPER, INK, MUTED, BLUE, PURPLE, ORANGE


def save(d, out, name):
    out.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    d.fig.savefig(buffer, format='png', dpi=140, facecolor=PAPER, edgecolor='none')
    plt.close(d.fig)
    buffer.seek(0)
    pending = out/(name+'.pending.png')
    with Image.open(buffer) as image:
        image.quantize(colors=128, method=Image.Quantize.FASTOCTREE,
                       dither=Image.Dither.NONE).save(pending, optimize=True)
    pending.replace(out/(name+'.png'))


def interaction(font, out):
    d = Diagram(font, '第一层 · 人机交互技术架构', '本层边界：ChatGPT 多端入口内的两类交互模块')
    d.node(450, 135, 700, 85, '用户 · 手机 / 桌面等端', '提交资料、提问、确认与纠正', external=True)
    d.group(45, 275, 1510, 290, 'L1 人机交互', '#F1F4EF')
    d.node(100, 335, 570, 175, '多端采集', '明确保存范围 · 接收内容与来源\n转交采集适配 · 展示保存回执\n区分原文保存、待审和正式发布')
    d.node(930, 335, 570, 175, '问答与确认', '接收主题 / 对象 / 时间范围\n展示答案、来源与资料缺口\n接收确认、纠正和补充')
    d.arrow([(600,220),(600,250),(385,250),(385,335)], both=True)
    d.arrow([(1000,220),(1000,250),(1215,250),(1215,335)], both=True)
    d.arrow([(930,430),(670,430)], color=ORANGE,
            label='确认 / 纠正 / 补充', at=(800,400))
    d.node(160, 640, 450, 100, 'L3 采集适配', '原文保存、读回与实际回执', external=True, color=BLUE)
    d.node(970, 640, 490, 100, 'L3 证据读取 ＋ L2 回答', '取得证据，再生成有依据的答案', external=True, color=BLUE)
    d.arrow([(385,510),(385,640)], color=BLUE, both=True,
            label='资料 / 保存结果', at=(385,598))
    d.arrow([(1215,510),(1215,640)], color=BLUE, both=True,
            label='问题 / 答案与引用', at=(1215,598))
    d.footer('框为职责模块，不代表独立服务；虚线为层外依赖。保存与回答的实际执行由第三层组织。')
    save(d, out, 'layer-1-architecture')


def knowledge(font, out):
    d = Diagram(font, '第二层 · 个人知识与决策技术架构', '本层边界：三个按需调用的模型模块，共用来源与版本约定')
    d.node(60, 145, 420, 110, 'L3 流程编排', '限定来源、相关旧记录与规则', external=True, color=BLUE)
    d.node(590, 145, 420, 110, 'L3 程序校验与编排', '校验通过后，独立调用审核', external=True, color=BLUE)
    d.node(1120, 145, 420, 110, 'L3 证据读取', '提供问题与有效证据集合', external=True, color=BLUE)
    d.group(35, 325, 1530, 335, '', '#F3EFF6', PURPLE)
    d.text(1545,348,'L2 模型模块',16,PURPLE,ha='right')
    d.node(60, 395, 420, 225, '提取与领域加工', '公共提取：身份、含义、时间\n按需领域：健康 / 事业 / 学习\n合并结果：候选记录与 Wiki\n共同事实保留一份', ai=True, color=PURPLE)
    d.node(590, 395, 420, 225, '独立 Review', '单次或对象 / 覆盖 / 整体审核\n原文＋完整最终正文＋精确关系\n输出绑定版本的审核证明\n通过 / 返工 / 待确认', ai=True, color=PURPLE)
    d.node(1120, 395, 420, 225, '组织证据与回答', '组织正式知识与相关新原文\n区分来源、时间与加工状态\n解释分歧和资料缺口\n返回答案与引用', ai=True, color=PURPLE)
    for x, label in [(270,'输入 / 候选变更'),(800,'证据与候选 / 审核意见'),(1330,'问题与证据 / 答案')]:
        d.arrow([(x,255),(x,395)], color=BLUE, both=True, label=label, at=(x,293))
    d.box(220, 695, 1160, 55, '#EEF4F5', BLUE)
    d.text(800,722,'候选、审核与结果交第三层处理；正式写入统一经过发布控制',20,BLUE)
    d.footer('三框是模型职责，不要求三个独立 Agent；Review 可采用同一模型的独立调用。第二层不直接写正式知识。')
    save(d, out, 'layer-2-architecture')


def engineering(font, out):
    d = Diagram(font, '第三层 · 公共工程能力技术架构', '本层边界：统一编排、程序约束与文件访问；外接交互、模型与存储')
    d.node(80, 130, 350, 95, 'L1 人机交互', '明确提交 / 即时问题', external=True)
    d.node(625, 130, 350, 120, 'L2 模型能力', '提取加工 / 独立 Review\n组织证据与回答', external=True, color=PURPLE)
    d.group(40, 270, 1520, 365, '', '#EEF4F5', BLUE)
    d.text(1500,293,'L3 公共工程能力',16,BLUE,ha='right')
    d.node(80, 330, 350, 105, '采集适配', '取得内容、去重、保存与回执')
    d.node(625, 330, 350, 105, '流程编排', '人工启动 · 版本与阶段状态')
    d.node(1170, 330, 350, 110, '证据读取', '正式知识、新原文及控制\n证据集合交 L2 回答', color=BLUE)
    d.node(80, 520, 350, 95, '发布控制', '核完整证明、内容、路径与版本')
    d.node(625, 520, 350, 95, '程序校验', '结构、引用和版本规则')
    d.node(1170, 520, 350, 95, 'Git 读写适配器', '统一版本读取、条件写入与读回', color=BLUE)
    d.arrow([(255,225),(255,330)], label='提交', at=(255,250))
    d.arrow([(430,175),(500,175),(500,115),(1550,115),(1550,380),(1520,380)], color=BLUE,
            label='即时问题', at=(1200,115))
    d.arrow([(800,250),(800,330)], color=PURPLE, both=True,
            label='按阶段调用 / 结果', at=(800,285))
    d.arrow([(800,435),(800,520)], both=True)
    d.arrow([(625,380),(530,380),(530,566),(430,566)],
            label='校验及 Review 通过', at=(530,492))
    d.arrow([(430,382),(475,382),(475,466),(1130,466),(1130,567),(1170,567)],
            color=BLUE, label='原文保存', at=(1045,466))
    d.arrow([(1345,520),(1345,440)], color=BLUE)
    d.arrow([(255,615),(255,666),(1345,666),(1345,615)],
            label='获准的正式变更', at=(800,666))
    d.node(1170, 700, 350, 60, 'L4 数据与存储', '', external=True, color=BLUE, size=20)
    d.arrow([(1430,615),(1430,700)], color=BLUE, both=True)
    d.text(530,735,'编排经适配器保存阶段进度；失败后核对持久状态续跑。',17,MUTED)
    d.footer('仅展示关键依赖，执行顺序见原理图。正式写入须获发布控制批准；采集与即时问答独立触发。')
    save(d, out, 'layer-3-architecture')


def storage(font, out):
    d = Diagram(font, '第四层 · 数据与存储技术架构', '本层边界：私有 Git 中的文件区域、版本历史与长期本地副本')
    d.node(370, 135, 700, 100, 'L3 Git 读写适配器', '统一按版本读取、受控写入与结果确认', external=True, color=BLUE)
    d.group(40, 290, 1160, 420, 'L4 数据与存储  ·  私有 Git / Markdown', '#F5F2EB')
    d.node(80, 355, 300, 125, '原始资料', '原文 / 链接 / 来源版本\nraw', size=21)
    d.node(465, 355, 300, 125, '候选变更', '待审核记录与 Wiki 变更\nstaging', size=21)
    d.node(850, 355, 310, 125, '正式记录＋Wiki', '独立对象 / 主题综合\nrecords / wiki', size=20)
    d.arrow([(720,235),(720,290)], color=BLUE, both=True)
    d.arrow([(1005,480),(1005,511),(230,511),(230,480)], color=BLUE,
            label='稳定 ID ＋ 来源版本 ＋ 原文片段', at=(615,511))
    d.node(80, 550, 1080, 80, '运行与控制记录、待确认清单', '批次 / 审核 / 回执 / 失败 / 禁止重建 / 待确认', size=21)
    d.box(80, 645, 1080, 45, '#E3EDF3', BLUE)
    d.text(620,668,'Git 版本历史  ·  相关正式变更一致提交  ·  来源回执定位指定快照',18,BLUE)
    d.node(1260, 410, 290, 170, '长期本地副本', '同步获准的远程版本\n读取 · 回溯 · 恢复\n可从远程重新取得', size=21)
    d.arrow([(1160,668),(1230,668),(1230,495),(1260,495)], color=BLUE,
            label='同步', at=(1230,615))
    d.text(640,740,'图示为逻辑区域；目录现状及字段统一见数据规范。',17,MUTED)
    d.footer('文本采用 YAML 元数据＋Markdown；第一版附件只存链接，不建专门索引。普通删除保留 Git 历史。')
    save(d, out, 'layer-4-architecture')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    for render in [interaction, knowledge, engineering, storage]:
        render(args.font, args.output_dir)
