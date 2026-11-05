#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py —— 派遣项目考勤异常双向确认系统 · 员工问答入口（阶段 2）
====================================================================
接口清单：
    POST /chat                          SSE 流式问答（主接口）
    GET  /api/sessions/{sid}/messages    拉取会话历史（刷新页面后恢复）
    GET  /health                          存活 + 索引就绪状态
    GET  /                               单页聊天界面（static/index.html）

本文件只做"协议编排"，所有业务逻辑都在 rag_engine.py 里——
这是阶段 1 就定下的分层纪律：api 层不碰算法，算法层不知道 HTTP。

运行：
    export LLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
    export LLM_API_KEY="你的智谱Key"
    export LLM_MODEL="glm-4-flash"
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.admin_db import AdminDB
from .core.auth import (
    ERR_ACCOUNT_DISABLED,
    ERR_FORBIDDEN_ADMIN,
    ERR_FORBIDDEN_SESSION,
    ERR_INVALID_CREDENTIALS,
    ERR_OLD_PASSWORD_WRONG,
    ERR_PASSWORD_WEAK,
    ERR_TOKEN_EXPIRED,
    ERR_TOKEN_INVALID,
    ERR_TOKEN_MISSING,
    ERR_USERNAME_TAKEN,
    ERR_USER_NOT_FOUND,
    AuthError,
    CurrentUser,
    create_access_token,
    decode_access_token,
    get_current_user,
    require_admin,
    require_user,
)
from .core.kb import KnowledgeBase
from .core.rag_engine import (
    Config,
    ErrorCode,
    RAGEngine,
    RAGError,
    trim_history,
)
from .utils.paths import (
    DEFAULT_DB_PATH,
    STATIC_DIR,
    TEMPLATE_DIR,
    ensure_runtime_dirs,
)

# ====================================================================
# 全局配置
# --------------------------------------------------------------------
# 路径统一由 utils/paths.py 解析：
#   static/ templates/ 是包内资源，随代码一起发布；
#   SQLite 与 RAG 索引缓存放项目根 data/，由 .gitignore 排除。
# 全部允许用环境变量覆盖（DB_PATH / DOC_PATH / INDEX_DIR）。
# ====================================================================
ensure_runtime_dirs()
CFG = Config.from_env()
DB_PATH = os.getenv("DB_PATH", str(DEFAULT_DB_PATH))

# 会话 ID 合法性：只允许字母数字和少量安全符号，防止注入和路径穿越
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

# 线程池：LLM 流式调用是同步阻塞 IO，必须丢出事件循环，
# 否则一个慢请求会把整个服务卡死
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


# ====================================================================
# 简易限流（内存滑动窗口）
# ----------------------------------------------------------------
# 原型够用。阶段 2 换 Redis，接口不变（check(key) -> bool）。
# ====================================================================
class RateLimiter:
    def __init__(self, max_calls: int = 20, window_sec: int = 60) -> None:
        self.max_calls = max_calls
        self.window = window_sec
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.max_calls:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


_LIMITER = RateLimiter(max_calls=20, window_sec=60)


# ====================================================================
# SQLite 存储层
# ----------------------------------------------------------------
# 两张表对应技术架构里的 conversations / messages，按 SQLite 语法调整：
# 去掉了 pgvector 的向量列（向量在 FAISS 里），tenant_id 先预留不接
# （原型匿名，阶段 2 接 JWT 时补上并启用 40301）。
# ====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新会话',
    user_id     TEXT,                          -- 阶段 2 接登录后填充
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL
                      REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL,           -- user | assistant | system
    content           TEXT NOT NULL DEFAULT '',
    citations         TEXT,                    -- JSON 数组，assistant 才有
    citation_check    TEXT,                    -- JSON，assistant 才有
    status            TEXT NOT NULL DEFAULT 'done',  -- streaming | done | error
    tokens_prompt     INTEGER NOT NULL DEFAULT 0,
    tokens_completion INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    error_code        INTEGER,                 -- 失败时记录，成功为 NULL
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_time
    ON messages(conversation_id, created_at);
