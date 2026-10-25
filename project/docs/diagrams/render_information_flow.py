#!/usr/bin/env python3
"""Render the four-layer information flow, with small model-call corner badges.

Requires matplotlib, Pillow and a CJK font; use --font to provide its path.
The diagram describes the target workflow, not an implemented runtime.
"""
from pathlib import Path
import argparse
import re
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, PathPatch
from matplotlib.path import Path as MPath
import matplotlib.patheffects as pe

W, H = 2480, 1580
PAPER = "#FCFBF7"
INK = "#40564D"
BLUE = "#477998"
ORANGE = "#B27847"
MUTED = "#718078"
MODEL = "#786398"

def render(font_path, output_dir):
    matplotlib.rcParams["svg.fonttype"] = "path"
    matplotlib.rcParams["svg.hashsalt"] = "personal-digital-memory-flow-v3"
    font = FontProperties(fname=str(font_path))
    fig = plt.figure(figsize=(W/100, H/100), dpi=100, facecolor=PAPER)
    ax = fig.add_axes([0,0,1,1])
    ax.set_xlim(0,W)
    ax.set_ylim(H,0)
    ax.set_axis_off()

    def text(x,y,value,size=19,color=INK,ha="center",**kwargs):
        return ax.text(x,y,value,fontsize=size,fontproperties=font,
                       color=color,ha=ha,va="center",zorder=8,**kwargs)

    def line(points,color=INK,lw=1.5,dashed=False,z=2,sketch=False):
        patch=PathPatch(MPath(points,[MPath.MOVETO]+[MPath.LINETO]*(len(points)-1)),
                        fill=False,edgecolor=color,lw=lw,
                        linestyle=(0,(5,5)) if dashed else "-",
                        capstyle="round",joinstyle="round",zorder=z)
        if sketch:
            patch.set_sketch_params(.35,180,1.4)
        ax.add_patch(patch)
        return patch

    def badge(x,y):
        # A small AI corner label: embedded inside the box, never a new node.
        ax.add_patch(FancyBboxPatch((x,y),30,22,
            boxstyle="round,pad=0,rounding_size=5",facecolor="#ECE6F4",
            edgecolor="#CFC2DE",lw=.65,zorder=7))
        text(x+15,y+11,"AI",size=11.5,color=MODEL)

    def node(x,y,title,body,width=340,height=116,color=INK,model=False):
        box=FancyBboxPatch((x,y),width,height,
            boxstyle="round,pad=0,rounding_size=12",facecolor="#FFFEFA",
            edgecolor=color,lw=1.45,zorder=5)
        box.set_sketch_params(.5,160,1.8)
        ax.add_patch(box)
        text(x+width/2,y+35,title,size=23,color=color)
        for i,s in enumerate(body.split("\n")):
            text(x+width/2,y+72+i*24,s,size=16.5,color=MUTED)
        if model:
            badge(x+width-41,y+10)

    def arrow(points,color=INK,dashed=False,label=None,at=None,size=16):
        path=MPath(points,[MPath.MOVETO]+[MPath.LINETO]*(len(points)-1))
        patch=FancyArrowPatch(path=path,arrowstyle="-|>",mutation_scale=17,
            lw=1.65,color=color,linestyle=(0,(6,5)) if dashed else "-",
            capstyle="round",joinstyle="round",zorder=3)
        patch.set_path_effects([pe.Stroke(linewidth=4.4,foreground=PAPER),pe.Normal()])
        ax.add_patch(patch)
        if label:
            text(*at,label,size=size,color=color,
                 bbox=dict(facecolor=PAPER,edgecolor="none",pad=3))

    text(64,62,"个人数字化记忆 · 信息流程",size=36,ha="left")
    text(66,111,"四条泳道对应四个架构层，箭头表示数据交接",size=20,color=MUTED,ha="left")
    text(2414,68,"dev11  /  人工触发  /  2026.10.19",size=16,color=MUTED,ha="right")
    for cx,label in [(510,"01  采集接入"),(1050,"02  自动加工"),
                     (1560,"03  审核发布"),(2110,"04  按需消费")]:
        text(cx,179,label,size=23)
        line([(cx-125,205),(cx+125,205)],color="#B4BFB2",lw=1.4,sketch=True)

    lanes=[
        (228,202,"第一层","人机交互","#FCF5E6","#9B824F"),
        (448,252,"第二层","个人知识\n与决策","#EEF4EC","#64816B"),
        (718,252,"第三层","公共工程\n能力","#EEF3F7","#648299"),
        (988,354,"第四层","数据与存储","#F3EFF7","#8A759D"),
    ]
    for y,h,level,name,fill,color in lanes:
        box=FancyBboxPatch((54,y),2368,h,boxstyle="round,pad=0,rounding_size=13",
                          facecolor=fill,edgecolor="#D9DED7",lw=.8,zorder=1)
        ax.add_patch(box)
        line([(270,y+18),(270,y+h-18)],color=color,lw=.9,z=2)
        text(162,y+h/2-30,level,size=20,color=color)
        text(162,y+h/2+24,name,size=23,color=color,linespacing=1.22)

    # One routine spans responsibilities in layers 2, 3 and 4.
    # Persistent stores remain readable outside a running batch.
    scope=FancyBboxPatch((832,444),998,888,
        boxstyle="round,pad=0,rounding_size=18",
        facecolor="none",edgecolor="#879173",lw=1.6,
        linestyle=(0,(8,5)),zorder=2.4)
    ax.add_patch(scope)
    text(1331,444,"知识加工任务编排",size=22,color="#6D795F",
         bbox=dict(facecolor=PAPER,edgecolor="none",pad=6))

    # Main handoffs use reserved vertical channels between aligned columns.
    arrow([(510,398),(510,790)],label="提交",at=(510,706))
    arrow([(510,906),(510,1060)],label="保存原文",at=(510,982))
    arrow([(680,1118),(780,1118),(780,848),(880,848)],
          label="待处理增量",at=(780,1016))
    arrow([(1010,790),(1010,646)],label="调用加工",at=(1010,738))
    arrow([(1220,588),(1270,588),(1270,878),(1220,878)],
          label="候选结果",at=(1270,738))
    arrow([(1050,906),(1050,1060)],label="保存候选",at=(1050,982))
    arrow([(1220,1118),(1324,1118),(1324,588),(1390,588)],
          label="候选＋证据",at=(1324,1016))
    arrow([(1560,646),(1560,790)],label="审核通过",at=(1560,738))
    arrow([(1560,906),(1560,1060)],label="发布通过项",at=(1560,982))
    arrow([(1730,1118),(1850,1118),(1850,852),(1940,852)],
          color=BLUE,label="正式知识",at=(1850,1016))
    arrow([(1940,340),(1894,340),(1894,819),(1940,819)],
          color=BLUE,label="问题",at=(1894,738))
    arrow([(2110,790),(2110,646)],color=BLUE,label="证据与状态",at=(2110,738))
    arrow([(2110,530),(2110,398)],color=BLUE,label="回答＋来源",at=(2110,466))

    # Exception routes stay outside the main node-to-node channels.
    arrow([(1560,530),(1560,487),(1050,487),(1050,530)],
          color=ORANGE,dashed=True,label="需修改 → 重新加工",at=(1300,487))
    arrow([(1730,588),(1780,588),(1780,1257),(1730,1257)],
          color=ORANGE,dashed=True,label="需澄清",at=(1780,1198))
    arrow([(1730,1280),(1815,1280),(1815,884),(1940,884)],
          color=ORANGE,dashed=True)
    arrow([(2110,282),(2110,251),(510,251),(510,282)],
          color=ORANGE,dashed=True,label="确认 / 纠错 / 补充 → 作为新输入",at=(1300,251))

    # Raw data may inform answers while retaining its original/unreviewed state.
    arrow([(510,1176),(510,1382),(2370,1382),(2370,852),(2280,852)],
          color=BLUE,dashed=True,
          label="未加工原文也可参与回答 · 须标明状态",at=(1400,1382),size=18)

    node(340,282,"明确提交 / 导入","对话 · 想法 · 文章\n视频 / 附件链接")
    node(1940,282,"提问与查看","查证问答 · 当前 / 历史\n来源与待确认事项")
    node(880,530,"提取与领域加工","公共提取 · 按需领域视角\n健康 / 事业 / 学习",model=True)
    node(1390,530,"模型 Review","单次或分解 · 精确内容版本\n原文、覆盖与整体一致性",model=True)
    node(1940,530,"组织证据与回答","注明来源、时间和状态\n资料不足时说明缺口",color=BLUE,model=True)
    node(340,790,"接入与保存","规范来源 · 去重\n保存成功后返回回执")
    node(880,790,"人工触发与流程控制","用户启动 · 自动加工\n读增量 · 调用 · 分支 / 重试")
    node(1390,790,"统一发布","校验审核对应版本\nGit 提交与成功回执")
    node(1940,790,"按需读取文件","目录 / 引用 / 文本搜索\n第一版不建专门索引",color=BLUE)
    node(340,1060,"原始资料 raw","原文 / 链接 / 来源 / 版本")
    node(880,1060,"候选 staging","待审核记录与 Wiki 变更")
    node(1390,1060,"正式记录 ＋ Wiki","独立记录与主题引用")
    node(1390,1222,"待确认清单","问题 · 依据 · 回复状态",height=90,color=ORANGE)
    text(2110,1090,"Git 保留版本与运行记录",size=19,color="#867196")
    text(2110,1132,"来源 · Review · 批次 · 发布",size=17,color=MUTED)
    text(2110,1164,"纠错与删除控制",size=17,color=MUTED)
    text(2110,1245,"附件只保存链接",size=19,color="#867196")
    text(2110,1277,"附件本体仍在原存放位置",size=17,color=MUTED)

    # Compact, aligned legend. AI indicates inference, not routine I/O.
    for x,c,d,label in [(340,INK,False,"主流程"),(665,BLUE,True,"原文参与回答"),(1080,ORANGE,True,"返工与确认")]:
        line([(x,1441),(x+62,1441)],color=c,lw=1.7,dashed=d,z=6)
        text(x+78,1441,label,size=17,color=c,ha="left")
    badge(1535,1430)
    text(1582,1441,"需要模型调用",size=17,color=MODEL,ha="left")
    text(66,1495,"虚线大框横跨第二至四层，表示知识加工任务范围；框内数据持久保存，可随时读取。",size=17,color=MUTED,ha="left")
    text(66,1534,"Review 不等于独立事实核验；回答不自动成为独立证据。图示为目标流程，完整链路仍待试跑。",size=17,color=MUTED,ha="left")

    output_dir.mkdir(parents=True,exist_ok=True)
    for ext in ["png","svg"]:
        fig.savefig(output_dir/("information-flow-swimlane."+ext),
                    dpi=135 if ext=="png" else 100,facecolor=PAPER,edgecolor="none",
                    metadata={"Date":None} if ext=="svg" else None)
    plt.close(fig)
    path=output_dir/"information-flow-swimlane.svg"
    value=path.read_text()
    value=re.sub(r"(?<![A-Za-z0-9])(-?\d+\.\d{3,})",
                 lambda m:format(float(m[1]),".2f").rstrip("0").rstrip("."),value)
    path.write_text("\n".join(line.rstrip() for line in value.splitlines()) + "\n")
    path=output_dir/"information-flow-swimlane.png"
    with Image.open(path) as img:
        img.quantize(colors=128,method=Image.Quantize.FASTOCTREE,
                     dither=Image.Dither.NONE).save(path,optimize=True)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--font",required=True,type=Path,help="Path to a CJK font, e.g. LXGW WenKai TC")
    p.add_argument("--output-dir",type=Path,default=Path(__file__).resolve().parent)
    a=p.parse_args()
    render(a.font,a.output_dir)
