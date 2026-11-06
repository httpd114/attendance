#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py —— 阶段 2 SSE 聊天 MVP · 归档版最小入口
====================================================================
这是「派遣项目考勤异常双向确认系统」最早可跑起来的那一版员工问答原型，
只做一件事：读一份本地制度 Markdown → 检索 → 流式回答 → 落库留痕。

接口清单（与 static/index.html 的契约完全对应，不可随意改事件顺序）：
    POST /chat                          SSE 流式问答
    GET  /api/sessions/{sid}/messages    刷新页面后恢复会话历史
    GET  /health                          存活 + 索引就绪状态
    GET  /                               单页聊天界面

【归档说明】
    rag_engine.py / static/index.html / 报销制度.md 三个文件完整取自
    Git 历史提交 0ffc8f4 的 chat-app/ 目录，字节级未改动。
    本文件是按阶段 2 的接口契约重建的**薄编排层**：历史上的 main.py
    后来在同一个文件里叠加了阶段 3 的登录、管理后台、审计与租户隔离，
    且建表 SQL 与 INSERT 语句依赖 migrate.py 补列，无法在不重写的前提下
    独立启动。因此这里只保留阶段 2 的协议编排，算法层仍调用原版
    RAGEngine.stream_chat()，分层纪律不变：api 层不碰算法。

运行（详见同目录 README.md）：
    cd src/mvp
    python -m uvicorn main:app --host 127.0.0.1 --port 8000

    不配置任何密钥也能启动：自动退化成 hash embedding（纯 BM25 检索）
    + MockLLM，用于验证检索、SSE、落库这条链路是否通。
"""

from __future__ import annotations

import asyncio
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

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from rag_engine import Config, ErrorCode, RAGEngine, RAGError, trim_history

# ====================================================================
# 全局配置
# ----------------------------------------------------------------
# 归档版把文档、索引缓存、数据库都锚定在**本文件所在目录**，
# 去掉历史版本「必须在某个 cwd 下启动」这个隐式前提。
# ====================================================================
BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("DOC_PATH", str(BASE_DIR / "报销制度.md"))

CFG = Config.from_env()
# 部署产物相对 src/mvp 的唯一改动：索引缓存目录允许由 INDEX_DIR 覆盖。
# 发布沙箱的应用目录通常是只读的，缓存必须落在可写目录（如 /tmp）。
CFG.index_dir = os.getenv("INDEX_DIR", str(BASE_DIR / ".rag_cache"))

# 没有密钥时自动离线降级：hash embedding 会整条关闭向量路、只走 BM25，
# build_llm() 返回 MockLLM。这样克隆下来不用申请任何账号就能验证链路。
if not os.getenv("EMBED_PROVIDER") and not os.getenv("LLM_API_KEY"):
    CFG.embed_provider = "hash"

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "chat.db"))
STATIC_DIR = BASE_DIR / "static"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

# LLM 流式调用是同步阻塞 IO，必须丢进线程池，否则一个慢请求会堵死事件循环。
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mvp-llm")


# ====================================================================
# 简易限流（进程内存滑动窗口）
# ----------------------------------------------------------------
# 原型的量级够用；生产环境换 Redis，接口保持 check(key) -> bool 不变。
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
# SQLite 存储层：两张表 conversations / messages
# ----------------------------------------------------------------
# 与历史版本的差别：这里去掉了 user_id / tenant_id 列。阶段 2 是匿名
# 原型，这两列是阶段 3 通过 migrate.py 补上的；归档版不引入迁移脚本，
# 因此建表语句与 INSERT 语句保持自洽（历史版本两者不一致，裸跑会报
# 「no such column: tenant_id」）。
# ====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新会话',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL
                      REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL,           -- user | assistant
    content           TEXT NOT NULL DEFAULT '',
    citations         TEXT,                    -- JSON 数组，assistant 才有
    citation_check    TEXT,                    -- JSON，assistant 才有
    status            TEXT NOT NULL DEFAULT 'done',  -- streaming | done | error
    error_code        INTEGER,                 -- 失败时记录，成功为 NULL
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_time
    ON messages(conversation_id, created_at);
"""