"""


def _now_iso() -> str:
    """统一用本地时间 ISO8601，带时区偏移，避免前后端解析歧义。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ChatDB:
    """SQLite 封装。

    三个工程处理：
    1. WAL 模式 —— 读写不互斥，接口层并发拉历史时不会被写操作堵住。
    2. check_same_thread=False + 写锁 —— sqlite3 默认线程检查会在这
       种"多线程池调用"的场景下直接抛异常。
    3. 所有调用通过 asyncio.to_thread 包一层，不阻塞事件循环。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- 内部：统一执行 ----
    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read_all(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _read_one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        """取第一行；没有则返回 None。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    # ---- 会话 ----
    def ensure_conversation(self, sid: str, user_id: str = "",
                            tenant_id: str = "") -> bool:
        """确保会话存在；返回 True 表示本次是新建。

        ⭐ 阶段 3 起绑定归属：
            - user_id：审计页按员工筛选的依据
            - tenant_id：租户隔离边界
            新建时写入；已存在时不覆盖（避免他人抢绑会话）。
        """
        rows = self._read_all(
            "SELECT id FROM conversations WHERE id = ?", (sid,)
        )
        if rows:
            return False
        now = _now_iso()
        self._write(
            "INSERT INTO conversations (id, title, user_id, tenant_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "新会话", user_id, tenant_id, now, now),
        )
        return True

    def conversation_exists(self, sid: str) -> bool:
        rows = self._read_all("SELECT id FROM conversations WHERE id = ?", (sid,))
        return bool(rows)

    def conversation_owner(self, sid: str) -> dict[str, Any]:
        """取会话归属（user_id / tenant_id），用于访问控制。"""
        rows = self._read_all(
            "SELECT id, user_id, tenant_id FROM conversations WHERE id = ?", (sid,)
        )
        return rows[0] if rows else {}

    # ---- 消息 ----
    def insert_message(self, mid: str, sid: str, role: str, content: str,
                       status: str = "done", tenant_id: str = "") -> None:
        """写入一条消息。

        ⚠️ 参数顺序陷阱：status 在 tenant_id **之前**。
        历史上调用处按位置传参时把 tenant_id 传到了 status 上，
        导致 tenant_id 永远为空、status 被写成一段乱码——
        且不报错，只是审计页按员工筛选永远筛不出东西。
        所以这里一律用关键字参数调用，见下方 /chat 路由。
        """
        self._write(
            "INSERT INTO messages (id, conversation_id, role, content, status, "
            "tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, sid, role, content, status, tenant_id, _now_iso()),
        )
        self._write(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (_now_iso(), sid)
        )

    def finish_message(self, mid: str, content: str, citations: list | None,
                       citation_check: dict | None, tokens: dict,
                       latency_ms: int) -> None:
        """流正常结束后，把占位的 streaming 消息补全为 done。"""
        self._write(
            "UPDATE messages SET content = ?, citations = ?, citation_check = ?, "
            "status = 'done', tokens_prompt = ?, tokens_completion = ?, "
            "latency_ms = ? WHERE id = ?",
            (
                content,
                json.dumps(citations, ensure_ascii=False) if citations else None,
                json.dumps(citation_check, ensure_ascii=False) if citation_check else None,
                tokens.get("prompt_tokens", 0),
                tokens.get("completion_tokens", 0),
                latency_ms,
                mid,
            ),
        )

    def fail_message(self, mid: str, error_code: int) -> None:
        """流中途失败：把消息标成 error 并记错误码。

        这样即使客户端断开，数据库里也能看到"这条回答没写完"，
        阶段 2 可以据此做续传或重试。
        """
        self._write(
            "UPDATE messages SET status = 'error', error_code = ? WHERE id = ?",
            (error_code, mid),
        )

    def recent_messages(self, sid: str, limit: int = 50) -> list[dict[str, Any]]:
        """取最近 limit 条，按时间正序返回。

        之所以多取一些（50 条而不是 12 条）：轮次和字数的双重截断在
        rag_engine.trim_history 里做，这里只负责"取够"。
        """
        rows = self._read_all(
            "SELECT role, content FROM messages "
            "WHERE conversation_id = ? AND status = 'done' "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (sid, limit),
        )
        return list(reversed(rows))

    def all_messages(self, sid: str) -> list[dict[str, Any]]:
        rows = self._read_all(
            "SELECT id, role, content, citations, citation_check, status, "
            "tokens_prompt, tokens_completion, latency_ms, error_code, created_at "
            "FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (sid,),
        )
        # citations / citation_check 是 JSON 文本，转成对象再出去
        for r in rows:
            for key in ("citations", "citation_check"):
                if r.get(key):
                    try:
                        r[key] = json.loads(r[key])
                    except json.JSONDecodeError:
                        r[key] = None
        return rows

    def audit_query(self, tenant_id: str, keyword: str = "",
                    user_id: str = "", date_from: str = "",
                    date_to: str = "", flag_status: str = "",
                    limit: int = 50, offset: int = 0
                    ) -> tuple[list[dict[str, Any]], int]:
        """审计查询：按员工/关键词/日期/标记状态筛选问答记录。

        ⭐ 所有条件都强制带 tenant_id——审计是"能追责"的基础，
        跨租户串数据等于审计失效。

        返回 (当前页记录, 总条数)。用 SQL LIMIT/OFFSET 而不是全量拉出来
        在 Python 里切，因为审计数据量增长很快，审计员往往一翻就是几页。

        ⚠️ 两个坑：
        1. messages 表**没有 user_id**，员工归属在 conversations.user_id 上，
           必须 JOIN conversations 才能按员工筛。
        2. audit_flags 一条消息可能有多条记录（重复打标），
           直接 LEFT JOIN 会让同一行消息在结果里重复出现、分页错乱。
           所以用子查询取该消息**最新**的一条标记。

        flag_status 语义：
            - "flagged"  只看被预筛标记过的
            - "suspected" / "confirmed" / "rejected"  按标记状态看
            - ""  全部（默认）
        """
        where = ["m.tenant_id = ?"]
        params: list[Any] = [tenant_id]

        if keyword:
            where.append("(m.content LIKE ? OR u.display_name LIKE ? "
                         "OR u.username LIKE ?)")
            like = f"%{keyword}%"
            params += [like, like, like]
        if user_id:
            where.append("c.user_id = ?")
            params.append(user_id)
        if date_from:
            where.append("m.created_at >= ?")
            params.append(f"{date_from}T00:00:00")
        if date_to:
            where.append("m.created_at <= ?")
            params.append(f"{date_to}T23:59:59")
        if flag_status == "flagged":
            where.append("f.id IS NOT NULL")
        elif flag_status in ("suspected", "confirmed", "rejected"):
            where.append("f.status = ?")
            params.append(flag_status)

        clause = " AND ".join(where)

        # 取每条消息最新的一条标记，避免一条消息多标记导致行重复
        latest_flag = (
            "(SELECT f.id FROM audit_flags f WHERE f.message_id = m.id "
            "ORDER BY f.id DESC LIMIT 1)"
        )

        total_row = self._read_one(
            f"SELECT COUNT(*) AS c FROM messages m "
            f"JOIN conversations c ON c.id = m.conversation_id "
            f"LEFT JOIN users u ON c.user_id = u.id "
            f"LEFT JOIN audit_flags f ON f.id = {latest_flag} "
            f"WHERE {clause}",
            tuple(params),
        )
        total = total_row["c"] if total_row else 0

        rows = self._read_all(
            f"SELECT m.id, m.conversation_id, m.role, m.content, m.status, "
            f"m.created_at, m.latency_ms, m.error_code, "
            f"c.user_id, u.display_name, u.username, u.employee_no, "
            f"f.id AS flag_id, f.status AS flag_status, f.reason AS flag_reason, "
            f"f.source AS flag_source, f.hit_rule, f.flagged_by, f.reviewed_at "
            f"FROM messages m "
            f"JOIN conversations c ON c.id = m.conversation_id "
            f"LEFT JOIN users u ON c.user_id = u.id "
            f"LEFT JOIN audit_flags f ON f.id = {latest_flag} "
            f"WHERE {clause} "
            f"ORDER BY m.created_at DESC, m.rowid DESC "
            f"LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        return rows, total

    def cleanup_stale_streaming(self) -> int:
        """把上次遗留的 streaming 消息标记为 error。

        为什么需要：客户端断网、刷新页面、服务重启，都会让一条 assistant
        消息永远停在 streaming 状态——它既不算完成（recent_messages 会
        过滤掉，不影响上下文），也不会自己消失，在历史里显示成一条空白
        回答。启动时统一收尸，把它们标成 error 并记错误码。

        返回清理条数，打进启动日志，便于运维发现异常中断的频率。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET status = 'error', error_code = ? "
                "WHERE status = 'streaming'",
                (ErrorCode.INTERNAL_ERROR[0],),
            )
            self._conn.commit()
            return cur.rowcount


_DB: ChatDB | None = None

# 管理域数据层（阶段 3）：用户/租户/知识库/模板/审计
_ADMIN_DB: AdminDB | None = None


# ====================================================================
# 应用生命周期：启动时构建 RAG 引擎（索引常驻内存）
# ----------------------------------------------------------------
# 阶段 3 改造：索引不再从"单个 md 文件"构建，而是从数据库构建。
# 数据库是唯一真相源，索引是派生视图——这样"上传/删除文档"才能
# 做到删除即刻生效，而不必重启服务。
# ====================================================================
_ENGINE: RAGEngine | None = None
_KB: KnowledgeBase | None = None


async def _seed_initial_document(tenant_code: str) -> None:
    """把阶段 2 的单份制度文档导入数据库（首次启动时执行一次）。

    ⭐ 导入后**永久留在库里**，之后的启动都直接读库重建索引，
    不再重复调 embedding API。这是"启动即快"的关键。

    ⚠️ 参数是 tenant_code（如 "default"），必须先解析成真实 tenant_id
    再操作业务表。业务表存的是 id（"t_xxx"）；若把 code 当 id 用，
    数据会落进一个永远查不到的孤儿分区——页面上文档数显示 0，
    但库里明明有数据。
    """
    assert _ADMIN_DB is not None

    # code → 真实 id
    tenant_id = _ADMIN_DB.get_tenant_id(tenant_code)
    if not tenant_id:
        print(f"[startup] 租户 {tenant_code} 不存在，跳过初始制度导入", flush=True)
        return

    # 已经导入过就不重复导入
    existing = _ADMIN_DB.list_documents(tenant_id)
    if existing:
        return

    seed = Path(CFG.doc_path)
    if not seed.exists():
        print(f"[startup] 未找到初始制度文件 {seed}，跳过导入", flush=True)
        return

    try:
        raw = seed.read_bytes()
        result = await asyncio.to_thread(
            _KB.ingest_document, tenant_id, seed.name, raw, "system"
        )
        print(f"[startup] 初始制度已导入：{seed.name}，"
              f"{result['chunk_count']} 块，耗时 {result['elapsed_ms']}ms", flush=True)
    except Exception as exc:
        # 导入失败不阻断启动——管理员可以在后台页手动上传
        print(f"[startup] 初始制度导入失败（可在管理后台上传）：{exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _DB, _ENGINE, _ADMIN_DB, _KB
    _DB = ChatDB(DB_PATH)
    # 管理域数据层：与 ChatDB 共用同一个 SQLite 文件
    _ADMIN_DB = AdminDB(DB_PATH)
    # 鉴权依赖里通过 request.app.state.db 取用，这里挂上
    app.state.db = _ADMIN_DB

    # 收尸：上次遗留的 streaming 消息标记为 error
    stale = await asyncio.to_thread(_DB.cleanup_stale_streaming)
    if stale:
        print(f"[startup] 清理了 {stale} 条未完成的流式消息", flush=True)

    t0 = time.time()
    try:
        _ENGINE = RAGEngine(CFG)
    except RAGError as exc:
        # 引擎起不来不能让服务变成"能连上但必错"的状态——
        # 直接启动失败，让运维在部署阶段就发现
        raise RuntimeError(f"RAG 引擎初始化失败：{exc.message}（{exc.detail}") from exc

    # 知识库：索引从数据库重建，向量直接读 BLOB，不调 embedding API
    _KB = KnowledgeBase(CFG, _ADMIN_DB, _ENGINE.provider, _ENGINE.vector_enabled)

    # 首次启动把阶段 2 的制度文档导入库
    await _seed_initial_document("default")

    # 孤儿分区自检：业务表里若残留了 tenant_code（"default"）而非
    # tenant_id（"t_xxx"），那批数据永远查不到——页面上显示 0 条，
    # 库里却有数据。这是"沉默的数据丢失"，必须主动报警而不是等用户发现。
    await _check_tenant_orphans()

    print(f"[startup] RAG 引擎就绪，向量路 "
          f"{'启用' if _ENGINE.vector_enabled else '禁用(纯BM25)'}，"
          f"耗时 {time.time() - t0:.1f}s", flush=True)
    yield
    # 收尾：关闭线程池，避免退出时挂住
    _LLM_EXECUTOR.shutdown(wait=False)


async def _check_tenant_orphans() -> None:
    """检查业务表里有没有把 tenant_code 当 tenant_id 用的残留数据。"""
    assert _ADMIN_DB is not None

    valid = {t["id"] for t in _ADMIN_DB.list_tenants()}
    if not valid:
        return

    for table in ("kb_documents", "kb_chunks", "conversations", "messages"):
        try:
            rows = _ADMIN_DB._read_all(
                f"SELECT DISTINCT tenant_id FROM {table} WHERE tenant_id IS NOT NULL"
            )
        except Exception:
            continue
        orphans = [r["tenant_id"] for r in rows if r["tenant_id"] not in valid]
        if orphans:
            print(f"[startup] ⚠️ {table} 表存在孤儿租户分区：{orphans}", flush=True)
            print(f"[startup]    这批数据不会被任何用户检索到，"
                  f"请执行 SQL 修正 tenant_id", flush=True)


app = FastAPI(title="派遣考勤异常双向确认 · 员工问答", version="0.3.0",
              lifespan=lifespan)

# 服务端渲染模板（阶段 3 管理后台）
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
# 静态资源：管理后台的 CSS 放在 static/admin.css
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ====================================================================
# 统一错误响应
# ----------------------------------------------------------------
# 你说"所有接口先返回错误码和中文错误信息"——这里落实为三层兜底：
#   1. RAGError            → 业务异常，带 code/message/detail
#   2. RequestValidationError → Pydantic 参数校验失败
#      （FastAPI 默认返回 422 + 英文，必须拦截改写）
#   3. StarletteHTTPException → 404 路由不存在 / 405 方法不允许
#      （同样默认英文，必须拦截改写）
# 任何一层漏掉，前端就会收到英文 detail，破坏约定。
# ====================================================================
def _request_id(request: Request) -> str:
    return "req_" + uuid.uuid4().hex[:12]


def _error_response(code: int, message: str, detail: str,
                    request_id: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={
            "code": code,
            "message": message,
            "detail": detail,
            "request_id": request_id,
        },
    )


# 错误码 → HTTP 状态码 显式映射。
# 不用"code < 50000 就 400"这种粗规则：会话不存在必须是 404、
# 限流必须是 429、模型超时必须是 504。粗暴映射会让前端
# 按状态码做的分支判断（比如 401 触发重新登录）全部失效。
_HTTP_STATUS: dict[int, int] = {
    ErrorCode.INVALID_JSON[0]: 400,
    ErrorCode.MISSING_QUESTION[0]: 400,
    ErrorCode.EMPTY_QUESTION[0]: 400,
    ErrorCode.QUESTION_TOO_LONG[0]: 400,
    ErrorCode.BAD_SESSION_ID[0]: 400,
    ErrorCode.SESSION_NOT_FOUND[0]: 404,
    ErrorCode.FORBIDDEN_SESSION[0]: 403,
    ErrorCode.RATE_LIMITED[0]: 429,
    ErrorCode.INDEX_NOT_READY[0]: 503,
    ErrorCode.RETRIEVAL_FAILED[0]: 503,
    ErrorCode.LLM_UNAVAILABLE[0]: 503,
    ErrorCode.LLM_TIMEOUT[0]: 504,
    ErrorCode.INTERNAL_ERROR[0]: 500,
}

# 框架层错误码（不走 RAGError，单独定义）
CODE_ROUTE_NOT_FOUND = (40401, "接口不存在")
CODE_METHOD_NOT_ALLOWED = (40501, "请求方法不支持")

# 鉴权类错误码 → HTTP 状态码显式映射
#   401xx = 身份未通过 → 401（前端据此跳登录）
#   402xx = 业务校验失败 → 400 / 409（参数或状态冲突，不是权限问题）
#   403xx = 权限不足   → 403
_AUTH_HTTP_STATUS: dict[int, int] = {
    ERR_INVALID_CREDENTIALS[0]: 401,   # 账密错误
    ERR_TOKEN_MISSING[0]: 401,         # 未带令牌
    ERR_TOKEN_INVALID[0]: 401,         # 令牌签名无效
    ERR_TOKEN_EXPIRED[0]: 401,         # 令牌过期
    ERR_PASSWORD_WEAK[0]: 400,         # 弱密码 → 参数不合规
    ERR_USERNAME_TAKEN[0]: 409,        # 用户名已存在 → 状态冲突
    ERR_USER_NOT_FOUND[0]: 404,        # 用户不存在
    ERR_OLD_PASSWORD_WRONG[0]: 400,    # 原密码错 → 参数错误
    ERR_FORBIDDEN_ADMIN[0]: 403,       # 角色不足
    ERR_FORBIDDEN_SESSION[0]: 403,     # 越权访问他人数据
    ERR_ACCOUNT_DISABLED[0]: 403,      # 账号停用
}


@app.exception_handler(RAGError)
async def rag_error_handler(request: Request, exc: RAGError) -> JSONResponse:
    status = _HTTP_STATUS.get(exc.code, 400)
    return _error_response(exc.code, exc.message, exc.detail,
                           _request_id(request), status)


def _is_admin_cookie(request: Request) -> bool:
    """看当前 Cookie 里的令牌是不是管理员。

    只用于"权限不足时往哪跳"这一件事——不做鉴权决策，
    真正的鉴权在 require_admin 依赖里（那里会查库确认账号状态）。
    """
    token = request.cookies.get("access_token")
    if not token:
        return False
    try:
        payload = decode_access_token(token)
        return payload.get("role") == "admin"
    except AuthError:
        return False


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError) -> Any:
    """鉴权异常统一处理。

    ⭐ 区分两种响应形态——这是 SSR 应用必须处理的一件事：
        - 页面请求 → 302 跳登录页，不能给用户看一段 JSON 错误
        - 接口请求（fetch / XHR）→ JSON 错误体，带错误码和中文信息

    判别逻辑分两层：
        1. 路径：/api/* 和 /chat 一律按接口处理（这两个是前端 fetch 调的）
        2. Accept 头：text/html 或 */* 视为浏览器导航。
           curl 默认发 */*，浏览器发 text/html,application/xhtml+xml,...，
           两种都要覆盖，否则用 curl 测页面时会拿到 JSON。
    """
    is_api = (request.url.path.startswith("/api/")
              or request.url.path.startswith("/chat"))
    accept = request.headers.get("accept", "")
    looks_like_browser = ("text/html" in accept
                          or "*/*" in accept
                          or accept == "")
    wants_html = (not is_api) and looks_like_browser

    if wants_html:
        # 页面请求 → 302 重定向
        #
        # ⭐ 必须区分两种情况，否则员工点后台链接会被扔回登录页，
        #    让他重新输一遍密码——明明已经登录了。
        #    1. 没登录 / 令牌无效 → 跳登录页
        #    2. 登录了但角色不够 → 跳回他该去的地方（首页）
        if exc.code == ERR_FORBIDDEN_ADMIN[0]:
            # 权限不足：能解析出用户说明已登录，送回首页
            target = "/admin" if _is_admin_cookie(request) else "/"
            return RedirectResponse(
                url=f"{target}?notice={quote(exc.message)}", status_code=302
            )

        return RedirectResponse(
            url=f"/login?error={quote(exc.message)}&next={quote(request.url.path)}",
            status_code=302,
        )

    # 接口请求 → 统一 JSON
    # ⭐ 必须显式映射，不能用"非 401 一律 403"的粗规则：
    #    402xx 段是**业务校验错误**（弱密码、用户名重复、原密码错），
    #    属于调用方参数问题，应返 400/409；只有 403xx 段才是权限不足。
    status = _AUTH_HTTP_STATUS.get(exc.code, 400)
    return _error_response(exc.code, exc.message, exc.detail,
                           _request_id(request), status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request,
                                   exc: RequestValidationError) -> JSONResponse:
    """把 Pydantic 的英文校验错误转成中文错误码。

    FastAPI 默认返回 422 + {"detail":[{"type":"missing","msg":"Field required"}]}，
    前端拿到的是一堆英文 type 名。这里按错误类型映射到我们的码表。
    """
    errors = exc.errors()
    first = errors[0] if errors else {}
    etype = first.get("type", "")
    loc = first.get("loc", [])
    field_name = loc[-1] if loc else ""

    if etype == "json_invalid":
        code, msg = ErrorCode.INVALID_JSON
        detail = "请求体不是合法 JSON，请检查引号与逗号"
    elif etype == "missing":
        code, msg = ErrorCode.MISSING_QUESTION
        detail = f"缺少必填参数 {field_name}"
    elif etype in ("string_too_long", "too_long"):
        code, msg = ErrorCode.QUESTION_TOO_LONG
        detail = f"{field_name} 超出长度上限"
    else:
        code, msg = ErrorCode.INVALID_JSON
        detail = f"参数 {field_name} 校验失败（{etype}）"

    return _error_response(code, msg, detail, _request_id(request), 400)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request,
                             exc: StarletteHTTPException) -> JSONResponse:
    """拦截框架的 404 / 405，返回中文错误码。"""
    if exc.status_code == 404:
        code, msg = CODE_ROUTE_NOT_FOUND
        detail = f"路径 {request.url.path} 不存在"
    elif exc.status_code == 405:
        code, msg = CODE_METHOD_NOT_ALLOWED
        detail = f"路径 {request.url.path} 不支持 {request.method}"
    else:
        code, msg = ErrorCode.INTERNAL_ERROR
        detail = str(exc.detail)
    return _error_response(code, msg, detail, _request_id(request),
                           exc.status_code)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何没预料到的异常也不能把堆栈暴露给前端。"""
    return _error_response(
        ErrorCode.INTERNAL_ERROR[0], ErrorCode.INTERNAL_ERROR[1],
        f"{type(exc).__name__}", _request_id(request), 500,
    )


# ====================================================================
# 请求体模型
# ====================================================================
class ChatRequest(BaseModel):
    session_id: str | None = Field(
        default=None, description="会话 ID；不传则服务端新建"
    )
    question: str = Field(..., description="员工问题，1-500 字")


# ====================================================================
# SSE 帧格式化
# ====================================================================
def sse_frame(event: str, data: dict[str, Any]) -> str:
    """按 SSE 协议拼一帧。

    两个细节：
    1. 每个 data 后必须跟一个空行，否则前端解析不出帧边界——
       这是 SSE 最常见的"看起来在发但前端收不到"的坑。
    2. ensure_ascii=False，中文直接发，前端不用二次解码。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ====================================================================
