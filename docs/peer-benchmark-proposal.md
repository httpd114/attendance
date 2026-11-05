# 派遣岗位需求单版本交接与人工复核 · 同行方案

> 对标对象：上海外服（集团）有限公司 · 人才派遣岗位需求单场景
> 上游输入：`discovery-report.md`（需求发现报告，合成演练）
> 案例来源：GitHub 公开仓库，核验日期 **2026-10-23**（星标数/许可为核验当日快照）
> 文档性质：同行方案（Peer Benchmark Proposal），**不是需求确认结论**

---

## 0. 阅读前提

本报告做的事是：把 `discovery-report.md` 里六条待验证需求（REQ-1 ~ REQ-6），翻译成一组**已被开源社区工程化验证过的技术构件**，再从 GitHub 找出对应实现，说明「借鉴什么、不借鉴什么、落地代价多大」。

三条边界必须先说清楚：

1. 上游报告全部证据来自**合成演练访谈与虚构样本**，重复澄清 22% 等基线为虚构数据。本报告不对这些数字做任何再加工。
2. 上游报告明确：四类来源（Buyer / Brief / Industry / Operator）**尚未收敛**，Industry 与 Operator 侧证据不足。本报告提供的正是 Industry 侧材料，但它**不能替代** Operator 侧的真实样本核验。
3. GitHub 星标数只说明社区采用度，**不等于**生产可用性、不等于合规适配。每个案例都标注了「不适用点」。

---

## 1. 从业务问题到技术构件

上游报告的核心瓶颈是：**岗位条件在电话中被修改，但交接单仍引用旧技能要求（f10、f11）**。

这个问题不是"抽取不准"，而是**四个构件同时缺失**。把六条 REQ 重排到构件上，才能对标到开源实践：

| 构件 | 定义 | 覆盖 REQ | 缺失时的表现（对应事实编号） |
|---|---|---|---|
| **C1 证据锚定** | 每条输出能定位回源文件的确切位置 | REQ-3 | 一线拿到长摘要，无法判断哪句对应哪张单（f15） |
| **C2 版本与时效** | 双时间轴 + 确认状态，判版本不靠文件名 | REQ-2、REQ-4 | 版本断链（f33）、回填时间被当成发生时间（f13） |
| **C3 人工回路** | 变更必须经具名人员批准，拒绝是路由不是异常 | REQ-1 | 口头改动无人回填、无人确认（f10、f22） |
| **C4 审计与复盘** | 谁查过、用哪版、谁确认；误报漏报进周度复盘 | REQ-5、REQ-6 | 审计只留最终摘要（f45）、只改模板不核效果（f55） |

> 这个拆分直接对应上游报告 `frameworks/workflow-decomposition.md` 的要求：区分**判断点与执行点**，在不可逆动作前设置确定性校验，为低置信/缺资料/冲突安排**具名人工接收人**。

---

## 2. 同行案例总览矩阵

| # | 仓库 | ★ | 语言 / 许可 | 核心机制 | 对应构件 | 借鉴度 |
|---|---|---|---|---|---|---|
| 1 | `google/langextract` | 39.0k | Python / Apache-2.0 | 抽取结果映射回源文本字符区间 | **C1** | 高（直接对口 REQ-3） |
| 2 | `langchain-ai/langgraph` | 43.7k | Python / MIT | `interrupt()` 动态中断 + `Command(resume=)` | **C3** | 高（直接对口 REQ-1） |
| 3 | `temporalio/temporal` | 23.9k | Go / MIT | Signal/Update + 持久化定时器 + Event History | C3 + C4 | 高（审计留痕最强） |
| 4 | `xtdb/xtdb` | 3.1k | Clojure / MPL-2.0 | 双时态（system time / valid time），SQL:2011 | **C2** | 中高（语义标杆，落地较重） |
| 5 | `sirixdb/sirix` | 1.2k | Java / BSD-3 | 追加式文档版本树 + 修订作者 + Merkle 校验 | C2 + C4 | 中（版本树可视化值得借鉴） |
| 6 | `django-commons/django-simple-history` | 2.5k | Python / BSD-3 | 模型级历史留存与回滚 | C2（轻量替代） | 中（四周试点更现实） |
| 7 | `OpenLineage/OpenLineage` | 2.7k | Java / Apache-2.0 | 字段级血缘元数据开放标准 | C4 + REQ-5 | 中（口径对齐） |
| 8 | `datacontract/datacontract-cli` | 1.1k | JS / MIT | 数据契约测试（缺失值 / 枚举 / 非空） | C1 前置 | 中（字段对齐 f31） |
| 9 | `HumanSignal/label-studio` | 28.5k | TS / Apache-2.0 | 标注 + 审阅 + 争议仲裁界面 | C1 + C4 | 中（复核界面现成） |
| 10 | `argilla-io/argilla` | 5.2k | Python / Apache-2.0 | AI 工程师与领域专家协同反馈闭环 | C4（REQ-6） | 中（误报漏报收集） |
| 11 | `mlflow/mlflow` | 28.5k | Python / Apache-2.0 | 运行级 tracing / 评估 / 制品留痕 | C4 | 中低（留痕可复用） |
| 12 | `camunda/camunda` | 4.3k | Java / 开源 | BPMN 人工任务 + 责任矩阵 | C3 | 中（责任边界建模参考） |
| 13 | `Netflix/conductor` | 12.8k | Java / Apache-2.0 | 微服务编排 + 人工任务节点 | C3 | 低-中（备选编排） |
| 14 | `567-labs/instructor` | 14.1k | Python / MIT | 结构化输出校验（Pydantic 约束） | C1 辅助 | 低-中 |
| 15 | `frappe/hrms` | 8.9k | Python / GPL-3.0 | 开源 HR 与薪资，含审批工作流 | 领域参考 | 低（领域贴近但 GPL 需注意） |

