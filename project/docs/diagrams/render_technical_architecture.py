#!/usr/bin/env python3
"""Render the four-layer V1 architecture aligned with technical-plan.md.

Requires matplotlib, Pillow and a CJK font. Modules describe target responsibilities.
"""
from pathlib import Path
import argparse
import re
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.path import Path as MPath
import matplotlib.patheffects as pe

W, H = 2240, 1600
PAPER = '#FCFBF7'
INK = '#40564D'
MUTED = '#718078'
BLUE = '#477998'
MODEL = '#786398'
ORANGE = '#A57243'


def render(font_path, output_dir):
    matplotlib.rcParams['svg.fonttype'] = 'path'
    matplotlib.rcParams['svg.hashsalt'] = 'pdm-layered-architecture-v1'
    font = FontProperties(fname=str(font_path))
    fig = plt.figure(figsize=(W/100, H/100), dpi=100, facecolor=PAPER)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.set_axis_off()

    def text(x, y, s, size=19, color=INK, ha='center', **kwargs):
        return ax.text(x, y, s, fontsize=size, fontproperties=font, color=color,
                       ha=ha, va='center', zorder=8, **kwargs)

    def box(x, y, w, h, fill='#FFFEFA', edge=INK, lw=1.3, sketch=True, z=4):
        p = FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0,rounding_size=12',
                          facecolor=fill, edgecolor=edge, lw=lw, zorder=z)
        if sketch:
            p.set_sketch_params(.4, 170, 1.6)
        ax.add_patch(p)
        return p

    def badge(x, y):
        box(x, y, 30, 23, '#ECE6F4', '#CFC2DE', .65, False, 7)
        text(x+15, y+11.5, 'AI', 11.5, MODEL)

    def node(x, y, w, h, title, body, ai=False, color=INK, title_size=22):
        box(x, y, w, h, edge=color)
        text(x+w/2, y+43, title, title_size, color)
        for i, s in enumerate(body.split('\n')):
            text(x+w/2, y+78+i*27, s, 16, MUTED)
        if ai:
            badge(x+w-42, y+8)

    def arrow(points, color=INK, both=False, label=None, at=None, rotate=0):
        path = MPath(points, [MPath.MOVETO]+[MPath.LINETO]*(len(points)-1))
        p = FancyArrowPatch(path=path, arrowstyle='<|-|>' if both else '-|>',
                            mutation_scale=17, lw=1.7, color=color, zorder=5,
                            capstyle='round', joinstyle='round')
        p.set_path_effects([pe.Stroke(linewidth=4.5, foreground=PAPER), pe.Normal()])
        ax.add_patch(p)
        if label:
            text(*at, label, size=16, color=color, rotation=rotate,
                 bbox=dict(facecolor=PAPER, edgecolor='none', pad=3))

    def layer(y, h, number, title, desc, fill):
        box(60, y, 2120, h, fill, '#D5DDD5', .8, False, 1)
        text(85, y+36, number, 16, MUTED, ha='left')
        text(160, y+h/2-8, title, 24, linespacing=1.4)
        text(160, y+h-28, desc, 14.5, MUTED)
        ax.plot([260, 260], [y+22, y+h-22], color='#D5DDD5', lw=.85, zorder=2)

    text(70, 61, '个人数字化记忆 · 分层技术架构', 35, ha='left')
    text(72, 109, '四层职责 · 模块边界 · 关键调用与数据流', 21, MUTED, ha='left')
    text(2170, 61, 'dev11 人工批  /  2026.10.19', 16, MUTED, ha='right')
    badge(2000, 97)
    text(2043, 109, '模型调用', 16, MODEL, ha='left')

    layer(180, 170, '第一层 / L1', '人机交互', 'ChatGPT 多端', '#F1F4EF')
    layer(400, 250, '第二层 / L2', '个人知识\n与决策', 'V1 聚焦知识库', '#F3EFF6')
    layer(700, 400, '第三层 / L3', '公共工程\n能力', '受控执行环境', '#EEF4F5')
    layer(1150, 300, '第四层 / L4', '数据与\n存储', '文件与版本历史', '#F5F2EB')

    # A task boundary spans responsibility layers; capture and Q&A remain outside.
    scope = box(635, 380, 1100, 1040, 'none', '#B99970', 1.35, False, 3)
    scope.set_linestyle((0, (6, 5)))
    text(1185, 380, '人工启动后的加工与发布范围', 19, ORANGE,
         bbox=dict(facecolor=PAPER, edgecolor='none', pad=5))

    node(290, 205, 310, 120, '多端采集', '对话片段 · 想法 · 资料')
    node(1770, 205, 310, 120, '问答与确认', '提问 · 看来源 · 确认 / 纠正')
    arrow([(1770, 266), (600, 266)], label='确认、纠正与补充作为新证据', at=(1185, 237))

    node(660, 450, 310, 135, '提取与领域加工', '公共提取 · 按需领域视角\n产出候选记录与 Wiki', ai=True, title_size=21)
    node(1125, 450, 420, 135, '独立 Review', '单次或对象 / 覆盖 / 整体审核\n核精确最终内容与版本', ai=True)
    node(1770, 450, 310, 135, '组织证据与回答', '带来源 · 时间 · 状态\n说明分歧与资料缺口', ai=True, color=BLUE, title_size=21)

    node(290, 800, 310, 120, '采集适配', '取得内容 · 去重\n保存后返回回执')
    node(660, 800, 310, 120, '流程编排', '人工启动 · 增量与版本\n阶段保存 · 失败续跑')
    node(1030, 800, 310, 120, '程序校验', '结构 · 引用 · 版本\n校验通过才送审')
    node(1400, 800, 310, 120, '发布控制', '完整证明 · 路径与版本\n只发布通过项')
    node(1770, 800, 310, 120, '证据读取', '正式知识＋相关新原文\n检查纠正与删除', color=BLUE)

    # Processing sequence crosses layers without granting models a write path.
    arrow([(445, 325), (445, 800)], label='明确提交', at=(445, 685))
    arrow([(815, 800), (815, 585)], label='① 调用', at=(815, 685))
    arrow([(970, 518), (1000, 518), (1000, 750), (1185, 750), (1185, 800)],
          label='② 候选', at=(1090, 726))
    arrow([(1260, 800), (1260, 585)], label='③ 校验通过', at=(1260, 685))
    arrow([(1450, 585), (1450, 750), (1555, 750), (1555, 800)],
          label='④ 审核通过', at=(1450, 685))

    # On-demand read/answer flow is distinct from the daily processing scope.
    arrow([(2080, 265), (2130, 265), (2130, 860), (2080, 860)],
          color=BLUE, label='提问', at=(2130, 685), rotate=90)
    arrow([(1925, 800), (1925, 585)], color=BLUE, label='证据集合', at=(1925, 685))
    arrow([(1925, 450), (1925, 325)], color=BLUE, label='回答', at=(1925, 380))

    box(290, 1000, 1790, 65, '#E3EDF3', '#7492A7', 1.3)
    text(1185, 1033, 'Git 读写适配器  ｜  按版本读取 · 条件写入 · 提交后读回', 23, BLUE)
    arrow([(445, 920), (445, 1000)], color=BLUE, label='保存原文', at=(445, 961))
    arrow([(815, 920), (815, 1000)], color=BLUE, both=True,
          label='输入 / 阶段结果', at=(815, 961))
    arrow([(1555, 920), (1555, 1000)], label='⑤ 发布', at=(1555, 961))
    arrow([(1925, 1000), (1925, 920)], color=BLUE, label='读取证据', at=(1925, 961))

    # Storage names match the layer/module table in the technical plan.
    box(275, 1195, 1455, 190, '#FAF8F2', '#CFC2AC', .9, False, 2)
    text(290, 1178, '私有 Git / Markdown', 17, MUTED, ha='left')
    for x, title, body in [
        (290, '原始资料', '原文 · 链接 · 来源版本'),
        (660, '候选变更', '待审核的记录与 Wiki'),
        (1030, '正式记录＋Wiki', '独立知识 · 主题综合'),
        (1400, '运行与控制记录', '进度 / 审核 / 回执\n待确认 / 删除控制'),
    ]:
        node(x, 1225, 310, 135, title, body, title_size=21)
    arrow([(1185, 1065), (1185, 1195)], color=BLUE, both=True,
          label='读写文件与版本', at=(1185, 1125))
    node(1800, 1225, 280, 135, '长期本地副本', '同步 · 读取 · 恢复', title_size=21)
    arrow([(1730, 1292), (1800, 1292)], color=BLUE)

    text(70, 1500, '深色线：处理交接    蓝线：数据读写与回答    虚线框：知识加工范围（跨第二至第四层）',
         18, MUTED, ha='left')
    text(70, 1545, '逻辑分层不等于独立部署；模型产出候选和审核意见，工程层控制正式发布。采集与即时问答独立触发。',
         18, MUTED, ha='left')
    output_dir.mkdir(parents=True, exist_ok=True)
    for ext in ['png', 'svg']:
        fig.savefig(output_dir/('technical-architecture.'+ext), dpi=135 if ext == 'png' else 100,
                    facecolor=PAPER, edgecolor='none',
                    metadata={'Date': None} if ext == 'svg' else None)
    plt.close(fig)
    svg = output_dir/'technical-architecture.svg'
    value = re.sub(r'(?<![A-Za-z0-9])(-?\d+\.\d{3,})',
                   lambda m: format(float(m[1]), '.2f').rstrip('0').rstrip('.'), svg.read_text())
    svg.write_text("\n".join(line.rstrip() for line in value.splitlines()) + "\n")
    png = output_dir/'technical-architecture.png'
    with Image.open(png) as img:
        img.quantize(colors=128, method=Image.Quantize.FASTOCTREE,
                     dither=Image.Dither.NONE).save(png, optimize=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--font', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    a = p.parse_args()
    render(a.font, a.output_dir)
