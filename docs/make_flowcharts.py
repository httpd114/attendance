# -*- coding: utf-8 -*-
"""生成「改造前 / 改造后」两张业务流程图（SVG + PNG）。

用法：
    python docs/make_flowcharts.py

依赖：仅 Python 标准库；PNG 由本机 Chrome / Edge 无头截图生成。
若本机没有浏览器，脚本仍会生成 SVG，并提示可用浏览器打开后另存为 PNG。

内容依据（全部来自本项目已有材料，未新增业务事实）：
  - docs/discovery-report.md                     f01 主链路、f10~f55 事实编号
  - data/派遣岗位需求单现状流程图v1.html          现状 13 步 / 5 处断点
  - scripts/docx-build/tobe-flow.html           To-Be 5 处断点的解法
  - docs/典型事件AI整理纪要.md                   FSG-SIM-2026-1021-047 时间线与量化分级
  - 派遣业务AI数智化需求分析方案 v1.0            AI-01~AI-10、红线、G0 门禁、状态机
"""
from __future__ import annotations

import html
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
W = 1680
LEFT_X, LEFT_W = 95, 790
RIGHT_X, RIGHT_W = 930, 660
TOP = 168
GAP = 24

# 配色：白底、深色文字，色块只做语义区分
TONE = {
    "normal": ("#E6F1FB", "#185FA5", "#0C447C"),
    "ai":     ("#E1F5EE", "#0F6E56", "#085041"),
    "human":  ("#FAEEDA", "#854F0B", "#633806"),
    "risk":   ("#FCEBEB", "#A32D2D", "#791F1F"),
    "start":  ("#F1EFE8", "#5F5E5A", "#2C2C2A"),
    "fixed":  ("#EAF3DE", "#639922", "#3B6D11"),
    "neutral": ("#F8FAFC", "#94A3B8", "#475569"),
}
# 不加内层引号：CSS 允许含空格的字体族名，避免 XML 属性中出现 &quot; 实体
FONT = "Microsoft YaHei,PingFang SC,Noto Sans CJK SC,sans-serif"


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


class Canvas:
    def __init__(self, w: int, h: int, title: str, subtitle: str, source: str):
        self.w, self.h = w, h
        self.p: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" font-family="{FONT}">',
            '<defs>',
            '<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto-start-reverse">'
            '<path d="M1 1L9 5L1 9" fill="none" stroke="#6B7280" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></marker>',
            '<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto-start-reverse">'
            '<path d="M1 1L9 5L1 9" fill="none" stroke="#A32D2D" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></marker>',
            '<marker id="arg" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto-start-reverse">'
            '<path d="M1 1L9 5L1 9" fill="none" stroke="#639922" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></marker>',
            '</defs>',
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#FFFFFF"/>',
        ]
        self.text(70, 52, title, 29, "#111827", bold=True)
        self.text(70, 86, subtitle, 14.5, "#6B7280")
        self.text(70, 112, source, 12.5, "#9CA3AF")
        self.line(70, 132, w - 70, 132, "#E5E7EB")

    def text(self, x, y, s, size=14, color="#334155", bold=False, anchor="start"):
        self.p.append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'font-weight="{"700" if bold else "400"}" text-anchor="{anchor}">{esc(s)}</text>'
        )

    def line(self, x1, y1, x2, y2, color="#6B7280", width=1.5, dashed=False, marker=""):
        da = ' stroke-dasharray="5,4"' if dashed else ""
        mk = f' marker-end="url(#{marker})"' if marker else ""
        self.p.append(
            f'<path d="M{x1} {y1}L{x2} {y2}" fill="none" stroke="{color}" '
            f'stroke-width="{width}"{da}{mk}/>'
        )

    def box(self, x, y, w, h, title, sub="", tone="normal", radius=9,
            title_size=16.5, sub_size=12.5, sw=1.2):
        fc, ec, tc = TONE[tone]
        self.p.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
            f'fill="{fc}" stroke="{ec}" stroke-width="{sw}"/>'
        )
        ty = y + (26 if sub else h / 2 + 6)
        self.text(x + 20, ty, title, title_size, tc, bold=True)
        if sub:
            self.text(x + 20, y + 48, sub, sub_size, ec)

    def diamond(self, cx, cy, w, h, title, tone="human"):
        fc, ec, tc = TONE[tone]
        pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        self.p.append(f'<polygon points="{pts}" fill="{fc}" stroke="{ec}" stroke-width="1.2"/>')
        self.text(cx, cy + 5, title, 15.5, tc, bold=True, anchor="middle")

    def badge(self, x, y, label, tone="risk", w=112):
        fc, ec, tc = TONE[tone]
        self.p.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="23" rx="11.5" '
            f'fill="{fc}" stroke="{ec}" stroke-width="0.8"/>'
        )
        self.text(x + w / 2, y + 15.5, label, 11.5, tc, bold=True, anchor="middle")

    def seq(self, cx, cy, n):
        self.p.append(f'<circle cx="{cx}" cy="{cy}" r="14" fill="#FFFFFF" stroke="#94A3B8" stroke-width="1.2"/>')
        self.text(cx, cy + 4.5, n, 12, "#475569", bold=True, anchor="middle")

    def branch_card(self, cy, label, title, sub, tone="risk", h=84, marker="arr"):
        """右侧异常分支卡片，与左侧判断点同一水平线。"""
        y = cy - h / 2
        self.line(RIGHT_X - 22, cy, RIGHT_X - 4, cy,
                  "#A32D2D" if tone == "risk" else "#639922", 1.6, marker=marker)
        self.text(RIGHT_X - 60, cy - 10, label, 12, "#6B7280", anchor="middle")
        self.box(RIGHT_X, y, RIGHT_W, h, title, sub, tone=tone,
                 title_size=15, sub_size=12, radius=8)

    def save(self, path: Path):
        self.p.append("</svg>")
        path.write_text("\n".join(self.p), encoding="utf-8")
        return path


