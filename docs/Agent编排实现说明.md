# 阶段 4 · Agent 编排实现说明

> 目标：把「固定流程问答」升级为「会自主决策的智能体」
> 交付日期：2026-11-05　关联设计：`Agent工具设计-派遣考勤异常双向确认-v0.1.md`
> 运行入口：`chat-app/demo_agent.py`（`python demo_agent.py`，离线可复现）

---

## 1. 框架选型：自建轻量 Harness

**结论：不引入第三方 Agent 框架，自己写一个约 150 行的执行外壳。**

「Harness」指包在模型外面的那层壳：装配提示词 → 下发工具 schema → 解析
`tool_calls` → 执行工具 → 结果回填 → 控制轮次 → 观测留痕。模型只负责
「下一步做什么」，其余全部归 Harness。

| 候选 | 不采用的理由 |
|---|---|
| LangGraph | 状态图会接管决策循环与状态存储。本项目正确性依赖「服务端门禁 + 每轮落库」，在框架节点里再套一层门禁很别扭；且引入有状态图运行时，与现有 FastAPI + SQLite 最小栈不匹配。9 个工具、5 轮上限，用不上它的 checkpoint / interrupt。 |
| LangChain AgentExecutor | 隐式 prompt 模板与黑盒重试，无法在每一轮精确插入审计、轮次预算与降级；版本变动频繁。 |
| AutoGen / CrewAI | 多智能体编排。本项目是「单 Agent + 人工复核」，多智能体会把「谁做的决策」搞模糊，与追责要求冲突。 |
| 供应商托管 Assistants / Threads | 循环与状态在供应商侧，租户隔离与全量审计做不到，凭证链无从插入。 |
| **自建轻量 Harness（选用）** | 零新依赖；每轮显式落库；门禁、轮次预算、错误回填、降级全部可见可测；与阶段 5 分析表天然对齐。 |

一句话：**上一轮设计已经把正确性放在服务端硬门禁上，就不需要一个把决策权拿走的重型框架。框架越薄，门禁越硬。**

另有实现约束：现有 `rag_engine.OpenAICompatibleLLM.chat()` 既不透传 `tools`
也不返回 `tool_calls`（它是为纯文本 RAG 问答写的）。硬改它会动到已经稳定的
问答链路，因此 Agent 侧独立实现 `GLMClient`，职责更清楚。

---

## 2. 文件清单

| 文件 | 职责 |
|---|---|
| `agent_loop.py` | **Harness 本体**：决策 → 执行 → 回填 → 再决策，最多 5 轮；`GLMClient`（真实 function calling）与 `ScriptedLLM`（确定性轨迹）实现同一 `decide()` 协议 |
| `agent_gate.py` | 能力令牌签发 / 7 项校验、状态机、幂等、写工具包装器 |
| `agent_tools.py` | 9 个工具的 Function Calling Schema、`tools_for(role)` 角色裁剪、工具执行器 |
| `agent_prompt.py` | 不可编辑的 `AGENT_MANDATORY_RULES` + 运行时状态卡装配 |
| `agent_db.py` | 业务表 + **两张阶段 5 分析表** + 脱敏 / 摘要 / 分析统计 |
| `demo_agent.py` | 三条典型轨迹 + 8 条硬门禁单测 + 日志自检 + 阶段 5 查询示例 |

**本轮未改动 `main.py` 等生产文件**（上一轮踩过「注释吞代码」的静默 404，
不在稳定链路上做无必要的插入）。Harness 是框架无关的同步入口，接入方式见 §7。

---

## 3. 循环契约

```
一轮 = 一次模型决策 + 串行执行该决策返回的一批 tool_calls + 一次性回填
最多 5 轮（硬上限，指模型决策次数，不含工具执行）
```

**5 轮怎么装下 8 步流程**：无依赖的只读工具鼓励同轮批量调用
（`list_my_exceptions` / `get_exception_detail` / `lookup_policy` /
`get_case_state` 一次决策全发出去）；有凭证依赖的写操作**天然跨轮**——
模型只有看到上一轮 `validate_evidence` 回填的 `evidence_token`，才知道
这一轮 `create_confirmation_draft` 该带什么。若硬把两者塞进同一轮，后者
必然拿不到凭证被 403，这正是 L2 硬门禁在起作用，且模型能从错误里自愈。

