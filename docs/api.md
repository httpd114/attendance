# 接口说明

## 1. 鉴权

- 登录 `POST /login` 提交表单 `tenant_code` / `username` / `password`，成功后写 HttpOnly Cookie（JWT）。
- 页面路由未登录 → 302 到 `/login`；**已登录但角色不足 → 302 回其可访问首页并带 `?notice=`**（不会强迫重新输密码）。
- 接口路由未登录 → 401 JSON；员工调管理接口 → 403 JSON。
- `tenant_id` / `user_id` / `role` 一律从 JWT 注入，接口与工具 schema **不接受**这些参数。
- 每次请求查库确认 `is_active`：账号停用后，已签发的 JWT 立即失效。
- 表单含中文字段时必须以 UTF-8 提交（浏览器默认行为）。用 Git Bash 的
  `curl -d "display_name=李四"` 会发出系统 GBK 字节导致乱码，应改用 `--data-urlencode`；
  这是客户端编码问题，不是服务端缺陷。

## 2. 统一错误体

所有业务与框架层错误返回同一结构，`message` 为可直接展示给员工的中文：

```json
{ "code": 50004, "message": "模型响应超时，请重试", "detail": "60s 未响应", "request_id": "..." }
```

| 场景 | HTTP |
|---|---|
| 参数／JSON／问题超长 | 400 |
| 未登录、令牌无效或过期 | 401 |
| 权限不足、跨租户资源 | 403 |
| 会话不存在、用户不存在 | 404 |
| 方法不支持 | 405 |
| 用户名已存在 | 409 |
| 请求体校验失败 | 422 |
| 触发限流 | 429 |
| 索引未就绪 / 检索失败 / 模型不可用 | 503 |
| 模型响应超时 | 504 |
| 未捕获内部错误 | 500 |

状态码是**显式映射表**（`main.py` 的 `_HTTP_STATUS` / `_AUTH_HTTP_STATUS`），
不是按 code 区间粗判——前端依赖 401 触发跳登录、429 触发退避，映射错了会连锁失效。

Agent 门禁另有 10 个结构化错误码，返回中文 `next_action` 供模型自愈：
`ERR_CAPABILITY_REQUIRED`、`ERR_TOKEN_INVALID`、`ERR_TOKEN_EXPIRED`、`ERR_TOKEN_SCOPE`、
`ERR_TOKEN_ACTION`、`ERR_TOKEN_USED`、`ERR_PARAMS_MISMATCH`、`ERR_ILLEGAL_TRANSITION`、
`ERR_EXCEPTION_NOT_FOUND`、`ERR_EXPLICIT_CONSENT`。

## 3. 路由清单（27 条）

> `tests/check_routes.py` 的 `EXPECTED_ROUTES` 是这份清单的可执行版本，新增路由须同步更新。

| 方法 | 路径 | 说明 | 角色 |
|---|---|---|---|
| GET | `/` | 员工问答单页 | 登录用户 |
| GET | `/health` | 健康检查 | 公开 |
| GET / POST | `/login` | 登录页与登录提交 | 公开 |
| GET | `/logout` | 退出 | 登录用户 |
| POST | `/chat` | SSE 流式问答 | 登录用户 |
| GET | `/api/sessions/{sid}/messages` | 拉取会话历史 | 会话属主 |
| GET | `/admin` | 后台首页 | 管理员 |
| GET | `/admin/knowledge` | 知识库列表 | 管理员 |
| POST | `/admin/knowledge/upload` | 上传制度文档 | 管理员 |
| POST | `/admin/knowledge/documents/{doc_id}/delete` | 删除文档并重建索引 | 管理员 |
| GET | `/admin/knowledge/documents/{doc_id}/chunks` | 切片预览 | 管理员 |
| POST | `/admin/knowledge/reindex` | 重建向量索引 | 管理员 |
| GET | `/admin/prompts` | 提示词模板与版本历史 | 管理员 |
| POST | `/admin/prompts/save` | 保存新版本（立即生效） | 管理员 |
| POST | `/admin/prompts/rollback` | 回滚（生成新版本） | 管理员 |
| GET | `/admin/audit` | 对话审计与预筛标记 | 管理员 |
| POST | `/admin/audit/flags/{flag_id}/review` | 人工复核确认／驳回 | 管理员 |
| GET | `/admin/users` | 用户列表 | 管理员 |
| POST | `/admin/users/create` | 建号 | 管理员 |
| POST | `/admin/users/{user_id}/toggle` | 启停用 | 管理员 |
| POST | `/admin/users/{user_id}/reset-password` | 重置密码 | 管理员 |
| GET | `/api/admin/users` | 用户列表 JSON | 管理员 |
| POST | `/api/admin/users` | 建号 JSON | 管理员 |
| POST | `/api/admin/users/{user_id}/reset-password` | 重置密码 JSON | 管理员 |
| GET | `/api/admin/kb/documents` | 文档列表 JSON | 管理员 |
| DELETE | `/api/admin/kb/documents/{doc_id}` | 删除文档 JSON | 管理员 |