def render_nodes(c: Canvas, nodes: list[dict], badge_tone: str) -> int:
    """按纵向顺序渲染主链，返回主链底部 y 坐标。"""
    cx = LEFT_X + LEFT_W / 2
    prev_bottom = None
    y = TOP
    for idx, n in enumerate(nodes, 1):
        is_dia = n["type"] == "diamond"
        h = 76 if is_dia else 62
        cy = y + h / 2
        if prev_bottom is not None:
            c.line(cx, prev_bottom + 2, cx, y - 7, marker="ar")
        if is_dia:
            dw = 560
            c.diamond(cx, cy, dw, h, n["title"], tone=n.get("tone", "human"))
            right_edge = cx + dw / 2
        else:
            c.box(LEFT_X, y, LEFT_W, h, n["title"], n.get("sub", ""),
                  tone=n.get("tone", "normal"))
            right_edge = LEFT_X + LEFT_W
        c.seq(70, cy, idx)
        if n.get("badge"):
            c.badge(LEFT_X + LEFT_W - 130, y + 6, n["badge"], tone=badge_tone, w=118)
        if n.get("branch"):
            b = n["branch"]
            c.line(right_edge + 2, cy, RIGHT_X - 24, cy,
                   "#A32D2D" if b.get("tone", "risk") == "risk" else "#639922",
                   1.4, dashed=True)
            c.branch_card(cy, b["label"], b["title"], b["sub"],
                          tone=b.get("tone", "risk"),
                          h=b.get("h", 84),
                          marker="arr" if b.get("tone", "risk") == "risk" else "arg")
        prev_bottom = y + h
        y += h + GAP
    return prev_bottom