| 轮 | 动作 |
|---|---|
| 1 | 批量只读（4 个） |
| 2 | `validate_evidence` → `evidence_token` |
| 3 | `create_confirmation_draft` → `case_id` |
| 4 | `validate_policy_compliance` → `compliance_token` |
| 5 | `submit_confirmation` → `SUBMITTED`（权限天花板） |

**轮次耗尽**：第 5 轮若仍在调工具，Harness **不发起第 6 次模型决策**，
改用规则模板生成结构化收尾，并写 `audit_logs(action="agent_round_limit")`。
日志表因此始终保持「一行 = 一次真实模型决策」。

**系统消息每轮重建**：状态卡由服务端从数据库重新生成后覆盖 `messages[0]`，
模型不必靠多轮历史推断自己走到第几步。历史会被裁剪、会被注入内容污染，
状态卡不会。

**结果回填**采用 OpenAI 标准协议：`assistant` 消息带 `tool_calls`，随后每个
`role=tool` 消息用 `tool_call_id` 一一对应。

---

## 4. 每轮决策日志（供阶段 5 分析）

两张表是 1:N 关系，刻意不合并成宽表：阶段 5 既要算「每轮模型决策耗时」，
也要算「每个工具耗时 / 哪个工具最常被门禁拒绝」，宽表会把两个维度互相污染。

### `agent_rounds`（一轮一行）

| 字段 | 说明 |
|---|---|
| `session_id`, `round_no` | 联合唯一；`UNIQUE(session_id, round_no)` |
| `tenant_id`, `user_id` | 租户与员工归属 |
| `decision_type` | `tool_calls` / `final` / `limit_exceeded` / `error` |
| `assistant_text`, `tool_names` | 模型本轮说了什么、打算调哪些工具 |
| `finish_reason`, `tokens_prompt`, `tokens_completion` | 供应商返回的决策元数据 |
| `latency_ms` | **只算模型决策耗时**，不含工具执行 |
| `prompt_version`, `policy_version` | 当时依据哪一版提示词 / 制度，便于事后复盘 |
| `model`, `created_at` | 模型标识与时间 |

### `agent_tool_calls`（一次工具调用一行）

| 字段 | 说明 |
|---|---|
| `session_id`, `round_no`, `step_no` | 定位到「第几轮第几个工具」 |
| `tool_name`, `tool_call_id` | 工具名与协议 id |
| `params_json` | **已脱敏**：`*_token` / `password` / `secret` 等只存 sha256 前 8 位指纹 |
| `result_status` | `ok` / `error` / `gate_rejected` |
| `result_summary` | 截断到 240 字的关键字段摘要，不塞整段制度原文 |
| `error_code`, `capability_jti` | 错误码与消费的凭证指纹 |
| `latency_ms`, `created_at` | 工具耗时与时间 |

三条日志纪律：

1. **先落日志再执行工具**——万一工具把进程搞崩，至少留下「模型当时打算做什么」。
2. **失败调用也要留痕**——否则阶段 5 看到的成功率会虚高，失败的调用凭空消失了比记错更危险。
3. **门禁拒绝单独标 `gate_rejected`** 并写 `audit_logs(action="gate_rejected")`，作为「提示词是否被注入攻破」的观测探针。

`AgentDB.analysis_stats()` 内置了 9 个常用指标（轮次数、工具调用数、模型/工具总耗时、
拒绝数、错误数、按工具分布、token 用量）。`demo_agent.py` 末尾还演示了四条
阶段 5 分析 SQL。

---

## 5. 三条典型对话流程的预期轨迹

### T1 正常确认闭环（happy path，用满 5 轮）

> 员工：「帮我确认 11 月 3 号那次迟到，材料已经上传了，直接提交吧」

