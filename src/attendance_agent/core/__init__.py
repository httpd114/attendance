"""核心业务逻辑层。

模块之间使用**显式相对导入**（``from .agent_db import ...``），
因此这些模块只能作为 ``attendance_agent.core`` 包的一部分被导入，
不能再当作独立脚本 ``python agent_db.py`` 直接运行。
需要单独跑的入口统一放在 ``examples/``、``scripts/``、``tests/``。

主要模块：
    auth / admin_db       JWT 鉴权、租户、用户、知识库、审计数据
    rag_engine / kb       切片、混合检索、多文档知识库与向量索引
    audit_rules           规则预筛（只召回不定性，必须人工复核）
    agent_db              业务表 + 阶段 5 决策日志表
    agent_gate            一次性能力令牌与状态机硬门禁
    agent_tools           9 个 Function Calling 工具 schema 与执行器
    agent_loop            自建轻量 Harness（最多 5 轮决策）
    agent_prompt          代码写死的强制规则段 + 运行时状态卡
"""
