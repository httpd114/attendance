"""通用小工具层：与业务无关、可被任意模块安全导入的辅助函数。"""

from .paths import (
    CONFIG_DIR,
    DATA_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_DOC_PATH,
    DOCS_DIR,
    INDEX_CACHE_DIR,
    PACKAGE_DIR,
    PROJECT_ROOT,
    SRC_DIR,
    STATIC_DIR,
    TEMPLATE_DIR,
    TESTS_DIR,
    ensure_runtime_dirs,
)

__all__ = [
    "CONFIG_DIR", "DATA_DIR", "DEFAULT_DB_PATH", "DEFAULT_DOC_PATH",
    "DOCS_DIR", "INDEX_CACHE_DIR", "PACKAGE_DIR", "PROJECT_ROOT",
    "SRC_DIR", "STATIC_DIR", "TEMPLATE_DIR", "TESTS_DIR",
    "ensure_runtime_dirs",
]