AS_IS = [
    dict(type="box", tone="start", title="客户提出或变更岗位需求",
         sub="电话 / 群消息为主 · 实际链路 f01：提需求 → 条件澄清 → 人员安排 → 到岗交接 → 反馈"),
    dict(type="diamond", title="变更是否正式留单？",
         branch=dict(label="否", title="14:12 群消息同步一句，现场先解决",
                     sub="先解决后补单；口头 v4 无书面留痕 · f10 f13 f22")),
    dict(type="box", title="15:40 事后补填需求单表格", badge="断点 1",
         sub="回填时间被当作业务发生时间，与 14:07 相差 93 分钟 · f13 f34"),
    dict(type="box", title="三类工具分散登记",
         sub="服务台 / 台账 / 文件库，无统一关联编号 · f30"),
    dict(type="box", title="人工跨工具比对找最新版本",
         sub="秦芮：版本断链已识别，只能人工看字段 · f33"),
    dict(type="diamond", title="能否确认最新版本？",
         branch=dict(label="否", title="按文件名或时间戳猜「最新版」",
                     sub="缺版本有效期 + 确认状态双条件 · f24 · 断点 2")),
    dict(type="box", title="猜版本结论进入交接", badge="断点 2",
         sub="靠人判断哪一版算数，系统无强制校验 · f24 f23"),
    dict(type="box", title="生成岗位交接单（仍挂 v3）",
         sub="唐映秋：每次交接记录接收人与版本，但版本本身可能已过期 · f20"),
    dict(type="diamond", title="交接单同步了最新技能要求？",
         branch=dict(label="否", title="交接单引用旧技能要求（核心瓶颈）",
                     sub="v3 已确认 vs 口头 v4 未确认；技能条件、班次 2 个字段差异 · f10 f11")),
    dict(type="box", title="条件不一致仍向下流转", badge="断点 3",
         sub="30 岗位批次、8 名待到岗中 3 名已按旧班次回执 · f02 f11（演练设定值）"),
    dict(type="box", title="一线专员提交变更",
         sub="可以提交，但没有正式确认权 · f22"),
    dict(type="diamond", title="业务负责人是否正式确认？",
         branch=dict(label="否", title="挂起、退回补充",
                     sub="未确认项不得归到对方名下；口头改动一律视为未确认 · f25 f22")),
    dict(type="box", title="到岗交接：只发「已处理」", badge="断点 4",
         sub="缺最新版本、未确认事项、截止时间三要素 · f23 f14"),
    dict(type="diamond", title="闭环前现场是否发现条件不一致？",
         branch=dict(label="是", title="16:05 人工发现 → 16:41 复核关闭",
                     sub="逐个核对 3 人、2 张单返工、36 分钟人工核对、4 条影响清单 · f14 f15 f45", h=92)),
    dict(type="box", title="投诉登记与周度复盘", badge="断点 5",
         sub="审计只留最终摘要、只改模板不核效果 · f45 f54 f55"),
    dict(type="box", tone="start", title="结束 · 进入下一批次需求",
         sub="14:07 发生 → 16:41 关闭，处置跨度 154 分钟 · 试点边界：只读导出，不自动决定派遣资格 f05"),
]

AS_IS_TIMELINE = [
    ("14:00 前", "v3 已确认", "普通话流利、收银经验 1 年以上；早班 08:00—16:00"),
    ("14:07", "电话临时加夜班", "客户口头 v4：可接受 22:00—06:00；无书面留痕"),
    ("14:12", "群消息同步一句", "现场同事在群里口头转述，先解决后补单"),
    ("15:40", "补填进表格", "系统接收时间；若当作发生时间，异常识别顺序会反转"),
    ("16:05", "闭环前人工发现", "交接单仍挂 v3，缺夜班条件；开始逐个核对"),
    ("16:41", "复核关闭", "挂未结项、留痕原值与来源位置，形成 4 条影响清单"),
]