# 把同步生成器包成异步迭代器
# ----------------------------------------------------------------
# LLM 流式调用用的是 requests（同步阻塞）。如果直接在 async 生成器里
# for 循环它，会占住事件循环——一个慢模型请求能让整个服务停止响应。
# 所以丢进线程池生产，用 asyncio.Queue 跨线程搬运。
# ====================================================================
async def aiter_sync_gen(sync_gen) -> AsyncIterator[Any]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def producer() -> None:
        try:
            for item in sync_gen:
                # call_soon_threadsafe 是跨线程安全投递的唯一正确方式
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except BaseException as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("__error__", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("__end__", None))

    # 只提交不等待——这样队列可以边生产边消费，才是真流式
    loop.run_in_executor(_LLM_EXECUTOR, producer)

    while True:
        item = await queue.get()
        if item[0] == "__end__":
            return
        if item[0] == "__error__":
            raise item[1]
        yield item


# ====================================================================
# POST /chat —— 主接口
# ====================================================================
@app.post("/chat")
async def chat(request: Request, body: ChatRequest,
               user: CurrentUser = Depends(require_user)) -> StreamingResponse:
    """SSE 流式问答。

    事件顺序（与前端约定，不可随意变更）：
        meta → status(retrieving) → citations → status(generating)
             → delta* → done
        任意一步失败 → error（之后不再发其他事件）

    ⭐ 阶段 3 起本接口需要登录：
        - 会话绑定 user_id，审计页才能"按员工筛选"
        - 限流键从 IP 改为 user_id（同一 IP 多人办公场景下
          用 IP 限流会误伤，管理员和员工互相踩）
    """
    rid = _request_id(request)

    # ---- 1) 参数校验（顺序即优先级）----
    question = (body.question or "").strip()
    if not question:
        raise RAGError(ErrorCode.EMPTY_QUESTION, detail="question 为空白字符串")
    if len(question) > CFG.max_question_len:
        raise RAGError(ErrorCode.QUESTION_TOO_LONG,
                       detail=f"当前 {len(question)} 字，上限 {CFG.max_question_len} 字")

    sid = body.session_id
    if sid is not None and not SESSION_ID_RE.match(sid):
        raise RAGError(ErrorCode.BAD_SESSION_ID, detail="仅允许 8-64 位字母数字_-")
    if sid is None:
        # 前端没传 → 服务端新建，通过 meta 事件回传
        sid = "c_" + uuid.uuid4().hex[:16]

    # ---- 2) 限流 ----
    # ⭐ 阶段 3：限流键从 IP 改为 user_id。
    #   派遣企业常见多人共用一台办公机，用 IP 限流会让同事
    #   互相踩额度；用 IP 挡不住换 IP 的滥用，用账号才准确。
    if not _LIMITER.allow(f"user:{user.id}"):
        raise RAGError(ErrorCode.RATE_LIMITED,
                       detail=f"账号 {user.username} 超过 20 次/分钟")

    # ---- 3) 索引就绪检查 ----
    if _KB is None:
        raise RAGError(ErrorCode.INDEX_NOT_READY, detail="知识库未初始化")

    # ---- 4) 会话落库 + 取历史（必须在插入本轮问题之前取，否则自己会成为历史）----
    is_new = await asyncio.to_thread(
        _DB.ensure_conversation, sid, user.id, user.tenant_id
    )
    history_rows = await asyncio.to_thread(_DB.recent_messages, sid)
    history = trim_history(history_rows, CFG.history_rounds, CFG.history_char_budget)

    user_mid = "m_" + uuid.uuid4().hex[:16]
    asst_mid = "m_" + uuid.uuid4().hex[:16]
    # ⭐ 用关键字参数传，避免位置错位把 tenant_id 写进 status
    await asyncio.to_thread(
        _DB.insert_message, user_mid, sid, "user", question,
        status="done", tenant_id=user.tenant_id,
    )

    # ---- 规则预筛（同步执行，不进流内）----
    # ⭐ 放在这里而不是 event_stream 里：预筛失败绝不能影响员工正常问答。
    #   审计是"锦上添花"的安全网，不能因为安全网坏了就让业务不可用。
    #   所以这里用 try 包住，任何异常都吞掉并记日志。
    try:
        from .core.audit_rules import prescreen_with_patterns
        screen = prescreen_with_patterns(question)
        if screen.should_flag:
            existing = await asyncio.to_thread(
                _ADMIN_DB.existing_flag, user.tenant_id, user_mid
            )
            if existing is None:
                await asyncio.to_thread(
                    _ADMIN_DB.add_flag, user.tenant_id, user_mid, "suspected",
                    "rule", screen.reason, screen.hit_rule, "",
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] 规则预筛异常，已跳过：{type(exc).__name__}: {exc}")

    # 先插一条 streaming 占位，流结束后再补全
    await asyncio.to_thread(
        _DB.insert_message, asst_mid, sid, "assistant", "",
        status="streaming", tenant_id=user.tenant_id,
    )

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield sse_frame("meta", {
                "session_id": sid,
                "message_id": asst_mid,
                "is_new_session": is_new,
            })
            yield sse_frame("status", {"stage": "retrieving", "text": "正在检索制度…"})

            # ---- 5) 检索（同步，丢线程池）----
            # ⭐ 按租户检索：知识库索引按 tenant_id 隔离，
            #   A 用工单位的员工绝不可能检索到 B 的制度。
            try:
                hits = await asyncio.to_thread(
                    _KB.retrieve, user.tenant_id, question
                )
            except RAGError as exc:
                await asyncio.to_thread(_DB.fail_message, asst_mid, exc.code)
                yield sse_frame("error", exc.to_dict(rid))
                return

            yield sse_frame("citations", {"items": hits})
            yield sse_frame("status", {"stage": "generating", "text": "正在生成回答…"})

            # ---- 6) 流式生成 ----
            parts: list[str] = []
            usage: dict[str, Any] = {}
            citations = hits
            citation_check: dict[str, Any] | None = None

            from .core.rag_engine import build_messages
            # ⭐ 取当前租户生效的提示词正文段（管理员后台可编辑）。
            #   强制段（引用编号/拒答规则）由代码写死，管理员改不了。
            prompt_row = await asyncio.to_thread(
                _ADMIN_DB.get_active_prompt, user.tenant_id
            )
            custom_body = prompt_row["body_text"] if prompt_row else ""
            messages = build_messages(question, hits, history,
                                      custom_body=custom_body)

            try:
                async for kind, payload in aiter_sync_gen(
                    _ENGINE.llm.chat_stream(
                        messages,
                        temperature=CFG.llm_temperature,
                        max_tokens=CFG.llm_max_tokens,
                    )
                ):
                    if kind == "delta":
                        parts.append(payload)
                        yield sse_frame("delta", {"text": payload})
                    elif kind == "done":
                        usage = payload
            except RAGError as exc:
                await asyncio.to_thread(_DB.fail_message, asst_mid, exc.code)
                yield sse_frame("error", exc.to_dict(rid))
                return

            answer = "".join(parts)

            # ---- 7) 引用自检（沿用阶段 1 的机械校验）----
            from .core.rag_engine import validate_citations
            citation_check = validate_citations(answer, len(hits))

            # ---- 8) 落库 + 收尾 ----
            await asyncio.to_thread(
                _DB.finish_message, asst_mid, answer, citations,
                citation_check, usage, usage.get("latency_ms", 0),
            )

            yield sse_frame("done", {
                "message_id": asst_mid,
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                },
                "latency_ms": usage.get("latency_ms", 0),
                "citation_check": citation_check,
            })

        except Exception as exc:  # noqa: BLE001
            # 兜底：流已经开始了，不能改 HTTP 状态码，只能发 error 事件
            await asyncio.to_thread(_DB.fail_message, asst_mid,
                                    ErrorCode.INTERNAL_ERROR[0])
            yield sse_frame("error", {
                "code": ErrorCode.INTERNAL_ERROR[0],
                "message": ErrorCode.INTERNAL_ERROR[1],
                "detail": f"{type(exc).__name__}: {exc}",
                "request_id": rid,
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # 三个头都是必须的，缺一个就可能被代理或浏览器缓冲，表现为"一次性全出来"
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ====================================================================
# GET /api/sessions/{sid}/messages —— 历史恢复
# ====================================================================
@app.get("/api/sessions/{sid}/messages")
async def get_messages(sid: str,
                       user: CurrentUser = Depends(require_user)) -> JSONResponse:
    rid = "req_" + uuid.uuid4().hex[:12]
    if not SESSION_ID_RE.match(sid):
        raise RAGError(ErrorCode.BAD_SESSION_ID, detail="仅允许 8-64 位字母数字_-")

    exists = await asyncio.to_thread(_DB.conversation_exists, sid)
    if not exists:
        raise RAGError(ErrorCode.SESSION_NOT_FOUND, detail=f"会话 {sid} 不存在")

    # ⭐ 租户隔离 + 归属校验：非管理员只能读自己的会话。
    #   管理员可以读本租户任意会话（审计要求全量可见）。
    #   注意 tenant_id 先判——它是硬边界，用户归属只是次级约束。
    owner = await asyncio.to_thread(_DB.conversation_owner, sid)
    if owner.get("tenant_id") and owner["tenant_id"] != user.tenant_id:
        raise AuthError(
            (40311, "无权访问该会话"),
            detail=f"会话属于其他租户",
        )
    if not user.is_admin and owner.get("user_id") != user.id:
        raise AuthError(
            (40311, "无权访问该会话"),
            detail=f"会话属于其他用户",
        )

    rows = await asyncio.to_thread(_DB.all_messages, sid)
    return JSONResponse(content={"session_id": sid, "messages": rows})


# ====================================================================
# GET /health —— 存活 + 索引状态
# ====================================================================
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine_ready": bool(_ENGINE and _ENGINE.store.ready),
        "vector_enabled": bool(_ENGINE and _ENGINE.vector_enabled),
        "chunks": _ENGINE.store.index.ntotal if _ENGINE else 0,
        "llm_model": CFG.llm_model or "mock",
    }


# ====================================================================
# 静态页面
# ====================================================================
@app.get("/")
async def index(request: Request,
               user: CurrentUser = Depends(require_user)) -> Any:
    """员工问答首页。

    阶段 3 起需要登录——会话要绑定 user_id 才能被审计追溯。
    未登录时 AuthError 处理器会 302 到 /login。

    ⭐ notice 参数：权限不足被踢回来时，AuthError 处理器会带上
    ?notice=xxx。这里把它渲染成横幅显示给用户——
    否则用户被弹回首页却不知道发生了什么，只会以为"系统坏了"。
    """
    notice = request.query_params.get("notice", "").strip()
    html_text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if notice:
        # ⭐ 样式内联，不依赖外部 CSS——员工端 index.html 是全内联样式的
        #   单文件页面，本来就不引 admin.css。
        #   内容走 html.escape 转义：notice 来自异常消息，不能直接拼进 HTML。
        banner = (
            '<div class="notice-banner" role="alert">'
            f'{html.escape(notice)}</div>'
            '<style>'
            '.notice-banner{position:fixed;top:0;left:0;right:0;z-index:9999;'
            'padding:11px 18px;background:#fef3c7;border-bottom:1px solid #fde0a8;'
            'color:#92400e;font-size:13.5px;text-align:center;'
            'box-shadow:0 2px 8px rgba(0,0,0,.06);'
            'animation:notice-fade 5s ease-in-out forwards;}'
            '@keyframes notice-fade{0%,70%{opacity:1;}'
            '100%{opacity:0;visibility:hidden;}}'
            '</style>'
        )
        html_text = html_text.replace(
            "</body>", f"{banner}\n</body>", 1
        )
    return HTMLResponse(html_text)


# ====================================================================
# 鉴权路由（阶段 3）
# ====================================================================
@app.get("/login")
async def login_page(request: Request) -> Any:
    """登录页。已登录直接进后台或问答页。"""
    # 已登录用户不该再看登录页
    try:
        token = request.cookies.get("access_token")
        if token:
            payload = decode_access_token(token)
            user_row = await asyncio.to_thread(_ADMIN_DB.get_user, payload["sub"])
            if user_row and user_row.get("is_active", 1):
                target = "/admin" if user_row["role"] == "admin" else "/"
                return RedirectResponse(url=target, status_code=302)
    except AuthError:
        pass  # 令牌无效 → 正常显示登录页

    tenants = await asyncio.to_thread(_ADMIN_DB.list_tenants)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "tenants": tenants,
            "default_tenant": "default",
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/login")
async def login_submit(request: Request,
                       tenant_code: str = Form(...),
                       username: str = Form(...),
                       password: str = Form(...)) -> Any:
    """账密登录。

    成功 → 下发 JWT（同时写 HttpOnly Cookie 供 SSR 页面跳转使用）
    失败 → 回登录页并显示中文错误
    """
    client_ip = request.client.host if request.client else "unknown"
    try:
        user = await asyncio.to_thread(
            _ADMIN_DB.authenticate, tenant_code, username, password
        )
    except AuthError as exc:
        # 登录失败回页面，不返回 JSON（这是表单提交）
        tenants = await asyncio.to_thread(_ADMIN_DB.list_tenants)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "tenants": tenants,
                "default_tenant": tenant_code,
                "error": exc.message,
            },
            status_code=401,
        )

    await asyncio.to_thread(_ADMIN_DB.record_login, user["id"])
    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user["tenant_id"], user["id"], user["display_name"] or user["username"],
        "login", "user", user["id"], {"username": user["username"]}, client_ip,
    )

    token = create_access_token(
        user["id"], user["tenant_id"], user["role"], user["username"]
    )
    # 管理员进后台，员工进问答页
    target = "/admin" if user["role"] == "admin" else "/"
    resp = RedirectResponse(url=target, status_code=302)
    # HttpOnly：JavaScript 读不到这个 cookie，XSS 偷不走令牌
    # SameSite=Lax：跨站跳转不带 cookie，防 CSRF 泄漏
    resp.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax",
        max_age=12 * 3600, path="/",
    )
    return resp


