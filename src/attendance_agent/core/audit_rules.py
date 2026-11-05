#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_rules.py —— 对话审计的规则预筛引擎
====================================================================
【为什么是"规则预筛 + 人工复核"】

纯人工审阅在派遣场景下不可行：一家用工单位一天几百条问答，
一个管理员根本看不完，于是审计页变成摆设。

但纯规则也不行：关键词匹配只能给出"疑似"，不能直接定性。
比如员工问"住宿标准是多少"是正常咨询，
问"住宿标准能不能多报点"就是违规——**字面只差两个字**，
正则分不出这个差别。

所以本模块的定位很明确：**只负责召回，不负责定性**。
规则命中 → 写入 audit_flags（status=suspected）→ 管理员在后台人工复核
→ 确认或驳回。系统永远不自动定性，这是合规底线。

【规则设计原则】
1. 低召回高精确：宁可漏也不误报。误报会让管理员对整个预警系统脱敏。
2. 可解释：每条命中都必须能说清"命中了哪个词、为什么可疑"，
   否则管理员无法判断，只能全部点"确认"，预警就失去意义。
3. 规则可扩展：新增规则只加一条 Rule 定义，不改主流程。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rule:
    """一条预筛规则。

    rule_id  会写进 audit_flags.hit_rule，审计时能反查是哪条规则命中的。
    keyword 触发关键词（子串匹配，忽略大小写）。
    weight  严重程度：3=高危 2=中危 1=低危。用于排序，人工先看高危。
    reason  命中原因说明，直接展示给管理员看。
    category 违规类型分类，用于审计统计。
    """
    rule_id: str
    keyword: str
    weight: int
    reason: str
    category: str


# ====================================================================
# 规则清单
# ----------------------------------------------------------------
# 分四类：
#   fraud      —— 费用套取 / 虚报
#   bypass     —— 试图绕开制度审批
#   sensitive  —— 涉及个人敏感信息
#   advice     —— 索要超出助手职责的建议（合规提示）
# ====================================================================
RULES: tuple[Rule, ...] = (
    # ---------- 费用套取类 ----------
    Rule("F01", "多报", 3, "试图让不合规费用获得报销，可能涉及虚报",
         "fraud"),
    Rule("F02", "虚报", 3, "直接询问虚报手法，存在费用套取风险",
         "fraud"),
    Rule("F03", "自己填", 2, "疑似要求代填或自行编造凭证信息", "fraud"),
    Rule("F04", "假发票", 3, "涉及虚假发票，属于明确违规", "fraud"),
    Rule("F05", "别的发票", 3, "尝试使用他人发票报销", "fraud"),
    Rule("F06", "拆单", 2, "拆单通常用于规避审批阈值", "fraud"),
    Rule("F07", "超标", 2, "询问超标费用的处理方式，需确认是否走特批",
         "fraud"),
    Rule("F08", "没发票", 2, "询问无票场景的报销方式，需人工确认口径",
         "fraud"),
    Rule("F09", "找关系", 3, "试图通过非制度途径解决报销", "fraud"),

    # ---------- 绕审批类 ----------
    Rule("B01", "不用审批", 3, "试图跳过审批流程", "bypass"),
    Rule("B02", "跳过审批", 3, "试图跳过审批流程", "bypass"),
    Rule("B03", "免审批", 3, "试图绕过审批环节", "bypass"),
    Rule("B04", "先报了再说", 2, "先报销后补手续，违反流程", "bypass"),
    Rule("B05", "帮我改", 2, "要求修改凭证或金额信息", "bypass"),
    Rule("B06", "直接给钱", 3, "绕开报销流程直接支付资金", "bypass"),

    # ---------- 敏感信息类 ----------
    Rule("S01", "别人的", 2, "涉及查询他人信息，需确认数据权限边界",
         "sensitive"),
    Rule("S02", "同事的工资", 2, "涉及他人薪酬信息，涉嫌越权查询",
         "sensitive"),
    Rule("S03", "工资多少", 1, "涉及薪酬信息，需确认是否在制度范围内",
         "sensitive"),

    # ---------- 职责边界类 ----------
    Rule("A01", "帮我写", 1, "要求代写申报材料，需确认是否越权代办",
         "advice"),
    Rule("A02", "能不能通过", 1, "询问审批结论，助手不应给出倾向性建议",
         "advice"),
)


@dataclass
class FlagHit:
    """一次命中的结果。"""
    rule_id: str
    keyword: str
    weight: int
    reason: str
    category: str