TO_BE = [
    dict(type="box", tone="start", title="三入口统一接入（电话 / 群消息 / 正式需求单）",
         sub="所有变更先有登记入口，不再让系统最后一个知道 · f01 f10"),
    dict(type="diamond", title="变更是否在规定时限补单？",
         branch=dict(label="否", title="超时升级业务负责人，暂停该岗位交接",
                     sub="先暂停、不默认放行，避免旧条件继续流转 · f44 · R-05")),
    dict(type="box", tone="ai", title="统一编号登记 · 双时间轴入库", badge="解断点 1 · AI-03",
         sub="业务发生时间 / 系统接收时间 / 确认状态三者分离；有效性判定用规则不用模型 · f30 f34"),
    dict(type="box", tone="ai", title="证据锚定抽取 → 一页缺项清单", badge="AI-01",
         sub="六项关键字段逐条回填来源文件 + 原文字符区间，不输出长摘要 · f15 f42"),
    dict(type="diamond", title="关键字段是否全部锚定？",
         branch=dict(label="否", title="人工核查队列（具名接收人）",
                     sub="缺口清单 + 来源位置一并提交，不生成交接单 · f44 · AI-01 门禁")),
    dict(type="box", tone="ai", title="版本双条件判定", badge="解断点 2 · AI-02",
         sub="版本有效期 AND 确认状态；文件名与时间戳不参与判定 · f24（G0 条件准入）"),
    dict(type="diamond", title="是否已存在确认的新版本？",
         branch=dict(label="是", title="旧版本标记作废 · 交接单强制刷新",
                     sub="保留作废原因，过期来源不得覆盖已确认版本 · f34", tone="fixed")),
    dict(type="box", tone="ai", title="生成岗位交接单", badge="解断点 3·4",
         sub="最新版本 + 未确认项 + 截止时间，三项缺一不发 · f23；变更影响清单只给候选不判责 · AI-02"),
    dict(type="diamond", title="业务负责人是否具名确认？",
         branch=dict(label="否", title="退回补充，记录退回人与时间",
                     sub="未确认项不得挂对方名下；模型无审批权 · f25 f22 · HC-A")),
    dict(type="box", tone="human", title="交接单发布 · 接收人签收",
         sub="接收人、版本、未结项全部落库可反查 · f20 f45"),
    dict(type="diamond", title="现场是否发现条件不一致？",
         branch=dict(label="是", title="差异登记 · 版本回滚",
                     sub="差异本身成为下一轮输入，不再靠人脑回溯原值 · f11", tone="fixed")),
    dict(type="box", tone="ai", title="审计留痕 + 周度复盘", badge="解断点 5 · AI-10",
         sub="误报 / 漏报 / 未确认项共同复盘；只改模板不核效果不算收口 · f45 f55"),
    dict(type="box", tone="ai", title="场景 B：制度知识库 RAG 问答", badge="AI-04",
         sub="BM25 + 中文字符二元组 + 向量三路融合；答案带引用编号，制度外问题拒答"),
    dict(type="box", tone="ai", title="风险规则预筛 → 人工复核队列", badge="AI-06",
         sub="命中关键词 / 正则只写 audit_flags(suspected)，系统永不自动判违规 · HC-01"),
    dict(type="box", tone="ai", title="Agent 确认闭环（最多 5 轮）", badge="AI-08 · AI-07",
         sub="证据校验 → 制度合规校验 → 建草稿 → 提交；一次性 HMAC 能力令牌 + 服务端状态机门禁"),
    dict(type="box", tone="human", title="管理员人工复核（Agent 权限天花板 SUBMITTED）",
         sub="复核通过 / 驳回 / 关闭 / 结算导出不注册为工具，全部在后台人工完成 · HC-05 · R-03"),
]

GUARD = [
    ("R-01 / R-02", "不自动决定派遣资格；口头变更一律视为未确认，须补单并经业务负责人确认"),
    ("R-03 / R-05", "AI 不是审批者，超时只升级不自动通过；无法确认版本即进人工队列"),
    ("R-07 / R-08", "试点仅脱敏只读导出，不写回三类工具；一期不直连薪资、不开票"),
    ("G0 门禁", "AI-02 / AI-05 须先取 3~5 个真实电话变更样本，补证未通过不得开工"),
]


def legend(c: Canvas, y: int, items):
    c.line(70, y - 22, W - 70, y - 22, "#E5E7EB")
    x = 70
    for tone, label in items:
        fc, ec, _ = TONE[tone]
        c.p.append(f'<rect x="{x}" y="{y}" width="26" height="16" rx="3" fill="{fc}" stroke="{ec}" stroke-width="1"/>')
        c.text(x + 34, y + 13, label, 12.5, "#475569")
        x += 34 + len(label) * 13 + 44


def disclaimer(c: Canvas, y: int, text: str):
    c.p.append(f'<rect x="70" y="{y}" width="{W - 140}" height="40" rx="6" fill="#F8FAFC" stroke="#E2E8F0" stroke-width="1"/>')
    c.text(86, y + 25, text, 12.5, "#64748B")


