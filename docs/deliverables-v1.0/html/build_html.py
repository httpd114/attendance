# -*- coding: utf-8 -*-
"""复用已验证的商务报告排版引擎，把本次两份 md 源稿转成单文件 HTML。

原引擎位于 output/上线交付运行手册-v1.0/html/build_html.py，
已通过 html-review 94 分门禁与 HTML→DOCX 转换验证，此处只替换文档配置。
"""
from __future__ import annotations

import html as html_mod
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENGINE = ROOT.parents[1] / "output" / "上线交付运行手册-v1.0" / "html" / "build_html.py"

spec = importlib.util.spec_from_file_location("doc_engine", ENGINE)
eng = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eng)

DOCS = [
    dict(
        md=ROOT / "sources" / "典型事件AI整理纪要.md",
        html="典型事件AI整理纪要.html",
        category="典型事件 · AI 辅助整理纪要",
        title1="派遣业务 AI 数智化项目",
        title2="典型事件 AI 整理纪要",
        subtitle="6 轮采访 · 时间线 · 量化三级分级 · 根因归纳 · 事实编号映射",
        meta=[
            ("适用对象", "业务负责人 / 质量负责人 / 需求分析人员"),
            ("版本", "v1.0 · AI 辅助整理稿 · 2026-11-06"),
            ("输入材料", "典型事件采访记录.txt（6 轮逐字稿）、discovery-report.md"),
            ("审核状态", "未复核；根因归纳与优先级须书面确认后方可引用"),
        ],
        note="受访者、单号、人数与耗时均为合成演练设定；80 单 / 22% 为虚构基线，不得作为 ROI 输入或验收目标。",
        summary=[
            "本纪要由 AI 对话模型对 6 轮采访逐字稿做转写归档、字段抽取、量化分级、时间线重建、根因归纳与事实编号映射，产出供需求评审使用的结构化稿件。",
            "事件主线：客户电话临时增加夜班要求（口头 v4 未确认），交接单仍引用已确认 v3；8 名待到岗人员中 3 名按旧班次回执，闭环前核对发现字段不一致，挂未结项并发起人工复核。",
            "最重要的整理结果是把数字分为三级：A 已采集规则（可写入需求规格）、B 演练设定值（仅限流程示例，不得进 ROI 与验收）、C 明确无统计（保持留空并列入 G0 补证）。",
            "AI 归纳的四层根因链属推断而非结论；其中「口头变更」的证据强度在两份调研报告中结论相反，须以 3~5 个真实样本补证后定级。全文设 HC-A~HC-D 四处人工确认点，未经签字不构成开发承诺。",
        ],
    ),
    dict(
        md=ROOT / "行业分析文档.md",
        html="行业分析文档.html",
        category="行业分析 · 公开信息研究",
        title1="派遣业务 AI 数智化项目",
        title2="行业分析文档",
        subtitle="规模与结构 · 政策合规 · 标杆企业 · 可比实践 · 适用性评估",
        meta=[
            ("适用对象", "项目决策层 / 产品与架构 / 合规与质量"),
            ("版本", "v1.0 · 2026-11-06"),
            ("核验日期", "2026-11-06（法规、年报、行业报告与公开案例）"),
            ("资料分级", "A 法规原文 / B 上市公司披露 / C 行业报告 / D 厂商案例"),
        ],
        note="本文件为公开行业信息分析，不构成投资建议，不替代客户书面 Brief 与 G0 证据门禁。",
        summary=[
            "2025 年人力资源服务业营收约 3.1 万亿元、同比 +3.7%，总量温和但结构剧变：灵活用工以 17.8% 复合增速扩张至 17675 亿元，传统劳务派遣收缩约 40%，行业从「卖人头」向「卖调度能力」迁移。",
            "合规是硬边界。《劳务派遣暂行规定》第 3、4、5、7 条对三性岗位、10% 用工比例、2 年以上合同、派遣协议 13 项载明内容有强制要求，2025—2026 年多地公布超比例处罚案例；本项目涉及的岗位性质、地点、人数、期限、工时与技能条件因此必须人工确认与全量留痕。",
            "标杆企业外服控股 2025 年营收 241.29 亿元、业务外包占 81.63%，研发投入 1.31 亿元占营收 0.64%，「数字外服」结项后转向 AI 招聘智能体与国际业务平台；低毛利外包主业决定 AI 的价值首先在降低核对与返工摩擦。",
            "可比技术案例普遍采用 RAG + RBAC + 审计 + 人工复核，并设置 groundedness >90%、answer correctness ≥0.90 等量化门禁；但行业报告显示仅 2.7% 企业达到规模化 AI 应用、5.5% 能以可衡量业务成果证明价值。结论：本项目应优先补证据、字段契约与复核闭环，行业增速不能替代本项目的适用性验证。",
        ],
    ),
]


def main():
    report = []
    for cfg in DOCS:
        md_path = Path(cfg["md"])
        md = md_path.read_text(encoding="utf-8")
        body, toc = eng.convert(md, md_path)
        doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="docx-page-size" content="A4">
<title>{html_mod.escape(cfg['title2'])} v1.0</title>
<style>{eng.CSS}</style>
</head>
<body>
{eng.cover(cfg)}
<section role="cover" style="margin:var(--sp-toc-top)">{eng.build_toc(toc)}</section>
<section role="body" data-page-restart="1" style="margin:var(--sp-body-top)">
{eng.summary(cfg)}
{body}
</section>
</body>
</html>
"""
        out = HERE / cfg["html"]
        out.write_text(doc, encoding="utf-8")
        report.append({
            "html": out.name, "bytes": out.stat().st_size, "toc": len(toc),
            "images": doc.count("data:image/png;base64,"),
            "tables": body.count("<table"), "has_pre": body.count("<pre>"),
        })
        print(json.dumps(report[-1], ensure_ascii=False))
    (ROOT / "results" / "html_build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
