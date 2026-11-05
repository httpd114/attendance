"""派遣项目考勤异常双向确认系统。

分层
----
``core/``   核心业务逻辑：认证、知识库、RAG 检索、管理后台数据层、
            Agent 状态机门禁、工具执行器与执行循环。
``utils/``  通用小工具：路径解析等无业务依赖的辅助函数。
``main.py`` FastAPI 应用入口（``python -m attendance_agent.main``）。

不在 ``__init__`` 里导入 main：导入包就会创建 FastAPI app、连数据库、
读环境变量，任何只想用 ``agent_gate`` 做单测的代码都会被副作用拖累。
需要应用对象时显式写 ``from attendance_agent.main import app``。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
