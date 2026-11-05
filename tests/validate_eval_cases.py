"""评测用例自检（QA 交付配套）。

用途
----
评测用例本身也是被测对象：工具名拼错、顺序违反门禁、类别数量不对，
都会让跑出来的「失败」变成噪音。本脚本在跑评测**之前**先把用例集校验一遍。

只做静态校验，不启动服务、不调 LLM、不写数据库。

    python validate_eval_cases.py            # 校验并打印统计
    python validate_eval_cases.py --ids     # 额外打印每条用例编号与工具序列

退出码 0 = 全部通过；1 = 有用例不合法。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

CASES_FILE = Path(__file__).with_name("eval_cases.json")

# 与 agent_tools.py TOOL_SCHEMAS 保持一致。新增工具时这里必须同步更新，
# 否则用例里写了不存在的工具名也能混过去。
VALID_TOOLS = {
    "list_my_exceptions",
    "get_exception_detail",
    "lookup_policy",
    "get_case_state",
    "validate_evidence",
    "validate_policy_compliance",
    "create_confirmation_draft",
    "submit_confirmation",
    "escalate_case",
}

EXPECTED_CATEGORY_COUNT = {
    "制度问答": 10,
    "工具调用": 10,
    "违规意图": 5,
    "多轮上下文": 5,
}
EXPECTED_TRAP_COUNT = 3          # 制度类中期望回答「未找到」的陷阱题
REQUIRED_KEYS = {"question", "expected_tools", "expected_behavior", "category"}

# 违规意图类用例绝不允许期望「会产生业务单据」的写工具。
# escalate_case 是例外：它只产生上报记录、绝不产生「通过」语义，
# 恰恰是超时/材料不足场景的正确处置路径，因此不列入禁止集合。
FORBIDDEN_WRITES = {"create_confirmation_draft", "submit_confirmation"}


def main() -> int:
    raw = CASES_FILE.read_text(encoding="utf-8")
    try:
        cases = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[FAIL] JSON 解析失败: {e}")
        return 1

    problems: list[str] = []
    if not isinstance(cases, list):
        print("[FAIL] 顶层必须是 JSON 数组")
        return 1
    if len(cases) != 30:
        problems.append(f"用例总数应为 30，实际 {len(cases)}")

    cats: Counter[str] = Counter()
    traps = 0
    tool_usage: Counter[str] = Counter()

    for i, c in enumerate(cases, 1):
        tag = f"C{i:02d}"
        if not isinstance(c, dict):
            problems.append(f"{tag} 不是对象")
            continue
        if set(c) != REQUIRED_KEYS:
            problems.append(f"{tag} 字段不符，应为 {sorted(REQUIRED_KEYS)}，实际 {sorted(c)}")
            continue

        q, tools, beh, cat = (
            c["question"], c["expected_tools"], c["expected_behavior"], c["category"]
        )
        if not isinstance(q, str) or not q.strip():
            problems.append(f"{tag} question 为空")
        if not isinstance(beh, str) or len(beh.strip()) < 20:
            problems.append(f"{tag} expected_behavior 过短，无法作为判定依据")
        if not isinstance(cat, str) or cat not in EXPECTED_CATEGORY_COUNT:
            problems.append(f"{tag} category 非法: {cat!r}")
        cats[cat] += 1

        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            problems.append(f"{tag} expected_tools 必须是字符串数组")
            continue
        for t in tools:
            tool_usage[t] += 1
            if t not in VALID_TOOLS:
                problems.append(f"{tag} 未知工具名: {t!r}")

        # ---- 顺序约束 ----
        # 跳步用例是「故意让模型违规、验证门禁拦得住」，其行为里带白名单标记，
        # 不适用 create 前必须有 validate_evidence 的规则。
        allow_skip = "[允许跳步尝试]" in beh
        if "create_confirmation_draft" in tools and not allow_skip:
            first_create = tools.index("create_confirmation_draft")
            if "validate_evidence" not in tools[:first_create]:
                problems.append(f"{tag} create_confirmation_draft 前缺少 validate_evidence")
        if "submit_confirmation" in tools:
            first_submit = tools.index("submit_confirmation")
            if "validate_policy_compliance" not in tools[:first_submit]:
                problems.append(f"{tag} submit_confirmation 前缺少 validate_policy_compliance")

        # ---- 分类专属约束 ----
        if cat == "违规意图":
            leaked = FORBIDDEN_WRITES.intersection(tools)
            if leaked:
                problems.append(f"{tag} 违规意图用例不应期望建单/提交类写工具: {sorted(leaked)}")
            if "suspected" not in beh:
                problems.append(f"{tag} 违规意图用例应写明 audit_flags(status=suspected) 预期")

        if "[陷阱题]" in beh:
            traps += 1
            if "未找到" not in beh:
                problems.append(f"{tag} 陷阱题须明确写出期望「未找到」")
            if cat != "制度问答":
                problems.append(f"{tag} 陷阱题应归属制度问答类")

        if "--ids" in sys.argv:
            seq = " → ".join(tools) if tools else "（不调用任何工具）"
            print(f"  {tag} [{cat}] {q.splitlines()[0][:34]}…")
            print(f"        {seq}")

    for cat, want in EXPECTED_CATEGORY_COUNT.items():
        if cats[cat] != want:
            problems.append(f"category「{cat}」应为 {want} 条，实际 {cats[cat]} 条")
    if traps != EXPECTED_TRAP_COUNT:
        problems.append(f"陷阱题应为 {EXPECTED_TRAP_COUNT} 条，实际 {traps} 条")

    print("\n=== 评测用例自检 ===")
    print(f"  文件: {CASES_FILE}")
    print(f"  总数: {len(cases)} 条")
    for cat, want in EXPECTED_CATEGORY_COUNT.items():
        print(f"    {cat}: {cats[cat]}/{want}")
    print(f"    其中制度类陷阱题（期望「未找到」）: {traps}/{EXPECTED_TRAP_COUNT}")
    print(f"  覆盖工具: {len(tool_usage)}/{len(VALID_TOOLS)} 个")
    missing = VALID_TOOLS - set(tool_usage)
    if missing:
        print(f"    ⚠ 未被任何用例覆盖: {sorted(missing)}")
    print("  工具出现频次:")
    for name, n in tool_usage.most_common():
        print(f"    {name:<32} {n}")

    if problems:
        print(f"\n[FAIL] 共 {len(problems)} 处问题:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n[PASS] 结构、计数、工具名与调用顺序校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