---

## 3. 分构件详解：案例 → 机制 → 借鉴

### 3.1 C1 证据锚定 —— `google/langextract`

**对应 REQ-3：一页缺项清单 + 来源位置，替代泛化摘要。**

上游 f15 原文：「我需要一页缺项清单和对应岗位服务需求单的来源位置，不需要一份很长的泛化摘要」。这句话几乎就是 LangExtract 的产品定义。

**核心机制（已核验）：**

- 每条抽取结果携带 `char_interval`，标记它在源文本中的**确切字符区间**；可视化时可直接高亮回原文。
- 若模型抽取的内容**无法在源文本中定位**（典型情况是模型从 few-shot 示例里抄了内容，而不是从输入抽取），`char_interval` 为 `None`。官方推荐的过滤写法：
  ```python
  [e for e in result.extractions if e.char_interval]
  ```
- 长文档用分块 + 并行 + 多轮 pass 提高召回：`extraction_passes`、`max_workers`、`max_char_buffer`。
- 输出 JSONL → `lx.visualize(...)` 生成**自包含交互式 HTML**，可在原始上下文中审阅数千条抽取。
- 支持 Gemini / OpenAI / Ollama；`output_schema` 在 Gemini 与 OpenAI 可用，**Ollama 当前不支持**。

**映射到本场景：**

| 上游要求 | LangExtract 对应物 |
|---|---|
| 一页缺项清单（f15） | `result.extractions` 列表，逐条列出 `extraction_class` + `extraction_text` |
| 来源位置（f15） | `char_interval` + `lx.visualize()` 高亮 |
| 每条结果关联来源（f42） | 结构化 JSONL，一行一条证据 |
| 缺失值与未确认值必须区分（f31） | `extraction_class` 枚举 + `attributes` 显式区分 `缺失` / `未确认` |

**不适用点 / 风险（必须写进方案）：**

1. **`char_interval is None` 的处理是硬约束。** 上游 f44 要求「无法确认版本或关键字段时进入人工处理队列，不能默认通过」。若直接把未锚定结果写进交接单，等于静默通过——**这正好落在傅景划的红线上**。必须显式路由到人工队列。
2. 官方 README 注明 **"This is not an officially supported Google product"**，且抽取准确度依赖所选模型、任务复杂度与示例质量，不存在准确率承诺。
3. 若试点要求本地化部署（脱敏只读导出 f32 语境下很可能如此），走 Ollama 会失去 `output_schema` 约束能力，需要额外用 `instructor` + Pydantic 补一层校验。

---

### 3.2 C2 版本与时效 —— 双时态：`xtdb/xtdb` / `sirixdb/sirix` / `django-simple-history`

**对应 REQ-2（版本有效期 + 确认状态双条件）与 REQ-4（业务发生时间 vs 系统接收时间分离）。**

上游 f34 原文：「更新岗位服务需求单时同时记录业务发生时间和系统接收时间；过期来源不能覆盖已确认的新版本」——这在数据库领域有标准名字：**双时态（bitemporal）**。

