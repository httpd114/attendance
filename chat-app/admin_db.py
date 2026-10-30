#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin_db.py —— 管理域数据层（阶段 3 · 第 1 批）
====================================================================
本模块负责管理后台的全部持久化：用户、租户、知识库文档与切片、
提示词模板、审计标记、管理动作日志。

⭐ 租户隔离的实现方式（用户拍板 Q3 = 租户隔离）：

    本模块所有对外方法，**第一个位置参数固定是 tenant_id**，
    并且每条 SQL 的 WHERE 条件都以 tenant_id 打头。

    这么做是为了让"忘记加租户条件"变成显眼的错误：
    方法签名强制你先想清楚租户，SQL 里漏了条件一眼能看出来。
    对比之下，如果 tenant_id 藏在 self 里，漏写 WHERE 条件
    不会有任何编译期提示——那才是真正的数据泄露来源。

    对外不暴露任何"不带 tenant_id 的查询方法"。

线程模型：沿用阶段 2 的做法——sqlite3 连接开 check_same_thread=False，
所有访问走一把 threading.Lock。管理后台是低并发场景，
这个方案足够，且与既有代码风格一致。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from auth import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    AuthError,
    ERR_ACCOUNT_DISABLED,
    ERR_INVALID_CREDENTIALS,
    ERR_OLD_PASSWORD_WRONG,
    ERR_USERNAME_TAKEN,
    ERR_USER_NOT_FOUND,
    check_password_strength,
    hash_password,
    verify_password,
)

# ====================================================================
# 表结构
# ====================================================================
ADMIN_SCHEMA = """
-- ---------- 租户 ----------
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,          -- t_ + uuid
    name        TEXT NOT NULL,
    code        TEXT NOT NULL UNIQUE,      -- 短代码，登录时选租户用
    status      TEXT NOT NULL DEFAULT 'active',  -- active | disabled
    created_at  TEXT NOT NULL
);

-- ---------- 用户 ----------
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,        -- u_ + uuid
    tenant_id     TEXT NOT NULL
                  REFERENCES tenants(id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL,           -- employee | admin
    employee_no   TEXT,                    -- 工号，审计按人筛选用
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT,                    -- 建号人（管理员建号时记录）
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    UNIQUE (tenant_id, username)          -- 用户名在租户内唯一
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id, role);

-- ---------- 知识库文档 ----------
CREATE TABLE IF NOT EXISTS kb_documents (
    id          TEXT PRIMARY KEY,          -- doc_ + uuid
    tenant_id   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    file_type   TEXT NOT NULL,             -- md | pdf
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'processing',  -- processing | ready | failed
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error_msg   TEXT,                      -- 失败原因
    uploaded_by TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kbdocs_tenant ON kb_documents(tenant_id, created_at);

-- ---------- 知识库切片（含向量） ----------
CREATE TABLE IF NOT EXISTS kb_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    doc_id      TEXT NOT NULL
                REFERENCES kb_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    section     TEXT NOT NULL DEFAULT '',  -- 章节标题（引用区显示）
    embedding   BLOB,                     -- float32 向量，4096 字节/块
    embed_model TEXT NOT NULL DEFAULT '', -- 模型标识，换模型必须区分
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kbchunks_doc ON kb_chunks(doc_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_kbchunks_tenant ON kb_chunks(tenant_id);

-- ---------- 提示词模板 ----------
CREATE TABLE IF NOT EXISTS prompt_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    body_text   TEXT NOT NULL,             -- 管理员可编辑部分
    version     INTEGER NOT NULL DEFAULT 1,
    is_active   INTEGER NOT NULL DEFAULT 0,
    updated_by  TEXT,
    updated_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompts_tenant ON prompt_templates(tenant_id, is_active);

-- ---------- 审计标记（疑似违规） ----------
CREATE TABLE IF NOT EXISTS audit_flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'suspected',  -- suspected | confirmed | rejected
    source      TEXT NOT NULL DEFAULT 'rule',       -- rule | manual
    reason      TEXT NOT NULL DEFAULT '',
    hit_rule    TEXT,                     -- 命中的规则标识
    flagged_by  TEXT,                     -- 人工复核人
    created_at  TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_flags_msg ON audit_flags(message_id);
CREATE INDEX IF NOT EXISTS idx_flags_tenant ON audit_flags(tenant_id, status);

-- ---------- 管理动作日志（谁改了什么） ----------
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    actor_id    TEXT,
    actor_name  TEXT,
    action      TEXT NOT NULL,             -- login / kb_upload / kb_delete / prompt_update ...
    target_type TEXT,
    target_id   TEXT,
    detail      TEXT,                     -- JSON
    ip          TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auditlogs_tenant ON audit_logs(tenant_id, created_at);
"""


