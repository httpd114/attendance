# 派遣项目考勤异常双向确认系统

面向派遣用工场景的考勤异常确认系统：员工就迟到、缺卡、加班争议等异常发起**确认或异议**，系统依据制度知识库给出带引用编号的解答，经材料校验与合规复核后把确认单推进到「已提交待复核」，由管理员在后台人工处置。

核心约束：**系统不会因超时自动通过任何异常**，**AI 不替审批人做决定**。一期只记录并导出结算数据，不直连薪资、不开票。

> 当前状态：功能开发完成，**尚未达到上线条件**。上线前请逐项勾验
> [`docs/上线前检查清单-派遣考勤异常双向确认-v1.0.md`](docs/上线前检查清单-派遣考勤异常双向确认-v1.0.md)
> （25 项中 12 项 P0 阻塞，含密钥默认值、日志保留策略、回滚基线）。

---

## 快速开始

### 1. 环境要求

Python 3.11+（开发与验证环境为 3.13）。SQLite 内置，无需额外数据库服务。
LLM 与向量服务当前对接智谱开放平台，需要一组可用的 API Key。

### 2. 安装

```bash
# 建议先建虚拟环境
python -m venv .venv
source .venv/bin/activate          # Windows Git Bash
# .venv\Scripts\activate          # Windows PowerShell

pip install -r requirements.txt
pip install -e .                  # 可选：以可编辑模式安装包，之后无需设 PYTHONPATH
```

若不做 `pip install -e .`，运行任何命令前需设置：

```bash
export PYTHONPATH="$PWD/src"      # Windows PowerShell: $env:PYTHONPATH = "$PWD/src"
```

### 3. 配置环境变量

**不要把密钥写进代码或提交到版本库。** 所有配置走环境变量：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_BASE_URL` | OpenAI 兼容接口地址 | 空（必填） |
| `LLM_API_KEY` | 模型密钥 | 空（必填） |
| `LLM_MODEL` | 模型名 | 空（如 `glm-4-flash`） |
| `EMBED_PROVIDER` | 向量服务 | `zhipu` |
| `EMBED_MODEL` / `EMBED_DIM` | 向量模型与维度 | `embedding-2` / `1024` |
| `LLM_TIMEOUT` | 单次 LLM 超时秒数 | `60` |
| `DOC_PATH` | 默认制度文档 | `data/报销制度.md` |
| `DB_PATH` | SQLite 位置 | `data/chat.db` |
| `INDEX_DIR` | RAG 索引缓存 | `data/.rag_cache` |
| `JWT_SECRET` | JWT 签名密钥 | ⚠️ 开发默认值，**生产必填** |
| `AGENT_GATE_SECRET` | Agent 能力令牌 HMAC 密钥 | ⚠️ 未配置时进入 `dev_mode`，**生产必填** |
| `HOST` / `PORT` | 监听地址与端口 | `127.0.0.1` / `8000` |

非机密默认参数另见 [`config/default.yaml`](config/default.yaml)；该文件是**文档性默认值**，程序实际从环境变量读取，不会自动加载 YAML。

本地最简启动示例：

```bash
export LLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
export LLM_API_KEY="<你的密钥>"
export LLM_MODEL="glm-4-flash"
export EMBED_PROVIDER="zhipu"
export PYTHONPATH="$PWD/src"

python -m attendance_agent.main
# 或：python -m uvicorn attendance_agent.main:app --host 127.0.0.1 --port 8000
```

首次启动会自动建表并写入种子数据。访问 <http://127.0.0.1:8000/>。

| 角色 | 账号 | 密码 |
|---|---|---|
| 管理员 | `admin` | `Admin@2026` |
| 员工 | `zhangsan` | `Emp@2026` |

> ⚠️ 上述账号与默认密钥仅用于本地演示，**上线前必须全部更换**。

### 4. 主要入口

| 路径入口 | 用途 |
|---|---|
| `/` | 员工问答页（SSE 流式输出、引用片段、拒答提示） |
| `/login` | 登录（租户编码 `default`） |
| `/admin` | 后台首页 |
| `/admin/knowledge` | 制度知识库：上传、切片预览、删除、重建索引 |
| `/admin/prompts` | 提示词模板：编辑即生效、版本历史、回滚 |
| `/admin/audit` | 对话审计：规则预筛标记、人工复核、动作日志 |
| `/admin/users` | 用户管理：建号、启停用、重置密码 |
| `/health` | 健康检查 |

完整接口清单与 SSE 事件协议见 [`docs/api.md`](docs/api.md)。

---

## 怎么验

```bash
# 纯静态自检，不需要服务、不需要 API Key
python tests/check_routes.py                 # 27 条路由 + 模板 + 「注释吞代码」扫描
python tests/validate_eval_cases.py         # 30 条评测用例集自身合法性
python -m pytest tests/test_core.py -q      # 核心单测：门禁、预筛、路径、提示词

# Agent Harness 三条轨迹 + 8 条硬门禁单测（独立演示库，不碰 chat.db）
python examples/demo.py

# 集成回归：需先启动服务
bash tests/test_auth.sh
bash tests/test_kb.sh
bash tests/test_admin3.sh