**核心机制（已核验）：**

- **XTDB**：所有数据按更新时刻版本化（**system time**），同时允许独立记录与查询该数据在业务域中何时生效（**valid time**）。实现 SQL:2011 标准的双时态语义，可经 Postgres 线协议访问，无需预先定义 schema。
- **SirixDB**：追加式（append-only）文档存储，每个 JSON/XML 资源保留完整修订树；修订元数据含 `sdb:revision`、`sdb:timestamp`、`sdb:item-history`、**作者跟踪与 commit message**，并可选逐节点 Merkle 哈希做篡改检测；提供 Web GUI 可视化修订历史与 diff。
- **django-simple-history**（轻量替代）：模型级历史留存，可从 admin 查看与回滚。

**映射到本场景：**

| 上游要求 | 双时态对应物 |
|---|---|
| 业务发生时间（f34） | **valid time** —— 电话里改条件的那一刻 |
| 系统接收时间（f34） | **system time** —— 补录进系统的那一刻 |
| 版本有效期（f24） | `valid_from` / `valid_to` 区间 |
| 确认状态（f24） | 与版本**正交的独立字段**，不混入时间戳 |
| 不靠文件名猜版本（f24） | 版本由查询判定（`AS OF` / `valid_at`），命名不参与语义 |
| 过期来源不覆盖已确认新版本（f34） | 写入时校验 valid time；早于已确认版本的来源拒绝覆盖 |
| 读取失败保留上次快照（f35） | 快照 + 时间戳标记，提示人工核查，不静默重试 |

**关键判断：不要用 XTDB 做试点。**

XTDB 语义最正、最贴合，但引入一门新的数据库（Clojure/Kotlin 栈、新运维面）**超出上游 f03 的预算约束**（四周、两名骨干、每周 ≤4 小时 ≈ 32 人时）。

推荐做法：**借鉴其语义，不搬运其实现**。在现有只读导出的基础上加一张快照表 + 四个字段即可复现核心语义：

```
valid_from        -- 业务发生时间（电话改条件的时刻）
valid_to          -- 有效期止
system_received_at-- 系统接收时间（补录时刻）
confirm_status    -- 确认状态：未确认 / 已确认 / 已作废（与时间戳正交）
invalidate_reason -- 作废原因（f24 要求保留）
confirmed_by      -- 确认人（f25 要求登记责任人）
```

SirixDB 的**修订树可视化与 diff**、以及**作者跟踪 + 变更说明**是值得单独借鉴的 UI 模式——它直接满足 f43「保留原始版本、修订原因与确认人」。

---

### 3.3 C3 人工回路与审批 —— `langchain-ai/langgraph` / `temporalio/temporal` / `camunda/camunda`

**对应 REQ-1：口头变更需强制补单并经业务负责人确认后才进入交接。**

这是六条 REQ 里唯一 P0 且直接卡住责任归属的一条。上游 f22 已明确权限边界：一线专员**可提交**变更，**正式确认由业务负责人完成，模型没有审批权限**。技术构件必须能表达这个边界，而不是"让模型判断要不要批"。

#### LangGraph：`interrupt()` 是"批准/拒绝"的一等公民

**核心机制（已核验）：**

- 在图节点或工具内调用 `interrupt(payload)`，图执行**暂停**；通过 checkpointer + `thread_id` 保存状态。
- 恢复时用相同 `thread_id` 传入 `Command(resume=<value>)`，该值成为 `interrupt()` 的返回值。
- `response_schema`（需 langgraph ≥ 1.2.12）可对人工输入做 Pydantic 校验，无效输入抛 `ValidationError`，可重试。
- **拒绝不是抛异常**：resume 值引导节点 `return Command(goto="cancel")`，写入 `rejected` 状态。
- 官方明确：**静态断点 `interrupt_before` / `interrupt_after` 仅用于调试，不推荐用于 HITL**；HITL 必须用动态 `interrupt()`。
- **恢复时节点从开头重跑**，`interrupt()` 之前的代码每次都会再执行 —— 因此 `interrupt()` 之前的副作用必须**幂等**，或移到 `interrupt()` 之后。

**为什么这套机制对口 REQ-1：**

