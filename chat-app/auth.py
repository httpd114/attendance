#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth.py —— 鉴权与租户底座（阶段 3 · 第 1 批）
====================================================================
职责边界：
    本模块只做「你是谁 / 你属于哪个租户 / 你能不能做这件事」，
    不碰任何业务数据，也不碰 FastAPI 的 Response 对象。

四层设计：
    1. 密码哈希      bcrypt（直接用 bcrypt 库，不经 passlib 包装）
    2. 令牌签发校验  PyJWT，HS256 对称签名
    3. 租户上下文    CurrentUser 依赖注入，全程携带 tenant_id
    4. 接口级权限    require_admin / require_user 两个依赖

⭐ 租户隔离是本轮的核心决定（用户拍板 Q3 = 租户隔离）：
    所有业务查询必须带 tenant_id 条件，管理员也只能看到本租户的数据。
    这不是"建议"，是硬约束——本期实现里 tenant_id 一律来自
    JWT 载荷，**绝不允许从请求体或查询参数读租户**，
    否则就是 IDOR 越权漏洞。

⚠️ 为什么不用 passlib：
    passlib 1.7.4 + bcrypt 4.x 有已知兼容性告警（__about__ 属性）。
    直接调 bcrypt 库更稳，接口也更少。实测两者均可用，
    这里选依赖更少的那条路。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ====================================================================
# 配置
# ====================================================================
JWT_ALGORITHM = "HS256"
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-me-in-production")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "12"))

# 角色常量
ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin"

# 错误码（延续 rag_engine.ErrorCode 的 5 位数约定）
# 401xx 未认证 / 403xx 权限不足 / 402xx 账号问题
ERR_INVALID_CREDENTIALS = (40110, "用户名或密码错误")
ERR_TOKEN_MISSING = (40111, "请先登录")
ERR_TOKEN_INVALID = (40112, "登录状态无效，请重新登录")
ERR_TOKEN_EXPIRED = (40113, "登录已过期，请重新登录")
ERR_FORBIDDEN_ADMIN = (40310, "该操作仅管理员可执行")
ERR_FORBIDDEN_SESSION = (40311, "无权访问该数据")
ERR_ACCOUNT_DISABLED = (40312, "账号已被停用，请联系管理员")
ERR_PASSWORD_WEAK = (40201, "密码强度不足，至少 8 位且包含字母和数字")
ERR_USERNAME_TAKEN = (40202, "用户名已存在")
ERR_USER_NOT_FOUND = (40203, "用户不存在")
ERR_OLD_PASSWORD_WRONG = (40204, "原密码不正确")

# 统一的鉴权错误：JWT 库抛的是英文异常，必须转成中文
_AUTH_SCHEME = HTTPBearer(auto_error=False)


class AuthError(Exception):
    """鉴权/账号异常。携带错误码，由接口层转成统一 JSON。"""

    def __init__(self, code_pair: tuple[int, str], detail: str = "") -> None:
        self.code, self.message = code_pair
        self.detail = detail
        super().__init__(f"[{self.code}] {self.message}")

    def to_dict(self, request_id: str = "") -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "request_id": request_id,
        }


