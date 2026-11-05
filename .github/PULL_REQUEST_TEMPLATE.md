## 改了什么

一句话概述，再说关键改动点（分条）。

## 为什么

关联 Issue / 需求条目，说明取舍。若否决过其他方案，写一句原因。

## 怎么验的

```
bash scripts/build.sh
python examples/demo.py
bash tests/test_auth.sh       # 需服务与密钥
bash tests/test_kb.sh
bash tests/test_admin3.sh
```

贴关键输出。没跑的项目请说明原因。

## 检查清单

- [ ] `bash scripts/build.sh` 全绿（语法 / 路由 / 用例集 / 单测 / 三条轨迹）
- [ ] 新增路由已同步 `tests/check_routes.py` 的 `EXPECTED_ROUTES`
- [ ] 新增评测用例已跑 `python tests/validate_eval_cases.py`
- [ ] 代码里没有硬编码绝对路径、密钥、API Key
- [ ] 日志中凭证仍是 8 位指纹，结果摘要仍受长度限制
- [ ] 写操作仍经过 `agent_gate.py`，没有新增「绕过校验」的快捷路径
- [ ] 中文字段用 UTF-8 提交（表单测试用 `--data-urlencode`，不是 `curl -d`）
- [ ] 更新了 `CHANGELOG.md`；行为变化同步更新 `README.md` / `docs/`
- [ ] 数据库 schema 变动已同步 `scripts/migrate.py` 并说明回退方式

## 安全相关

- [ ] 本次改动涉及鉴权、门禁、注入防护、脱敏或审计（如是，请在下方说明并请求重点复核）

说明：

## 回滚方式

写明「出问题时回到哪个版本 / 数据库怎么恢复」。没有可回滚制品时如实写「暂无」，
不要假设「可以 git revert」——请先确认版本基线是否已建立。