def build_as_is() -> tuple[Canvas, int]:
    c = Canvas(W, 1930,
               "图 1 · 改造前：岗位服务需求单现状流程（As-Is）",
               "13 步主链 / 5 处断点 / 4 个判断分支；以典型事件 FSG-SIM-2026-1021-047 的 14:07 → 16:41 时间线贯穿标注",
               "依据：discovery-report.md（f01 f10~f55）· 派遣岗位需求单现状流程图 v1 · 典型事件 AI 整理纪要 · 需求分析方案 v1.0 §1.1 / §2")
    bottom = render_nodes(c, AS_IS, badge_tone="risk")

    ty = bottom + 58
    c.text(70, ty - 18, "典型事件时间线 · FSG-SIM-2026-1021-047（AI 重建，须人工复核；全部为演练设定值）",
           16, "#111827", bold=True)
    for i, (t, title, sub) in enumerate(AS_IS_TIMELINE):
        x = 70 + i * 260
        c.p.append(f'<rect x="{x}" y="{ty}" width="248" height="104" rx="8" fill="#F8FAFC" stroke="#CBD5E1" stroke-width="1"/>')
        c.text(x + 14, ty + 24, t, 14, "#A32D2D", bold=True)
        c.text(x + 14, ty + 48, title, 14, "#0F172A", bold=True)
        # 说明文字按 17 字折成两行，避免超出 248px 卡片
        c.text(x + 14, ty + 68, sub[:17], 11.5, "#475569")
        if len(sub) > 17:
            c.text(x + 14, ty + 86, sub[17:34], 11.5, "#475569")
    qy = ty + 136
    c.text(70, qy, "量化口径（B 级演练设定值，禁止作 ROI 输入与验收基线）", 14, "#111827", bold=True)
    for i, (num, desc) in enumerate([
        ("36 分钟", "3 人 × 约 12 分钟人工核对"),
        ("93 分钟", "发生 14:07 与回填 15:40 混淆"),
        ("154 分钟", "14:07 → 16:41 处置跨度"),
        ("3 / 8 人", "按 v3 回执状态口径计数，非估算"),
    ]):
        x = 70 + i * 390
        c.p.append(f'<rect x="{x}" y="{qy + 14}" width="372" height="52" rx="7" fill="#FCEBEB" stroke="#F3C6C6" stroke-width="1"/>')
        c.text(x + 14, qy + 47, num, 19, "#A32D2D", bold=True)
        c.text(x + 122, qy + 45, desc[:22], 11, "#791F1F")
    ry = qy + 84
    c.p.append(f'<rect x="70" y="{ry}" width="{W - 140}" height="48" rx="7" fill="#FFF7ED" stroke="#FCD9A8" stroke-width="1"/>')
    c.text(86, ry + 20, "AI 归纳的四层根因链（推断，须 HC-C 书面采信）：L1 口头变更缺强制补单留痕 → L2 版本有效期与确认状态无系统级校验 → L3 三类工具无统一关联编号 → L4 辅助结果只给长摘要不给缺项清单", 12.5, "#9A5B13")
    c.text(86, ry + 38, "L1 在两份调研报告中证据强度冲突（36/36 列为 P0 根因，6/36 指为转述无独立证据），G0 补证前不得定级；「80 单 / 重复澄清 22%」为虚构基线 f12，本图未用于任何结论", 12, "#9A5B13")

    legend(c, ry + 88, [("normal", "常规步骤"), ("human", "判断点（分支）"),
                        ("risk", "断点 / 异常路径"), ("start", "起止"), ("neutral", "演练数据标注")])
    disclaimer(c, ry + 122,
               "性质声明：受访者、单号、时间点、人数与耗时均为合成演练设定，不代表任何真实企业经营数据；本图只作流程评审输入，不构成开发承诺或验收依据。")
    return c, ry + 172


