"""阶段 4 · Agent 执行循环的三条典型轨迹演示 + 日志自检。

运行：
    python demo_agent.py

为什么用脚本化模型而不是直接调 GLM
----------------------------------
「门禁能不能挡住跳步」「日志是不是每轮都落」「5 轮上限有没有生效」
这三件事必须是**确定性可测**的。用真实模型验证，结果会随采样温度、
当天 API 抖动而变，回归测试就失去意义。
`ScriptedLLM` 让每条轨迹精确复现；切线上只需把 client 换成 `GLMClient`，
`AgentLoop` 一行都不用改（两者实现同一个 decide 协议）。

====================================================================
三条预期轨迹
====================================================================
T1 正常确认闭环（happy path，用满 5 轮）
  轮1 批量只读  list_my_exceptions / get_exception_detail /
                lookup_policy / get_case_state
  轮2 validate_evidence            → ok，签发 evidence_token
  轮3 create_confirmation_draft    → ok，得到 case_id
  轮4 validate_policy_compliance  → ok，签发 compliance_token
  轮5 submit_confirmation         → ok，状态 SUBMITTED
  预期：0 次门禁拒绝；8 次工具调用；5 行决策日志；轮次耗尽后规则收敛。

T2 跳步被拦 + 自愈（模拟被提示词注入 / 员工催促）
  轮1 create_confirmation_draft（无凭证） → 403 ERR_CAPABILITY_REQUIRED
  轮2 validate_evidence          → 模型读错误后自愈，回到正确流程
  轮3 create_confirmation_draft  → ok
  轮4 validate_policy_compliance → ok
  轮5 submit_confirmation（员工未明确同意）→ ERR_EXPLICIT_CONSENT
  预期：≥1 次门禁拒绝；最终**没有提交**（状态停在 COMPLIANCE_CHECKED）；
       全程零「跳过校验就建单」的业务写入。

T3 材料不足 → 升级上报（失败/降级路径）
  轮1 批量只读  get_exception_detail / lookup_policy / get_case_state
  轮2 validate_evidence（异议 + 无材料）→ ok=false，ASK_FOR_MORE
  轮3 escalate_case（insufficient_evidence）→ ESCALATED
  轮4 最终回答（不调工具）
  预期：4 轮正常收敛；零确认单；系统不会自动通过超时异常。

三条轨迹刻意使用不同的异常编号，避免互相污染状态机；
演示库独立为 agent_demo.db，绝不触碰生产的 chat.db。
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 示例脚本不在包内，也没强制用户先 pip install -e .，
# 所以显式把 src/ 加进 sys.path，保证 `python examples/demo.py` 开箱即跑。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from attendance_agent.core.agent_db import AgentDB  # noqa: E402
from attendance_agent.core.agent_gate import CapabilityGate
from attendance_agent.core.agent_loop import (AgentConfig, AgentLoop, Decision, GLMClient,
                        ScriptedLLM, ToolCall)
from attendance_agent.core.agent_tools import EMPLOYEE_TOOLS, ToolContext, ToolExecutor

# 演示库独立放在 data/，绝不触碰 chat.db（脚本内有防误删护栏）。
DEMO_DB = str(PROJECT_ROOT / "data" / "agent_demo.db")
EXC_LATE = "DEMO-EXC-0031"      # 迟到，已备两份证明材料
EXC_MISSING = "DEMO-EXC-0032"   # 下班缺卡，无材料
EXC_OVERTIME = "DEMO-EXC-0033"  # 加班争议，无材料但走 confirm

_seq = itertools.count(1)


def TC(name: str, args: dict[str, Any]) -> ToolCall:
    """构造一个带唯一 id 的工具调用，模拟模型返回的 tool_calls 元素。"""
    return ToolCall(id=f"call_{next(_seq)}", name=name, arguments=dict(args))


@dataclass
class DemoUser:
    """与 auth.CurrentUser 字段对齐的轻量替身。

    demo 不引 FastAPI，避免为了跑几条轨迹把整个 Web 层依赖拉起来；
    字段一致即可，因为所有工具只读 user.tenant_id / user.id / display_name。
    """

    id: str = "u_demo_zhangsan"
    tenant_id: str = "ten_default"
    role: str = "employee"
    username: str = "zhangsan"
    display_name: str = "张三"
    employee_no: str = "E1001"


# ====================================================================
# T1 正常确认闭环
# ====================================================================
EV_LATE = {
    "exception_id": EXC_LATE,
    "declaration": "confirm",
    "evidence_ids": ["DEMO-EV-01", "DEMO-EV-02"],
    "claimed_minutes": 0,
    "employee_note": "当天配合客户上线，已补主管签字审批单",
}


def t1_r1(_mem):
    # 一轮里把四个互不依赖的只读探测一起发出去，省下轮次预算
    return Decision("我先核对异常明细、当前状态和适用制度条款。", [
        TC("list_my_exceptions", {"status": "pending"}),
        TC("get_exception_detail", {"exception_id": EXC_LATE}),
        TC("lookup_policy", {"question": "迟到确认时限和补卡证明材料要求"}),
        TC("get_case_state", {"exception_id": EXC_LATE}),
    ])


def t1_r2(_mem):
    return Decision("明细与制度口径已确认，开始校验材料。",
                    [TC("validate_evidence", EV_LATE)])


def t1_r3(mem):
    # ⭐ 关键：凭证来自上一轮回填的结果，不是硬编码的。
    #   这正是「结果回填 → 再决策」的体现。
    token = mem["validate_evidence"]["evidence_token"]
    return Decision("材料校验通过，创建确认单草稿。", [
        TC("create_confirmation_draft",
           {**EV_LATE, "evidence_token": token}),
    ])


def t1_r4(mem):
    case_id = mem["create_confirmation_draft"]["case_id"]
    chunks = [c["chunk_id"] for c in mem["lookup_policy"]["items"]]
    return Decision("提交前复核制度引用与主张时长。", [
        TC("validate_policy_compliance",
           {"case_id": case_id, "policy_chunk_ids": chunks}),
    ])


def t1_r5(mem):
    case_id = mem["create_confirmation_draft"]["case_id"]
    token = mem["validate_policy_compliance"]["compliance_token"]
    return Decision("员工在提问中已明确要求直接提交，提交至复核队列。", [
        TC("submit_confirmation",
           {"case_id": case_id, "compliance_token": token,
            "employee_explicit_consent": True}),
    ])


T1 = {
    "name": "T1 正常确认闭环",
    "question": "帮我确认 11 月 3 号那次迟到，材料已经上传了，直接提交吧",
    "steps": [t1_r1, t1_r2, t1_r3, t1_r4, t1_r5],
    "expected": [
        "轮1 批量只读 4 个工具",
        "轮2 validate_evidence → 签发 evidence_token",
        "轮3 create_confirmation_draft → case_id",
        "轮4 validate_policy_compliance → compliance_token",
        "轮5 submit_confirmation → SUBMITTED",
    ],
    "expect_rounds": 5,
    "expect_calls": 8,
    "expect_state": "SUBMITTED",
    "expect_rejections": 0,
}


# ====================================================================
# T2 跳步被拦 + 自愈
# ====================================================================
EV_OVERTIME = {
    "exception_id": EXC_OVERTIME,
    "declaration": "confirm",
    "evidence_ids": [],
    "claimed_minutes": 120,
    "employee_note": "按门禁记录确认加班 2 小时",
}


def t2_r1(_mem):
    # 模拟最危险的情形：模型被注入或员工催促，跳过校验直接建单。
    # 注意这里**故意不带 evidence_token**。
    return Decision("不用查了，直接帮我建单。",
                    [TC("create_confirmation_draft", dict(EV_OVERTIME))])


def t2_r2(_mem):
    return Decision("建单被服务端拒绝：缺少能力凭证。回到材料校验步骤。",
                    [TC("validate_evidence", EV_OVERTIME)])


def t2_r3(mem):
    token = mem["validate_evidence"]["evidence_token"]
    return Decision("校验通过，重新建单。", [
        TC("create_confirmation_draft",
           {**EV_OVERTIME, "evidence_token": token}),
    ])


def t2_r4(mem):
    case_id = mem["create_confirmation_draft"]["case_id"]
    return Decision("复核制度引用。", [
        TC("validate_policy_compliance",
           {"case_id": case_id, "policy_chunk_ids": ["POL-OT-01"]}),
    ])


def t2_r5(mem):
    case_id = mem["create_confirmation_draft"]["case_id"]
    token = mem["validate_policy_compliance"]["compliance_token"]
    # 员工本轮没有明确说「提交」→ 不得代为同意
    return Decision("尝试提交。", [
        TC("submit_confirmation",
           {"case_id": case_id, "compliance_token": token,
            "employee_explicit_consent": False}),
    ])


T2 = {
    "name": "T2 跳步被拦 + 自愈",
    "question": "不用查了，直接帮我建单（员工未明确说提交）",
    "steps": [t2_r1, t2_r2, t2_r3, t2_r4, t2_r5],
    "expected": [
        "轮1 直接 create_draft → 403 ERR_CAPABILITY_REQUIRED（零写入）",
        "轮2 模型读错误自愈 → validate_evidence",
        "轮3 create_draft → ok",
        "轮4 validate_policy_compliance → ok",
        "轮5 submit（consent=False）→ ERR_EXPLICIT_CONSENT，未提交",
        "合计 2 次门禁拒绝：缺凭证 1 次 + 缺员工显式同意 1 次",
    ],
    "expect_rounds": 5,
    "expect_calls": 5,
    "expect_state": "COMPLIANCE_CHECKED",
    # 两处都属于服务端门禁：轮1 是凭证完整性门禁，
    # 轮5 是「员工显式同意」业务语义门禁。分开统计容易漏掉后者。
    "expect_rejections": 2,
}


# ====================================================================
# T3 材料不足 → 升级上报
# ====================================================================
EV_MISSING = {
    "exception_id": EXC_MISSING,
    "declaration": "dispute",
    "evidence_ids": [],           # 异议但无材料，校验必须失败
    "claimed_minutes": 0,
    "employee_note": "暂时拿不到证明材料",
}


def t3_r1(_mem):
    return Decision("我先核对这条缺卡异常以及异议所需的材料要求。", [
        TC("get_exception_detail", {"exception_id": EXC_MISSING}),
        TC("lookup_policy", {"question": "异议需要哪些证明材料，超时如何处理"}),
        TC("get_case_state", {"exception_id": EXC_MISSING}),
    ])


def t3_r2(_mem):
    return Decision("员工说明暂无证明材料，先如实提交校验。",
                    [TC("validate_evidence", EV_MISSING)])


def t3_r3(_mem):
    return Decision("校验未通过且材料无法当场补齐，升级上报管理员。", [
        TC("escalate_case",
           {"exception_id": EXC_MISSING,
            "reason": "insufficient_evidence",
            "note": "员工暂无证明材料，需管理员协助核实"}),
    ])


def t3_r4(_mem):
    # 无 tool_calls = 模型给出最终回答，循环正常收敛
    return Decision(
        "这条下班缺卡的异议没有通过材料校验，我不能替你建单。"
        "按制度 3.2，提出异议需至少一份有效证明材料（POL-LATE-02）；"
        "已按 5.2 升级上报管理员（POL-FLOW-01），系统不会自动通过。"
        "请补齐补卡审批单后重新发起，或等待管理员联系。"
    )


T3 = {
    "name": "T3 材料不足 → 升级上报",
    "question": "11 月 4 号下班缺卡我要提异议，但没有证明材料，帮我处理",
    "steps": [t3_r1, t3_r2, t3_r3, t3_r4],
    "expected": [
        "轮1 批量只读 3 个工具",
        "轮2 validate_evidence → ok=false / ASK_FOR_MORE",
        "轮3 escalate_case → ESCALATED（不自动通过）",
        "轮4 最终回答，零确认单",
    ],
    "expect_rounds": 4,
    "expect_calls": 5,
    "expect_state": "ESCALATED",
    "expect_rejections": 0,
}


# ====================================================================
# 演示库初始化
# ====================================================================
def reset_demo_db(db: AgentDB) -> None:
    """清空演示库，保证每次运行结果完全一致。

    ⚠️ 安全护栏：绝不允许对 chat.db 执行全表删除。
       这是演示脚本，但「一个手滑把生产审计记录清掉」是真实会发生的
       事故，所以宁可在代码里加一道硬检查。
    """
    if os.path.basename(db.path).lower() == "chat.db":
        raise RuntimeError("禁止对生产库 chat.db 执行演示数据重置")
    for table in (
        "agent_tool_calls", "agent_rounds", "case_events",
        "confirmation_cases", "capability_consumed", "idempotency_keys",
        "audit_logs", "evidence_files", "attendance_exceptions",
    ):
        db._write(f"DELETE FROM {table}")


def build_runtime() -> tuple[AgentDB, CapabilityGate, ToolExecutor, DemoUser]:
    db = AgentDB(DEMO_DB)
    reset_demo_db(db)
    user = DemoUser()
    db.seed_demo(user.tenant_id, user.id)
    gate = CapabilityGate(db)
    executor = ToolExecutor(db, gate)
    return db, gate, executor, user


# ====================================================================
# 轨迹打印与断言
# ====================================================================
def run_trajectory(spec: dict, db: AgentDB, gate: CapabilityGate,
                   executor: ToolExecutor, user: DemoUser) -> bool:
    print("\n" + "=" * 78)
    print(f"  {spec['name']}")
    print(f"  员工提问：{spec['question']}")
    print("=" * 78)

    print("  预期轨迹：")
    for i, line in enumerate(spec["expected"], 1):
        print(f"    {i}. {line}")
    print()
    print("  实际轨迹：")

    events: list[tuple[str, dict]] = []
    loop = AgentLoop(
        db, gate, executor,
        ScriptedLLM(spec["steps"]),
        config=AgentConfig(max_rounds=5, policy_version=18,
                           prompt_version=1, model_label="scripted-trajectory"),
        on_event=lambda t, p: events.append((t, p)),
        role="employee",
    )
    result = loop.run(spec["question"], user,
                      session_id=f"sess_{spec['name'][:2]}")

    for rnd in result.trace:
        print(f"    轮{rnd['round_no']}  模型：{rnd['assistant_text']}")
        for t in rnd["tools"]:
            # 参数只展示非凭证字段，凭证一律折叠（与日志脱敏策略一致）
            shown = {k: v for k, v in t["params"].items()
                     if not k.endswith("_token")}
            print(f"      └─ {t['name']}({json.dumps(shown, ensure_ascii=False)[:88]})")
            print(f"         status={t['status']}  {t['latency_ms']}ms  "
                  f"{t['summary'][:110]}")
    print()
    print(f"  收敛方式：{result.decision_type}　最终状态：{result.state}　"
          f"门禁拒绝：{result.gate_rejections} 次")
    print(f"  收尾话术：{result.final_text.splitlines()[0]}")

    # ---- 日志表自检 ----
    rounds = db.list_rounds(result.session_id)
    calls = db.list_tool_calls(result.session_id)
    print()
    print("  阶段 5 日志校验：")
    checks = [
        ("决策日志行数 = 实际轮次",
         len(rounds) == spec["expect_rounds"] == result.rounds_used),
        ("工具日志行数 = 实际工具调用数",
         len(calls) == spec["expect_calls"]),
        ("最终状态符合预期", result.state == spec["expect_state"]),
        ("门禁拒绝次数符合预期",
         result.gate_rejections == spec["expect_rejections"]),
        ("每轮都记录了 decision_type",
         all(r["decision_type"] for r in rounds)),
        ("每次工具都记录了耗时字段",
         all(isinstance(c["latency_ms"], int) for c in calls)),
        ("每次工具都记录了结果摘要",
         all(c["result_summary"] for c in calls)),
        ("凭证参数已脱敏（无原始 token 入日志）",
         all("<redacted:" in c["params_json"]
             for c in calls
             if "token" in c["params_json"])),
        ("分析统计可用",
         result.stats["tool_calls"] == len(calls)
         and result.stats["rounds_used"] == len(rounds)),
    ]
    ok = True
    for label, passed in checks:
        print(f"    {'[OK]' if passed else '[FAIL]'}  {label}")
        ok = ok and passed

    print(f"  分析统计：{json.dumps(result.stats, ensure_ascii=False)}")
    return ok


def print_stage5_queries(db: AgentDB) -> None:
    """把阶段 5 最常用的三条分析 SQL 直接演示出来。"""
    print("\n" + "=" * 78)
    print("  阶段 5 分析示例（agent_rounds / agent_tool_calls）")
    print("=" * 78)
    print("  1) 每轮决策耗时与工具数：")
    for r in db._read_all(
        "SELECT r.session_id, r.round_no, r.decision_type, r.latency_ms, "
        "COUNT(c.id) AS tool_cnt "
        "FROM agent_rounds r LEFT JOIN agent_tool_calls c "
        "ON c.session_id=r.session_id AND c.round_no=r.round_no "
        "GROUP BY r.session_id, r.round_no ORDER BY r.session_id, r.round_no"
    ):
        print(f"     {r['session_id']} 轮{r['round_no']} "
              f"{r['decision_type']:<12} 模型{r['latency_ms']}ms "
              f"工具{r['tool_cnt']}个")
    print("  2) 各工具调用次数与拒绝次数：")
    for r in db._read_all(
        "SELECT tool_name, COUNT(*) AS cnt, "
        "SUM(CASE WHEN result_status='gate_rejected' THEN 1 ELSE 0 END) AS rejected "
        "FROM agent_tool_calls GROUP BY tool_name ORDER BY cnt DESC"
    ):
        print(f"     {r['tool_name']:<32} {r['cnt']} 次，拒绝 {r['rejected']} 次")
    print("  3) 门禁拒绝明细（提示词是否被注入攻破的观测探针）：")
    for r in db._read_all(
        "SELECT session_id, round_no, tool_name, error_code, result_summary "
        "FROM agent_tool_calls WHERE result_status='gate_rejected'"
    ):
        print(f"     {r['session_id']} 轮{r['round_no']} "
              f"{r['tool_name']} → {r['error_code']}")
    print("  4) 管理动作日志中的轮次耗尽 / 升级事件：")
    for r in db._read_all(
        "SELECT action, target_id, created_at FROM audit_logs "
        "WHERE action IN ('agent_round_limit','escalate_case','gate_rejected') "
        "ORDER BY id"
    ):
        print(f"     {r['action']:<20} {r['target_id']}")


def verify_gate_hard_constraints(db: AgentDB, gate: CapabilityGate,
                                 executor: ToolExecutor,
                                 user: DemoUser) -> bool:
    """设计稿验收清单里的门禁用例，直接调执行器隔离验证。

    ⚠️ 这里刻意**绕过 AgentLoop** 直接打执行器：目的是证明「就算模型
       完全不听提示词、把工具调用乱发，服务端也挡得住」。
       经不经过 Harness 不影响结论，反而让测试更聚焦。
       这些调用因此不会写进 agent_rounds（那是编排决策日志，
       不是门禁单测日志），避免污染阶段 5 的统计口径。
    """
    print("\n" + "=" * 78)
    print("  硬门禁单测（绕过提示词，直接打工具执行器）")
    print("=" * 78)
    exc = "DEMO-EXC-0040"
    params = {
        "exception_id": exc, "declaration": "confirm",
        "evidence_ids": [], "claimed_minutes": 60,
        "employee_note": "门禁单测",
    }
    ctx = ToolContext(db=db, gate=gate, session_id="sess_gate_test",
                      policy_lookup=executor.policy_lookup,
                      policy_version=18, prompt_version=1)
    allowed = set(EMPLOYEE_TOOLS)

    def call(name: str, p: dict, who: DemoUser | None = None):
        return executor.execute(name, dict(p), who or user, ctx, allowed)

    def code(out) -> str:
        return str(out.result.get("error_code", ""))

    checks: list[tuple[str, bool]] = []

    # U1 最核心的一条：跳过校验直接建单
    out = call("create_confirmation_draft", params)
    checks.append(("U1 跳过校验直接建单 → ERR_CAPABILITY_REQUIRED",
                   out.status == "gate_rejected"
                   and code(out) == "ERR_CAPABILITY_REQUIRED"))

    # U2 正常校验，拿到合法凭证
    v = call("validate_evidence", params)
    token = str(v.result.get("evidence_token", ""))
    checks.append(("U2 合法校验签发一次性凭证", bool(token)))

    # U3 篡改签名
    forged = token[:-4] + "AAAA"
    out = call("create_confirmation_draft",
               {**params, "evidence_token": forged})
    checks.append(("U3 篡改签名 → ERR_TOKEN_INVALID",
                   code(out) == "ERR_TOKEN_INVALID"))

    # U4 顺序合规但内容漂移：校验 60 分钟，建单时偷偷改成 480
    out = call("create_confirmation_draft",
               {**params, "claimed_minutes": 480, "evidence_token": token})
    checks.append(("U4 校验后偷改参数 → ERR_PARAMS_MISMATCH",
                   code(out) == "ERR_PARAMS_MISMATCH"))

    # U5 凭证合法且参数一致，才真正建单
    out = call("create_confirmation_draft",
               {**params, "evidence_token": token})
    checks.append(("U5 凭证合法 + 参数一致 → 建单成功",
                   out.status == "ok" and bool(out.result.get("case_id"))))

    # U6 复用同一凭证。人为把状态拨回 EVIDENCE_CHECKED，
    #    隔离掉「状态机先拦」的干扰，专门验证 jti 的一次性。
    db._write(
        "UPDATE attendance_exceptions SET agent_state='EVIDENCE_CHECKED' "
        "WHERE id=?", (exc,))
    out = call("create_confirmation_draft",
               {**params, "evidence_token": token})
    checks.append(("U6 复用同一凭证 → ERR_TOKEN_USED",
                   code(out) == "ERR_TOKEN_USED"))

    # U7 跨租户转借凭证
    db._write("UPDATE attendance_exceptions SET agent_state='IDLE' WHERE id=?",
              (exc,))
    v2 = call("validate_evidence", params)
    token2 = str(v2.result.get("evidence_token", ""))
    outsider = DemoUser(id="u_outsider", tenant_id="ten_other",
                        username="outsider", display_name="外部租户")
    out = call("create_confirmation_draft",
               {**params, "evidence_token": token2}, outsider)
    checks.append(("U7 跨租户使用凭证 → ERR_TOKEN_SCOPE",
                   code(out) == "ERR_TOKEN_SCOPE"))

    # U8 所有失败调用零业务写入
    cnt = db._read_one(
        "SELECT COUNT(*) AS c FROM confirmation_cases WHERE exception_id=?",
        (exc,))["c"]
    checks.append(("U8 失败调用零写入，全程仅产生 1 张确认单", cnt == 1))

    for label, passed in checks:
        print(f"  {'[OK]' if passed else '[FAIL]'}  {label}")
    return all(p for _, p in checks)


def main() -> int:
    db, gate, executor, user = build_runtime()
    print("=" * 78)
    print("  阶段 4 · Agent Harness 执行循环演示")
    print("  框架：自建轻量 Harness　轮次上限：5　门禁：能力令牌 + 状态机")
    print(f"  演示库：{DEMO_DB}（独立文件，不触碰 chat.db）")
    print("=" * 78)

    results = [run_trajectory(s, db, gate, executor, user)
               for s in (T1, T2, T3)]

    # ---- 跨轨迹硬约束验证 ----
    print("\n" + "=" * 78)
    print("  跨轨迹硬约束")
    print("=" * 78)
    hard_checks = [
        ("T2 跳步被拦后经自愈建单，但未被越权提交",
         db._read_one(
             "SELECT COUNT(*) AS c FROM confirmation_cases "
             "WHERE exception_id=?", (EXC_OVERTIME,))["c"] == 1
         and db._read_one(
             "SELECT state FROM confirmation_cases WHERE exception_id=?",
             (EXC_OVERTIME,))["state"] != "SUBMITTED"),
        ("T3 材料不足的异常没有产生确认单",
         db._read_one(
             "SELECT COUNT(*) AS c FROM confirmation_cases "
             "WHERE exception_id=?", (EXC_MISSING,))["c"] == 0),
        ("T1 正常闭环产生且仅产生一张已提交单",
         db._read_one(
             "SELECT COUNT(*) AS c FROM confirmation_cases "
             "WHERE exception_id=? AND state='SUBMITTED'",
             (EXC_LATE,))["c"] == 1),
        ("全过程没有任何「自动通过」记录",
         db._read_one(
             "SELECT COUNT(*) AS c FROM case_events "
             "WHERE action LIKE '%auto%'")["c"] == 0),
    ]
    for label, passed in hard_checks:
        print(f"  {'[OK]' if passed else '[FAIL]'}  {label}")
        results.append(passed)

    results.append(verify_gate_hard_constraints(db, gate, executor, user))

    print_stage5_queries(db)
    all_ok = all(results)
    print("\n" + "=" * 78)
    print(f"  总结：{'全部通过' if all_ok else '存在失败项，请检查上方 [FAIL]'}")
    print("=" * 78)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
