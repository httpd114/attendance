# 阶段 2 MVP 归档 · `src/mvp/`

派遣项目考勤异常双向确认系统**最早能跑起来的那一版**：本地制度 Markdown → 混合检索（BM25 + 向量）→ SSE 流式问答 → SQLite 留痕。单进程、匿名、无登录、无后台，一个页面一个接口。

> 本目录是**历史归档**，不再迭代。当前维护中的完整实现在 [`src/attendance_agent/`](../attendance_agent/)。

---

## 1. 文件来源与改动

三个核心文件完整取自 Git 历史提交 `0ffc8f4`（`init project`，当时的 `chat-app/` 平铺目录），并经 SHA-256 校验字节级一致；同一版本也保留在本地备份 `.workbuddy/backup/pre-restructure-20261105/chat-app/`。

| 文件 | 来源 | 处理 |
| --- | --- | --- |
| `rag_engine.py` | `0ffc8f4:chat-app/rag_engine.py` | **原样归档**，字节未改。含切片、BM25/向量混合检索、引用编号、拒答、提示词组装、`RAGEngine.stream_chat()` |
| `static/index.html` | `0ffc8f4:chat-app/static/index.html` | **原样归档**。真流式打字机、16ms 合帧、引用渲染、刷新恢复历史 |
| `报销制度.md` | `0ffc8f4:chat-app/报销制度.md` | **原样归档**。阶段 2 的唯一知识源 |
| `main.py` | 按阶段 2 接口契约**重建的薄编排层** | 见下 |
| `README.md` / `requirements.txt` | 归档时新增 | 说明与最小依赖 |

**为什么 `main.py` 没有原样搬过来**：历史上的 `chat-app/main.py` 在阶段 2 之后又叠加了登录鉴权、租户隔离、制度知识库后台、对话审计、用户管理等路由（同一文件涨到 1775 行），`/chat` 也改成从 `KnowledgeBase` 按租户检索、依赖 `AdminDB` 取提示词与写审计标记；建表 SQL 里没有 `tenant_id`，INSERT 却有，必须先跑 `migrate.py` 补列才能启动。照搬既不是 MVP，也无法独立运行。因此这里只保留阶段 2 的协议编排（SSE 事件、限流、会话落库、中文错误体），算法层仍调用原版 `RAGEngine.stream_chat()`，分层纪律不变。

历史版本的完整代码仍可在 Git 历史 `0ffc8f4` 中查阅。

---

## 2. 快速启动

```bash
# 1) 安装依赖（任选其一）
pip install -r requirements.txt                # 最小依赖，不锁版本
pip install -r ../../requirements.txt          # 项目已验证的精确版本组合

# 2) 启动（务必先进入本目录：rag_engine 是平铺导入）
cd src/mvp
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 即可提问。

**不配置任何密钥也能跑通**：未设置 `LLM_API_KEY` 时自动降级为 `hash` embedding（整条向量路关闭，纯 BM25 检索）+ `MockLLM`（回显提示词片段），用于验证「检索 → SSE → 落库」链路。接真实模型：

```bash
export LLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
export LLM_API_KEY="<你的密钥>"
export LLM_MODEL="glm-4-flash"
export EMBED_PROVIDER="zhipu"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 环境变量

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `DOC_PATH` | 制度 Markdown 路径 | 本目录 `报销制度.md` |
| `DB_PATH` | SQLite 会话库路径 | 本目录 `chat.db` |
| `EMBED_PROVIDER` | `zhipu` / `fastembed` / `hash` | 无密钥时 `hash`，否则 `zhipu` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI 兼容模型与 embedding 凭证 | 空 → `MockLLM` |
| `TOP_K` / `CHUNK_SIZE` / `HISTORY_ROUNDS` | 检索与上下文参数 | 见 `rag_engine.Config` |

索引缓存写在本目录 `.rag_cache/`，会话库 `chat.db`，均已被仓库 `.gitignore` 排除。

---

## 3. 接口契约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/chat` | SSE 流式问答，请求体 `{session_id?, question}` |
| `GET` | `/api/sessions/{sid}/messages` | 刷新页面后恢复会话历史 |
| `GET` | `/health` | 存活、索引块数、向量路开关、当前模型 |
| `GET` | `/` | 单页聊天界面 |

SSE 事件顺序固定：`meta → status(retrieving) → citations → status(generating) → delta* → done`；任一步失败改发 `error` 且之后不再发其他事件。非 200 与流内错误都返回 `code / message(中文) / detail / request_id` 四段结构。

---

## 4. 已知限制（阶段 2 原貌，非缺陷清单）

- **匿名**：无登录、无租户隔离，任何能访问端口的人都能读所有会话；不可用于生产。
- **限流是进程内存计数**：多 worker / 重启即失效，生产需换 Redis（接口 `allow(key) -> bool` 已预留）。
- **引用自检是机械校验**：能查出编号越界，查不出「编号没越界但指错块」。
- **无管理后台 / 无 Agent 编排 / 无审计**：这些都属于后续阶段，见 `src/attendance_agent/`。
- 归档版默认用 `MockLLM` 与纯 BM25，回答质量不代表线上效果。