def build_to_be() -> tuple[Canvas, int]:
    c = Canvas(W, 1930,
               "图 2 · 改造后：AI 数智化后的岗位服务需求单流程（To-Be）",
               "5 处断点逐一关闭：补单留痕 → 证据锚定 → 双条件判版 → 具名审批 → 全程审计；只读接入，不自动决定派遣资格",
               "依据：派遣业务 AI 数智化需求分析方案 v1.0 §3.2（AI-01~AI-10）/ §4.2 红线 / §4.4 状态机 · To-Be 流程图 v1 · 上线交付运行手册 HC 控制点")
    bottom = render_nodes(c, TO_BE, badge_tone="fixed")

    gy = bottom + 52
    c.text(70, gy - 16, "不可协商的护栏与门禁（服务端确定性校验，不依赖系统提示词）", 16, "#111827", bold=True)
    for i, (tag, desc) in enumerate(GUARD):
        x = 70 + (i % 2) * 780
        y = gy + (i // 2) * 66
        c.p.append(f'<rect x="{x}" y="{y}" width="760" height="56" rx="7" fill="#F8FAFC" stroke="#CBD5E1" stroke-width="1"/>')
        c.badge(x + 12, y + 16, tag, tone="neutral", w=112)
        c.text(x + 136, y + 34, desc[:46], 12.5, "#334155")
    sy = gy + 148
    c.p.append(f'<rect x="70" y="{sy}" width="{W - 140}" height="56" rx="7" fill="#EAF3DE" stroke="#A9D18E" stroke-width="1"/>')
    c.text(86, sy + 23, "Agent 内部状态机：IDLE → EVIDENCE_CHECKED → DRAFT_CREATED → COMPLIANCE_CHECKED → SUBMITTED（任意态可转 ESCALATED）", 13, "#3B6D11", bold=True)
    c.text(86, sy + 43, "四层能力边界：L1 按角色裁剪工具白名单 · L2 服务端状态机 + 一次性 HMAC 能力令牌（7 项校验） · L3 幂等 + 全量审计 · L4 系统提示词只改善话术、不承担正确性", 12.5, "#3B6D11")

    legend(c, sy + 96, [("normal", "常规步骤"), ("ai", "AI 自动环节"), ("human", "人工审批点"),
                        ("risk", "异常 / 超时路径"), ("fixed", "断点已解 / 护栏")])
    disclaimer(c, sy + 130,
               "范围声明：AI-09 跨团队口径对齐、AI-10 周度复盘归因为二期；AI-07 Agent 确认闭环目前未接入主聊天链路。X-01~X-05 负向清单（推测客户意图、自动审批、自动写回、静默放行）明确不做。")
    return c, sy + 180


def find_browser() -> str | None:
    env = os.environ.get("CHROME_PATH")
    candidates = [
        Path(env) if env else None,
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p and p.is_file():
            return str(p)
    return None


def to_png(browser: str | None, svg: Path, png: Path, w: int, h: int):
    if not browser:
        print(f"  ! 未找到 Chrome / Edge，跳过 PNG：{png.name}（可设置 CHROME_PATH 后重跑）")
        return
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--hide-scrollbars", f"--screenshot={png}",
           f"--window-size={w},{h}", "--force-device-scale-factor=1", svg.as_uri()]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if png.is_file() and png.stat().st_size > 10000:
        print(f"  ✓ PNG {png.name}  {png.stat().st_size // 1024} KB")
    else:
        print(f"  ! PNG 生成失败：{png.name}  {(r.stderr or '')[-300:]}")


def main():
    browser = find_browser()
    specs = [
        ("流程图-01-改造前-现状与典型事件.svg", "流程图-01-改造前-现状与典型事件.png", build_as_is),
        ("流程图-02-改造后-AI数智化.svg", "流程图-02-改造后-AI数智化.png", build_to_be),
    ]
    for svg_name, png_name, builder in specs:
        c, content_bottom = builder()
        # 画布高度按实际内容裁切，避免底部大片留白
        c.h = content_bottom + 30
        # p[6] 是白底 rect，需与最终画布高度同步
        c.p[6] = f'<rect x="0" y="0" width="{c.w}" height="{c.h}" fill="#FFFFFF"/>'
        c.p[0] = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{c.w}" height="{c.h}" '
                  f'viewBox="0 0 {c.w} {c.h}" font-family="{FONT}">')
        svg = c.save(HERE / svg_name)
        print(f"✓ SVG {svg.name}  {c.w}×{c.h}")
        to_png(browser, svg, HERE / png_name, c.w, c.h)
    print("完成。可用浏览器打开 SVG 后另存为 PNG / PDF。")


if __name__ == "__main__":
    main()