def _now_iso() -> str:
    """本地时间 ISO8601（带时区偏移），避免前后端解析歧义。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ChatDB:
    """SQLite 封装：WAL + 写锁 + 单连接跨线程复用。

    三个工程处理沿用历史实现：
    1. WAL 模式，读写不互斥；
    2. check_same_thread=False + 写锁，配合线程池调用；
    3. 所有调用由接口层用 asyncio.to_thread 包一层，不阻塞事件循环。
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

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read_all(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # ---- 会话 ----
    def ensure_conversation(self, sid: str) -> bool:
        """确保会话存在；返回 True 表示本次是新建（前端据此写 localStorage）。"""
        if self._read_all("SELECT id FROM conversations WHERE id = ?", (sid,)):
            return False
        now = _now_iso()
        self._write(
            "INSERT INTO conversations (id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, "新会话", now, now),
        )
        return True

    def conversation_exists(self, sid: str) -> bool:
        return bool(self._read_all(
            "SELECT id FROM conversations WHERE id = ?", (sid,)
        ))

    # ---- 消息 ----
    def insert_message(self, mid: str, sid: str, role: str, content: str,
                       status: str = "done") -> None:
        self._write(
            "INSERT INTO messages (id, conversation_id, role, content, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, sid, role, content, status, _now_iso()),
        )
        self._write(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now_iso(), sid),
        )

    def finish_message(self, mid: str, content: str,
                       citations: list | None, citation_check: dict | None,
                       tokens: dict, latency_ms: int) -> None:
        """流正常结束后，把 streaming 占位补全为 done。"""
        self._write(
            "UPDATE messages SET content = ?, citations = ?, citation_check = ?, "
            "status = 'done', error_code = NULL WHERE id = ?",
            (
                content,
                json.dumps(citations, ensure_ascii=False) if citations else None,
                json.dumps(citation_check, ensure_ascii=False) if citation_check else None,
                mid,
            ),
        )

    def fail_message(self, mid: str, error_code: int) -> None:
        """流中途失败：标记 error 并记错误码，客户端断开也能在库里看到。"""
        self._write(
            "UPDATE messages SET status = 'error', error_code = ? WHERE id = ?",
            (error_code, mid),
        )

    def recent_messages(self, sid: str, limit: int = 50) -> list[dict[str, Any]]:
        """取最近 limit 条并按时间正序返回；轮次/字数截断交给 trim_history。"""
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
            "error_code, created_at FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (sid,),
        )
        for row in rows:
            for key in ("citations", "citation_check"):
                if row.get(key):
                    try:
                        row[key] = json.loads(row[key])
                    except json.JSONDecodeError:
                        row[key] = None
        return rows

    def cleanup_stale_streaming(self) -> int:
        """收尸：上次进程退出时没写完的 streaming 消息统一标成 error。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET status = 'error', error_code = ? "
                "WHERE status = 'streaming'",
                (ErrorCode.INTERNAL_ERROR[0],),
            )
            self._conn.commit()
            return cur.rowcount


_DB: ChatDB | None = None
_ENGINE: RAGEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时构建一次 RAG 引擎，索引常驻内存。"""
    global _DB, _ENGINE
    _DB = ChatDB(DB_PATH)
    stale = _DB.cleanup_stale_streaming()
    if stale:
        print(f"[startup] 清理了 {stale} 条未完成的流式消息", flush=True)

    t0 = time.time()
    try:
        _ENGINE = RAGEngine(CFG)
    except RAGError as exc:
        # 引擎起不来不能让服务变成「能连上但必错」的状态，直接启动失败
        raise RuntimeError(
            f"RAG 引擎初始化失败：{exc.message}（{exc.detail}）"
        ) from exc

    print(f"[startup] RAG 引擎就绪，向量路 "
          f"{'启用' if _ENGINE.vector_enabled else '禁用(纯BM25)'}，"
          f"耗时 {time.time() - t0:.1f}s", flush=True)
    yield
    # 线程池交给解释器退出时的 atexit 清理，避免 --reload 重启后拿到已关闭的池