@app.get("/logout")
async def logout(request: Request) -> Any:
    """退出登录。写审计日志再清 Cookie。"""
    try:
        token = request.cookies.get("access_token")
        if token:
            payload = decode_access_token(token)
            user_row = await asyncio.to_thread(_ADMIN_DB.get_user, payload["sub"])
            if user_row:
                await asyncio.to_thread(
                    _ADMIN_DB.log_action,
                    user_row["tenant_id"], user_row["id"],
                    user_row.get("display_name") or user_row["username"],
                    "logout", "user", user_row["id"], {},
                    request.client.host if request.client else "",
                )
    except AuthError:
        pass  # 没登录也允许直接退

    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("access_token", path="/")
    return resp


# ====================================================================
# 用户管理（用户拍板 Q2 = 管理员在后台建号，不开放注册）
# ====================================================================
@app.get("/api/admin/users")
async def api_list_users(user: CurrentUser = Depends(require_admin)
                         ) -> JSONResponse:
    rows = await asyncio.to_thread(_ADMIN_DB.list_users, user.tenant_id)
    return JSONResponse(content={"users": rows})


class CreateUserRequest(BaseModel):
    username: str = Field(..., description="登录用户名")
    password: str = Field(..., description="初始密码，至少 8 位含字母数字")
    display_name: str = Field(default="", description="显示名")
    role: str = Field(default="employee", description="employee | admin")
    employee_no: str = Field(default="", description="工号")


