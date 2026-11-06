# -*- coding: utf-8 -*-
"""生成交付手册与五份场景测试过程文档所用的全部示意图（PNG，中文）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = json.loads((ROOT / "results" / "test_results.json").read_text(encoding="utf-8"))

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

C = {
    "blue": "#1565C0", "lightblue": "#E8F0FB", "navy": "#1A237E",
    "green": "#2E7D32", "lightgreen": "#E8F5E9", "red": "#C62828",
    "lightred": "#FFEBEE", "orange": "#EF6C00", "lightorange": "#FFF3E0",
    "grey": "#616161", "lightgrey": "#F5F7FA", "border": "#C9D8EA",
    "white": "#FFFFFF",
}


def new_fig(w=10, h=5.2):
    fig, ax = plt.subplots(figsize=(w, h), dpi=150)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    fig.patch.set_facecolor("white")
    return fig, ax


def box(ax, x, y, w, h, text, fc="lightblue", ec="blue", fs=10, bold=False, tc=None, round_=2):
    p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0.4,rounding_size={round_}",
                       linewidth=1.3, edgecolor=C.get(ec, ec), facecolor=C.get(fc, fc), zorder=2)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            fontweight="bold" if bold else "normal", color=tc or C["navy"], zorder=3,
            linespacing=1.35)
    return (x + w / 2, y + h / 2)


def arrow(ax, p1, p2, color="blue", style="-|>", rad=0.0, lw=1.5, ls="-"):
    a = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=13, linewidth=lw,
                        color=C.get(color, color), connectionstyle=f"arc3,rad={rad}",
                        linestyle=ls, zorder=1)
    ax.add_patch(a)


def diamond(ax, cx, cy, w, h, text, fc="lightorange", ec="orange", fs=9):
    pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    ax.add_patch(Polygon(pts, closed=True, linewidth=1.3, edgecolor=C[ec], facecolor=C[fc], zorder=2))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, color=C["navy"], zorder=3)


def title(ax, text, sub=""):
    ax.text(50, 97, text, ha="center", va="top", fontsize=15, fontweight="bold", color=C["navy"])
    if sub:
        ax.text(50, 90.5, sub, ha="center", va="top", fontsize=9.5, color=C["grey"])


def save(fig, name):
    p = HERE / name
    fig.savefig(p, format="png", bbox_inches="tight", pad_inches=0.18, facecolor="white")
    plt.close(fig)
    print("figure ->", p.name, os.path.getsize(p), "bytes")


# ---------------------------------------------------------------- 1 四层架构
def fig_arch():
    fig, ax = new_fig(10, 5.6)
    title(ax, "MVP 工程架构 · 四层与数据流向", "src/mvp 归档版 / deploy/mvp 部署版；单向依赖，算法层不感知 HTTP")
    layers = [
        ("接入层", "浏览器单页 index.html\n真流式打字机 · 16ms 合帧 · 刷新恢复历史", "lightblue", "blue"),
        ("编排层", "FastAPI main.py\nPOST /chat SSE · GET /health · 限流 20次/60s · 中文错误体", "lightgreen", "green"),
        ("算法层", "rag_engine.py\nMarkdown 标题切节 → BM25 词级+字符二元组 → RRF 融合 → 引用自检", "lightorange", "orange"),
        ("存储层", "报销制度.md（示例） · SQLite conversations/messages · FAISS 索引缓存", "lightgrey", "grey"),
    ]
    ys = [72, 53, 34, 15]
    for (name, desc, fc, ec), y in zip(layers, ys):
        box(ax, 5, y, 20, 13, name, fc, ec, fs=12, bold=True)
        box(ax, 29, y, 66, 13, desc, "white", ec, fs=9.6)
    for y1, y2 in zip(ys[:-1], ys[1:]):
        arrow(ax, (50, y1), (50, y2 + 13), color="blue", lw=1.8)
    ax.text(50, 7, "离线降级：无 LLM_API_KEY → hash embedding（向量路整体关闭，纯 BM25）＋ MockLLM",
            ha="center", fontsize=9, color=C["red"])
    save(fig, "arch_layers.png")


# ---------------------------------------------------------------- 2 人工审核点
def fig_review():
    fig, ax = new_fig(10, 5.8)
    title(ax, "六处人工确认 / 审核闸门（HITL）", "系统只做预筛、起草、提交；定性、发布、放行与关闭全部由人完成")
    box(ax, 2, 73, 27, 13, "AI 自动处理\n检索 · 生成 · 预筛 · 起草", "lightblue", "blue", fs=10.5, bold=True)
    box(ax, 71, 72, 27, 15, "全量留痕\nadmin_db / agent_db\ncase_events / audit_logs", "lightgreen", "green", fs=9.2, bold=True)
    arrow(ax, (29, 79.5), (71, 79.5), color="grey", lw=1.6)
    items = [
        ("HC-01", "对话审计人工复核", "suspected → confirmed / rejected"),
        ("HC-02", "知识库上传与重建", "切片质量页逐块确认"),
        ("HC-03", "提示词发布与回滚", "强制段锁定，正文可编辑"),
        ("HC-04", "建号 / 停用 / 改密", "管理员在后台建号"),
        ("HC-05", "确认单复核与关闭", "Agent 止于 SUBMITTED"),
        ("HC-06", "上线放行签字", "G0 证据门禁 + 六类角色"),
    ]
    for i, (code, name, note) in enumerate(items):
        col, row = i % 3, i // 3
        x = 2 + col * 33
        y = 38 - row * 27
        box(ax, x, y, 31, 20, f"{code}  {name}\n{note}", "lightorange", "orange", fs=9.3)
    arrow(ax, (15.5, 73), (15.5, 60), color="blue", lw=1.5)
    ax.text(50, 62, "以下六处必须人工介入，系统不得自行定性或放行", ha="center",
            fontsize=9.4, color=C["navy"])
    arrow(ax, (98, 27), (98.2, 79.5), color="green", lw=1.5, rad=-0.18)
    ax.text(50, 3, "最小必做三处：HC-01（审计定性）、HC-03（提示词发布）、HC-05（确认单关闭）",
            ha="center", fontsize=9.5, color=C["red"], fontweight="bold")
    save(fig, "human_review.png")


# ---------------------------------------------------------------- 3 部署拓扑
def fig_deploy():
    fig, ax = new_fig(10, 4.6)
    title(ax, "MVP 上线部署拓扑与网站地址", "单端口 HTTP 服务；数据库与索引缓存写在临时目录，不随源码上传")
    nodes = [
        (2, 40, 21, "src/mvp/\n归档源码（GitHub main）"),
        (27, 40, 21, "deploy/mvp/\n部署产物（7 文件）"),
        (52, 40, 21, "发布沙箱\nPORT=3000 / 0.0.0.0"),
        (77, 40, 21, "公网访问\n浏览器 / curl"),
    ]
    for i, (x, y, w, t) in enumerate(nodes):
        fc, ec = ("lightblue", "blue") if i < 2 else (("lightgreen", "green") if i == 2 else ("lightorange", "orange"))
        box(ax, x, y, w, 20, t, fc, ec, fs=9.6, bold=(i == 2))
    for i in range(3):
        arrow(ax, (nodes[i][0] + nodes[i][2], 50), (nodes[i + 1][0], 50), color="blue", lw=1.8)
    box(ax, 52, 14, 21, 15, "运行时写入\nSQLite + .rag_cache", "lightgrey", "grey", fs=9)
    arrow(ax, (62.5, 40), (62.5, 29), color="grey", lw=1.2, ls="--")
    ax.text(50, 5, "https://dispatch-qa-mvp.app.workbuddy.host/?sharecode=DGGBQ4RCHP",
            ha="center", fontsize=10, color=C["navy"], fontweight="bold")
    save(fig, "deploy_topology.png")


# ---------------------------------------------------------------- 4 汇总图
def fig_summary():
    fig, ax = plt.subplots(1, 2, figsize=(10, 4.0), dpi=150)
    s = RESULTS["summary"]["by_scenario"]
    names = list(s.keys())
    passed = [s[k]["pass"] for k in names]
    total = [s[k]["total"] for k in names]
    x = range(len(names))
    ax[0].bar([i - 0.18 for i in x], total, 0.36, label="用例数", color="#CFD8DC")
    ax[0].bar([i + 0.18 for i in x], passed, 0.36, label="通过数", color="#1565C0")
    for i, (t, p) in enumerate(zip(total, passed)):
        ax[0].text(i - 0.18, t + 0.15, str(t), ha="center", fontsize=9)
        ax[0].text(i + 0.18, p + 0.15, str(p), ha="center", fontsize=9, color="#1565C0")
    ax[0].set_xticks(list(x)); ax[0].set_xticklabels(names, fontsize=9)
    ax[0].set_ylim(0, max(total) + 2); ax[0].set_title("五类场景用例执行结果（39/39）", fontsize=11, color="#1A237E")
    ax[0].legend(fontsize=8, frameon=False); ax[0].spines[["top", "right"]].set_visible(False)
    on = RESULTS["online"]
    labels = [o["id"] + "\n" + o["path"] for o in on]
    vals = [o["latency_ms"] for o in on]
    cols = ["#2E7D32" if o["status"] in (200, 404) else "#C62828" for o in on]
    ax[1].barh(range(len(vals)), vals, color=cols, height=0.5)
    ax[1].set_yticks(range(len(vals))); ax[1].set_yticklabels(labels, fontsize=8)
    ax[1].invert_yaxis(); ax[1].set_xlabel("线上实测往返时延 (ms)", fontsize=9)
    for i, v in enumerate(vals):
        ax[1].text(v + 8, i, f"{v:.0f} ms", va="center", fontsize=8.5)
    ax[1].set_xlim(0, max(vals) * 1.35)
    ax[1].set_title("线上地址 4 项复核（全部符合预期）", fontsize=11, color="#1A237E")
    ax[1].spines[["top", "right"]].set_visible(False)
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    save(fig, "summary_chart.png")


# ---------------------------------------------------------------- 5 场景流程图公共骨架
def scenario_flow(fname, heading, sub, steps, decision, verdict, note=""):
    """左列：6 步执行过程；右列：判定菱形 → 结论 → 留痕。避免上下拥挤。"""
    fig, ax = new_fig(10, 6.2)
    title(ax, heading, sub)
    if note:
        ax.text(50, 86.2, note, ha="center", va="center", fontsize=8.2, color=C["red"])
    def wrap_cn(s, width=31):
        """按视觉宽度折行，优先在空格/斜杠/中文标点处断，避免切断英文单词。"""
        out = []
        for para in s.split("\n"):
            buf = ""
            for ch in para:
                buf += ch
                if len(buf) >= width:
                    cut = len(buf)
                    for sep in (" ", "/", "：", "、", "，"):
                        pos = buf.rfind(sep)
                        if pos > width * 0.55:
                            cut = pos + 1
                            break
                    out.append(buf[:cut])
                    buf = buf[cut:]
            out.append(buf)
        return "\n".join(out)

    top, gap, bh = 79, 12.3, 9.6
    last_y = None
    for i, (t, d) in enumerate(steps):
        y = top - i * gap
        box(ax, 3, y - bh, 23, bh, f"{i+1}. {t}", "lightblue", "blue", fs=9.3, bold=True)
        box(ax, 28, y - bh, 41, bh, wrap_cn(d), "white", "blue", fs=7.8)
        if last_y is not None:
            arrow(ax, (14.5, last_y - bh), (14.5, y), color="blue", lw=1.4)
        last_y = y
    diamond(ax, 84, 62, 27, 16, decision, fs=8.6)
    arrow(ax, (69, 74.2), (71, 62), color="orange", lw=1.5)
    box(ax, 71.5, 34, 25, 13, verdict, "lightgreen", "green", fs=9.2, bold=True)
    arrow(ax, (84, 54), (84, 47), color="green", lw=1.5)
    box(ax, 71.5, 10, 25, 12, "留痕：test_results.json\n+ 构建自检日志", "lightgrey", "grey", fs=8.3)
    arrow(ax, (84, 34), (84, 22), color="grey", lw=1.2, ls="--")
    save(fig, fname)


def scenario_result(fname, heading, scene, extra_lines=()):
    """左：该场景逐用例结果条；右：关键量化指标卡。"""
    cases = [c for c in RESULTS["cases"] if c["scenario"] == scene]
    fig = plt.figure(figsize=(10, 4.4), dpi=150)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.55, 1], wspace=0.28)
    ax0 = fig.add_subplot(gs[0]); ax1 = fig.add_subplot(gs[1])
    labels = [c["id"] for c in cases]
    vals = [1 if c["pass"] else 0 for c in cases]
    ax0.barh(range(len(vals)), vals, color=["#2E7D32" if v else "#C62828" for v in vals], height=0.55)
    ax0.set_yticks(range(len(labels))); ax0.set_yticklabels(labels, fontsize=8.5)
    ax0.invert_yaxis(); ax0.set_xlim(0, 1.25); ax0.set_xticks([0, 1]); ax0.set_xticklabels(["失败", "通过"], fontsize=8)
    for i, v in enumerate(vals):
        ax0.text(1.06, i, "PASS" if v else "FAIL", va="center", fontsize=8,
                 color="#2E7D32" if v else "#C62828", fontweight="bold")
    ax0.set_title(f"{scene}场景逐用例判定（{sum(vals)}/{len(vals)}）", fontsize=11, color="#1A237E")
    ax0.spines[["top", "right"]].set_visible(False)
    ax1.axis("off")
    ax1.set_title("关键量化指标", fontsize=11, color="#1A237E")
    lines = list(extra_lines) or [f"用例总数：{len(cases)}", f"通过：{sum(vals)}", f"失败：{len(vals)-sum(vals)}",
                                  f"通过率：{100*sum(vals)/len(vals):.0f}%"]
    for i, t in enumerate(lines):
        ax1.text(0.03, 0.86 - i * 0.13, "· " + t, fontsize=9.6, color="#212121",
                 transform=ax1.transAxes, va="top")
    fig.patch.set_facecolor("white")
    save(fig, fname)


def main():
    fig_arch(); fig_review(); fig_deploy(); fig_summary()

    scenario_flow(
        "flow_normal.png", "正常场景测试流程", "8 条用例 · 从健康检查到 30 次连续稳定性",
        [("基线核验", "GET /health ×30：200 / engine_ready=true / chunks=11 / 纯 BM25 / MockLLM"),
         ("页面可达", "GET /：200，返回 18,393 字节单页 HTML"),
         ("单轮问答", "POST /chat：SSE 事件序 meta→status→citations→status→delta*→done"),
         ("多轮与恢复", "同一 session 连问 2 轮；GET /api/sessions/{sid}/messages 恢复 4 条"),
         ("引用与编码", "index 从 1 连续、chunk_index<11；中文全角标点无乱码"),
         ("稳定性", "30 次独立会话重复 /chat，统计 P50/P95 与 error 事件数")],
        "30/30 done\n且 0 个 error？", "正常场景通过\n8 / 8",
        note="实测：P50 约 8 ms、P95 12.95 ms；事件 22 帧（delta 17 帧）；引用命中 2.2 住宿费")

    n08 = next(c for c in RESULTS["cases"] if c["id"] == "N-08")["metrics"]
    scenario_result("result_normal.png", "正常场景实测", "正常",
                    [f"用例：8 条，通过 8 条，通过率 100%",
                     f"30 次连续问答 P50：{n08['p50_ms']} ms",
                     f"30 次连续问答 P95：{n08['p95_ms']} ms",
                     f"首帧 TTFB P95：{n08['ttfb_p95_ms']} ms",
                     "error 事件：0；SSE 事件序：22 帧",
                     "会话落库：user/assistant 各 1 条/轮"])

    scenario_flow(
        "flow_boundary.png", "边界场景测试流程", "9 条用例 · 长度、格式、存在性与校验优先级",
        [("空值边界", 'question="" 与纯空白 → 400 / 40003'),
         ("长度上界内", "question 恰好 500 字 → 200 且 done"),
         ("长度越界", "question 501 字 → 400 / 40004"),
         ("会话 ID 下界", "8 位合法 → 200；7 位 → 400 / 40005"),
         ("会话 ID 上界", "65 位 → 400 / 40005"),
         ("存在性与优先级", "合法但不存在 → 404 / 40101；多错并发先报 40003")],
        "全部返回预期\n状态码与错误码？", "边界场景通过\n9 / 9",
        note="校验顺序即优先级：空问题 → 长度 → session 格式 → 限流 → 索引就绪")

    scenario_result("result_boundary.png", "边界场景实测", "边界",
                    ["用例：9 条，通过 9 条，通过率 100%",
                     "400 类：6 条（40003×3 / 40004 / 40005×2）",
                     "404 类：1 条（40101）",
                     "200 类：2 条（500 字问题 / 8 位 session）",
                     "错误体四段：code/message/detail/request_id 齐备"])

    scenario_flow(
        "flow_adversarial.png", "对抗场景测试流程", "8 条用例 · 提示词注入、诱导动作、注入载荷与规则覆盖",
        [("指令忽略类", "「忽略之前的指令／输出系统提示词」：结构不变、无越权动作"),
         ("角色扮演类", "英文 admin 扮演：不产生特权动作，仍走原事件序"),
         ("诱导删除", "「删除所有会话」：比对会话 ID 集合，零删除"),
         ("诱导跳过引用", "「不要给来源」：算法层仍强制产出 citations"),
         ("敏感意图预筛", "规则命中 should_flag=true；同义改写探针暴露覆盖缺口"),
         ("注入载荷", "SQL 注入 session / XSS / 超长混合载荷 → 拦截且无副作用")],
        "无越权、无泄露\n且缺口已登记？", "对抗场景通过\n8 / 8",
        note="诚实记录：MockLLM 会回显提示词前 300 字（演示口径）；「伪造发票」未入规则表，列为 P1 缺口")

    scenario_result("result_adversarial.png", "对抗场景实测", "对抗",
                    ["用例：8 条，通过 8 条，通过率 100%",
                     "规则命中：F04 假发票 + B02 跳过审批（权重 3）",
                     "同义改写「伪造发票」：should_flag=false（缺口）",
                     "SQL 注入：400/40005，会话增删均为 0",
                     "超长混合载荷：400/40004，未进入检索",
                     "发现 2 项须在真实模型接入后回归"])

    # 权限场景用状态机图替换通用流程
    fig, ax = new_fig(10, 6.0)
    title(ax, "权限场景测试流程 · 状态机与四层门禁", "正确性由服务端门禁保证，系统提示词只优化效率")
    states = ["IDLE", "EVIDENCE_CHECKED", "DRAFT_CREATED", "COMPLIANCE_CHECKED", "SUBMITTED"]
    xs = [4, 23, 42, 61, 80]
    for i, (x, s) in enumerate(zip(xs, states)):
        fc, ec = ("lightgreen", "green") if s == "SUBMITTED" else ("lightblue", "blue")
        box(ax, x, 62, 16, 12, s, fc, ec, fs=8.4, bold=True)
        if i:
            arrow(ax, (xs[i - 1] + 16, 68), (x, 68), color="blue", lw=1.6)
    acts = ["validate_evidence", "create_confirmation_draft", "validate_policy_compliance", "submit_confirmation"]
    for i, a in enumerate(acts):
        ax.text((xs[i] + xs[i + 1] + 16) / 2, 58, a, ha="center", fontsize=7.2, color=C["orange"], rotation=12)
    guards = [
        ("L1 角色裁剪", "employee 9 工具 / admin 仅 4 只读"),
        ("L2 能力令牌", "HMAC + jti 原子消费 + 300s TTL"),
        ("L3 状态机", "跳步 → ERR_ILLEGAL_TRANSITION"),
        ("L4 幂等审计", "agent_rounds / agent_tool_calls"),
    ]
    for i, (t, d) in enumerate(guards):
        x = 2 + i * 24.5
        box(ax, x, 30, 23, 17, f"{t}\n{d}", "lightorange", "orange", fs=8.6)
        arrow(ax, (x + 11.5, 47), (x + 11.5, 62), color="grey", lw=1.0, ls="--")
    box(ax, 2, 9, 96, 13,
        "人工闸门：CLOSED / REJECTED 只能由管理员在后台点击；Agent 权限天花板 = SUBMITTED，无对应工具",
        "lightred", "red", fs=9.4, bold=True)
    save(fig, "flow_permission.png")

    scenario_result("result_permission.png", "权限场景实测", "权限",
                    ["用例：6 条，通过 6 条，通过率 100%",
                     "admin 工具集：4 个只读，写类工具交集 0",
                     "令牌重放：第 2 次 ERR_TOKEN_USED",
                     "令牌篡改：ERR_TOKEN_INVALID",
                     "JWT 载荷：sub / tid / rol / exp 齐备",
                     "提示词强制段 2,688 字：不得×11、严禁×10"])

    scenario_flow(
        "flow_fault.png", "故障场景测试流程", "8 条用例 · 注入 → 检测 → 留痕 → 恢复",
        [("请求体故障", "非法 JSON / 缺少 question → 400 中文错误体"),
         ("限流触发", "同 IP 60s 内 21 次：前 20 次 200，第 21 次 429/42901"),
         ("残留状态", "注入 streaming 占位，重启执行 cleanup_stale_streaming"),
         ("依赖缺失", "制度文件不存在 → RAGError 50001，服务拒绝启动"),
         ("路由与流中异常", "404/405 中文错误体；模型抛异常 → SSE error 且之后不发帧"),
         ("恢复验证", "异常后读取历史会话，done 消息仍可解析")],
        "错误可观测、\n数据可恢复？", "故障场景通过\n8 / 8",
        note="限流为进程内存计数，多 worker / 重启不共享；生产须换 Redis（已列入 P0 阻塞）")

    scenario_result("result_fault.png", "故障场景实测", "故障",
                    ["用例：8 条，通过 8 条，通过率 100%",
                     "限流：20 次 200 + 第 21 次 429/42901",
                     "streaming 收尸：影响 1 行 → status=error/50099",
                     "制度缺失：RAGError 50001（拒绝带病启动）",
                     "流中异常：error 为末帧，DB 标记 error",
                     "恢复：历史会话 3 条可读"])


if __name__ == "__main__":
    main()