app = FastAPI(title="派遣考勤异常双向确认 · 员工问答 MVP（阶段 2 归档）",
              version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ====================================================================
# 统一错误响应：所有异常都返回 code + 中文 message + detail
# ----------------------------------------------------------------
# FastAPI 默认的 422 / 404 / 405 是英文且结构不统一，前端按统一结构
# 渲染错误卡片，所以这里逐个拦截改写。
# ====================================================================
_HTTP_STATUS: dict[int, int] = {
    ErrorCode.BAD_SESSION_ID[0]: 400,
    ErrorCode.SESSION_NOT_FOUND[0]: 404,
    ErrorCode.RATE_LIMITED[0]: 429,
    ErrorCode.INDEX_NOT_READY[0]: 503,
    ErrorCode.RETRIEVAL_FAILED[0]: 503,
    ErrorCode.LLM_UNAVAILABLE[0]: 503,
    ErrorCode.LLM_TIMEOUT[0]: 504,
    ErrorCode.INTERNAL_ERROR[0]: 500,
}


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex[:12]


def _error_response(code: int, message: str, detail: str,
                    rid: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message,
                 "detail": detail, "request_id": rid},
    )


@app.exception_handler(RAGError)
async def rag_error_handler(request: Request, exc: RAGError) -> JSONResponse:
    return _error_response(exc.code, exc.message, exc.detail,
                           _request_id(request), _HTTP_STATUS.get(exc.code, 400))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request,
                                   exc: RequestValidationError) -> JSONResponse:
    return _error_response(40002, "请求参数不合法", str(exc.errors()),
                           _request_id(request), 400)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request,
                             exc: StarletteHTTPException) -> JSONResponse:
    messages = {404: "接口不存在", 405: "请求方法不支持"}
    return _error_response(
        {404: 40401, 405: 40501}.get(exc.status_code, exc.status_code),
        messages.get(exc.status_code, "请求失败"), str(exc.detail),
        _request_id(request), exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request,
                                  exc: Exception) -> JSONResponse:
    # 归档版把异常类型带进 detail，方便离线排查；生产环境应改成只回通用提示
    return _error_response(ErrorCode.INTERNAL_ERROR[0],
                           ErrorCode.INTERNAL_ERROR[1],
                           f"{type(exc).__name__}: {exc}",
                           _request_id(request), 500)


# ====================================================================
# 请求体模型 / SSE 帧 / 同步生成器转异步迭代器
# ====================================================================
class ChatRequest(BaseModel):
    session_id: str | None = Field(
        default=None, description="会话 ID；不传则服务端新建"
    )
    question: str = Field(..., description="员工问题，1-500 字")


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """拼一帧 SSE。data 后必须跟空行，否则前端解析不出帧边界。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def aiter_sync_gen(sync_gen) -> AsyncIterator[tuple[str, Any]]:
    """把同步生成器搬到事件循环：线程池生产，asyncio.Queue 跨线程搬运。

    LLM / 检索都是同步阻塞 IO，直接在 async 生成器里 for 会占住事件循环。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def producer() -> None:
        try:
            for item in sync_gen:
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except BaseException as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("__error__", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("__end__", None))

    # 只提交不等待，边生产边消费才是真流式
    loop.run_in_executor(_LLM_EXECUTOR, producer)

    while True:
        kind, payload = await queue.get()
        if kind == "__end__":
            return
        if kind == "__error__":
            raise payload
        yield kind, payload