@app.post("/api/admin/users")
async def api_create_user(body: CreateUserRequest,
                          request: Request,
                          user: CurrentUser = Depends(require_admin)
                          ) -> JSONResponse:
    """管理员建号。

    租户边界取自 JWT（user.tenant_id），**不接受请求体传租户**——
    否则就是越权建号漏洞。
    """
    created = await asyncio.to_thread(
        _ADMIN_DB.create_user,
        user.tenant_id, body.username, body.password,
        body.display_name, body.role, body.employee_no, user.id,
    )
    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "user_create", "user", created["id"],
        {"username": created["username"], "role": created["role"]},
        request.client.host if request.client else "",
    )
    # 返回里绝不能带 password_hash
    safe = {k: v for k, v in created.items() if k != "password_hash"}
    return JSONResponse(content={"user": safe})


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., description="新密码")


@app.post("/api/admin/users/{user_id}/reset-password")
async def api_reset_password(user_id: str, body: ResetPasswordRequest,
                              request: Request,
                              user: CurrentUser = Depends(require_admin)
                              ) -> JSONResponse:
    """管理员重置密码（员工遗忘密码时用）。"""
    await asyncio.to_thread(
        _ADMIN_DB.reset_password, user.tenant_id, user_id, body.new_password
    )
    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "password_reset", "user", user_id, {},
        request.client.host if request.client else "",
    )
    return JSONResponse(content={"ok": True, "message": "密码已重置"})


