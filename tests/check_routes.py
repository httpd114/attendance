#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_routes.py —— 路由与页面完整性自检
====================================================================
【为什么需要这个脚本】

开发过程中踩过一次真实的坑：往 main.py 里插入新路由时，
一次 Edit 把 `# ====` 注释行和下一行的 `@app.get(...)` 合并成了一行——
注释把装饰器吃掉了，Python 不报错、程序照常启动，
但那个接口直接 404。

这类故障最恶心的地方在于：**语法检查全绿、启动日志正常**，
只有实际调那个接口时才发现。所以它必须有一个自动化守卫，
而不是靠人记得去点一遍。

本脚本做两件事：
  1. 静态检查：扫描所有 .py 文件，找出"注释行里藏着代码"的可疑行
  2. 动态检查：拉起路由表，核对应当存在的路由是否齐全

用法：
    python check_routes.py            # 只做静态检查（不需要服务在跑）
    python check_routes.py --live     # 静态 + 动态（需要服务已启动）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 检查器在 tests/，被测代码在 src/attendance_agent/（含 core/ 与 utils/ 子包）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = PROJECT_ROOT / "src" / "attendance_agent"
BASE_DIR = PACKAGE_DIR          # 模板文件仍在包内 templates/

# ====================================================================
# 一、应当存在的路由清单
# ----------------------------------------------------------------
# 格式：(方法, 路径)。路径里的 {param} 用正则处理。
# 每新增一个后台页面/接口，都往这里加一条——
# 这份清单就是"功能契约"，漏了就红。
# ====================================================================
EXPECTED_ROUTES: list[tuple[str, str]] = [
    # ---- 公共 ----
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/logout"),
    ("POST", "/chat"),
    ("GET", "/api/sessions/{sid}/messages"),

    # ---- 员工端 ----
    # （问答页是静态文件，没有独立路由）

    # ---- 管理后台页面 ----
    ("GET", "/admin"),
    ("GET", "/admin/knowledge"),
    ("POST", "/admin/knowledge/upload"),
    ("POST", "/admin/knowledge/documents/{doc_id}/delete"),
    ("GET", "/admin/knowledge/documents/{doc_id}/chunks"),
    ("POST", "/admin/knowledge/reindex"),
    ("GET", "/admin/prompts"),
    ("POST", "/admin/prompts/save"),
    ("POST", "/admin/prompts/rollback"),
    ("GET", "/admin/audit"),
    ("POST", "/admin/audit/flags/{flag_id}/review"),
    ("GET", "/admin/users"),
    ("POST", "/admin/users/create"),
    ("POST", "/admin/users/{user_id}/toggle"),
    ("POST", "/admin/users/{user_id}/reset-password"),

    # ---- 管理后台 API ----
    ("GET", "/api/admin/users"),
    ("POST", "/api/admin/users"),
    ("POST", "/api/admin/users/{user_id}/reset-password"),
    ("GET", "/api/admin/kb/documents"),
    ("DELETE", "/api/admin/kb/documents/{doc_id}"),
]

# ====================================================================
# 二、静态检查：注释吞代码
# ====================================================================
# 判定规则：行首是 #，但行内出现了代码特征
CODE_IN_COMMENT = re.compile(
    r"(@app\.|@router\.|"
    r"^\s*#\s*(async\s+)?def\s+|"
    r"^\s*#\s*class\s+|"
    r"^\s*#\s*return\b|"
    r"^\s*#\s*(import|from)\s+\w)"
)

# 检查器自身要跳过：文档里举的反例本身就是"注释吞代码"的写法，
# 不排除的话它会永远报自己的问题，变成永远红的告警——那就没人看了。
SELF_EXCLUDE = {"check_routes.py"}


def iter_py_files() -> list[Path]:
    """递归列出包内所有 Python 源文件。

    重构后代码分散在包根 / core/ / utils/ 三层，
    只扫 BASE_DIR.glob("*.py") 会漏掉两个子包里的路由与注释。
    """
    return [f for f in sorted(PACKAGE_DIR.rglob("*.py"))
            if "__pycache__" not in f.parts and f.name not in SELF_EXCLUDE]


def static_check() -> list[str]:
    """扫描所有 .py，找"注释吞代码"的可疑行。返回问题列表。"""
    problems: list[str] = []
    for py in iter_py_files():
        if py.name in SELF_EXCLUDE:
            continue
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for idx, raw in enumerate(lines, 1):
            s = raw.strip()
            if not s.startswith("#"):
                continue
            m = CODE_IN_COMMENT.search(s)
            if m:
                problems.append(
                    f"{py.name}:{idx}  注释行里疑似藏着代码 → {s[:88]}"
                )
    return problems


