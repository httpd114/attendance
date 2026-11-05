# 贡献指南

感谢参与。本项目是企业信息系统的教学／原型交付，贡献时请把**可追溯性**放在第一位：
任何改动都要能回答「谁改的、为什么改、怎么验的」。

## 开发环境

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
export PYTHONPATH="$PWD/src"      # 未做 pip install -e . 时必需
```

启动与配置见 [`README.md`](README.md)。**不要**把 API Key、JWT 密钥写进代码、
配置文件或测试用例；本地用环境变量，`.env` 已被忽略。

## 提交前必跑

```bash
bash scripts/build.sh                    # 语法 + 路由自检 + 用例自检 + 单测 + demo
bash tests/test_auth.sh                  # 集成回归（需服务已启动）
bash tests/test_kb.sh
bash tests/test_admin3.sh
```

`scripts/build.sh` 不依赖网络、不依赖 API Key，适合放进 CI；三个 `test_*.sh`
需要本地服务与一组有效密钥。

## 代码约定

- **新代码放对层**：业务逻辑进 `src/attendance_agent/core/`，无业务依赖的辅助函数进
  `utils/`，HTTP 路由与依赖注入放 `main.py`。
- **包内用相对导入**（`from .agent_db import ...`），不要在包内写绝对模块名；
  `examples/` / `scripts/` / `tests/` 里用 `from attendance_agent.core...` 并显式把
  `src/` 加入 `sys.path`。
- **路径不要硬编码**。统一从 `utils/paths.py` 取 `PROJECT_ROOT` / `DATA_DIR`，
  允许环境变量覆盖；不要在代码里写开发机绝对路径。
- **写操作必须过门禁**。新增任何会落业务数据的工具，先在
  `core/agent_gate.py` 补状态机前置条件与能力令牌校验，再写执行逻辑。
  提示词里的「禁止跳步」只是效率优化，不是防线。
- **日志先落库再执行工具**，失败调用也要留痕；凭证类字段只存 sha256 前 8 位指纹。
- 中文注释写「为什么」而不是「这行做什么」。项目里已有的踩坑注释是重要的知识资产，
  修改代码时请同步更新它们。
- 涉及制度结论的回答必须带真实 `chunk_id`；制度未覆盖时明确拒答，不得用常识补白。

## 新增路由时

`tests/check_routes.py` 里的 `EXPECTED_ROUTES` 是功能契约。加一个页面或接口，
**同时**往清单里加一条并跑一次自检。历史上曾因一次编辑把装饰器并进注释行，
Python 不报错、服务正常启动，但接口静默 404 —— 这份自检就是为此存在的。

## 评测用例

`tests/eval_cases.json` 严格保持 4 字段（`question` / `expected_tools` /
`expected_behavior` / `category`）与 30 条分布（10 / 10 / 5 / 5）。
增删用例后跑 `python tests/validate_eval_cases.py`，它会校验计数、工具名合法性与
调用顺序门禁；`[允许跳步尝试]` 是唯一顺序白名单。

## 提交与 PR

1. 从 `main` 建分支，命名 `feat/xxx`、`fix/xxx`、`docs/xxx`。
2. 提交信息用中文一句话说清改动，例如 `修复审计查询跨租户 JOIN 漏掉 user_id`。
3. PR 描述里写：改了什么、为什么、怎么验的（贴关键命令输出）、是否影响数据库 schema。
4. 数据库结构变动必须同时更新 `scripts/migrate.py`，并说明回退方式。
5. 安全相关改动（鉴权、门禁、注入防护、脱敏）请在 PR 里标注，便于重点复核。

## 安全披露

发现密钥泄露、越权、可绕过门禁等问题，**不要提公开 Issue**，
请直接联系项目维护者并附最小复现步骤。确认修复后再公开。

## 行为准则

就事论事，评审针对代码不针对人。对「为什么这样设计」有疑问时优先查
[`docs/design.md`](docs/design.md) 与代码里的踩坑注释。