# ====================================================================
# 管理后台页面（服务端渲染）
# ----------------------------------------------------------------
# ⭐ 所有后台页面都挂在 /admin 下，权限由 require_admin 依赖强制。
#   非管理员访问会被 AuthError 处理器 302 到登录页——
#   而不是返回 403，因为 403 会暴露"后台存在"这个信息。
# ====================================================================
@app.get("/admin")
async def admin_home(request: Request,
                     user: CurrentUser = Depends(require_admin)) -> Any:
    """后台首页：关键指标概览。"""
    kb_stats = await asyncio.to_thread(_KB.get_stats, user.tenant_id)
    audit_stats = await asyncio.to_thread(
        _ADMIN_DB.audit_stats, user.tenant_id
    )
    prompt = await asyncio.to_thread(
        _ADMIN_DB.get_active_prompt, user.tenant_id
    )
    return templates.TemplateResponse(
        request,
        "admin_home.html",
        {
            "user": user,
            "active": "home",
            "kb_stats": kb_stats,
            "audit_stats": audit_stats,
            "prompt": prompt,
        },
    )


# ====================================================================
# 制度知识库
# ====================================================================
@app.get("/admin/knowledge")
async def admin_knowledge(request: Request,
                          user: CurrentUser = Depends(require_admin)) -> Any:
    """知识库管理页：上传、列表、删除、切片预览。"""
    documents = await asyncio.to_thread(_KB.list_documents, user.tenant_id)
    stats = await asyncio.to_thread(_KB.get_stats, user.tenant_id)
    return templates.TemplateResponse(
        request,
        "admin_knowledge.html",
        {
            "user": user,
            "active": "knowledge",
            "documents": documents,
            "stats": stats,
            "cfg": CFG,
            "flash": request.query_params.get("flash", ""),
        },
    )


@app.post("/admin/knowledge/upload")
async def admin_kb_upload(request: Request,
                          file: UploadFile = File(...),
                          user: CurrentUser = Depends(require_admin)) -> Any:
    """上传制度文档 → 切片 → 向量化 → 入库 → 重建索引。

    ⚠️ 这个接口是**同步阻塞**的（要调 embedding API），
    所以走 run_in_threadpool 而不是直接 await。
    """
    raw = await file.read()
    filename = file.filename or "未命名文档"

    # 大小上限：10MB。制度文档不该有这么大，
    # 超了多半是上传错了文件，不是真需求。
    if len(raw) > 10 * 1024 * 1024:
        return RedirectResponse(
            url=f"/admin/knowledge?flash={quote('文件超过 10MB 上限')}",
            status_code=302,
        )

    try:
        result = await asyncio.to_thread(
            _KB.ingest_document, user.tenant_id, filename, raw, user.id
        )
        flash = f"《{filename}》上传成功，切片 {result['chunk_count']} 块"
    except RAGError as exc:
        flash = f"《{filename}》上传失败：{exc.message}"
    except Exception as exc:
        flash = f"《{filename}》上传失败：{type(exc).__name__}"

    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "kb_upload", "document", filename, {"flash": flash},
        request.client.host if request.client else "",
    )
    return RedirectResponse(
        url=f"/admin/knowledge?flash={quote(flash)}", status_code=302
    )