# ====================================================================
# 三、静态检查：装饰器被注释（更精确的二次筛查）
# ====================================================================
def decorator_check() -> list[str]:
    """找出"注释块结尾 + 装饰器被并入注释行"的情况。

    典型形态：
        # ================
        # ================@app.get("/x")     ← 装饰器在这一行被吃掉
        async def x(): ...
    """
    problems: list[str] = []
    for py in iter_py_files():
        if py.name in SELF_EXCLUDE:
            continue
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for idx, raw in enumerate(lines, 1):
            s = raw.strip()
            if s.startswith("#") and ("@app." in s or "@router." in s):
                problems.append(
                    f"{py.name}:{idx}  装饰器疑似被注释吞掉 → {s[:88]}"
                )
    return problems


# ====================================================================
# 四、静态检查：路由函数是否有对应的 async def
# ====================================================================
def handler_check() -> list[str]:
    """每个 @app.xxx 装饰器后面必须紧跟一个函数定义。

    没有函数定义的装饰器是"悬空装饰器"——
    通常意味着上一次编辑把结构改坏了。
    """
    problems: list[str] = []
    for py in iter_py_files():
        if py.name in SELF_EXCLUDE:
            continue
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for idx, raw in enumerate(lines, 1):
            s = raw.strip()
            if not (s.startswith("@app.") or s.startswith("@router.")):
                continue
            # 往下找下一个非空、非注释行，应该是 async def / def
            for j in range(idx, min(idx + 6, len(lines))):
                nxt = lines[j].strip()
                if not nxt or nxt.startswith("#"):
                    continue
                if nxt.startswith(("async def ", "def ")):
                    break
                problems.append(
                    f"{py.name}:{idx}  装饰器后没有函数定义 → {s[:60]} / {nxt[:40]}"
                )
                break
    return problems


# ====================================================================
# 五、动态检查：路由表是否齐全
# ====================================================================
def live_check(base: str = "http://127.0.0.1:8000") -> list[str]:
    """导入 app，检查实际注册的路由是否覆盖 EXPECTED_ROUTES。"""
    problems: list[str] = []
    try:
        import main  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return [f"无法导入 main 模块：{type(exc).__name__}: {exc}"]

    actual: set[tuple[str, str]] = set()
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            actual.add((m, path))

    def _norm(p: str) -> str:
        """把 {param} 归一成 {}，便于比较。"""
        return re.sub(r"\{[^}]+\}", "{}", p)

    actual_norm = {(m, _norm(p)) for m, p in actual}

    for method, path in EXPECTED_ROUTES:
        key = (method, _norm(path))
        if key not in actual_norm:
            problems.append(f"缺失路由：{method} {path}")

    return problems


# ====================================================================
# 六、模板文件检查
# ====================================================================
REQUIRED_TEMPLATES = [
    "base.html",
    "login.html",
    "admin_base.html",
    "admin_home.html",
    "admin_knowledge.html",
    "admin_knowledge_chunks.html",
    "admin_prompts.html",
    "admin_audit.html",
    "admin_users.html",
]


def template_check() -> list[str]:
    problems: list[str] = []
    for name in REQUIRED_TEMPLATES:
        p = BASE_DIR / "templates" / name
        if not p.exists():
            problems.append(f"缺少模板文件：templates/{name}")
    return problems


# ====================================================================
# 入口
# ====================================================================
def main() -> int:
    live = "--live" in sys.argv

    print("=" * 62)
    print("  路由与页面完整性自检")
    print("=" * 62)

    groups = [
        ("静态检查 · 注释吞代码", static_check()),
        ("静态检查 · 装饰器被注释", decorator_check()),
        ("静态检查 · 悬空装饰器", handler_check()),
        ("静态检查 · 模板文件", template_check()),
    ]
    if live:
        groups.append(("动态检查 · 路由表", live_check()))

    total = 0
    for title, problems in groups:
        if problems:
            total += len(problems)
            print(f"\n[FAIL] {title} —— {len(problems)} 处问题")
            for p in problems:
                print(f"       · {p}")
        else:
            print(f"[ OK ] {title}")

    print()
    if total:
        print(f"结论：发现 {total} 处问题，需修复。")
        return 1

    checked = len(EXPECTED_ROUTES) if live else 0
    suffix = f"，核对路由 {checked} 条" if live else ""
    print(f"结论：全部通过{suffix}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
