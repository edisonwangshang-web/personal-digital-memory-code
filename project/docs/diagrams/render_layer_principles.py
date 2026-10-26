#!/usr/bin/env python3
"""Render four compact implementation diagrams aligned with technical-plan.md §3.

Requires matplotlib, Pillow and a CJK font; all diagrams describe the V1 draft.
"""
from pathlib import Path
from io import BytesIO
import argparse
import re
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.path import Path as MPath
import matplotlib.patheffects as pe
from PIL import Image

W, H = 1600, 820
PAPER = '#FCFBF7'
INK, MUTED = '#40564D', '#718078'
BLUE, PURPLE, ORANGE = '#477998', '#786398', '#A57243'


class Diagram:
    def __init__(self, font, title, subtitle):
        self.font = FontProperties(fname=str(font))
        self.fig = plt.figure(figsize=(W/100, H/100), dpi=100, facecolor=PAPER)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(H, 0)
        self.ax.set_axis_off()
        self.text(45, 45, title, 28, ha='left')
        self.text(45, 92, subtitle, 17, MUTED, ha='left')
        self.text(1550, 44, 'V1 实施草案', 15, MUTED, ha='right')

    def text(self, x, y, value, size=18, color=INK, ha='center', **kwargs):
        return self.ax.text(x, y, value, fontsize=size, fontproperties=self.font,
                            color=color, ha=ha, va='center', zorder=8, **kwargs)

    def box(self, x, y, w, h, fill='#FFFEFA', color=INK, dashed=False, z=3):
        p = FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0,rounding_size=11',
                          facecolor=fill, edgecolor=color, linewidth=1.25, zorder=z)
        if dashed:
            p.set_linestyle((0, (5, 4)))
        else:
            p.set_sketch_params(.3, 170, 1.6)
        self.ax.add_patch(p)
        return p

    def group(self, x, y, w, h, label, fill, color=INK):
        self.box(x, y, w, h, fill, '#CFD8D1', z=1)
        self.text(x+17, y+23, label, 16, color, ha='left')

    def node(self, x, y, w, h, title, body='', color=INK, external=False, ai=False, size=21):
        self.box(x, y, w, h, '#F3F4F1' if external else '#FFFEFA', color, external)
        self.text(x+w/2, y+32, title, size, color)
        lines = body.split('\n') if body else []
        for i, line in enumerate(lines):
            self.text(x+w/2, y+67+i*26, line, 16, MUTED)
        if ai:
            self.box(x+w-36, y+7, 27, 20, '#ECE6F4', '#CFC2DE', z=6)
            self.text(x+w-22.5, y+17, 'AI', 10, PURPLE)

    def arrow(self, points, color=INK, label=None, at=None, both=False):
        path = MPath(points, [MPath.MOVETO]+[MPath.LINETO]*(len(points)-1))
        p = FancyArrowPatch(path=path, arrowstyle='<|-|>' if both else '-|>',
                            mutation_scale=15, linewidth=1.5, color=color,
                            zorder=5, capstyle='round', joinstyle='round')
        p.set_path_effects([pe.Stroke(linewidth=4, foreground=PAPER), pe.Normal()])
        self.ax.add_patch(p)
        if label:
            self.text(*at, label, 15, color,
                      bbox=dict(facecolor=PAPER, edgecolor='none', pad=3))

    def footer(self, value):
        self.text(45, 780, value, 16, MUTED, ha='left')

    def save(self, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        png_data = BytesIO()
        self.fig.savefig(png_data, format='png', dpi=140, facecolor=PAPER, edgecolor='none')
        self.fig.savefig(directory/(name+'.svg'), dpi=100, facecolor=PAPER,
                         edgecolor='none', metadata={'Date': None})
        plt.close(self.fig)
        svg = directory/(name+'.svg')
        value = re.sub(r'(?<![A-Za-z0-9])(-?\d+\.\d{3,})',
                       lambda m: format(float(m[1]), '.2f').rstrip('0').rstrip('.'),
                       svg.read_text())
        svg.write_text("\n".join(line.rstrip() for line in value.splitlines()) + "\n")
        png = directory/(name+'.png')
        png_data.seek(0)
        pending = directory/(name+'.pending.png')
        with Image.open(png_data) as image:
            image.quantize(colors=128, method=Image.Quantize.FASTOCTREE,
                           dither=Image.Dither.NONE).save(pending, optimize=True)
        pending.replace(png)


def interaction(font, out):
    d = Diagram(font, '第一层 · 人机交互', '实现原理：明确提交与即时提问分路，结果返回同一入口')
    d.group(390, 140, 360, 455, '本层模块 · ChatGPT 多端入口', '#F1F4EF')
    d.group(820, 140, 365, 455, '层外调用 · 工程与模型能力', '#EEF4F5', BLUE)
    d.node(50, 210, 270, 120, '明确提交', '对话片段 · 想法 · 资料\n限定本次保存范围')
    d.node(420, 210, 300, 120, '多端采集', '接收内容与来源上下文')
    d.node(845, 210, 315, 120, 'L3 采集适配', '取得内容 · 去重\n经 Git 写入并读回', external=True, color=BLUE)
    d.node(1250, 210, 300, 120, '保存回执', '原文已保存 / 正文未取得\n不等于知识已发布')
    d.node(50, 440, 270, 120, '即时提问', '主题 · 对象 · 时间范围')
    d.node(420, 440, 300, 120, '问答与确认', '接收问题 · 展示答案\n查看来源 · 提供反馈')
    d.node(845, 415, 315, 165, 'L3 证据读取', '取得相关证据\n↓\nL2 组织证据与回答', external=True, color=BLUE)
    d.node(1250, 440, 300, 120, '带依据的答案', '来源 · 时间 · 状态\n分歧与缺口')
    for y in [270, 500]:
        for a, b in [(320, 420), (720, 845), (1160, 1250)]:
            d.arrow([(a, y), (b, y)], color=BLUE if a >= 720 else INK)
    d.arrow([(1400, 560), (1400, 670), (780, 670), (780, 370), (570, 370), (570, 330)],
            color=ORANGE, label='确认、纠正与补充 → 明确提交后成为新证据', at=(1000, 699))
    d.footer('普通讨论不自动采集；保存状态与发布状态分别呈现。虚线节点为层外能力，具体执行由第三层负责。')
    d.save(out, 'layer-1-interaction')


def knowledge(font, out):
    d = Diagram(font, '第二层 · 个人知识与决策', '实现原理：有来源的候选 → 对照原文独立审核；问答按需组织证据')
    d.group(320, 125, 920, 180, '提取与领域加工  ·  AI', '#F3EFF6', PURPLE)
    d.node(40, 190, 225, 100, '限定输入', '原文＋相关旧记录', external=True, size=20)
    d.node(350, 185, 245, 105, '公共提取', '说话者 · 时间 · 含义', size=20)
    d.node(660, 185, 250, 105, '按需领域加工', '健康 / 事业 / 学习', size=20)
    d.node(975, 185, 240, 105, '合并候选', '记录与 Wiki 变更', size=20)
    d.node(1300, 185, 260, 105, 'L3 程序校验', '结构 · 引用 · 版本', external=True, color=BLUE, size=20)
    for a,b in [(265,350),(595,660),(910,975),(1215,1300)]:
        d.arrow([(a,240),(b,240)])
    d.node(60, 400, 365, 120, '审核依据', '原始证据＋相关旧记录\n规则与对应版本', external=True)
    d.node(630, 400, 345, 120, '独立 Review', '单次或对象 / 覆盖 / 整体\n核完整最终正文与版本', ai=True, color=PURPLE)
    d.node(1190, 400, 365, 120, '审核结果交第三层', '通过 → 发布控制\n返工 / 待确认 → 保存状态', external=True, color=BLUE, size=20)
    d.arrow([(150,290),(150,400)])
    d.arrow([(425,460),(630,460)], label='原文独立入审', at=(525,433))
    d.arrow([(1430,290),(1430,350),(800,350),(800,400)], color=BLUE,
            label='校验通过后，携候选调用 Review', at=(1100,350))
    d.arrow([(975,460),(1190,460)], label='绑定版本', at=(1080,433))
    d.group(40, 580, 1515, 145, '按需问答 · 不等待批次加工', '#EEF4F5', BLUE)
    d.node(60, 620, 365, 85, 'L3 提供证据集合', '正式记录＋相关新原文与控制', external=True, color=BLUE, size=20)
    d.node(630, 620, 345, 85, '组织证据与回答', '区分来源 · 时间 · 状态', ai=True, color=BLUE, size=20)
    d.node(1190, 620, 345, 85, '返回第一层', '答案＋引用＋资料缺口', external=True, color=BLUE, size=20)
    d.arrow([(425,663),(630,663)], color=BLUE)
    d.arrow([(975,663),(1190,663)], color=BLUE)
    d.footer('共同事实只提取一份，领域按需选择；模型不直接发布正式知识。独立 Review 不等于外部事实核验。')
    d.save(out, 'layer-2-knowledge')


def engineering(font, out):
    d = Diagram(font, '第三层 · 公共工程能力', '实现原理：流程编排调用模型，程序约束发布，持久状态支持中断续跑')
    d.node(50, 170, 300, 130, '流程编排', '人工触发后自动推进\n选增量 · 固定输入版本')
    d.node(450, 170, 300, 130, '调用 L2 加工', '提取与领域加工\n返回候选并保存', external=True, color=PURPLE)
    d.node(850, 170, 300, 130, '程序校验', '结构 · 引用 · 版本\n通过后才送审')
    d.node(1250, 170, 300, 130, '调用 L2 Review', '单次或独立分解节点\n返回精确版本审核证明', external=True, color=PURPLE, size=20)
    for a,b in [(350,450),(750,850),(1150,1250)]:
        d.arrow([(a,235),(b,235)])
    d.node(1250, 405, 300, 130, '发布控制', '完整证明 · 内容 · 路径\n基础版本一致才放行')
    d.node(850, 405, 300, 130, 'Git 读写适配器', '条件写入 · 一致提交\n远程读回后确认', color=BLUE, size=20)
    d.node(450, 405, 300, 130, 'L4 数据与存储', '正式记录＋Wiki\n运行与控制记录', external=True, color=BLUE, size=20)
    d.node(50, 405, 300, 130, '恢复与续跑', '编排内：核对已有结果\n只重做不能复用的阶段')
    d.arrow([(1400,300),(1400,405)], label='通过项', at=(1400,350))
    d.arrow([(1250,470),(1150,470)], label='变更包', at=(1200,442))
    d.arrow([(850,470),(750,470)], color=BLUE)
    d.arrow([(450,470),(350,470)], color=BLUE)
    d.arrow([(200,405),(200,300)], color=ORANGE, label='按条件继续', at=(200,350))
    d.text(790,350,'各阶段保存进度；失败、返工与待确认不阻塞无关通过项',16,MUTED)
    d.group(40, 595, 1520, 125, '按需访问 · 独立于批次加工', '#EEF4F5', BLUE)
    d.node(65, 635, 285, 65, '采集适配', '', size=20)
    d.node(450, 635, 300, 65, '证据读取', '', color=BLUE, size=20)
    d.text(1040,668,'共用 Git 读写适配器',20,BLUE)
    d.text(1390,668,'读取 / 保存原文',16,MUTED)
    d.arrow([(350,667),(390,667),(390,738),(1190,738),(1190,560),(1000,560),(1000,535)], color=BLUE)
    d.arrow([(930,535),(930,567),(800,567),(800,667),(750,667)], color=BLUE)
    d.footer('正式写入仅接受发布控制批准的变更包；模型调用是层外依赖。待确认登记后可结束本轮。')
    d.save(out, 'layer-3-engineering')


def storage(font, out):
    d = Diagram(font, '第四层 · 数据与存储', '实现原理：文件区分信息角色，稳定标识连接证据，Git 保存一致版本')
    d.group(40, 130, 1065, 555, '私有 Git / Markdown  ·  YAML 元数据＋正文', '#F5F2EB')
    d.node(75, 195, 400, 125, '原始资料', '实际取得的原文 / 链接\n来源 ID · 来源版本 · 片段')
    d.node(595, 195, 470, 125, '正式记录＋Wiki', '独立知识记录 ↔ 主题组织与综合\n各自保留版本与来源依据')
    d.node(75, 425, 400, 125, '候选变更', '待审核的记录与 Wiki 变更\n对象 ID 发布后沿用')
    d.node(595, 425, 470, 125, '运行与控制记录', '进度 · 审核 · 发布回执\n待确认 · 删除与禁止重建控制')
    d.arrow([(595,258),(475,258)], color=BLUE, label='引用原文', at=(535,228))
    d.arrow([(275,425),(275,368),(830,368),(830,320)],
            label='L3 校验 → L2 Review → L3 发布控制', at=(690,368))
    d.box(75,595,990,65,'#E3EDF3',BLUE)
    d.text(570,627,'基础版本检查 → 相关正式变更一致提交 → 远程读回确认',21,BLUE)
    d.node(1195, 330, 345, 150, '长期本地副本', '同步私有远程版本\n读取 · 回溯 · 恢复\n临时目录不承担唯一存储')
    d.arrow([(1065,627),(1145,627),(1145,405),(1195,405)], color=BLUE,
            label='同步', at=(1145,540))
    d.text(1360,205,'来源回执定位历史',20,BLUE)
    d.text(1360,245,'来源 ID＋版本 → 提交＋路径',16,MUTED)
    d.text(1360,280,'读取指定快照，再定位原文片段',16,MUTED)
    d.text(790,727,'目录映射以《数据规范》为准；候选、正式记录和运行目录在试跑中落实。',17,MUTED)
    d.footer('来源与内容版本不等于 Git 提交号；附件先存链接，普通删除保留历史。读写由第三层统一执行。')
    d.save(out, 'layer-4-storage')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    matplotlib.rcParams['svg.fonttype'] = 'path'
    matplotlib.rcParams['svg.hashsalt'] = 'pdm-layer-principles-v1'
    for render in [interaction, knowledge, engineering, storage]:
        render(args.font, args.output_dir)