@app.post("/admin/knowledge/documents/{doc_id}/delete")
async def admin_kb_delete(request: Request, doc_id: str,
                          user: CurrentUser = Depends(require_admin)) -> Any:
    """删除文档并重建索引。

    ⭐ 删除后立即重建索引，员工的下一句提问就不会再命中这份文档。
    如果不重建，FAISS 里还留着它的切片——用户以为删了，问出来还是旧条款。
    """
    ok = await asyncio.to_thread(_KB.delete_document, user.tenant_id, doc_id)
    flash = "文档已删除，索引已重建" if ok else "删除失败：文档不存在"

    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "kb_delete", "document", doc_id, {"ok": ok},
        request.client.host if request.client else "",
    )
    return RedirectResponse(
        url=f"/admin/knowledge?flash={quote(flash)}", status_code=302
    )


@app.get("/admin/knowledge/documents/{doc_id}/chunks")
async def admin_kb_chunks(request: Request, doc_id: str,
                          user: CurrentUser = Depends(require_admin)) -> Any:
    """切片预览（分页）。

    ⭐ 这个页面是管理员检查切片质量的工具——
    切片切坏了，问答一定答错，而切片质量从答案里看不出来，
    必须能直接看到"这一块到底是什么"。
    """
    document = await asyncio.to_thread(
        _ADMIN_DB.get_document, user.tenant_id, doc_id
    )
    if document is None:
        return RedirectResponse(
            url=f"/admin/knowledge?flash={quote('文档不存在')}", status_code=302
        )

    page = max(1, int(request.query_params.get("page", "1")))
    per_page = 20
    total = await asyncio.to_thread(
        _ADMIN_DB.count_chunks, user.tenant_id, doc_id
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    chunks = await asyncio.to_thread(
        _KB.list_chunks, user.tenant_id, doc_id, per_page, (page - 1) * per_page
    )
    return templates.TemplateResponse(
        request,
        "admin_knowledge_chunks.html",
        {
            "user": user,
            "active": "knowledge",
            "document": document,
            "chunks": chunks,
            "page": page,
            "total": total,
            "total_pages": total_pages,
        },
    )


@app.post("/admin/knowledge/reindex")
async def admin_kb_reindex(request: Request,
                           user: CurrentUser = Depends(require_admin)) -> Any:
    """手动全量重建索引（兜底按钮）。

    ⭐ 重建只从数据库读向量，**不调 embedding API**——
    实测 500 块重建 0.002s，所以这个按钮可以随便点。
    """
    result = await asyncio.to_thread(_KB.rebuild_index, user.tenant_id)
    flash = (f"索引已重建：{result['chunk_count']} 块，"
             f"耗时 {result['elapsed_ms']}ms")
    return RedirectResponse(
        url=f"/admin/knowledge?flash={quote(flash)}", status_code=302
    )


# ====================================================================
# 用户管理页面（SSR 版，配套上面的 /api/admin/users 接口）
# ====================================================================
@app.get("/admin/users")
async def admin_users(request: Request,
                      user: CurrentUser = Depends(require_admin)) -> Any:
    """用户管理页：建号、启停用、重置密码。"""
    users = await asyncio.to_thread(_ADMIN_DB.list_users, user.tenant_id)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "user": user,
            "active": "users",
            "users": users,
            "flash": request.query_params.get("flash", ""),
        },
    )


@app.post("/admin/users/create")
async def admin_users_create(request: Request,
                             username: str = Form(...),
                             password: str = Form(...),
                             display_name: str = Form(""),
                             role: str = Form("employee"),
                             employee_no: str = Form(""),
                             user: CurrentUser = Depends(require_admin)) -> Any:
    """管理员建号。

    ⭐ 租户边界取自 JWT，**不接受表单传租户**——否则是越权建号漏洞。
    """
    try:
        created = await asyncio.to_thread(
            _ADMIN_DB.create_user,
            user.tenant_id, username, password, display_name, role,
            employee_no, user.id,
        )
        flash = f"账号 {created['username']} 创建成功"
        await asyncio.to_thread(
            _ADMIN_DB.log_action,
            user.tenant_id, user.id, user.display_name,
            "user_create", "user", created["id"],
            {"username": created["username"], "role": created["role"]},
            request.client.host if request.client else "",
        )
    except AuthError as exc:
        flash = f"创建失败：{exc.message}"
    except Exception as exc:  # noqa: BLE001
        flash = f"创建失败：{type(exc).__name__}"

    return RedirectResponse(
        url=f"/admin/users?flash={quote(flash)}", status_code=302
    )


@app.post("/admin/users/{user_id}/toggle")
async def admin_users_toggle(request: Request, user_id: str,
                             user: CurrentUser = Depends(require_admin)) -> Any:
    """启用 / 停用账号。

    ⭐ 停用后该用户已签发的 JWT 会立即失效——get_current_user 每次请求
    都查库确认 is_active，不信任令牌里的状态。
    """
    target = await asyncio.to_thread(_ADMIN_DB.get_user, user_id)
    if target is None or target["tenant_id"] != user.tenant_id:
        flash = "账号不存在或不属于当前租户"
    else:
        new_active = 0 if target.get("is_active", 1) else 1
        await asyncio.to_thread(
            _ADMIN_DB.update_user, user.tenant_id, user_id,
            display_name=target.get("display_name", ""),
            role=target.get("role", "employee"),
            employee_no=target.get("employee_no", ""),
            is_active=new_active,
        )
        flash = f"账号 {target['username']} 已{'启用' if new_active else '停用'}"
        await asyncio.to_thread(
            _ADMIN_DB.log_action,
            user.tenant_id, user.id, user.display_name,
            "user_toggle", "user", user_id,
            {"username": target["username"], "is_active": new_active},
            request.client.host if request.client else "",
        )

    return RedirectResponse(
        url=f"/admin/users?flash={quote(flash)}", status_code=302
    )


@app.post("/admin/users/{user_id}/reset-password")
async def admin_users_reset_password(request: Request, user_id: str,
                                     new_password: str = Form(...),
                                     user: CurrentUser = Depends(require_admin)) -> Any:
    """管理员重置密码。"""
    try:
        await asyncio.to_thread(
            _ADMIN_DB.reset_password, user.tenant_id, user_id, new_password,
        )
        flash = "密码已重置"
        await asyncio.to_thread(
            _ADMIN_DB.log_action,
            user.tenant_id, user.id, user.display_name,
            "user_reset_password", "user", user_id, {},
            request.client.host if request.client else "",
        )
    except AuthError as exc:
        flash = f"重置失败：{exc.message}"
    except Exception as exc:  # noqa: BLE001
        flash = f"重置失败：{type(exc).__name__}"

    return RedirectResponse(
        url=f"/admin/users?flash={quote(flash)}", status_code=302
    )


