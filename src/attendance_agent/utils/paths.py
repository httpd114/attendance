"""统一路径解析（项目结构重构后新增）。

为什么需要这个模块
------------------
重构前所有模块都平铺在 ``chat-app/`` 一个目录里，代码用
``Path(__file__).parent`` 就能找到 static / templates / chat.db。
改成 ``src/<package>/core`` 的分层结构后，不同层级的文件向上跳的层数不同，
各自写 ``parents[n]`` 极易算错，而且换个部署方式（如打成 wheel 安装）
就会全部失效。所以把「项目根在哪」这件事收敛到这一个模块里。

约定
----
* ``PACKAGE_DIR``  包目录，static/ 与 templates/ 作为包资源放在这一层。
* ``PROJECT_ROOT`` 项目根（README.md / config/ / data/ 所在层）。
* ``DATA_DIR``     运行时数据与样例数据。SQLite 库、RAG 索引缓存都放这里，
                   统一由 .gitignore 排除，避免把用户数据提交进版本库。

所有路径都允许通过环境变量覆盖（见 main.py 的 DB_PATH、rag_engine 的
DOC_PATH / INDEX_DIR），本模块只提供**默认值**。
"""

from __future__ import annotations

from pathlib import Path

# utils/paths.py → parents[0]=utils, parents[1]=attendance_agent
PACKAGE_DIR = Path(__file__).resolve().parents[1]
# attendance_agent → src → 项目根
PROJECT_ROOT = PACKAGE_DIR.parents[1]

SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"
DOCS_DIR = PROJECT_ROOT / "docs"
TESTS_DIR = PROJECT_ROOT / "tests"

# 包内资源：随代码一起发布
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATE_DIR = PACKAGE_DIR / "templates"

# 运行时默认值：可用 DB_PATH / DOC_PATH / INDEX_DIR 环境变量覆盖
DEFAULT_DB_PATH = DATA_DIR / "chat.db"
DEFAULT_DOC_PATH = DATA_DIR / "报销制度.md"
INDEX_CACHE_DIR = DATA_DIR / ".rag_cache"


def ensure_runtime_dirs() -> None:
    """确保运行时目录存在。

    SQLite 不会自动创建父目录，RAG 索引缓存首次写入前也需要目录就位。
    在应用启动时调用一次即可，重复调用是幂等的。
    """
    for d in (DATA_DIR, INDEX_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