| 上游约束 | LangGraph 对应行为 |
|---|---|
| 模型没有审批权限（f22） | 审批由外部 resume 输入，不由图内逻辑决定 |
| 未确认项不能归到对方名下（f25） | 中断 payload 携带提交人 + 变更内容，resume 携带确认人身份 |
| 不能默认通过（f44） | 无 resume = 永久停在中断点，不会自动放行 |
| 不悄悄重试业务动作（f35） | 节点重跑语义强制要求副作用幂等，天然排斥隐式重试 |
| 截止时间（f23） | 中断可配持久化超时，超时走升级分支 |

#### Temporal：超时升级与审计历史

- **Signal / Update** 接收人工输入，**持久化定时器（durable timer）** 表达"补单时限"——超时即自动升级，对应 f23 要求的「交接须列明截止时间」。
- **Event History** 完整记录每一次状态迁移与输入，天然满足 f45「审计要看到谁查过、用哪个版本、提出什么建议及谁确认，访问记录不能只留最终摘要」。

#### Camunda / Conductor：责任矩阵建模

`camunda/camunda`（BPMN 人工任务）与 `Netflix/conductor`（人工任务节点）的价值在于**把责任边界画成流程模型**，正好对应 f25「跨团队变更需要登记责任人」。上游 f04 划定的三方权力结构（业务负责人定范围 / 质量负责人保留停止权 / 客户协同代表确认用途）用 BPMN 泳道表达最为直观。

**不适用点：** Camunda 与 Conductor 是 Java 栈的重量级编排平台，四周试点内引入不现实。建议**只借鉴其责任矩阵的表达方式**，运行层用 LangGraph 或 Temporal 实现。

---

### 3.4 C4 审计与复盘 —— `OpenLineage` / `datacontract-cli` / `argilla` / `mlflow`

**对应 REQ-5（跨团队口径对齐）与 REQ-6（投诉与周度复盘）。**

| 上游要求 | 案例 | 机制 |
|---|---|---|
| 以原始记录定义口径，不比对两张汇总表（f53） | **OpenLineage** | 字段级血缘，把"这个数从哪来"变成可查询元数据，而非口头约定 |
| 关键字段对齐、缺失值与未确认值区分（f31） | **datacontract-cli** | 数据契约测试：字段存在性、枚举取值、非空约束，失败即阻断 |
| 每周收集误报、漏报、未确认项共同复盘（f55） | **Argilla / Label Studio** | 领域专家标注界面 + 反馈闭环，把"误报"变成可统计的结构化记录 |
| 谁查过、用哪个版本、谁确认（f45） | **Temporal Event History** / **MLflow tracing** | 运行级完整留痕 |
| 只修模板不核对效果不足以结束试点（f55） | **MLflow 评估** | 同一批样本跨版本跑评估，效果可比 |

`HumanSignal/label-studio`（28.5k ★）与 `argilla-io/argilla`（5.2k ★）的取舍：Label Studio 通用性强、界面成熟，适合给林亦舟、傅景这类非工程角色做复核；Argilla 更贴近 LLM 输出的反馈闭环。试点期建议 **Label Studio 优先**（人多、界面稳），复盘数据结构参考 Argilla 的反馈模型。

---

## 4. 整合参考架构

```
┌─ L0 只读接入层 ────────────────────────────────────────────┐
│  三类工具（服务台/台账/文件库）脱敏只读导出        [f32]     │
│  datacontract-cli：字段契约校验（缺失 ≠ 未确认）   [f31]     │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─ L1 证据锚定层 ────────────────────────────────────────────┐
│  langextract：抽取 + char_interval 锚定                     │
│  → char_interval is None ⇒ 强制进人工队列（不写交接单）[f44] │
│  → lx.visualize() 生成一页清单 + 高亮来源         [f15][f42] │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─ L2 版本与时效层 ──────────────────────────────────────────┐
│  快照表 + 双时间戳（语义对齐 XTDB 双时态）                  │
│  valid_from / valid_to / system_received_at / confirm_status│
│  判定：版本有效期 AND 确认状态 —— 双条件，文件名不参与 [f24] │
│  过期来源不得覆盖已确认新版本                        [f34]   │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─ L3 人工回路层 ────────────────────────────────────────────┐
│  langgraph interrupt(payload = 变更内容 + 提交人)            │
│      ├─ resume(业务负责人确认)  → goto "进入交接"           │
│      ├─ resume(拒绝)            → goto "取消"，写 rejected  │
│      └─ 超时（durable timer）   → 升级傅景 / 暂停试点 [f44] │
│  责任矩阵建模参考 Camunda BPMN 泳道                  [f25]   │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─ L4 审计与复盘层 ──────────────────────────────────────────┐
│  Temporal Event History / MLflow：谁查过·哪版·谁确认  [f45]  │
│  OpenLineage：字段级血缘，支撑跨团队口径             [f53]   │
│  Label Studio：误报/漏报/未确认项周度复盘            [f55]   │
└─────────────────────────────────────────────────────────────┘
```

