#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署产物的启动入口：监听 PORT 环境变量并绑定 0.0.0.0。

与 src/mvp/ 归档版的关系：
    main.py / rag_engine.py / static/index.html / 报销制度.md 均从归档版复制，
    仅 main.py 增加 INDEX_DIR 可覆盖一行（沙箱应用目录只读）。
    本文件只负责把运行时目录指到可写位置并拉起 uvicorn，不改任何业务代码。

本地等价启动：
    cd deploy/mvp && python -m uvicorn main:app --host 127.0.0.1 --port 8000
"""
import os
import tempfile

# 必须在 import main 之前设置：main.py 在模块加载期就读取 DB_PATH / INDEX_DIR
_RUNTIME_DIR = os.getenv("RUNTIME_DIR") or os.path.join(tempfile.gettempdir(), "mvp-runtime")
os.makedirs(_RUNTIME_DIR, exist_ok=True)
os.environ.setdefault("DB_PATH", os.path.join(_RUNTIME_DIR, "chat.db"))
os.environ.setdefault("INDEX_DIR", os.path.join(_RUNTIME_DIR, ".rag_cache"))

from main import app  # noqa: E402  (必须晚于环境变量设置)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
