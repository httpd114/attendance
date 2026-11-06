# MVP 部署产物（对外演示环境）

本目录是 `src/mvp/` 阶段 2 问答助手归档版的**部署副本**，用于发布为单端口 HTTP 服务。

## 安全声明（务必随链接一并告知访问者）

- 这是**匿名演示环境**：无登录、无租户隔离、无审计，**禁止录入任何真实员工、客户或经营数据**。
- 知识源 `报销制度.md` 是项目原型阶段编写的**示例制度**，文档首行已注明「非真实经营制度」。
- 未配置 `LLM_API_KEY`，服务以 `hash` embedding（纯 BM25 检索）+ `MockLLM` 运行，回答为链路演示内容，**不具备业务参考价值**。
- 数据库与索引缓存写在系统临时目录，随沙箱生命周期存在，不保证持久。
- 该环境**不是生产系统**，生产版本仍有 12 项 P0 阻塞未关闭（见 `docs/上线前检查清单-派遣考勤异常双向确认-v1.0.md`）。

## 与归档源码的差异

| 文件 | 处理 |
| --- | --- |
| `rag_engine.py`、`static/index.html`、`报销制度.md` | 与 `src/mvp/` 字节一致 |
| `main.py` | 与 `src/mvp/` 仅差一行：`CFG.index_dir` 改为可由 `INDEX_DIR` 覆盖，避免沙箱只读目录写入失败 |
| `serve.py` | 新增。设置 `DB_PATH` / `INDEX_DIR` 到临时目录，读取 `PORT` 并绑定 `0.0.0.0` |
| `requirements.txt` | 新增。锁定大版本范围的部署依赖 |

## 启动

```bash
pip install -r requirements.txt
python serve.py                 # 默认 PORT=8000，监听 0.0.0.0
PORT=3000 python serve.py       # 发布平台注入端口
```

## 冒烟验证

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","engine_ready":true,"vector_enabled":false,"chunks":11,"llm_model":"mock"}

curl -sN -H 'Content-Type: application/json' \
  -d '{"question":"差旅住宿费上限是多少"}' http://127.0.0.1:8000/chat
```

SSE 事件顺序固定：`meta → status(retrieving) → citations → status(generating) → delta* → done`。