**红线（贯穿全层，对应 f05 / f52）：** 不自动决定录用或派遣资格，不向真实人员发送安排，对外承诺仍由业务人员确认。因此 L3 之后**没有自动写回通路**——只读导出（f32）与人工确认（f22）共同决定了这一点。

---

## 5. 四周试点落地路径

预算约束来自 f03：**四周、两名业务骨干、每周投入不超过四小时**（约 32 人时）。以下安排刻意把"引入新基础设施"压到最低。

| 周 | 目标 | 动作 | 交付物 | 停止条件 |
|---|---|---|---|---|
| **W1** | 字段契约 | 30 岗位脱敏只读导出；用 `datacontract-cli` 校验六项关键字段（岗位代号/地点/班次/技能条件/版本/确认人）；确认**缺失值**与**未确认值**能否区分（f31） | 字段契约 + 缺口清单 | 版本或确认状态字段大面积缺失 → 暂停，回秦芮确认替代比对键（f30） |
| **W2** | 证据锚定 | `langextract` 跑 30 岗位，输出 JSONL + 交互式 HTML；验证 `char_interval` 能否定位到源单 | 一页缺项清单原型 | 未锚定比例过高 → 不进 W3 |
| **W3** | 人工回路 | 林亦舟、傅景各审一份清单；`interrupt()` 接入补单-确认链路；配置超时升级 | 复核记录 + 审批链路 | 授权确认人清单未定 → **不启动审批**（傅景 f44 前提） |
| **W4** | 审计与复盘 | 接入运行留痕；收集误报/漏报/未确认项；按 f02 + f42 验收 | 验收报告 + 复盘记录 | 30 岗位未全部列出硬条件与待确认项 → 不放行 |

**验收口径（来自 f02 / f42，不自行发明）：**
1. 30 个岗位全部列出硬条件和待确认项，变更有影响清单；
2. 每条结果关联来源和人工复核记录。

**评估维度（来自 f51，不能用单一效率指标）：** 处理工时 + 返工次数 + 确认完整率，三者同时看。

---

## 6. 适用性边界与风险

这一节的份量和前面的案例盘点同样重要。

1. **开源案例解决「机制」，不解决「责任」。** 授权确认人清单（傅景追问）、保留期限与删除触发条件（f43）、脱敏导出审批人（秦芮追问）——这些**没有任何开源仓库能替你定**。它们必须由顾明岚、傅景给出。机制建好了但责任没定，审计链仍然是断的。

2. **星标高 ≠ 可生产。** `google/langextract` README 明确写着 "not an officially supported Google product"。`frappe/hrms` 是 GPL-3.0，若涉及二次分发需评估许可传染性。

3. **Industry 侧证据补齐了，Operator 侧仍不足。** 本报告补的是上游四类来源里的 Industry。但 f10、f11 描述的电话变更实际频次、3-5 个真实样本，仍需林亦舟与唐映秋提供。没有真实样本，W2 的未锚定比例无从解释。

4. **不要因为有了双时态就换数据库。** XTDB / SirixDB 是语义标杆，不是落地建议。四周试点用快照表 + 双时间戳复现语义即可。

5. **不要把模型置信度等同于业务风险。** 上游 `outcome-risk-matrix` 框架的这条判断，在 `char_interval is None` 上体现得最直接：锚定失败是**结构性事实**，不是概率问题，必须走人工队列而非阈值过滤。

6. **本报告所有案例数据为 2026-10-23 快照。** 星标数、许可、API 形态（尤其 LangGraph 的 `response_schema` 需 ≥1.2.12、LangExtract 的 `output_schema` 在 Ollama 不可用）都可能变化，落地前需复查。

---

## 7. 待验证追问 → 技术决策映射

上游报告列了 8 条追问。这里说明**每条答案如何改变架构**，便于访谈时直接定位：