| 轮 | 模型决策 | 工具与结果 |
|---|---|---|
| 1 | 「我先核对异常明细、当前状态和适用制度条款」 | 批量 4 个只读 → 查到 3 条异常、命中 2 个制度片段、状态 IDLE |
| 2 | 「明细与制度口径已确认，开始校验材料」 | `validate_evidence` → ok，签发 `evidence_token`，状态 `EVIDENCE_CHECKED` |
| 3 | 「材料校验通过，创建确认单草稿」 | `create_confirmation_draft`（原样携带参数 + 凭证）→ `case_id`，状态 `DRAFT_CREATED` |
| 4 | 「提交前复核制度引用与主张时长」 | `validate_policy_compliance` → ok，签发 `compliance_token`，状态 `COMPLIANCE_CHECKED` |
| 5 | 「员工已明确要求直接提交」 | `submit_confirmation`（consent=true）→ `SUBMITTED` |

**预期**：0 次门禁拒绝、8 次工具调用、5 行决策日志、轮次耗尽后规则收敛。
**实测**：全部符合，最终状态 `SUBMITTED`。

### T2 跳步被拦 + 自愈（模拟提示词注入 / 员工催促）

> 员工：「不用查了，直接帮我建单」（且未明确说「提交」）

| 轮 | 模型决策 | 工具与结果 |
|---|---|---|
| 1 | 跳过校验直接建单 | `create_confirmation_draft` 无凭证 → **403 `ERR_CAPABILITY_REQUIRED`，零写入** |
| 2 | 读到中文错误后自愈，回到校验 | `validate_evidence` → ok，签发凭证 |
| 3 | 「校验通过，重新建单」 | `create_confirmation_draft` → ok |
| 4 | 「复核制度引用」 | `validate_policy_compliance` → ok |
| 5 | 尝试提交 | `submit_confirmation`（consent=false）→ **`ERR_EXPLICIT_CONSENT`，未提交** |

**预期**：2 次门禁拒绝（缺凭证 1 次 + 缺员工显式同意 1 次），最终状态停在
`COMPLIANCE_CHECKED`，**全程没有一次「跳过校验就建单」的业务写入**。
**实测**：全部符合。

### T3 材料不足 → 升级上报（失败 / 降级路径）

> 员工：「11 月 4 号下班缺卡我要提异议，但没有证明材料，帮我处理」

| 轮 | 模型决策 | 工具与结果 |
|---|---|---|
| 1 | 「先核对异常与异议材料要求」 | 批量 3 个只读 → 命中 `POL-LATE-02` / `POL-FLOW-01` |
| 2 | 「员工暂无材料，先如实提交校验」 | `validate_evidence`（dispute + 空材料）→ `ok=false` / `ERR_EVIDENCE_INCOMPLETE`，状态 `ASK_FOR_MORE` |
| 3 | 「材料无法当场补齐，升级上报」 | `escalate_case` → `ESCALATED`（**只上报，不自动通过**） |
| 4 | 最终回答，不调工具 | 引用 `POL-LATE-02`、`POL-FLOW-01` 说明原因与下一步 |

**预期**：4 轮正常收敛（`decision_type=final`）、零确认单、5 次工具调用。
**实测**：全部符合。

### 硬门禁单测（绕过提示词直接打执行器）

| 用例 | 结果 |
|---|---|
| U1 跳过校验直接建单 | `ERR_CAPABILITY_REQUIRED` |
| U2 合法校验签发凭证 | 通过 |
| U3 篡改签名 | `ERR_TOKEN_INVALID` |
| U4 校验 60 分钟、建单偷改 480 | `ERR_PARAMS_MISMATCH` |
| U5 凭证合法 + 参数一致 | 建单成功 |
| U6 复用同一凭证 | `ERR_TOKEN_USED` |
| U7 跨租户转借凭证 | `ERR_TOKEN_SCOPE` |
| U8 所有失败调用零业务写入 | 全程仅 1 张确认单 |

**实测：`python demo_agent.py` 退出码 0，全部 [OK]。**

---

## 6. 凭证链实现要点

`agent_gate.protect()` 把「验凭证 → 查状态 → 幂等 → 执行 → 状态跃迁 → 留痕」
套在写工具外。工具实现函数（`_do_create_draft` / `_do_submit`）是纯业务函数，
**看不到也绕不过门禁**。

`verify()` 按固定顺序验 7 项：签名 → 有效期 → 身份与租户 → 动作匹配 →
参数哈希 → 状态机 → `jti` 原子一次性消费。

