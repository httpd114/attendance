"""阶段 4 · Agent 执行循环（自建轻量 Harness）。

====================================================================
一、框架选型：为什么是自建轻量 Harness
====================================================================
「Harness」指包在模型外面的执行外壳：装配提示词 → 下发工具 schema →
解析 tool_calls → 执行工具 → 结果回填 → 控制轮次 → 观测留痕。
模型只负责「下一步做什么」，其余全部归 Harness，不归模型。

| 候选 | 不采用的理由 |
|---|---|
| LangGraph | 状态图会接管决策循环与状态存储。本项目正确性依赖「服务端门禁 + 每轮落库」，框架节点里再套一层门禁很别扭；且引入有状态图运行时，与现有 FastAPI + SQLite 最小栈不匹配。9 个工具、5 轮上限，用不上它的 checkpoint / interrupt。 |
| LangChain AgentExecutor | 隐式 prompt 模板与黑盒重试，无法在每一轮精确插入审计、轮次预算与降级；版本变动频繁，生产风险高于收益。 |
| AutoGen / CrewAI | 多智能体编排。本项目是「单 Agent + 人工复核」，多智能体是过度设计，还会把「谁做的决策」搞模糊，与追责要求冲突。 |
| 供应商托管 Assistants / Threads | 循环与状态在供应商侧，租户隔离与全量审计做不到，凭证链无从插入。 |
| **自建轻量 Harness（选用）** | 循环本体约 150 行，零新依赖（复用现有 requests + SQLite）；每轮显式落库；门禁、轮次预算、错误回填、降级全部可见可测；与阶段 5 分析表天然对齐。 |

一句话：上一轮设计已经把「正确性」放在服务端硬门禁上，就不需要一个
把决策权拿走的重型框架。**框架越薄，门禁越硬。**

====================================================================
二、「轮」的定义（直接决定 5 轮够不够用）
====================================================================
一轮 = 一次模型决策 + 串行执行该决策返回的一批 tool_calls + 一次性回填。

- 无依赖的只读工具鼓励**同轮批量调用**（list / detail / lookup_policy /
  get_case_state 一次决策全发出去），避免把 5 轮预算浪费在探查上。
- 有凭证依赖的写操作**天然跨轮**：模型只有看到上一轮 validate_evidence
  回填的 evidence_token，才知道这一轮 create_confirmation_draft 该带什么。
  若模型硬把两者放进同一轮，后者会因为拿不到凭证被门禁 403——
  这不是 bug，正是 L2 在起作用，且模型能从错误里自愈。

于是主流程刚好压进 5 轮：
  轮1 批量只读 → 轮2 validate_evidence → 轮3 create_draft
  → 轮4 validate_policy_compliance → 轮5 submit_confirmation

轮次耗尽的收敛
--------------
第 5 轮若仍在调工具，Harness **不再发起第 6 次模型决策**，而是用已回填
的工具结果生成结构化收尾文本（规则模板，非模型生成），并写
audit_logs(action="agent_round_limit")。日志表因此始终保持「一行 = 一次
真实模型决策」，阶段 5 看到 rounds_used=5 且没有 final 行即可判定预算耗尽。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .agent_db import AgentDB, summarize_result
from .agent_gate import CapabilityGate
from .agent_prompt import (StateSnapshot, allowed_actions_for,
                          build_agent_system_prompt)
from .agent_tools import (ToolContext, ToolExecutor, ToolOutcome,
                         tools_for)


# ====================================================================
# 统一决策协议
# --------------------------------------------------------------------
# 真实 GLM 客户端与轨迹脚本客户端实现同一个 decide()，Harness 不关心
# 背后是 HTTP 还是预设脚本。这样三条预期轨迹可以完全离线复现，
# 而切到线上只需换一个 client 实例，循环本体一行不改。
# ====================================================================
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    assistant_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    tokens_prompt: int = 0
    tokens_completion: int = 0
    latency_ms: int = 0

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


class LLMClient(Protocol):
    def decide(self, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]], *,
               round_no: int,
               memory: dict[str, dict[str, Any]]) -> Decision: ...


class LLMDecisionError(Exception):
    """模型决策不可用（超时 / HTTP / 解析失败）。"""


class GLMClient:
    """智谱 / 任意 OpenAI 兼容端点的 function calling 客户端。

    ⚠️ 没有复用 rag_engine.OpenAICompatibleLLM：那个类是为纯文本问答写的，
       `chat()` 既不透传 tools 也不返回 tool_calls。硬改它会动到已经稳定的
       RAG 问答链路，得不偿失。Agent 侧独立一个客户端，职责更清楚。
    """

    def __init__(self, base_url: str = "", api_key: str = "",
                 model: str = "glm-4-flash", timeout: int = 60,
                 temperature: float = 0.1) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.model = model or os.getenv("LLM_MODEL", "glm-4-flash")
        self.timeout = timeout
        self.temperature = temperature

    def decide(self, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]], *, round_no: int,
               memory: dict[str, dict[str, Any]]) -> Decision:
        import requests

        t0 = time.time()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": self.temperature,
                    "max_tokens": 1024,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - 统一转成编排层可降级的错误
            raise LLMDecisionError(
                f"模型决策失败：{type(exc).__name__}: {exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}

        calls: list[ToolCall] = []
        for i, raw in enumerate(msg.get("tool_calls") or []):
            fn = raw.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(
                id=raw.get("id") or f"call_{round_no}_{i}",
                name=str(fn.get("name", "")),
                arguments=args,
            ))

        return Decision(
            assistant_text=str(msg.get("content") or ""),
            tool_calls=calls,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            model=str(data.get("model") or self.model),
            tokens_prompt=int(usage.get("prompt_tokens", 0) or 0),
            tokens_completion=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=int((time.time() - t0) * 1000),
        )


# 轨迹步骤：可以是固定 Decision，也可以是「读上一轮回填结果再决策」的函数。
# 后者是演示「结果回填 → 再决策」的关键：凭证、case_id 都是上一轮才产生的，
# 轨迹必须能动态取到，硬编码整条轨迹会假掉。
ScriptStep = Callable[[dict[str, dict[str, Any]]], "Decision | None"]


class ScriptedLLM:
    """离线轨迹客户端：按轮返回预设决策，用于三条预期轨迹与回归自测。

    存在的意义不是「假装模型」，而是把 Agent 循环变成**确定性可测**的：
    门禁是否被绕过、日志是否每轮都落、轮次上限是否生效，这些不该依赖
    GLM 当天的心情和采样温度来验证。
    """

    name = "scripted-trajectory"

    def __init__(self, steps: list[Any]) -> None:
        self.steps = steps

    def decide(self, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]], *, round_no: int,
               memory: dict[str, dict[str, Any]]) -> Decision:
        if round_no > len(self.steps):
            return Decision(assistant_text="（轨迹已结束）",
                            model=self.name)
        step = self.steps[round_no - 1]
        decision = step(memory) if callable(step) else step
        # None 表示「模型本轮不调工具，直接作答」
        if decision is None:
            return Decision(assistant_text="（本轮无工具调用）",
                            model=self.name)
        decision.model = self.name
        # 给个确定性的小耗时，让日志里的 latency_ms 字段不为 0，
        # 便于阶段 5 演示统计而不误导为真实推理耗时。
        decision.latency_ms = decision.latency_ms or (7 + round_no)
        return decision


# ====================================================================
# 配置与结果
# ====================================================================
@dataclass
class AgentConfig:
    max_rounds: int = 5            # ⭐ 硬上限，不含工具执行，指模型决策次数
    policy_version: int = 18
    prompt_version: int = 1
    custom_body: str = ""          # 租户可编辑正文段（来自 prompt_templates）
    model_label: str = "glm-4-flash"


@dataclass
class AgentResult:
    session_id: str
    final_text: str
    rounds_used: int
    decision_type: str             # final / limit_exceeded / fallback
    state: str
    gate_rejections: int
    trace: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    """Harness 本体：模型决策 → 执行工具 → 结果回填 → 再决策。

    设计约束
    --------
    1. 每轮**先落日志再执行工具**：万一工具把进程搞崩，至少留下
       「模型当时打算做什么」，阶段 5 复盘不会断档。
    2. 工具日志写在 try/finally 语义位置，失败调用也要留痕。
       否则成功率会虚高——失败的调用凭空消失了，比记错更危险。
    3. 系统消息每轮重建（状态卡刷新）。OpenAI 兼容协议每轮都发完整
       messages，所以动态 system 是合法且必要的：模型不该靠记忆推断
       自己走到第几步。
    4. 身份从 user 注入，messages 里不放 tenant_id/user_id。
    """

    def __init__(self, db: AgentDB, gate: CapabilityGate,
                 executor: ToolExecutor, llm: LLMClient,
                 config: AgentConfig | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 role: str = "employee") -> None:
        self.db = db
        self.gate = gate
        self.executor = executor
        self.llm = llm
        self.cfg = config or AgentConfig()
        self.on_event = on_event or (lambda _t, _p: None)
        # ⭐ 工具集在构造期按角色确定，之后整场会话不再变更。
        #   员工看不到复核/导出工具，管理员 Agent 只拿到只读诊断工具。
        self._tools = tools_for(role)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """事件回调。接 SSE 时把这里映射到前端状态提示即可，循环不用改。"""
        try:
            self.on_event(event, payload)
        except Exception:
            # 观察者异常绝不能影响编排正确性
            pass

    # ----------------------------------------------------------------
    # 运行时状态卡：每轮从数据库 + 上下文重新生成
    # ----------------------------------------------------------------
    def _snapshot(self, user: Any, ctx: ToolContext,
                  focus_exception: str, round_no: int) -> StateSnapshot:
        state = (self.db.get_state(user.tenant_id, user.id, focus_exception)
                 if focus_exception else "IDLE")
        return StateSnapshot(
            exception_id=focus_exception,
            state=state,
            allowed_actions=allowed_actions_for(state),
            completed_steps=list(ctx.completed_steps),
            # 只给指纹前缀，模型不需要再看到完整凭证
            held_tokens=[j[:8] for j in ctx.issued_jtis],
            remaining_rounds=self.cfg.max_rounds - round_no + 1,
            policy_version=self.cfg.policy_version,
            prompt_version=self.cfg.prompt_version,
        )

    def _system_content(self, snap: StateSnapshot) -> str:
        return build_agent_system_prompt(
            snap, custom_body=self.cfg.custom_body,
            policy_version=self.cfg.policy_version,
            prompt_version=self.cfg.prompt_version,
        )

    # ----------------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------------
    def run(self, question: str, user: Any,
            session_id: str | None = None) -> AgentResult:
        sid = session_id or ("agent_" + uuid.uuid4().hex[:12])
        ctx = ToolContext(
            db=self.db, gate=self.gate, session_id=sid,
            policy_lookup=self.executor.policy_lookup,
            policy_version=self.cfg.policy_version,
            prompt_version=self.cfg.prompt_version,
        )
        allowed_names = {t["function"]["name"] for t in self._tools}
        focus_exception = ""
        trace: list[dict[str, Any]] = []
        gate_rejections = 0
        final_text = ""
        decision_type = "limit_exceeded"

        # 规则预筛：与现有 /chat 保持一致（只召回、不定性），
        # 且用 try 包住——审计是安全网，不能因为安全网坏了让业务不可用。
        try:
            from .audit_rules import prescreen_with_patterns
            screen = prescreen_with_patterns(question)
            if screen.should_flag:
                self.db.log_action(
                    user.tenant_id, user.id, user.display_name,
                    "agent_prescreen_hit", target_type="agent_session",
                    target_id=sid,
                    detail={"reason": screen.reason,
                            "session_id": sid},
                )
        except Exception:
            pass

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": ""},
            {"role": "user", "content": question},
        ]

        for round_no in range(1, self.cfg.max_rounds + 1):
            # ---- 1) 刷新状态卡（服务端客观事实，覆盖上一轮）----
            snap = self._snapshot(user, ctx, focus_exception, round_no)
            messages[0]["content"] = self._system_content(snap)
            self._emit("round_start", {
                "round_no": round_no, "state": snap.state,
                "remaining_rounds": snap.remaining_rounds,
            })

            # ---- 2) 模型决策 ----
            try:
                decision = self.llm.decide(
                    messages, self._tools, round_no=round_no,
                    memory=ctx.memory,
                )
            except LLMDecisionError as exc:
                # 模型不可用 → 降级为「已收集结果的结构化总结」，
                # 绝不静默丢弃已执行的工具结果（那些可能已经写了数据）。
                final_text = self._finalize(ctx, snap, reason="llm_unavailable")
                self.db.log_round(
                    sid, user.tenant_id, user.id, round_no,
                    decision_type="error", assistant_text=str(exc),
                    model=self.cfg.model_label,
                    prompt_version=self.cfg.prompt_version,
                    policy_version=self.cfg.policy_version,
                    latency_ms=0,
                )
                decision_type = "fallback"
                break

            # ---- 3) ⭐ 每轮决策先落日志，再动工具 ----
            tool_names = [c.name for c in decision.tool_calls]
            self.db.log_round(
                sid, user.tenant_id, user.id, round_no,
                decision_type="tool_calls" if tool_names else "final",
                assistant_text=decision.assistant_text,
                tool_names=tool_names,
                model=decision.model or self.cfg.model_label,
                prompt_version=self.cfg.prompt_version,
                policy_version=self.cfg.policy_version,
                finish_reason=decision.finish_reason,
                tokens_prompt=decision.tokens_prompt,
                tokens_completion=decision.tokens_completion,
                latency_ms=decision.latency_ms,
            )
            self._emit("decision", {
                "round_no": round_no,
                "assistant_text": decision.assistant_text,
                "tool_names": tool_names,
            })

            # ---- 4) 最终回答：不调工具，循环正常收敛 ----
            if decision.is_final:
                final_text = decision.assistant_text or self._finalize(
                    ctx, snap, reason="completed")
                decision_type = "final"
                self._emit("final", {"round_no": round_no,
                                     "text": final_text})
                break

            # ---- 5) 把 assistant 的 tool_calls 追加进消息 ----
            # OpenAI / 智谱协议要求：assistant 消息带 tool_calls，
            # 随后每个 tool 结果用 role=tool + tool_call_id 一一对应回填。
            messages.append({
                "role": "assistant",
                "content": decision.assistant_text or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {
                         "name": c.name,
                         "arguments": json.dumps(c.arguments,
                                                 ensure_ascii=False),
                     }}
                    for c in decision.tool_calls
                ],
            })

            round_trace: dict[str, Any] = {
                "round_no": round_no,
                "assistant_text": decision.assistant_text,
                "tools": [],
            }

            # ---- 6) 串行执行本轮所有工具，逐个回填 ----
            # 串行而非并行：同一批里可能混着有依赖的调用（模型试图
            # 一轮内 validate + create），并行会放大竞态。串行下后者
            # 必然拿不到凭证，被门禁拦住，模型下一轮可自愈。
            for step_no, call in enumerate(decision.tool_calls, 1):
                self._emit("tool_start", {
                    "round_no": round_no, "step_no": step_no,
                    "tool_name": call.name,
                })
                t0 = time.time()
                outcome: ToolOutcome = self.executor.execute(
                    call.name, call.arguments, user, ctx, allowed_names)
                latency = int((time.time() - t0) * 1000)

                # ⭐ 每次工具调用都落一行：工具名 / 参数 / 耗时 / 结果摘要
                self.db.log_tool_call(
                    sid, user.tenant_id, round_no, step_no,
                    tool_name=call.name, params=call.arguments,
                    result=outcome.result, result_status=outcome.status,
                    latency_ms=latency, tool_call_id=call.id,
                    error_code=str(outcome.result.get("error_code", "")),
                    capability_jti=outcome.capability_jti,
                )
                if outcome.status == "gate_rejected":
                    gate_rejections += 1

                # 维护「当前聚焦的异常」，供下一轮状态卡使用。
                # 只认工具参数里显式带的 exception_id；提交类工具只有
                # case_id，此时沿用前面轮次已锁定的异常，不重新推断。
                exc_id = str(call.arguments.get("exception_id", ""))
                if exc_id:
                    focus_exception = exc_id

                result_message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(outcome.result,
                                          ensure_ascii=False, default=str),
                }
                messages.append(result_message)
                self._emit("tool_done", {
                    "round_no": round_no, "step_no": step_no,
                    "tool_name": call.name, "status": outcome.status,
                    "latency_ms": latency,
                    "summary": summarize_result(outcome.result, 100),
                })
                round_trace["tools"].append({
                    "name": call.name,
                    "params": call.arguments,
                    "status": outcome.status,
                    "summary": summarize_result(outcome.result, 140),
                    "latency_ms": latency,
                })

            trace.append(round_trace)
            # ---- 7) 结果已全部回填，回到步骤 1 再决策 ----

        else:
            # for 正常走完 = 5 轮全用于工具调用，没有轮次给模型收尾。
            # ⚠️ 这里绝不发起第 6 次 decide()。用规则模板收敛，
            #    并在动作日志留痕，保证「最多 5 轮」是硬约束。
            snap = self._snapshot(user, ctx, focus_exception,
                                  self.cfg.max_rounds)
            final_text = self._finalize(ctx, snap, reason="round_limit")
            self.db.log_action(
                user.tenant_id, user.id, user.display_name,
                "agent_round_limit", target_type="agent_session",
                target_id=sid,
                detail={"max_rounds": self.cfg.max_rounds,
                        "completed_steps": ctx.completed_steps,
                        "gate_rejections": gate_rejections},
            )
            self._emit("limit_exceeded", {
                "max_rounds": self.cfg.max_rounds, "text": final_text})
            decision_type = "limit_exceeded"

        state = (self.db.get_state(user.tenant_id, user.id, focus_exception)
                 if focus_exception else "IDLE")
        return AgentResult(
            session_id=sid, final_text=final_text,
            rounds_used=len(self.db.list_rounds(sid)),
            decision_type=decision_type, state=state,
            gate_rejections=gate_rejections, trace=trace,
            stats=self.db.analysis_stats(sid),
        )

    # ----------------------------------------------------------------
    # 强制收敛：规则模板生成收尾文本，不再调用模型
    # ----------------------------------------------------------------
    def _finalize(self, ctx: ToolContext, snap: StateSnapshot,
                  reason: str) -> str:
        """把已执行的工具结果整理成员工能看懂的收尾话术。

        刻意不用 LLM：轮次预算已经用完，再调一次模型就违反「最多 5 轮」。
        而且这类总结是确定性的，用模板比让模型自由发挥更可控、更省钱。
        """
        # 业务已经办完时不要再强调「轮次用尽」——员工看到
        # 「已提交」和「先到此为止」并列会以为办理失败了。
        # 轮次上限是内部约束，只有业务未办完时才需要如实告知。
        if snap.state == "SUBMITTED":
            head = "确认单已提交至管理员复核队列，本次办理完成。"
        else:
            head = {
                "round_limit": "已达到 5 轮决策上限，本次会话先到此为止。",
                "llm_unavailable": "模型服务暂时不可用，已保留本次会话的处理结果。",
                "completed": "本次处理已完成。",
            }.get(reason, "本次会话结束。")

        lines = [head, ""]
        if ctx.completed_steps:
            lines.append("已完成的步骤：" + "、".join(ctx.completed_steps))
        lines.append(f"当前状态：{snap.state}")

        state_hint = {
            "IDLE": "尚未建单，请补充材料后重新开始。",
            "ASK_FOR_MORE": "材料校验未通过，请补齐证明后重新校验。",
            "EVIDENCE_CHECKED": "材料已校验通过，确认单尚未创建。",
            "DRAFT_CREATED": "草稿已创建，尚未提交复核。",
            "COMPLIANCE_CHECKED": "合规复核已通过，等待你明确确认提交。",
            "SUBMITTED": "已提交至管理员复核队列，请等待人工复核结果。",
            "ESCALATED": "已升级上报管理员，系统不会自动通过超时异常。",
        }.get(snap.state, "请联系管理员确认后续处理。")
        lines.append("下一步：" + state_hint)

        if reason == "round_limit" and snap.state in (
                "IDLE", "ASK_FOR_MORE", "EVIDENCE_CHECKED"):
            lines.append("")
            lines.append("提示：受轮次上限保护，系统不会在校验未完成的情况下")
            lines.append("自动替你建单。请发起新一轮会话继续办理。")
        return "\n".join(lines)