# 一次性全跑
bash scripts/build.sh
```

`tests/eval_cases.json` 是 30 条 QA 评测用例（制度问答 10 含 3 条陷阱题、工具调用 10、违规意图 5、多轮上下文 5），字段为 `question / expected_tools / expected_behavior / category`。

---

## 目录结构

```
attendance/
├── README.md                 # 本文件：项目是啥、怎么装、怎么跑
├── LICENSE                   # MIT
├── CHANGELOG.md              # 版本记录
├── CONTRIBUTING.md           # 贡献指南
├── pyproject.toml            # 包元数据与构建配置
├── requirements.txt          # 已验证版本的依赖清单
├── .gitignore
│
├── src/attendance_agent/     # 应用包
│   ├── main.py               # FastAPI 入口（python -m attendance_agent.main）
│   ├── core/                 # 核心业务：鉴权、知识库、RAG、Agent 门禁与循环
│   ├── utils/                # 通用工具：统一路径解析
│   ├── static/               # 前端资源（员工端 HTML、后台 CSS）
│   └── templates/            # Jinja2 页面模板
│
├── docs/                     # 设计、API、需求、方案、报价、上线清单
│   ├── design.md             # 架构与关键设计决策
│   └── api.md                # 接口与 SSE 协议
├── tests/                    # 单测、路由自检、集成回归脚本、评测用例集
├── examples/                 # Agent 三条轨迹演示、阶段 1 RAG 原型
├── scripts/                  # build / deploy / 数据库迁移 / 文档构建
├── config/default.yaml       # 非机密默认参数（文档性质）
├── data/                     # 样例制度文档、图片；运行时 SQLite 与索引缓存
└── .github/                  # CI 工作流、Issue 与 PR 模板
```

路径解析集中在 `src/attendance_agent/utils/paths.py`：`static/`、`templates/` 随包发布，SQLite 与索引缓存放项目根 `data/`，均可由环境变量覆盖。

### 从旧结构迁移过来的文件在哪

重构前所有代码平铺在 `chat-app/`，文档散落在项目根。映射如下（原始副本已备份到 `.workbuddy/backup/pre-restructure-20261105/`）：

| 原位置 | 新位置 |
|---|---|
| `chat-app/main.py` | `src/attendance_agent/main.py` |
| `chat-app/{admin_db,agent_*,audit_rules,auth,kb,rag_engine}.py` | `src/attendance_agent/core/` |
| `chat-app/static/`、`templates/` | `src/attendance_agent/` 同名目录 |
| `chat-app/报销制度.md`、`*.db` | `data/` |
| `chat-app/test_*.sh`、`check_routes.py`、`eval_cases.json` | `tests/` |
| `chat-app/demo_agent.py` | `examples/demo.py` |
| `chat-app/migrate.py` | `scripts/migrate.py` |
| `rag-proto/` | `examples/rag-proto/` |
| 项目根 `*.md` / `*.docx` | `docs/`（保留原文件名） |
| 项目根图片与流程图 HTML | `data/` |
| `output/docx-build/` | `scripts/docx-build/` |

模块间导入已由平铺 `from admin_db import ...` 改为包内相对导入，因此**不能再** `cd src/attendance_agent && python main.py`；统一使用 `python -m attendance_agent.main`。

---

## 设计要点

- **四层架构**：入口层 / 服务层 / 智能体编排层 / 数据模型层，详见 [`docs/design.md`](docs/design.md)。
- **提示词两段式**：代码写死的强制段（引用编号、拒答、NEVER 清单）管理员不可编辑；业务正文段可按租户编辑并版本化。
- **正确性靠服务端，不靠提示词**：`agent_gate.py` 用 HMAC 一次性能力令牌 + 状态机做硬门禁，模型跳步拿不到凭证就写不进业务表。
- **规则预筛只召回、不定性**：命中只标「待复核」，必须人工确认或驳回；只扫员工提问，不扫助手回答。
- **最少 5 轮决策上限**：轮次耗尽用规则模板收敛，并写 `audit_logs(agent_round_limit)`。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/design.md` | 架构、状态机、能力令牌链、关键取舍 |
| `docs/api.md` | HTTP 接口、SSE 事件、错误码、9 个工具 schema |
| `docs/需求清单-派遣考勤异常双向确认-v0.1.md` | 需求边界与 10 个评审结论 |
| `docs/Agent工具设计-派遣考勤异常双向确认-v0.1.md` | Function Calling 规范与调用顺序约束 |
| `docs/Agent编排实现说明.md` | Harness 实现与三条轨迹 |
| `docs/上线前检查清单-派遣考勤异常双向确认-v1.0.md` | 25 项上线勾验 |
| `docs/discovery-report.md` | 现状调研 |
| `docs/peer-benchmark-proposal.md` | 同行方案对标 |

---

## 已知限制

- 项目当前**不是可上线状态**，见上线前检查清单；尤其是默认密钥、无日志保留策略、无回滚制品。
- 引用编号「未越界但指错 chunk」的语义校验尚未根治。
- 制度知识源有两套：`/chat` 走 `data/报销制度.md` 的 RAG 库，Agent 演示用内置 `POL-*` 片段；`make_rag_policy_lookup()` 需按真实检索器签名适配，否则会静默回退演示片段。
- 限流是**进程内**滑动窗口，多 worker 部署前需换共享计数。

## 许可

[MIT](LICENSE)。若作为企业内部系统交付，请按组织要求替换为专有许可。