## 4. SSE 协议

`POST /chat`，请求体：

```json
{ "session_id": "s_xxx（首次可省略）", "question": "出差住宿费标准是多少？" }
```

响应 `Content-Type: text/event-stream`，每帧形如 `event: <type>\ndata: <json>\n\n`。
**顺序固定**：

| 事件 | 载荷 | 时机 |
|---|---|---|
| `meta` | `{session_id, request_id}` | 最先，前端据此保存会话 ID |
| `status` | `{text}` | 「正在检索制度…」／「正在生成…」 |
| `citations` | `{items: [{index, source_file, section, snippet}]}` | **在 delta 之前**，回答还在生成时底部来源就已显示 |
| `delta` | `{text}` | 流式增量，前端 16ms 合帧后打字机渲染 |
| `done` | `{citation_check: {...}}` | 正常结束，携带引用自检与拒答标记 |
| `error` | 统一错误体 | 流中途失败 |

前端对 `error`、网络异常、以及「连接被截断但没收到 done」三种情况都要收尾：
隐藏状态条、移除光标、展示中文错误。非 200 时读取 JSON 错误体展示。

引用编号 `[n]` **只在本轮生效**：历史轮次的 `[n]` 指当时那批检索结果，
与新一批不一致，因此系统提示词明确限定「本轮」。

## 5. Agent 工具（9 个，Function Calling 规范）

完整 JSON Schema 见 `Agent工具设计-派遣考勤异常双向确认-v0.1.md` 与
`src/attendance_agent/core/agent_tools.py` 的 `TOOL_SCHEMAS`。

| 组 | 工具 |
|---|---|
| 只读感知 | `list_my_exceptions`、`get_exception_detail`、`lookup_policy`、`get_case_state` |
| 校验（签发凭证） | `validate_evidence`、`validate_policy_compliance` |
| 受保护写入 | `create_confirmation_draft`、`submit_confirmation` |
| 升级上报 | `escalate_case` |

固定工作流（**不得跳步**）：

```
1 list_my_exceptions → 2 get_exception_detail → 3 lookup_policy → 4 get_case_state
→ 5 validate_evidence（签发 evidence_token）
→ 6 create_confirmation_draft（回传 token，参数须与第 5 步完全一致）
→ 7 validate_policy_compliance（签发 compliance_token）
→ 8 submit_confirmation（须员工本轮明确同意 + compliance_token）
```

`tools_for(role)` 在**构造请求之前**按角色裁剪 schema：管理员 Agent 只有前 4 个只读工具。
`create_confirmation_draft` / `submit_confirmation` 由 `agent_gate.py` 在服务端校验签名、
有效期、身份与租户、动作匹配、`jti` 原子消费、参数摘要一致、状态机允许共 7 项——
提示词无法绕过。

## 6. 数据模型速览

| 表 | 用途 |
|---|---|
| `tenants` / `users` | 租户与账号（bcrypt 密码、启用状态、角色） |
| `conversations` / `messages` | 会话与消息（含 `status`、`latency_ms`、tokens、错误码） |
| `kb_documents` / `kb_chunks` | 多文档知识库与切片，embedding 以 BLOB 存 SQLite |
| `prompt_templates` | 提示词正文版本链（保存即生效，回滚生成新版本） |
| `audit_flags` / `audit_logs` | 预筛标记、人工复核、管理动作与门禁拒绝 |
| `attendance_exceptions` / `evidence_files` / `confirmation_cases` | 考勤异常、证明材料、确认单 |
| `agent_rounds` / `agent_tool_calls` | 阶段 5 决策与工具调用日志 |
| `capability_jti` / `idempotency` | 一次性凭证消费记录与幂等键 |

## 7. curl 示例

```bash
# 登录（保存 Cookie）
curl -s -c cookies.txt -X POST http://127.0.0.1:8000/login \
  -d "tenant_code=default&username=zhangsan&password=Emp@2026"

# 流式问答（-N 关闭缓冲）
curl -N -b cookies.txt -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"出差住宿费标准是多少？"}'

# 知识库列表（管理员）
curl -s -b admin_cookies.txt http://127.0.0.1:8000/api/admin/kb/documents
```
