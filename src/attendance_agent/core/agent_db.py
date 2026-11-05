"""Agent 域数据访问层（阶段 4）。

职责边界
--------
本模块只管「Agent 编排」自己产生的数据，不碰 `ChatDB`（问答消息）与
`AdminDB`（用户/知识库/提示词）的已有表。但**共用同一个 SQLite 文件**，
这样阶段 5 做分析时，一次 JOIN 就能把「员工问了什么 → Agent 决策了什么
→ 工具改了什么业务数据 → 管理员怎么复核的」串成一条完整时间线。

为什么不把所有表塞进 AdminDB
----------------------------
AdminDB 面向低频管理操作，ChatDB 面向高频问答写，Agent 域则是
「一轮决策 → 多次工具调用」的嵌套写。三者的并发特征、生命周期、
字段演进速度都不同，混在一起会让本来清晰的模块变成上帝类。
共用一个连接风格（threading.Lock + WAL），但各自持有连接对象。

两张给阶段 5 用的分析表
---------------------
`agent_rounds`     一轮决策一行：模型说了什么、打算调哪些工具、
                   模型推理耗时、token 用量、轮次结束原因。
`agent_tool_calls` 一次工具调用一行：工具名、参数（脱敏）、耗时、
                   结果摘要、错误码、用到的能力令牌 jti。

⭐ 一轮对多次工具调用是 1:N 关系。用两张表而不是一张宽表，是因为
   阶段 5 既要算「每轮决策耗时」（看模型是否在某轮突然变慢），
   也要算「每个工具耗时」「哪个工具最常被门禁拒绝」（看流程卡点）。
   宽表会把这两个维度的统计互相污染。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any


# ====================================================================
# 表结构
# --------------------------------------------------------------------
# 说明：`audit_logs` 与 AdminDB 里的同名表字段保持一致。
# 这里再写一次 CREATE TABLE IF NOT EXISTS，是为了让 demo 用独立
# 数据库文件（agent_demo.db）时也能独立跑通，不必先启动整个服务。
# SQLite 的 IF NOT EXISTS 保证在共享 chat.db 时不会覆盖已有表。
# ====================================================================
AGENT_SCHEMA = """
-- 考勤异常原始记录（真实系统由考勤机/用工单位同步，这里做演示种子）
CREATE TABLE IF NOT EXISTS attendance_exceptions (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    work_date      TEXT NOT NULL,
    exception_type TEXT NOT NULL,        -- late / missing_punch / overtime_dispute
    punch_time     TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    rule_hit       TEXT NOT NULL,        -- 触发了哪条规则
    employee_note  TEXT DEFAULT '',
    -- ⭐ 状态机单一真源放在异常上，而不是确认单上。
    --    建单之前还没有 case，如果状态存两处就必然出现双写不一致。
    agent_state    TEXT NOT NULL DEFAULT 'IDLE',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exc_tenant_user
    ON attendance_exceptions(tenant_id, user_id, agent_state);

-- 员工上传的证明材料
CREATE TABLE IF NOT EXISTS evidence_files (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    exception_id  TEXT NOT NULL,
    file_type     TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 确认单（Agent 最多只能推到 SUBMITTED）
CREATE TABLE IF NOT EXISTS confirmation_cases (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    exception_id     TEXT NOT NULL,
    declaration      TEXT NOT NULL,       -- confirm / dispute
    evidence_ids     TEXT NOT NULL,       -- JSON 数组
    claimed_minutes  INTEGER NOT NULL,
    employee_note    TEXT DEFAULT '',
    state            TEXT NOT NULL,       -- 冗余一份，便于后台列表展示
    policy_chunk_ids TEXT DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    -- ⭐ 幂等约束：同一员工对同一异常、同一表态只能有一张单。
    --    模型重试、网络重放、用户连点都靠这条 UNIQUE 兜底，
    --    比在应用层「先查再插」可靠（先查再插有竞态窗口）。
    UNIQUE(tenant_id, exception_id, declaration)
);

-- 状态机跃迁事件（审计用：谁在什么时候把状态从 A 改成了 B）
CREATE TABLE IF NOT EXISTS case_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    case_id     TEXT DEFAULT '',
    exception_id TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

-- ⭐⭐ 阶段 5 分析主表：每轮决策一行
CREATE TABLE IF NOT EXISTS agent_rounds (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT NOT NULL,
    tenant_id          TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    round_no           INTEGER NOT NULL,
    model              TEXT DEFAULT '',
    prompt_version     INTEGER DEFAULT 0,
    policy_version     INTEGER DEFAULT 0,
    decision_type      TEXT NOT NULL,     -- tool_calls / final / limit_exceeded / error
    assistant_text     TEXT DEFAULT '',
    tool_names         TEXT DEFAULT '[]', -- JSON 数组，本轮打算调的工具
    finish_reason      TEXT DEFAULT '',
    tokens_prompt      INTEGER DEFAULT 0,
    tokens_completion  INTEGER DEFAULT 0,
    latency_ms         INTEGER DEFAULT 0, -- ⭐ 只算「模型决策」耗时，不含工具执行
    created_at         TEXT NOT NULL,
    UNIQUE(session_id, round_no)
);
CREATE INDEX IF NOT EXISTS idx_rounds_session
    ON agent_rounds(session_id, round_no);
CREATE INDEX IF NOT EXISTS idx_rounds_tenant_time
    ON agent_rounds(tenant_id, created_at);

-- ⭐⭐ 阶段 5 分析明细表：每次工具调用一行
CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    round_no        INTEGER NOT NULL,
    step_no         INTEGER NOT NULL,     -- 同一轮内的第几个工具
    tool_call_id    TEXT DEFAULT '',
    tool_name       TEXT NOT NULL,
    params_json     TEXT NOT NULL,        -- 已脱敏，见 safe_params()
    result_status   TEXT NOT NULL,        -- ok / error / gate_rejected
    result_summary  TEXT DEFAULT '',      -- 截断后的摘要，不塞整段制度原文
    error_code      TEXT DEFAULT '',
    latency_ms      INTEGER NOT NULL,
    capability_jti  TEXT DEFAULT '',      -- 消费了哪个一次性凭证
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_session
    ON agent_tool_calls(session_id, round_no, step_no);
CREATE INDEX IF NOT EXISTS idx_calls_tool
    ON agent_tool_calls(tenant_id, tool_name, result_status);

-- 一次性凭证消费记录。生产建议换 Redis SETNX；SQLite 下靠
-- PRIMARY KEY + INSERT OR IGNORE 的原子性实现等价语义。
CREATE TABLE IF NOT EXISTS capability_consumed (
    jti         TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

-- 写操作幂等结果缓存
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 与 AdminDB 同构，供 demo 独立运行时写入管理动作日志
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    actor_name  TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_type TEXT DEFAULT '',
    target_id   TEXT DEFAULT '',
    detail      TEXT,
    ip          TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
"""


# ====================================================================
# 日志脱敏与摘要
# --------------------------------------------------------------------
# 审计要的是「当时传了什么」，不是「把密钥抄一份进日志」。
# 能力令牌、密码这类字段只存指纹（sha256 前 8 位）：既能关联同一次
# 凭证在多个工具间的流转，又不会让日志库变成凭证泄露源。
# ====================================================================
_SENSITIVE_HINTS = ("token", "password", "secret", "api_key", "authorization")
_SUMMARY_KEYS = (
    "ok", "error_code", "message", "case_id", "state", "status",
    "exception_id", "count", "reason", "missing", "missing_fields",
    "token_jti", "declared", "next_action",
)


def _now_iso() -> str:
    """与 AdminDB 保持一致的时间格式（带时区偏移）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _clip(value: Any, limit: int = 160) -> Any:
    """长文本裁剪。制度原文、员工说明不全文入日志，避免日志库膨胀。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…(共{len(value)}字)"
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:20]]
    return value


def safe_params(params: dict[str, Any]) -> str:
    """把工具参数序列化成可入日志的 JSON（敏感字段替换为指纹）。"""
    cleaned: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if any(hint in key.lower() for hint in _SENSITIVE_HINTS):
            cleaned[key] = f"<redacted:{_fingerprint(str(value))}>"
        else:
            cleaned[key] = _clip(value)
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)


def summarize_result(result: dict[str, Any], limit: int = 240) -> str:
    """从工具返回值里挑关键字段做摘要，而不是把整个 dict 塞进日志。

    ⭐ 为什么要摘要：`lookup_policy` 会返回整段制度原文，全量落库
       一轮就能写进几 KB；阶段 5 分析时却只需要知道「命中几个片段」。
       摘要既控制体积，也避免日志表变成第二个知识库。
    """
    if not isinstance(result, dict):
        return _clip(str(result), limit)
    picked = {k: _clip(result[k], 120) for k in _SUMMARY_KEYS if k in result}
    # 列表类结果额外记条数，便于阶段 5 统计「每轮拉了多少候选」
    for key, value in result.items():
        if isinstance(value, list) and key not in picked:
            picked[key] = f"<{len(value)} 项>"
    text = json.dumps(picked, ensure_ascii=False, sort_keys=True, default=str)
    return text[:limit] + ("…" if len(text) > limit else "")


class AgentDB:
    """Agent 域数据访问对象。线程安全（threading.Lock + WAL）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(AGENT_SCHEMA)
            self._conn.commit()

    # ---- 内部统一执行 ----
    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _read_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # =================================================================
    # 业务数据：考勤异常 / 证据 / 确认单 / 状态机
    # =================================================================
    def list_exceptions(self, tenant_id: str, user_id: str,
                        status: str = "pending") -> list[dict[str, Any]]:
        """只查本人、本租户的异常。

        ⭐ user_id 强制来自 JWT 解析出的 CurrentUser，工具参数里没有
           user_id 这个字段，模型无法通过改参数去查别人的考勤记录。
        """
        sql = ("SELECT id, work_date, exception_type, punch_time, "
               "scheduled_time, rule_hit, agent_state "
               "FROM attendance_exceptions WHERE tenant_id=? AND user_id=?")
        params: list[Any] = [tenant_id, user_id]
        if status == "pending":
            # IDLE / ASK_FOR_MORE / EVIDENCE_CHECKED 都算「还没走完」
            sql += " AND agent_state NOT IN ('SUBMITTED','CLOSED','ESCALATED')"
        sql += " ORDER BY work_date DESC"
        return self._read_all(sql, tuple(params))

    def get_exception(self, tenant_id: str, user_id: str,
                      exception_id: str) -> dict[str, Any] | None:
        """带租户 + 本人双重条件的单条查询。

        查不到时返回 None，由工具层统一转成 404 中文提示——
        ⚠️ 不要返回「该异常不存在于本租户」这类区分性文案，
           否则攻击者可以借此枚举其他租户的异常编号是否存在。
        """
        return self._read_one(
            "SELECT * FROM attendance_exceptions "
            "WHERE tenant_id=? AND user_id=? AND id=?",
            (tenant_id, user_id, exception_id),
        )

    def get_state(self, tenant_id: str, user_id: str,
                  exception_id: str) -> str:
        row = self.get_exception(tenant_id, user_id, exception_id)
        return row["agent_state"] if row else "NOT_FOUND"

    def set_state(self, tenant_id: str, user_id: str, exception_id: str,
                  new_state: str, action: str, actor: str,
                  case_id: str = "", detail: dict | None = None) -> str:
        """状态跃迁 + 写事件，放在同一个锁内保证原子。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT agent_state FROM attendance_exceptions "
                "WHERE tenant_id=? AND user_id=? AND id=?",
                (tenant_id, user_id, exception_id),
            ).fetchone()
            old = row["agent_state"] if row else "NOT_FOUND"
            self._conn.execute(
                "UPDATE attendance_exceptions SET agent_state=? "
                "WHERE tenant_id=? AND user_id=? AND id=?",
                (new_state, tenant_id, user_id, exception_id),
            )
            if case_id:
                self._conn.execute(
                    "UPDATE confirmation_cases SET state=?, updated_at=? "
                    "WHERE tenant_id=? AND id=?",
                    (new_state, _now_iso(), tenant_id, case_id),
                )
            self._conn.execute(
                "INSERT INTO case_events (tenant_id, case_id, exception_id, "
                "from_state, to_state, action, actor, detail, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, case_id, exception_id, old, new_state, action,
                 actor, json.dumps(detail or {}, ensure_ascii=False), _now_iso()),
            )
            self._conn.commit()
        return old

    def get_evidence(self, tenant_id: str, user_id: str,
                     evidence_id: str) -> dict[str, Any] | None:
        return self._read_one(
            "SELECT * FROM evidence_files "
            "WHERE tenant_id=? AND user_id=? AND id=?",
            (tenant_id, user_id, evidence_id),
        )

    def create_case(self, tenant_id: str, user_id: str, case_id: str,
                    exception_id: str, declaration: str,
                    evidence_ids: list[str], claimed_minutes: int,
                    employee_note: str) -> dict[str, Any]:
        """创建确认单草稿。UNIQUE 约束兜底并发重复建单。"""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO confirmation_cases "
                "(id, tenant_id, user_id, exception_id, declaration, "
                " evidence_ids, claimed_minutes, employee_note, state, "
                " policy_chunk_ids, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, tenant_id, user_id, exception_id, declaration,
                 json.dumps(evidence_ids, ensure_ascii=False),
                 claimed_minutes, employee_note, "DRAFT_CREATED", "[]",
                 now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM confirmation_cases "
                "WHERE tenant_id=? AND exception_id=? AND declaration=?",
                (tenant_id, exception_id, declaration),
            ).fetchone()
        return dict(row) if row else {}

    def get_case(self, tenant_id: str, user_id: str,
                 case_id: str) -> dict[str, Any] | None:
        return self._read_one(
            "SELECT * FROM confirmation_cases "
            "WHERE tenant_id=? AND user_id=? AND id=?",
            (tenant_id, user_id, case_id),
        )

    # =================================================================
    # 能力令牌消费 & 幂等
    # =================================================================
    def consume_jti(self, jti: str, tenant_id: str, user_id: str,
                    action: str) -> bool:
        """原子消费一次性凭证，返回 True 表示「这个 jti 之前没用过」。

        ⭐ 必须用单条 INSERT OR IGNORE 判断，不能先 SELECT 再 INSERT：
           两个并发请求会同时 SELECT 到「不存在」，然后都插入成功。
       rowcount 是 SQLite 给的原子结果，等价于 Redis 的 SETNX。
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO capability_consumed "
                "(jti, tenant_id, user_id, action, consumed_at) "
                "VALUES (?,?,?,?,?)",
                (jti, tenant_id, user_id, action, _now_iso()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def get_idempotency(self, key: str, tenant_id: str) -> dict | None:
        row = self._read_one(
            "SELECT result_json FROM idempotency_keys "
            "WHERE key=? AND tenant_id=?", (key, tenant_id),
        )
        return json.loads(row["result_json"]) if row else None

    def put_idempotency(self, key: str, tenant_id: str,
                        result: dict[str, Any]) -> None:
        self._write(
            "INSERT OR IGNORE INTO idempotency_keys "
            "(key, tenant_id, result_json, created_at) VALUES (?,?,?,?)",
            (key, tenant_id,
             json.dumps(result, ensure_ascii=False, default=str), _now_iso()),
        )

    # =================================================================
    # ⭐ 阶段 5 分析：决策日志写入
    # =================================================================
    def log_round(self, session_id: str, tenant_id: str, user_id: str,
                  round_no: int, *, decision_type: str,
                  assistant_text: str = "", tool_names: list[str] | None = None,
                  model: str = "", prompt_version: int = 0,
                  policy_version: int = 0, finish_reason: str = "",
                  tokens_prompt: int = 0, tokens_completion: int = 0,
                  latency_ms: int = 0) -> None:
        """记录一轮模型决策。

        注意 latency_ms 只统计「模型决策」耗时，工具耗时在
        agent_tool_calls 里单独记。两者分开，阶段 5 才能回答
        「这一轮慢，是模型慢还是某个工具慢」。
        """
        self._write(
            "INSERT OR REPLACE INTO agent_rounds "
            "(session_id, tenant_id, user_id, round_no, model, "
            " prompt_version, policy_version, decision_type, "
            " assistant_text, tool_names, finish_reason, "
            " tokens_prompt, tokens_completion, latency_ms, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, tenant_id, user_id, round_no, model,
             prompt_version, policy_version, decision_type,
             _clip(assistant_text, 500),
             json.dumps(tool_names or [], ensure_ascii=False),
             finish_reason, tokens_prompt, tokens_completion,
             latency_ms, _now_iso()),
        )

    def log_tool_call(self, session_id: str, tenant_id: str,
                      round_no: int, step_no: int, *,
                      tool_name: str, params: dict[str, Any],
                      result: dict[str, Any], result_status: str,
                      latency_ms: int, tool_call_id: str = "",
                      error_code: str = "", capability_jti: str = "") -> None:
        """记录一次工具调用（工具名 / 参数 / 耗时 / 结果摘要）。

        ⭐ 日志写入放在 Harness 的 finally 语义位置：哪怕工具抛异常，
           也必须留下一条 error 记录。否则阶段 5 看到的成功率会虚高——
           失败的调用凭空消失了，这比记错更危险。
        """
        self._write(
            "INSERT INTO agent_tool_calls "
            "(session_id, tenant_id, round_no, step_no, tool_call_id, "
            " tool_name, params_json, result_status, result_summary, "
            " error_code, latency_ms, capability_jti, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, tenant_id, round_no, step_no, tool_call_id,
             tool_name, safe_params(params), result_status,
             summarize_result(result), error_code, latency_ms,
             capability_jti, _now_iso()),
        )

    def log_action(self, tenant_id: str, actor_id: str, actor_name: str,
                   action: str, target_type: str = "", target_id: str = "",
                   detail: dict | None = None) -> None:
        """写管理动作日志（与 AdminDB.audit_logs 同构）。

        门禁拒绝（gate_rejected）必须落这里：它是观测「提示词是否
        被注入攻破」的探针，只拒绝不记录等于把攻击痕迹抹掉了。
        """
        self._write(
            "INSERT INTO audit_logs (tenant_id, actor_id, actor_name, "
            "action, target_type, target_id, detail, ip, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (tenant_id, actor_id, actor_name, action, target_type, target_id,
             json.dumps(detail, ensure_ascii=False, default=str)
             if detail else None, "", _now_iso()),
        )

    # ---- 阶段 5 分析查询 ----
    def list_rounds(self, session_id: str) -> list[dict[str, Any]]:
        return self._read_all(
            "SELECT * FROM agent_rounds WHERE session_id=? ORDER BY round_no",
            (session_id,),
        )

    def list_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        return self._read_all(
            "SELECT * FROM agent_tool_calls WHERE session_id=? "
            "ORDER BY round_no, step_no", (session_id,),
        )

    def analysis_stats(self, session_id: str) -> dict[str, Any]:
        """阶段 5 里最常用的四个指标，先内置好，省得每次现写 SQL。"""
        rounds = self.list_rounds(session_id)
        calls = self.list_tool_calls(session_id)
        by_tool: dict[str, int] = {}
        for c in calls:
            by_tool[c["tool_name"]] = by_tool.get(c["tool_name"], 0) + 1
        return {
            "rounds_used": len(rounds),
            "tool_calls": len(calls),
            "model_latency_ms": sum(r["latency_ms"] for r in rounds),
            "tool_latency_ms": sum(c["latency_ms"] for c in calls),
            "gate_rejections": sum(
                1 for c in calls if c["result_status"] == "gate_rejected"),
            "errors": sum(1 for c in calls if c["result_status"] == "error"),
            "by_tool": by_tool,
            "tokens_prompt": sum(r["tokens_prompt"] for r in rounds),
            "tokens_completion": sum(r["tokens_completion"] for r in rounds),
        }

    # =================================================================
    # 演示种子数据（幂等，可重复执行）
    # =================================================================
    def seed_demo(self, tenant_id: str, user_id: str) -> None:
        """插入三条带 DEMO 前缀的考勤异常与两份证明材料。

        用固定 ID 而不是随机 ID，是为了让三条轨迹脚本可以硬编码
        exception_id——轨迹示例必须可复现，随机 ID 会让文档里的
        预期轨迹每次跑都对不上。
        """
        now = _now_iso()
        rows = [
            ("DEMO-EXC-0031", "2026-11-03", "late", "09:42", "09:00",
             "迟到超过 30 分钟，需员工确认或提供补卡证明", "", "IDLE"),
            ("DEMO-EXC-0032", "2026-11-04", "missing_punch", "--:--", "18:00",
             "下班缺卡，需确认实际离岗时间", "", "IDLE"),
            ("DEMO-EXC-0033", "2026-11-05", "overtime_dispute", "20:15", "18:00",
             "加班时长申报与门禁记录不一致", "当天配合客户上线，申请按 2 小时计", "IDLE"),
            # 专供 verify_gate_hard_constraints() 的干净异常：
            # 三条轨迹会把上面三条推到各自终态，门禁单测需要一条 IDLE 的
            ("DEMO-EXC-0040", "2026-11-06", "late", "09:35", "09:00",
             "门禁单测专用记录", "", "IDLE"),
        ]
        with self._lock:
            for (eid, date, etype, punch, sched, rule, note, state) in rows:
                self._conn.execute(
                    "INSERT OR IGNORE INTO attendance_exceptions "
                    "(id, tenant_id, user_id, work_date, exception_type, "
                    " punch_time, scheduled_time, rule_hit, employee_note, "
                    " agent_state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, tenant_id, user_id, date, etype, punch, sched,
                     rule, note, state, now),
                )
            for ev_id, exc_id, ftype, fname in (
                ("DEMO-EV-01", "DEMO-EXC-0031", "补卡审批单", "补卡审批_1103.pdf"),
                ("DEMO-EV-02", "DEMO-EXC-0031", "客户拜访记录", "拜访记录_1103.png"),
            ):
                self._conn.execute(
                    "INSERT OR IGNORE INTO evidence_files "
                    "(id, tenant_id, user_id, exception_id, file_type, "
                    " filename, sha256, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ev_id, tenant_id, user_id, exc_id, ftype, fname,
                     hashlib.sha256(fname.encode()).hexdigest(), now),
                )
            self._conn.commit()