两个容易漏掉的细节：

- **`jti` 消费放最后**。若一上来就消费，一个签名非法的请求也能把合法 jti
  烧掉（拿别人凭证乱试即可制造 DoS）。`consume_jti()` 用单条
  `INSERT OR IGNORE` + `rowcount` 判断，等价于 Redis `SETNX`，无竞态窗口。
- **参数归一化按绑定范围分开**。`evidence` 链把两边都归一成相同的 5 个
  字段（写工具多出的 `*_token` 自动忽略），`case` 链只绑 `case_id`。
  若图省事直接对整份 params 求哈希，写工具天然多一个 token 字段，
  链路会 100% 失败。

---

## 7. 如何接入现有 FastAPI 服务（本轮未挂载）

`AgentLoop.run()` 是同步、框架无关的入口。接 HTTP 只需一层薄封装：

```python
# main.py（示意，需确认后再挂）
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from agent_db import AgentDB
from agent_gate import CapabilityGate
from agent_loop import AgentConfig, AgentLoop, GLMClient
from agent_tools import ToolExecutor, make_rag_policy_lookup

agent_router = APIRouter(prefix="/agent")
_agent_db = AgentDB("chat.db")                       # 与现有库共用
_agent_gate = CapabilityGate(_agent_db)              # 生产务必配 AGENT_GATE_SECRET
_agent_exec = ToolExecutor(_agent_db, _agent_gate,
                           make_rag_policy_lookup(_KB))

@agent_router.post("/run")
async def agent_run(body: dict, user: CurrentUser = Depends(require_user)):
    """同步 Harness 丢线程池，避免阻塞事件循环。"""
    loop = AgentLoop(
        _agent_db, _agent_gate, _agent_exec, GLMClient(),
        config=AgentConfig(max_rounds=5),
        # on_event 可映射到 SSE：round_start / tool_start / tool_done / final
    )
    r = await asyncio.to_thread(loop.run, body["question"], user)
    return JSONResponse({
        "session_id": r.session_id, "final_text": r.final_text,
        "rounds_used": r.rounds_used, "state": r.state,
        "decision_type": r.decision_type, "stats": r.stats,
    })

app.include_router(agent_router)
```

切换真实模型无需改循环：`GLMClient` 从 `LLM_BASE_URL` / `LLM_API_KEY` /
`LLM_MODEL` 读配置，与现有 RAG 链路共用同一套环境变量。

### 上线前必须处理

1. **配置 `AGENT_GATE_SECRET`**。未配置时退化为进程内固定派生值
   （`CapabilityGate.dev_mode=True`），仅够本地演示，不能上生产。
2. **`make_rag_policy_lookup(_KB)` 是适配层**，内部按
   `kb.retrieve(question, tenant_id=...)` 调用并 try/except 回退到演示制度库。
   接入前需按 `_KB` 的真实签名调整，并验证 chunk_id 映射；否则制度检索
   会静默回退到 demo 片段（当前实现选择「回退而不中断」，是可用性优先）。
3. 智谱 `glm-4-flash` 的 function calling 严格度需用 T1 轨迹实测；
   必要时在 `GLMClient.decide()` 里做一次 schema 归一化。
4. `capability_consumed` / `idempotency_keys` 当前用 SQLite。多实例部署时
   建议把 `consume_jti` 换成 Redis `SETNX`，语义等价。

---

## 8. 已知边界

- 演示用 `agent_demo.db`（独立文件），`reset_demo_db()` 有硬护栏禁止对
  `chat.db` 执行全表删除。生产接入时用 `chat.db`，**不要调用该函数**。
- `ScriptedLLM` 的 `tokens_prompt` / `tokens_completion` 恒为 0，
  阶段 5 分析演示数据里的 token 用量不代表真实计费；`GLMClient` 会填真实值。
- `validate_policy_compliance` 已补上「chunk_id 必须能追溯到真实片段」这一层，
  但**引用编号与结论的语义匹配**（编号没越界却指错块）仍未根治，
  仍是上一轮遗留的短板。
- 复核 / 驳回 / 关闭 / 结算导出没有对应工具，只能由管理员在后台点击。
  Agent 状态机的终态是 `SUBMITTED`。