# ====================================================================
# 密码哈希
# ====================================================================
def hash_password(plain: str) -> str:
    """bcrypt 加盐哈希。

    bcrypt 自带盐，不要额外拼 salt——那会削弱它的设计。
    rounds=12 是安全性和登录延迟的平衡点（约 200-300ms）。
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。哈希串格式异常时返回 False 而不是抛异常。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def check_password_strength(plain: str) -> None:
    """密码强度校验。弱密码直接拒绝，不给重试机会。

    这里只做最低限度（长度 + 字母 + 数字）。真正的强密码策略
    应该由客户的安全基线决定，原型阶段不越界。
    """
    if len(plain) < 8:
        raise AuthError(ERR_PASSWORD_WEAK, detail=f"当前长度 {len(plain)} 位")
    if not any(c.isalpha() for c in plain):
        raise AuthError(ERR_PASSWORD_WEAK, detail="缺少字母")
    if not any(c.isdigit() for c in plain):
        raise AuthError(ERR_PASSWORD_WEAK, detail="缺少数字")


# ====================================================================
# JWT 签发与校验
# ====================================================================
def create_access_token(user_id: str, tenant_id: str, role: str,
                        username: str = "") -> str:
    """签发访问令牌。

    载荷里带 tenant_id 是关键——服务端后续所有数据访问都从这里
    取租户边界，绝不接受客户端传入。
    """
    now = int(time.time())
    payload = {
        "sub": user_id,           # subject：用户 ID
        "tid": tenant_id,         # tenant：租户 ID
        "rol": role,              # role：角色
        "unm": username,          # username：仅便于日志排查
        "iat": now,
        "exp": now + JWT_EXPIRE_HOURS * 3600,
        "jti": uuid.uuid4().hex[:12],   # 令牌唯一 ID，便于将来做黑名单
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """解析并校验令牌。

    把 JWT 库的三种英文异常翻译成中文错误码——
    这是阶段 2 定下的约定：所有接口先返回错误码和中文信息。
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(ERR_TOKEN_EXPIRED, detail=str(exc)) from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(ERR_TOKEN_INVALID, detail=str(exc)) from exc

    for field in ("sub", "tid", "rol"):
        if field not in payload:
            raise AuthError(ERR_TOKEN_INVALID, detail=f"载荷缺字段 {field}")
    return payload


# ====================================================================
# 当前用户上下文
# ====================================================================
@dataclass
class CurrentUser:
    """从 JWT 解析出的调用者身份。

    ⭐ 这是整个系统的租户隔离锚点：所有 service 层函数的
    tenant_id 参数都从这里来，不从别处来。
    """
    id: str
    tenant_id: str
    role: str
    username: str
    display_name: str = ""
    employee_no: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _extract_token(request: Request,
                   creds: Optional[HTTPAuthorizationCredentials]) -> str:
    """从请求里取令牌。

    两种来源都支持：
      1. Authorization: Bearer xxx（接口调用方，前端 fetch 用）
      2. Cookie: access_token（SSR 页面跳转用）

    之所以要支持 Cookie：管理后台是服务端渲染，页面间跳转靠
    浏览器自动带 Cookie；如果只有 Bearer，刷新页面就得重新
    塞 token 到 URL 或存储里。
    """
    if creds is not None and creds.credentials:
        return creds.credentials
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token
    raise AuthError(ERR_TOKEN_MISSING)


async def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_AUTH_SCHEME),
) -> CurrentUser:
    """FastAPI 依赖：解析当前登录用户。

    未登录时抛 AuthError（40111），由 main.py 的统一异常处理器
    转成 JSON。注意这里**不**直接抛 HTTPException——
    那样会走 FastAPI 默认的英文错误体，破坏中文错误码约定。
    """
    token = _extract_token(request, creds)
    payload = decode_access_token(token)

    # 从库里取一次用户，确认账号仍然有效。
    # 不信任 JWT 里的角色——管理员被降级后，旧令牌应立即失效。
    #
    # ⚠️ 数据库访问是同步的（sqlite3），必须丢线程池，
    #    否则每个请求都在事件循环里做磁盘 IO，并发一上来就卡。
    db = request.app.state.db
    user = await asyncio.to_thread(db.get_user, payload["sub"])
    if user is None:
        raise AuthError(ERR_TOKEN_INVALID, detail="用户不存在或已删除")
    if not user.get("is_active", 1):
        raise AuthError(ERR_ACCOUNT_DISABLED)
    if user["tenant_id"] != payload["tid"]:
        # 令牌与库里的租户不一致 = 令牌被篡改或跨租户复用
        raise AuthError(ERR_TOKEN_INVALID, detail="租户校验失败")

    return CurrentUser(
        id=user["id"],
        tenant_id=user["tenant_id"],
        role=user["role"],
        username=user["username"],
        display_name=user.get("display_name") or user["username"],
        employee_no=user.get("employee_no") or "",
    )


async def require_user(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """要求已登录（任何角色）。"""
    return user


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """要求管理员角色。

    这是"接口级权限校验"的核心依赖。所有 /api/admin/* 和
    /admin/* 路由都必须挂它。
    """
    if not user.is_admin:
        raise AuthError(ERR_FORBIDDEN_ADMIN,
                        detail=f"当前角色为 {user.role}，需要 {ROLE_ADMIN}")
    return user