# ====================================================================
# 对话审计
# ====================================================================
@app.get("/admin/audit")
async def admin_audit(request: Request,
                       user: CurrentUser = Depends(require_admin)) -> Any:
    """对话审计页：按员工/关键词/日期/预筛状态筛选，复核疑似违规。

    ⭐ 审计页的定位是"能追责"——所以：
        1. 所有查询强制带 tenant_id，跨租户数据绝不出现
        2. 记录不只是提问，也包含助手回答（问答全貌）
        3. 复核动作本身也写进 audit_logs，谁在什么时候判了什么都能查
    """
    q = request.query_params.get("q", "").strip()
    user_id = request.query_params.get("user_id", "").strip()
    flag_status = request.query_params.get("flag_status", "").strip()
    date_from = request.query_params.get("date_from", "").strip()
    date_to = request.query_params.get("date_to", "").strip()

    page = max(1, int(request.query_params.get("page", "1") or 1))
    per_page = 30

    rows, total = await asyncio.to_thread(
        _DB.audit_query, user.tenant_id, q, user_id, date_from, date_to,
        flag_status, per_page, (page - 1) * per_page,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    users = await asyncio.to_thread(
        _ADMIN_DB.list_users, user.tenant_id, "employee"
    )
    audit_stats = await asyncio.to_thread(
        _ADMIN_DB.audit_stats, user.tenant_id
    )
    action_logs = await asyncio.to_thread(
        _ADMIN_DB.list_action_logs, user.tenant_id, 100
    )

    # 翻页链接要把当前筛选条件带上，否则翻页等于清空筛选
    def _page_url(p: int) -> str:
        params = []
        if q:
            params.append(f"q={quote(q)}")
        if user_id:
            params.append(f"user_id={quote(user_id)}")
        if flag_status:
            params.append(f"flag_status={quote(flag_status)}")
        if date_from:
            params.append(f"date_from={quote(date_from)}")
        if date_to:
            params.append(f"date_to={quote(date_to)}")
        params.append(f"page={p}")
        return "/admin/audit?" + "&".join(params)

    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "user": user,
            "active": "audit",
            "records": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "prev_url": _page_url(page - 1),
            "next_url": _page_url(page + 1),
            "q": q,
            "user_id": user_id,
            "flag_status": flag_status,
            "date_from": date_from,
            "date_to": date_to,
            "users": users,
            "audit_stats": audit_stats,
            "action_logs": action_logs,
            "flash": request.query_params.get("flash", ""),
        },
    )


@app.post("/admin/audit/flags/{flag_id}/review")
async def admin_audit_review(request: Request, flag_id: int,
                             decision: str = Form(...),
                             reason: str = Form(""),
                             user: CurrentUser = Depends(require_admin)) -> Any:
    """人工复核一条预筛标记：确认违规 / 驳回。

    ⭐ 系统只做召回，不做定性——最终结论必须由人给出，
    并且这个"人给了什么结论"本身也要留痕。
    """
    if decision not in ("confirmed", "rejected"):
        flash = "复核结论不合法"
    else:
        reason = (reason or "").strip()
        if not reason:
            reason = "管理员复核确认" if decision == "confirmed" else "管理员复核驳回"
        try:
            await asyncio.to_thread(
                _ADMIN_DB.update_flag, user.tenant_id, flag_id,
                decision, reason, user.display_name,
            )
            flash = "已确认为违规" if decision == "confirmed" else "已驳回该标记"
        except Exception as exc:  # noqa: BLE001
            flash = f"复核失败：{type(exc).__name__}"

    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "audit_review", "flag", str(flag_id),
        {"decision": decision, "reason": reason},
        request.client.host if request.client else "",
    )
    return RedirectResponse(
        url=f"/admin/audit?flash={quote(flash)}", status_code=302
    )


# ====================================================================
# 提示词模板管理
# ====================================================================
@app.get("/admin/prompts")
async def admin_prompts(request: Request,
                        user: CurrentUser = Depends(require_admin)) -> Any:
    """提示词模板页：查看强制段、编辑正文段、版本历史与回滚。

    ⭐ 强制段只读、正文段可编辑，这是刻意的分层：
    强制段承载引用编号与拒答约束，是"可追溯"的技术前提；
    正文段承载业务口径，是管理员真正需要高频调整的部分。
    """
    from .core.rag_engine import DEFAULT_PROMPT_BODY, MANDATORY_RULES

    prompt = await asyncio.to_thread(
        _ADMIN_DB.get_active_prompt, user.tenant_id
    )
    history = await asyncio.to_thread(
        _ADMIN_DB.list_prompt_history, user.tenant_id, 50
    )
    return templates.TemplateResponse(
        request,
        "admin_prompts.html",
        {
            "user": user,
            "active": "prompts",
            "prompt": prompt,
            "history": history,
            "mandatory": MANDATORY_RULES,
            "body_text": prompt["body_text"] if prompt else DEFAULT_PROMPT_BODY,
            "flash": request.query_params.get("flash", ""),
        },
    )


@app.post("/admin/prompts/save")
async def admin_prompts_save(request: Request,
                             body_text: str = Form(""),
                             name: str = Form(""),
                             user: CurrentUser = Depends(require_admin)) -> Any:
    """保存提示词正文段 → 生成新版本 → 立即生效。

    ⚠️ 强制段（MANDATORY_RULES）**不在这里拼进去**——
    它在 rag_engine.build_system_prompt 里拼，管理员改不了。
    这里只存"可编辑部分"，这样即便数据库被改坏，
    引用编号规则也不会丢。
    """
    body_text = (body_text or "").strip()
    name = (name or "").strip() or "制度答疑默认模板"

    truncated = False
    if len(body_text) > 8000:
        body_text = body_text[:8000]
        truncated = True

    saved = await asyncio.to_thread(
        _ADMIN_DB.save_prompt, user.tenant_id, name, body_text, user.display_name
    )
    flash = f"提示词已保存并立即生效（v{saved['version']}）"
    if truncated:
        flash += "，正文超长已截断至 8000 字"

    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "prompt_update", "prompt", str(saved["version"]),
        {"version": saved["version"], "name": name, "length": len(body_text)},
        request.client.host if request.client else "",
    )
    return RedirectResponse(
        url=f"/admin/prompts?flash={quote(flash)}", status_code=302
    )


@app.post("/admin/prompts/rollback")
async def admin_prompts_rollback(request: Request,
                                 version: int = Form(...),
                                 user: CurrentUser = Depends(require_admin)) -> Any:
    """回滚到指定版本。

    ⭐ 回滚不是把历史行的 is_active 改回去，而是**把历史内容作为新版本写入**。
    那样"版本号"和"实际生效顺序"永远一致，审计时能说清哪一版真正上线过、
    什么时候被谁回滚掉了。
    """
    try:
        saved = await asyncio.to_thread(
            _ADMIN_DB.rollback_prompt, user.tenant_id, version, user.display_name
        )
        flash = f"已回滚到 v{version} 内容，并生成新版本 v{saved['version']} 生效"
    except AuthError as exc:
        flash = f"回滚失败：{exc.message}"

    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "prompt_rollback", "prompt", str(version), {"flash": flash},
        request.client.host if request.client else "",
    )
    return RedirectResponse(
        url=f"/admin/prompts?flash={quote(flash)}", status_code=302
    )


# ====================================================================
# 知识库 API（给前端 fetch 用，与 SSR 页面并存）
# ====================================================================
@app.get("/api/admin/kb/documents")
async def api_kb_documents(user: CurrentUser = Depends(require_admin)
                           ) -> JSONResponse:
    docs = await asyncio.to_thread(_KB.list_documents, user.tenant_id)
    stats = await asyncio.to_thread(_KB.get_stats, user.tenant_id)
    return JSONResponse(content={"documents": docs, "stats": stats})


@app.delete("/api/admin/kb/documents/{doc_id}")
async def api_kb_delete(doc_id: str, request: Request,
                        user: CurrentUser = Depends(require_admin)) -> JSONResponse:
    ok = await asyncio.to_thread(_KB.delete_document, user.tenant_id, doc_id)
    if not ok:
        raise RAGError(ErrorCode.INTERNAL_ERROR, detail="文档不存在或不属于当前租户")
    await asyncio.to_thread(
        _ADMIN_DB.log_action,
        user.tenant_id, user.id, user.display_name,
        "kb_delete", "document", doc_id, {"ok": True},
        request.client.host if request.client else "",
    )
    return JSONResponse(content={"ok": True, "message": "文档已删除，索引已重建"})


if __name__ == "__main__":
    import uvicorn

    # 用字符串目标而不是 app 对象：相对导入需要包上下文，
    # 直接传 app 对象在 `python main.py` 下会触发
    # "Attempted relative import with no known parent package"。
    # 启动方式统一为：PYTHONPATH=src python -m attendance_agent.main
    # 默认只监听本机，生产环境由反向代理暴露（见 docs/上线前检查清单）。
    uvicorn.run(
        "attendance_agent.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
