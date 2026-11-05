---
name: 缺陷报告
about: 功能表现与预期不符时提交
title: "[Bug] "
labels: bug
assignees: ''
---

## 现象

一句话说清「发生了什么」，不要写「不好用」。

## 复现步骤

1. 以什么身份登录（管理员 / 员工 / 哪个租户）
2. 访问哪个页面或调用哪个接口
3. 输入了什么
4. 观察到什么

## 期望行为

写清可判定的标准，例如「应返回 403 且不泄露资源是否存在」。
涉及制度问答时请注明期望引用的章节或 `chunk_id`。

## 实际行为

贴关键响应、SSE 事件序列或错误体。**请先删掉 API Key、Cookie、_token 等敏感信息。**

## 环境

- 版本 / commit：
- Python 版本：
- 启动方式（`python -m attendance_agent.main` / uvicorn / 容器）：
- 浏览器（仅前端问题）：

## 已跑过的自检

```
python tests/check_routes.py
python tests/validate_eval_cases.py
python -m pytest tests/test_core.py -q
```

贴输出或说明跳过原因。

## 补充

日志片段、截图、相关表记录（脱敏后）。若涉及数据错乱，请说明是否可能已写入
`confirmation_cases` 或 `audit_logs`。
