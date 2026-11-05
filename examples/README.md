# examples/ 可运行示例

## `demo.py` · Agent 三条典型轨迹（推荐先跑这个）

不启动服务、不需要 API Key、不碰 `data/chat.db`，用 `ScriptedLLM` 复现
模型决策序列，因此可以稳定重放：

```bash
python examples/demo.py
```

跑完会输出三条轨迹与 8 条绕过提示词的硬门禁单测，退出码 0 表示全通过：

| 轨迹 | 预期结果 |
|---|---|
| T1 正常确认闭环 | 5 轮 / 8 工具 / 最终 `SUBMITTED` / 0 拒绝 |
| T2 跳步被拦自愈 | 首个 `create_confirmation_draft` 被 `ERR_CAPABILITY_REQUIRED` 拦下，模型回补校验；末次因员工未明确同意被 `ERR_EXPLICIT_CONSENT` 拦下，零越权提交 |
| T3 材料不足升级 | 校验 `ok=false` → `escalate_case` → `ESCALATED`，零确认单 |

8 条单测直接打工具执行器（**故意绕过 AgentLoop**），验证伪造签名、参数漂移、
复用 `jti`、跨租户转借都被服务端门禁拒绝，且失败调用零业务写入。
演示库是独立的 `data/agent_demo.db`，脚本内有「禁止对 chat.db 做全表删除」的护栏。

## `rag-proto/` · 阶段 1 裸问答 RAG 原型

早期单脚本原型，用来验证 Markdown 按标题切片、FAISS 索引、混合检索与拒答。
它**不依赖主应用包**，可单独运行：

```bash
cd examples/rag-proto
LLM_API_KEY=<你的密钥> python rag_proto.py
```

主应用现在的检索能力在 `src/attendance_agent/core/rag_engine.py` 与 `kb.py`，
这个原型只作为可追溯的演进记录保留，不要再往里加新功能。

## 共同约定

两个示例都不需要 `pip install -e .`：`demo.py` 会自己把 `src/` 加进
`sys.path`。若你改了包结构导致示例跑不起来，请同步修这里的路径说明。
