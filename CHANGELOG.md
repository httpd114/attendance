# 更新记录

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号参考 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added · 阶段 2 MVP 归档与 GitHub 版本基线

- 新增 `src/mvp/`：阶段 2 SSE 聊天最小可运行版本归档。`rag_engine.py`、
  `static/index.html`、`报销制度.md` 取自历史提交 `0ffc8f4`（SHA-256 校验一致）；
  `main.py` 是按阶段 2 接口契约重建的薄编排层——历史入口后来叠加了登录与
  管理后台，且建表 SQL 依赖 `migrate.py` 补列，无法原样独立启动。
- 归档版无密钥即可离线运行（纯 BM25 + MockLLM），已通过 `/health`、首页、
  SSE 事件序列、会话历史落库、400/404 中文错误体的端到端冒烟。
- 项目接入 GitHub（`httpd114/attendance`）：以远端 `main` 为基线做重构提交，
  清理旧 `chat-app/` 及误入库的 SQLite / 缓存 / 临时测试文件；
  `scripts/build.sh` 全量自检 7/7 通过。
- CI 矩阵收敛为 Python 3.13（固定依赖中的 `numpy 2.5.x` 要求 ≥3.12），
  README 补齐 3.11 用户改用 `pip install -e .` 的说明。

### 待关闭的上线阻塞项

见 [`docs/上线前检查清单-派遣考勤异常双向确认-v1.0.md`](docs/上线前检查清单-派遣考勤异常双向确认-v1.0.md)，
共 12 项 P0。优先级最高的三项：

- 替换 `JWT_SECRET` / `AGENT_GATE_SECRET` 的开发默认值，缺失时拒绝启动。
- 制定并落地日志保留策略（当前无任何 TTL / 归档 / 清理）。
- 建立版本基线与可回滚制品（已有 Git 历史与 GitHub 远端，仍缺镜像、发布包与回滚演练）。

## [0.1.0] - 2026-11-05

### Added · 标准项目骨架与工程化配套

- 重构为 `src/` 布局：应用包 `src/attendance_agent/`，核心业务进 `core/`，
  通用工具进 `utils/`，新增 `utils/paths.py` 统一解析项目根与数据目录。
- 平铺模块导入改为包内相对导入；`static/` / `templates/` 作为包资源随代码发布，
  SQLite 与 RAG 索引缓存迁到 `data/` 并由 `.gitignore` 排除。
- 新增 `README.md`、`LICENSE`(MIT)、`.gitignore`、`requirements.txt`、
  `pyproject.toml`、`CONTRIBUTING.md`、`config/default.yaml`。
- 新增 `docs/design.md` / `docs/api.md`，业务与交付文档统一归档到 `docs/`。
- 新增 `tests/test_core.py` 核心单测、`scripts/build.sh` / `deploy.sh`、
  `.github/workflows/ci.yml` 与 Issue / PR 模板。
- 重构前完整副本备份于 `.workbuddy/backup/pre-restructure-20261105/`。

### Added · 阶段成果（同日早前）

- **阶段 1**：裸问答 RAG 原型（Markdown 按标题切片、BM25 + 字符二元组 + 向量 RRF 融合、FAISS 索引、引用自检与拒答白名单）。
- **阶段 2**：FastAPI SSE 聊天服务，事件序列 `meta → status → citations → delta → done/error`，SQLite 会话持久化，8 类中文错误码。
- **阶段 3**：管理后台四个批次——JWT 鉴权与租户隔离、多文档知识库、提示词模板版本管理、规则预筛与对话审计、用户管理。
- **阶段 4**：Agent 工具 Function Calling 规范（9 个工具）与自建轻量 Harness：
  最多 5 轮决策、一次性 HMAC 能力令牌、状态机硬门禁、每轮决策与工具调用日志表、
  三条典型轨迹演示与 8 条硬门禁单测。
- **QA 资产**：30 条评测用例集 `tests/eval_cases.json` 及其自检脚本。
- **上线治理**：25 项上线前检查清单（5 维度 × 5 项，含负责人、验证方法、通过标准）。

### Changed

- 服务默认监听地址由 `0.0.0.0` 改为 `127.0.0.1`（生产由反向代理暴露）；
  入口统一为 `python -m attendance_agent.main`，不再支持 `cd` 进源码目录直跑。
- 制度文档默认路径由 CWD 相对的 `./报销制度.md` 改为项目根 `data/报销制度.md`，
  并新增 `DOC_PATH` / `INDEX_DIR` 覆盖能力。
- 集成回归脚本移除机器专属绝对路径：临时目录跟随脚本位置，解释器可用 `PYTHON` 覆盖。

### Known Issues

- 引用编号「未越界但指错 chunk」的语义校验未根治。
- 两套制度知识源（RAG 库与 Agent 内置 `POL-*` 片段）章节号冲突，
  `make_rag_policy_lookup()` 未适配时会静默回退演示片段。
- 限流为进程内滑动窗口，多 worker 部署前需换共享计数。

> 版本基线尚未建立（项目此前无 git 历史），compare / tag 链接待首次发布后补充。
> 这是上线前检查清单中的 P0 阻塞项 E1。
