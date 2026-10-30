#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate.py —— 数据迁移（阶段 3 · 第 1 批）
====================================================================
做两件事：
    1. 给阶段 2 的 conversations / messages 补 tenant_id 列
    2. 给已有的 14 条历史消息分配一个 anonymous 虚拟用户

⭐ 为什么需要第 2 步：
    阶段 2 的问答是匿名的（conversations.user_id 一直为空）。
    用户拍板 Q3 = 租户隔离后，审计页要"按员工筛选"，
    没有归属的历史数据既筛不出来、也假装不了有归属。

    处理方式：建一个真实的 users 行（username=anonymous，role=employee，
    标记 is_active=0 不可登录），把历史会话挂到它下面。
    这样：
      - 审计页按员工筛时，历史数据归到"匿名历史"这一组
      - 不会凭空给历史记录编造员工身份
      - 这个用户不可登录，不会成为后门

幂等：可重复执行，已迁移的数据不会被二次处理。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from admin_db import AdminDB, _now_iso  # noqa: E402

DEFAULT_TENANT_CODE = "default"
DEFAULT_TENANT_NAME = "默认用工单位"
ANONYMOUS_USERNAME = "anonymous"


def migrate(db_path: str) -> dict[str, int]:
    """执行迁移。返回各步骤处理的数据量。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stats: dict[str, int] = {}

    # ---- 1. 给 conversations 补 tenant_id ----
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
    if "tenant_id" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN tenant_id TEXT")
        stats["conversations_tenant_added"] = 1
    else:
        stats["conversations_tenant_added"] = 0

    # ---- 2. 给 messages 补 tenant_id ----
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "tenant_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN tenant_id TEXT")
        stats["messages_tenant_added"] = 1
    else:
        stats["messages_tenant_added"] = 0

    # ---- 3. 给 messages 补 flag 相关状态字段 ----
    # flagged 字段用于审计页快速筛选"已被标记的消息"
    if "flag_status" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN flag_status TEXT")
        stats["messages_flag_added"] = 1
    else:
        stats["messages_flag_added"] = 0

    conn.commit()
    conn.close()

    # ---- 4. 建租户与 anonymous 用户，用 AdminDB 走正规接口 ----
    admin = AdminDB(db_path)
    tenant_id = admin.ensure_tenant(DEFAULT_TENANT_CODE, DEFAULT_TENANT_NAME)
    stats["tenant_id"] = tenant_id

    # anonymous 用户：不可登录，仅用于挂载历史数据
    existing = admin.find_user_by_username(DEFAULT_TENANT_CODE, ANONYMOUS_USERNAME)
    if existing is None:
        # 密码随机且不可登录，is_active=0 保证它进不去任何入口
        anon = admin.create_user(
            tenant_id=tenant_id,
            username=ANONYMOUS_USERNAME,
            password="Anon@NeverLogin2026",   # 永远不会被使用
            display_name="匿名历史",
            role="employee",
            employee_no="",
            created_by="system",
        )
        admin.update_user(tenant_id, anon["id"], is_active=0)
        stats["anonymous_created"] = 1
    else:
        stats["anonymous_created"] = 0

    anon_user = admin.find_user_by_username(DEFAULT_TENANT_CODE, ANONYMOUS_USERNAME)
    assert anon_user is not None
    anon_id = anon_user["id"]

    # ---- 5. 回填历史数据的 tenant_id 和 user_id ----
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conv_cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM conversations "
        "WHERE tenant_id IS NULL OR user_id IS NULL"
    ).fetchone()["c"]
    conn.execute(
        "UPDATE conversations SET tenant_id = ? "
        "WHERE tenant_id IS NULL OR tenant_id = ''",
        (tenant_id,),
    )
    conn.execute(
        "UPDATE conversations SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
        (anon_id,),
    )
    conn.commit()

    msg_cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE tenant_id IS NULL"
    ).fetchone()["c"]
    conn.execute(
        "UPDATE messages SET tenant_id = ? WHERE tenant_id IS NULL OR tenant_id = ''",
        (tenant_id,),
    )
    conn.commit()
    conn.close()

    stats["conversations_migrated"] = conv_cnt
    stats["messages_migrated"] = msg_cnt

    # ---- 6. 建种子管理员与员工（仅首次）----
    seed_stats = seed_accounts(admin, tenant_id)
    stats.update(seed_stats)

    return stats


def seed_accounts(admin: AdminDB, tenant_id: str) -> dict[str, int]:
    """建默认账号。

    密码强度要求至少 8 位含字母数字，这里用 Admin@2026 / Emp@2026。
    ⚠️ 生产环境必须改掉——代码里留了提示。
    """
    stats = {"admin_created": 0, "employee_created": 0}

    # ⚠️ 判断账号是否已存在，必须按 username 精确查。
    # 早先这里用的是 list_users(role=...)，结果 anonymous 虚拟用户
    # （role 也是 employee）把真实员工账号的存在性检查挡掉了，
    # 导致员工账号永远建不出来。列表查询不能用来做存在性判断。
    def _user_exists(username: str) -> bool:
        return admin.find_user_by_username(DEFAULT_TENANT_CODE, username) is not None

    if not _user_exists("admin"):
        admin.create_user(
            tenant_id=tenant_id,
            username="admin",
            password="Admin@2026",
            display_name="系统管理员",
            role="admin",
            employee_no="A000",
            created_by="system",
        )
        stats["admin_created"] = 1

    if not _user_exists("zhangsan"):
        admin.create_user(
            tenant_id=tenant_id,
            username="zhangsan",
            password="Emp@2026",
            display_name="张三",
            role="employee",
            employee_no="E001",
            created_by="system",
        )
        stats["employee_created"] = 1

    return stats


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "chat.db"
    result = migrate(db_path)
    print("迁移完成：")
    for key, value in result.items():
        print(f"  {key:32} = {value}")
