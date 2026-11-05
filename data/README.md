# data/ 目录说明

本目录放**样例数据**与**运行时生成的数据**，两者混放但入库规则不同。

## 允许入库（样例 / 交付资源）

| 文件 | 用途 |
|---|---|
| `报销制度.md` | 默认制度文档，`DOC_PATH` 默认值指向这里；RAG 问答与知识库上传的样例源 |
| `绘制ai数智化后流程图.jpg`、`mmexport*.jpg` | 流程图与需求调研截图 |
| `派遣岗位需求单现状流程图v1.html` | 现状流程图源文件的静态副本 |

## 不入库（由 `.gitignore` 排除）

| 文件 / 目录 | 说明 |
|---|---|
| `chat.db`、`chat.db-shm`、`chat.db-wal` | 应用运行时 SQLite（WAL 模式），含真实会话、用户与审计数据 |
| `agent_demo.db` | `examples/demo.py` 的独立演示库 |
| `.rag_cache/` | FAISS / embedding 索引缓存，可从 `kb_chunks` 的向量 BLOB 重建 |
| `*.bak*` | 迁移或部署脚本生成的一致性备份 |

> 服务默认从项目根 `data/chat.db` 读写；可用 `DB_PATH` 覆盖。
> 索引缓存可用 `INDEX_DIR` 覆盖，制度文档可用 `DOC_PATH` 覆盖。
> 路径解析统一在 `src/attendance_agent/utils/paths.py`。

## 重要

**不要把生产数据库提交进版本库。** 上线前检查清单 A1／A3 还要求：
对话原文里的个人信息需按角色掩码展示，且必须制定日志保留与到期处置策略
（当前尚未实现，属 P0 阻塞）。

备份请使用 SQLite 在线一致性备份（`scripts/deploy.sh` 用的是
`sqlite3.Connection.backup()`），不要直接 `cp` 一个开着的 WAL 数据库。