@dataclass
class PrescreenResult:
    """预筛的整体结果。"""
    hits: list[FlagHit] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def should_flag(self) -> bool:
        """是否需要推给管理员。

        ⭐ 权重阈值取 2：只有低危（weight=1）的命中不进预警池。
        weight=1 那几条（"帮我写""能不能通过""工资多少"）在正常咨询里
        出现频率很高，全量推预警会让管理员被噪音淹没。
        """
        return any(h.weight >= 2 for h in self.hits)

    @property
    def max_weight(self) -> int:
        return max((h.weight for h in self.hits), default=0)

    @property
    def reason(self) -> str:
        """合并后的原因说明，直接写进 audit_flags.reason。"""
        if not self.hits:
            return ""
        parts = [f"[{h.rule_id}] {h.reason}" for h in self.hits]
        return "；".join(parts)

    @property
    def hit_rule(self) -> str:
        """合并后的规则编号，逗号分隔。"""
        return ",".join(h.rule_id for h in self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [
                {
                    "rule_id": h.rule_id,
                    "keyword": h.keyword,
                    "weight": h.weight,
                    "reason": h.reason,
                    "category": h.category,
                }
                for h in self.hits
            ],
            "should_flag": self.should_flag,
            "max_weight": self.max_weight,
            "reason": self.reason,
            "hit_rule": self.hit_rule,
        }


def prescreen(text: str, rules: tuple[Rule, ...] = RULES) -> PrescreenResult:
    """对一条消息做规则预筛。

    ⭐ 只扫**员工提问**（user role），不扫助手回答。
    助手回答里出现"虚报""超标"是正常的——它在复述制度原文和风险提示。
    如果连回答一起扫，所有引用了风险条款的回答都会被标记成违规，
    预警量直接爆炸。

    参数 text：消息正文。
    返回：PrescreenResult，含命中明细和是否需要预警。
    """
    result = PrescreenResult()
    if not text:
        return result

    lowered = text.lower()
    for rule in rules:
        if rule.keyword.lower() in lowered:
            result.hits.append(
                FlagHit(
                    rule_id=rule.rule_id,
                    keyword=rule.keyword,
                    weight=rule.weight,
                    reason=rule.reason,
                    category=rule.category,
                )
            )
            result.matched_keywords.append(rule.keyword)

    # 同一类别多条命中只保留最高权重的一条，避免 reason 里刷屏
    # （比如一句话里"多报""虚报"同时出现，列两条意义不大）
    best: dict[str, FlagHit] = {}
    for h in result.hits:
        if h.category not in best or h.weight > best[h.category].weight:
            best[h.category] = h
    result.hits = list(best.values())
    # 高权重排前面
    result.hits.sort(key=lambda h: -h.weight)

    return result


# ====================================================================
# 简单正则补充规则
# ----------------------------------------------------------------
# 纯关键词漏掉两类典型违规话术：
#   "能不能帮我把金额改大一点"  —— "改" + 数字
#   "这个数能不能再高一些"      —— 模糊表达，没有关键词
# 所以补几条正则，但同样只做召回不做定性。
# ====================================================================
_PATTERNS: tuple[tuple[str, str, int, str, str], ...] = (
    (r"改(大|高|多)一点", "P01", 3, "试图调高报销金额", "fraud"),
    (r"(多|加)报\s*\d", "P02", 3, "要求按更高金额报销", "fraud"),
    (r"帮(我|我)?(垫|垫付|先付)", "P03", 2, "涉及垫资，常见于资金体外循环",
     "fraud"),
)


def prescreen_with_patterns(text: str) -> PrescreenResult:
    """在关键词规则基础上补跑正则规则。"""
    result = prescreen(text)
    if not text:
        return result

    for pattern, rule_id, weight, reason, category in _PATTERNS:
        if re.search(pattern, text):
            # 已被同类关键词规则命中就不重复加
            if any(h.category == category and h.weight >= weight
                   for h in result.hits):
                continue
            result.hits.append(
                FlagHit(rule_id=rule_id, keyword=pattern, weight=weight,
                        reason=reason, category=category)
            )
    result.hits.sort(key=lambda h: -h.weight)
    return result


def describe_rules() -> list[dict[str, Any]]:
    """导出规则清单，供后台页面展示（让管理员知道系统在按什么筛）。"""
    return [
        {
            "rule_id": r.rule_id,
            "keyword": r.keyword,
            "weight": r.weight,
            "reason": r.reason,
            "category": r.category,
        }
        for r in RULES
    ]


if __name__ == "__main__":
    # 快速自测：几类典型问法
    samples = [
        "住宿标准是多少",
        "差旅费超标了怎么报",
        "能不能帮我把金额改大一点",
        "这个发票能不能开高一些",
        "我不想走审批，直接给钱可以吗",
        "帮我写一份报销申请",
    ]
    for s in samples:
        r = prescreen_with_patterns(s)
        mark = "⚠️ 预警" if r.should_flag else "   通过"
        print(f"{mark} | {s}")
        print(f"        命中 {r.hit_rule or '无'} · {r.reason or '—'}")