# ====================================================================
# POST /chat —— 主接口
# ====================================================================
@app.post("/chat")
async def chat(request: Request, body: ChatRequest) -> StreamingResponse:
    """SSE 流式问答。

    事件顺序（与前端约定，不可随意变更）：
        meta → status(retrieving) → citations → status(generating)
             → delta* → done
        任意一步失败 → error（之后不再发其他事件）
    """
    rid = _request_id(request)

    # ---- 1) 参数校验：顺序即优先级 ----
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

    # ---- 2) 限流：匿名原型按客户端 IP ----
    client_key = request.client.host if request.client else "anonymous"
    if not _LIMITER.allow(client_key):
        raise RAGError(ErrorCode.RATE_LIMITED, detail="超过 20 次/分钟")

    # ---- 3) 索引就绪检查 ----
    if _ENGINE is None or _DB is None:
        raise RAGError(ErrorCode.INDEX_NOT_READY, detail="服务尚未初始化完成")

    # ---- 4) 会话落库 + 取历史（必须在插入本轮问题之前取）----
    is_new = await asyncio.to_thread(_DB.ensure_conversation, sid)
    history_rows = await asyncio.to_thread(_DB.recent_messages, sid)
    history = trim_history(history_rows, CFG.history_rounds,
                           CFG.history_char_budget)

    user_mid = "m_" + uuid.uuid4().hex[:16]
    asst_mid = "m_" + uuid.uuid4().hex[:16]
    await asyncio.to_thread(_DB.insert_message, user_mid, sid, "user", question)
    # 先插 streaming 占位，流结束后补全；客户端断开也留得下痕迹
    await asyncio.to_thread(_DB.insert_message, asst_mid, sid, "assistant", "",
                            status="streaming")

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield sse_frame("meta", {
                "session_id": sid,
                "message_id": asst_mid,
                "is_new_session": is_new,
            })
            yield sse_frame("status", {"stage": "retrieving", "text": "正在检索制度…"})

            # ---- 5) 检索 + 生成：算法层 stream_chat 直接产出 (event, data) ----
            async for kind, payload in aiter_sync_gen(
                _ENGINE.stream_chat(question, history)
            ):
                if kind == "citations":
                    yield sse_frame("citations", payload)
                    yield sse_frame("status", {
                        "stage": "generating", "text": "正在生成回答…"
                    })
                elif kind == "delta":
                    yield sse_frame("delta", payload)
                elif kind == "done":
                    await asyncio.to_thread(
                        _DB.finish_message, asst_mid,
                        payload.get("answer", ""),
                        payload.get("citations"),
                        payload.get("citation_check"),
                        payload.get("usage", {}),
                        payload.get("latency_ms", 0),
                    )
                    yield sse_frame("done", {
                        "message_id": asst_mid,
                        "usage": payload.get("usage", {}),
                        "latency_ms": payload.get("latency_ms", 0),
                        "citation_check": payload.get("citation_check"),
                    })

        except RAGError as exc:
            await asyncio.to_thread(_DB.fail_message, asst_mid, exc.code)
            yield sse_frame("error", exc.to_dict(rid))
        except Exception as exc:  # noqa: BLE001
            # 流已开始，不能改 HTTP 状态码，只能发 error 事件
            await asyncio.to_thread(
                _DB.fail_message, asst_mid, ErrorCode.INTERNAL_ERROR[0]
            )
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
            # 三个头缺一个都可能被代理/浏览器缓冲，表现为「一次性全出来」
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ====================================================================
# GET /api/sessions/{sid}/messages —— 刷新页面后恢复历史
# ====================================================================
@app.get("/api/sessions/{sid}/messages")
async def get_messages(sid: str) -> JSONResponse:
    if not SESSION_ID_RE.match(sid):
        raise RAGError(ErrorCode.BAD_SESSION_ID, detail="仅允许 8-64 位字母数字_-")
    assert _DB is not None
    if not await asyncio.to_thread(_DB.conversation_exists, sid):
        raise RAGError(ErrorCode.SESSION_NOT_FOUND, detail=f"会话 {sid} 不存在")
    rows = await asyncio.to_thread(_DB.all_messages, sid)
    return JSONResponse(content={"session_id": sid, "messages": rows})


# ====================================================================
# GET /health —— 存活 + 索引状态
# ====================================================================
@app.get("/health")
async def health() -> dict[str, Any]:
    index = getattr(_ENGINE.store, "index", None) if _ENGINE else None
    return {
        "status": "ok",
        "engine_ready": bool(_ENGINE and _ENGINE.store.ready),
        "vector_enabled": bool(_ENGINE and _ENGINE.vector_enabled),
        "chunks": getattr(index, "ntotal", 0) if index is not None else 0,
        "llm_model": CFG.llm_model or "mock",
    }


# ====================================================================
# GET / —— 单页聊天界面
# ====================================================================
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