| 追问（对象） | 若答案如此 | 架构影响 |
|---|---|---|
| 口头变更是否一律视为未确认？（顾明岚） | 是 | C3 中断成为**必经节点**，无旁路；若否，需额外定义例外分支 |
| 授权确认人清单是谁？（傅景） | 清单明确 | `interrupt()` 的 resume 做身份校验；**清单未定则 W3 不启动** |
| 电话修改后的留痕与复核入口设在哪里？（傅景） | 有入口 | C3 与 C4 打通；无入口则 C4 只能留痕不能回溯 |
| 需求单无统一关联编号时用什么替代键？（秦芮） | 有复合键 | C1/C2 的关联成立；无替代键则跨工具比对只能人工，W1 即卡住 |
| 一页缺项清单需要哪些字段？（林亦舟） | 字段明确 | `extraction_class` 枚举定稿；否则 W2 输出无法验收 |
| 变更以哪份原始记录为准？（叶书） | 确定基准 | OpenLineage 血缘有锚点；未定则口径对齐无基础 |
| 保留期限与删除触发条件？（顾明岚 + 傅景） | 有约定 | C4 可设生命周期；无约定则清理动作可能截断审计链（f45） |
| Industry / Operator 侧证据是否需补采？ | 需补 | 本报告为 Industry 侧输入；Operator 侧仍需真实样本 |

---

## 附录 A：仓库核验清单

核验日期：2026-10-23。星标数为当日快照。

| 仓库 | ★ | 语言 | 许可 | 核验要点 |
|---|---|---|---|---|
| `langchain-ai/langgraph` | 43,735 | Python | MIT | `interrupt()` / `Command(resume=)` / checkpointer + thread_id / `response_schema`（≥1.2.12） |
| `google/langextract` | 39,004 | Python | Apache-2.0 | `char_interval` 锚定；未锚定 → `None`；`lx.visualize()`；Ollama 不支持 `output_schema` |
| `HumanSignal/label-studio` | 28,529 | TypeScript | Apache-2.0 | 多类型标注，标准化输出格式 |
| `mlflow/mlflow` | 28,501 | Python | Apache-2.0 | 面向 agent / LLM 的 tracing 与评估 |
| `deepset-ai/haystack` | 26,814 | Python | Apache-2.0 | 上下文工程化 LLM 应用编排（备选） |
| `temporalio/temporal` | 23,928 | Go | MIT | 持久化执行；Event History 审计 |
| `Netflix/conductor` | 12,752 | Java | Apache-2.0 | 微服务编排（备选） |
| `fivetran/great_expectations` | 11,891 | Python | Apache-2.0 | 数据质量断言（备选；仓库已迁至 fivetran 组织） |
| `doccano/doccano` | 10,802 | Python | MIT | 轻量标注（备选） |
| `frappe/hrms` | 8,949 | Python | **GPL-3.0** | 开源 HR / 薪资，含审批工作流 |
| `argilla-io/argilla` | 5,173 | Python | Apache-2.0 | 领域专家反馈闭环 |
| `camunda/camunda` | 4,337 | Java | 开源 | BPMN 人工任务、责任矩阵 |
| `xtdb/xtdb` | 3,087 | Clojure / Kotlin | MPL-2.0 | 双时态（system time + valid time），SQL:2011 |
| `OpenLineage/OpenLineage` | 2,719 | Java | Apache-2.0 | 血缘元数据开放标准 |
| `django-commons/django-simple-history` | 2,465 | Python | BSD-3 | 模型历史留存与回滚（轻量替代） |
| `sirixdb/sirix` | 1,220 | Java | BSD-3 | 追加式版本树、作者跟踪、Merkle 校验、修订 diff GUI |
| `datacontract/datacontract-cli` | 1,098 | JavaScript | MIT | 数据契约测试 |

## 附录 B：检索过但未采纳

| 方向 | 检索结果 | 未采纳理由 |
|---|---|---|
| 排班 / 花名册开源（`nmh-roster-generator` 等） | 最高 ★2 | 星标过低、维护不可靠，且解决的是排班优化而非版本交接 |
| 通用审批流脚手架（`flowops` 等） | ★0 | 无社区验证 |
| `EventStore` / Kogito | 未返回有效数据 / ★19 | 前者接口未核验，后者已归档为 incubator |

---

*本方案的事实来自 `discovery-report.md`（合成演练，待验证），案例来自 GitHub 公开仓库（核验 2026-10-23）。Industry 侧证据已补齐，Operator 侧真实样本仍缺失。任何结论在顾明岚、傅景确认责任边界前，均不得作为验收依据。*