def _now_iso() -> str:
    """统一时间格式。沿用阶段 2 的实现，带时区偏移。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class AdminDB:
    """管理域数据访问对象。

    与阶段 2 的 ChatDB 并列，共享同一个 SQLite 文件。
    为什么不合并成一个类：ChatDB 面向高频问答写，AdminDB 面向
    低频管理操作，两者的生命周期和并发特征不同。共用一个连接，
    但职责分开。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(ADMIN_SCHEMA)
            self._conn.commit()

    # ---- 内部：统一执行 ----
    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _read_one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    # =================================================================
    # 租户
    # =================================================================
    def ensure_tenant(self, code: str, name: str) -> str:
        """确保租户存在，返回 tenant_id。幂等。"""
        existing = self._read_one("SELECT id FROM tenants WHERE code = ?", (code,))
        if existing:
            return existing["id"]
        tid = "t_" + uuid.uuid4().hex[:16]
        self._write(
            "INSERT INTO tenants (id, name, code, status, created_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (tid, name, code, _now_iso()),
        )
        return tid

    def list_tenants(self) -> list[dict[str, Any]]:
        return self._read_all(
            "SELECT id, name, code, status, created_at FROM tenants ORDER BY created_at"
        )

    def get_tenant_id(self, code: str) -> Optional[str]:
        """按 code 查真实 tenant_id。

        ⭐ 必须区分 code 和 id：
        code 是人写的短标识（"default"），id 是系统生成的（"t_xxx"）。
        所有业务表存的是 **id**。如果哪一处把 code 当 id 用，
        数据会写进一个"看起来正常但永远查不到"的孤儿分区——
        页面上文档数是 0，但库里明明有数据，极难排查。
        """
        row = self._read_one("SELECT id FROM tenants WHERE code = ?", (code,))
        return row["id"] if row else None

    # =================================================================
    # 用户
    # =================================================================
    def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        """按 ID 查用户。鉴权时用（不按租户过滤，因为要先确认身份）。"""
        return self._read_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def find_user_by_username(self, tenant_code: str,
                              username: str) -> Optional[dict[str, Any]]:
        """登录用：按租户代码 + 用户名查找。

        用户名在租户内唯一（表上有 UNIQUE 约束），
        所以必须带 tenant_id 查，否则同名用户会串。
        """
        return self._read_one(
            "SELECT u.* FROM users u JOIN tenants t ON u.tenant_id = t.id "
            "WHERE t.code = ? AND u.username = ?",
            (tenant_code, username),
        )

    def list_users(self, tenant_id: str,
                   role: str = "") -> list[dict[str, Any]]:
        """列出本租户用户。管理员建号页用。"""
        sql = ("SELECT id, username, display_name, role, employee_no, "
               "is_active, created_at, last_login_at FROM users WHERE tenant_id = ?")
        params: tuple = (tenant_id,)
        if role:
            sql += " AND role = ?"
            params = (tenant_id, role)
        sql += " ORDER BY role DESC, created_at"
        return self._read_all(sql, params)

    def create_user(self, tenant_id: str, username: str, password: str,
                    display_name: str = "", role: str = ROLE_EMPLOYEE,
                    employee_no: str = "", created_by: str = "") -> dict[str, Any]:
        """管理员建号（用户拍板 Q2 = 管理员在后台建号，不开放注册）。"""
        check_password_strength(password)
        if role not in (ROLE_EMPLOYEE, ROLE_ADMIN):
            raise AuthError((40010, "角色不合法"), detail=f"role={role}")

        # 用户名在租户内唯一，靠 UNIQUE 约束兜底，但先查一次给出友好中文提示
        dup = self._read_one(
            "SELECT id FROM users WHERE tenant_id = ? AND username = ?",
            (tenant_id, username),
        )
        if dup:
            raise AuthError(ERR_USERNAME_TAKEN,
                            detail=f"租户内已存在用户 {username}")

        uid = "u_" + uuid.uuid4().hex[:16]
        self._write(
            "INSERT INTO users (id, tenant_id, username, password_hash, "
            "display_name, role, employee_no, is_active, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (uid, tenant_id, username, hash_password(password),
             display_name or username, role, employee_no, created_by, _now_iso()),
        )
        user = self.get_user(uid)
        assert user is not None
        return user

    def update_user(self, tenant_id: str, user_id: str,
                    **fields: Any) -> None:
        """改用户（停用 / 改角色 / 改显示名）。"""
        allowed = {"display_name", "role", "employee_no", "is_active"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return
        params.extend([user_id, tenant_id])
        self._write(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ? AND tenant_id = ?",
            tuple(params),
        )

    def set_password(self, tenant_id: str, user_id: str,
                     old_password: str, new_password: str) -> None:
        """改密码。必须校验原密码。"""
        user = self._read_one(
            "SELECT * FROM users WHERE id = ? AND tenant_id = ?",
            (user_id, tenant_id),
        )
        if user is None:
            raise AuthError(ERR_USER_NOT_FOUND)
        if not verify_password(old_password, user["password_hash"]):
            raise AuthError(ERR_OLD_PASSWORD_WRONG)
        check_password_strength(new_password)
        self._write(
            "UPDATE users SET password_hash = ? WHERE id = ? AND tenant_id = ?",
            (hash_password(new_password), user_id, tenant_id),
        )

    def reset_password(self, tenant_id: str, user_id: str,
                       new_password: str) -> None:
        """管理员重置密码（不需要原密码）。"""
        check_password_strength(new_password)
        self._write(
            "UPDATE users SET password_hash = ? WHERE id = ? AND tenant_id = ?",
            (hash_password(new_password), user_id, tenant_id),
        )

    def record_login(self, user_id: str) -> None:
        self._write("UPDATE users SET last_login_at = ? WHERE id = ?",
                    (_now_iso(), user_id))

    def authenticate(self, tenant_code: str, username: str,
                     password: str) -> dict[str, Any]:
        """账密登录校验。

        ⚠️ 安全要点：**用户不存在和密码错误返回同一个错误码**。
        如果分开提示，攻击者可以用错误码差异枚举出哪些用户名存在。
        """
        user = self.find_user_by_username(tenant_code, username)
        if user is None:
            # 走一次假哈希校验，让响应时间与"用户存在但密码错"接近，
            # 避免通过耗时差异枚举用户名
            hash_password(password)
            raise AuthError(ERR_INVALID_CREDENTIALS)
        if not verify_password(password, user["password_hash"]):
            raise AuthError(ERR_INVALID_CREDENTIALS)
        if not user.get("is_active", 1):
            raise AuthError(ERR_ACCOUNT_DISABLED)
        return user

    # =================================================================
    # 知识库
    # =================================================================
    def create_document(self, tenant_id: str, filename: str, file_type: str,
                        size_bytes: int, uploaded_by: str) -> str:
        doc_id = "doc_" + uuid.uuid4().hex[:16]
        self._write(
            "INSERT INTO kb_documents (id, tenant_id, filename, file_type, "
            "size_bytes, status, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'processing', ?, ?)",
            (doc_id, tenant_id, filename, file_type, size_bytes,
             uploaded_by, _now_iso()),
        )
        return doc_id

    def finish_document(self, tenant_id: str, doc_id: str, status: str,
                        chunk_count: int = 0, error_msg: str = "") -> None:
        self._write(
            "UPDATE kb_documents SET status = ?, chunk_count = ?, error_msg = ? "
            "WHERE id = ? AND tenant_id = ?",
            (status, chunk_count, error_msg or None, doc_id, tenant_id),
        )

    def list_documents(self, tenant_id: str) -> list[dict[str, Any]]:
        return self._read_all(
            "SELECT id, filename, file_type, size_bytes, status, chunk_count, "
            "error_msg, uploaded_by, created_at FROM kb_documents "
            "WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        )

    def get_document(self, tenant_id: str, doc_id: str) -> Optional[dict[str, Any]]:
        return self._read_one(
            "SELECT * FROM kb_documents WHERE id = ? AND tenant_id = ?",
            (doc_id, tenant_id),
        )

    def delete_document(self, tenant_id: str, doc_id: str) -> bool:
        """删除文档及其切片。

        CASCADE 会连带删掉 kb_chunks，但外键约束只在 PRAGMA
        foreign_keys=ON 时生效——连接初始化时已经打开了。
        """
        doc = self.get_document(tenant_id, doc_id)
        if doc is None:
            return False
        self._write("DELETE FROM kb_documents WHERE id = ? AND tenant_id = ?",
                    (doc_id, tenant_id))
        return True

    def insert_chunks(self, tenant_id: str, doc_id: str,
                      chunks: list[dict[str, Any]]) -> None:
        """批量写入切片（含向量 BLOB）。

        embed_model 一起存：换 embedding 模型后旧向量维度对不上，
        这是阶段 1 踩过的静默错误（索引按模型分文件）。
        """
        now = _now_iso()
        rows = [
            (tenant_id, doc_id, i, c["content"], c.get("section", ""),
             c.get("embedding"), c.get("embed_model", ""), now)
            for i, c in enumerate(chunks)
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO kb_chunks (tenant_id, doc_id, chunk_index, content, "
                "section, embedding, embed_model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def list_chunks(self, tenant_id: str, doc_id: str,
                    limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """切片预览（分页）。

        ⚠️ 这里**返回完整 content**——管理页要显示切片原文，
        这是管理员检查切片质量的唯一途径。检索结果里只给片段，
        两者用途不同，不能共用同一份数据。
        """
        rows = self._read_all(
            "SELECT id, chunk_index, content, section, embed_model "
            "FROM kb_chunks WHERE doc_id = ? AND tenant_id = ? "
            "ORDER BY chunk_index LIMIT ? OFFSET ?",
            (doc_id, tenant_id, limit, offset),
        )
        for r in rows:
            r["has_embedding"] = bool(r.get("embed_model"))
            r["content_len"] = len(r.get("content") or "")
        return rows

    def count_chunks(self, tenant_id: str, doc_id: str) -> int:
        row = self._read_one(
            "SELECT COUNT(*) AS c FROM kb_chunks WHERE doc_id = ? AND tenant_id = ?",
            (doc_id, tenant_id),
        )
        return row["c"] if row else 0

    def all_chunks_with_vectors(self, tenant_id: str
                                ) -> list[dict[str, Any]]:
        """取出本租户全部切片及向量，用于重建索引。

        只取本租户的——这是租户隔离在检索侧的落点：
        即使 A 租户的切片还在库里，检索也碰不到。

        ⭐ 多文档后必须 JOIN 出 filename：引用区要显示"哪份制度"，
        少了这一列，管理员上传 3 份制度后，引用全显示同一个名字。
        """
        rows = self._read_all(
            "SELECT c.id, c.doc_id, c.chunk_index, c.content, c.section, "
            "       c.embedding, c.embed_model, d.filename "
            "FROM kb_chunks c "
            "LEFT JOIN kb_documents d ON d.id = c.doc_id "
            "WHERE c.tenant_id = ? "
            "ORDER BY c.doc_id, c.chunk_index",
            (tenant_id,),
        )
        return rows

    def clear_chunks(self, tenant_id: str, doc_id: str = "") -> int:
        """清空切片。

        doc_id 为空 → 清空本租户全部（重建索引前的全量重灌）；
        指定 doc_id → 只清空某份文档（上传失败回滚时用）。
        """
        if doc_id:
            row = self._read_one(
                "SELECT COUNT(*) AS c FROM kb_chunks "
                "WHERE tenant_id = ? AND doc_id = ?",
                (tenant_id, doc_id),
            )
            self._write(
                "DELETE FROM kb_chunks WHERE tenant_id = ? AND doc_id = ?",
                (tenant_id, doc_id),
            )
        else:
            row = self._read_one(
                "SELECT COUNT(*) AS c FROM kb_chunks WHERE tenant_id = ?",
                (tenant_id,),
            )
            self._write("DELETE FROM kb_chunks WHERE tenant_id = ?", (tenant_id,))
        return row["c"] if row else 0

    # =================================================================
    # 提示词模板
    # =================================================================
    def get_active_prompt(self, tenant_id: str) -> Optional[dict[str, Any]]:
        return self._read_one(
            "SELECT * FROM prompt_templates WHERE tenant_id = ? AND is_active = 1 "
            "ORDER BY version DESC LIMIT 1",
            (tenant_id,),
        )

    def save_prompt(self, tenant_id: str, name: str, body_text: str,
                    updated_by: str) -> dict[str, Any]:
        """保存提示词模板并立即生效。

        "立即生效"的实现：旧版本 is_active 置 0，新版本置 1。
        不做物理删除——历史版本要能查、能回滚（审计要求）。
        """
        prev = self.get_active_prompt(tenant_id)
        version = (prev["version"] + 1) if prev else 1
        now = _now_iso()
        self._write(
            "UPDATE prompt_templates SET is_active = 0 WHERE tenant_id = ?",
            (tenant_id,),
        )
        self._write(
            "INSERT INTO prompt_templates (tenant_id, name, body_text, version, "
            "is_active, updated_by, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (tenant_id, name, body_text, version, updated_by, now, now),
        )
        result = self.get_active_prompt(tenant_id)
        assert result is not None
        return result

    def list_prompt_history(self, tenant_id: str, limit: int = 20
                            ) -> list[dict[str, Any]]:
        return self._read_all(
            "SELECT id, name, body_text, version, is_active, updated_by, "
            "updated_at FROM prompt_templates WHERE tenant_id = ? "
            "ORDER BY version DESC LIMIT ?",
            (tenant_id, limit),
        )

    def rollback_prompt(self, tenant_id: str, version: int,
                        updated_by: str) -> dict[str, Any]:
        """回滚到指定版本：把该版本内容作为新版本写入并生效。

        不直接改历史行的 is_active——那样会让"版本号"和"生效顺序"
        脱节，审计时说不清哪一版真正上线过。
        """
        target = self._read_one(
            "SELECT * FROM prompt_templates WHERE tenant_id = ? AND version = ?",
            (tenant_id, version),
        )
        if target is None:
            raise AuthError((40410, "该模板版本不存在"),
                            detail=f"version={version}")
        return self.save_prompt(tenant_id, target["name"], target["body_text"],
                                updated_by)

    # =================================================================
    # 审计标记
    # =================================================================
    def add_flag(self, tenant_id: str, message_id: str, status: str,
                 source: str, reason: str, hit_rule: str = "",
                 flagged_by: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO audit_flags (tenant_id, message_id, status, source, "
            "reason, hit_rule, flagged_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, message_id, status, source, reason, hit_rule,
             flagged_by, _now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def update_flag(self, tenant_id: str, flag_id: int, status: str,
                    reason: str = "", flagged_by: str = "") -> None:
        self._write(
            "UPDATE audit_flags SET status = ?, reason = ?, flagged_by = ?, "
            "reviewed_at = ? WHERE id = ? AND tenant_id = ?",
            (status, reason, flagged_by, _now_iso(), flag_id, tenant_id),
        )

    def list_flags(self, tenant_id: str, status: str = "",
                   limit: int = 200) -> list[dict[str, Any]]:
        sql = ("SELECT f.id, f.message_id, f.status, f.source, f.reason, "
               "f.hit_rule, f.flagged_by, f.created_at, f.reviewed_at, "
               "m.role, m.content, m.conversation_id "
               "FROM audit_flags f LEFT JOIN messages m ON f.message_id = m.id "
               "WHERE f.tenant_id = ?")
        params: tuple = (tenant_id,)
        if status:
            sql += " AND f.status = ?"
            params = (tenant_id, status)
        sql += " ORDER BY f.created_at DESC LIMIT ?"
        params = params + (limit,)
        return self._read_all(sql, params)

    def existing_flag(self, tenant_id: str, message_id: str
                      ) -> Optional[dict[str, Any]]:
        """查某条消息是否已被标记（防止重复打标）。"""
        return self._read_one(
            "SELECT * FROM audit_flags WHERE tenant_id = ? AND message_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tenant_id, message_id),
        )

    # =================================================================
    # 审计统计
    # =================================================================
    def audit_stats(self, tenant_id: str) -> dict[str, Any]:
        """后台首页的统计卡片。

        ⭐ 所有统计都带 tenant_id 条件——统计也是数据，
        跨租户汇总会让 A 的管理员看到 B 的业务量。
        """
        suspected = self._read_one(
            "SELECT COUNT(*) AS c FROM audit_flags "
            "WHERE tenant_id = ? AND status = 'suspected'",
            (tenant_id,),
        )
        flagged_total = self._read_one(
            "SELECT COUNT(*) AS c FROM audit_flags WHERE tenant_id = ?",
            (tenant_id,),
        )
        total_conv = self._read_one(
            "SELECT COUNT(*) AS c FROM conversations WHERE tenant_id = ?",
            (tenant_id,),
        )
        today = self._read_one(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE tenant_id = ? AND role = 'user' "
            "AND created_at >= date('now', 'localtime')",
            (tenant_id,),
        )
        return {
            "suspected_count": suspected["c"] if suspected else 0,
            "flagged_total": flagged_total["c"] if flagged_total else 0,
            "total_conversations": total_conv["c"] if total_conv else 0,
            "today_count": today["c"] if today else 0,
        }

    # =================================================================
    # 管理动作日志
    # =================================================================
    def log_action(self, tenant_id: str, actor_id: str, actor_name: str,
                   action: str, target_type: str = "", target_id: str = "",
                   detail: dict[str, Any] | None = None,
                   ip: str = "") -> None:
        self._write(
            "INSERT INTO audit_logs (tenant_id, actor_id, actor_name, action, "
            "target_type, target_id, detail, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, actor_id, actor_name, action, target_type, target_id,
             json.dumps(detail, ensure_ascii=False) if detail else None,
             ip, _now_iso()),
        )

    def list_action_logs(self, tenant_id: str, limit: int = 100
                         ) -> list[dict[str, Any]]:
        rows = self._read_all(
            "SELECT id, actor_name, action, target_type, target_id, detail, "
            "ip, created_at FROM audit_logs WHERE tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        )
        for r in rows:
            if r.get("detail"):
                try:
                    r["detail"] = json.loads(r["detail"])
                except json.JSONDecodeError:
                    pass
        return rows
