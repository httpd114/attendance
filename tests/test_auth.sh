#!/usr/bin/env bash
# 鉴权流程自动化测试（第 1 批验收）
B="http://127.0.0.1:8000"
W="$(cd "$(dirname "$0")" && pwd)/.tmp_test"
PYTHON="${PYTHON:-python}"   # 可用 PYTHON=/path/to/python 覆盖
mkdir -p "$W"

# 登录拿 cookie
curl -s -o /dev/null -c "$W/admin.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=admin&password=Admin@2026"
curl -s -o /dev/null -c "$W/emp.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=zhangsan&password=Emp@2026"

echo "=== T8 员工访问管理接口 → 期望 403 ==="
curl -s -b "$W/emp.txt" -w "\n  HTTP %{http_code}\n" $B/api/admin/users

echo "=== T11 管理员访问管理接口 → 期望 200 ==="
curl -s -b "$W/admin.txt" -w "\n  HTTP %{http_code}\n" $B/api/admin/users

echo "=== T12 员工访问问答首页 → 期望 200 ==="
curl -s -o /dev/null -b "$W/emp.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" $B/

echo "=== T13 已登录访问 /login → 期望 302 ==="
curl -s -o /dev/null -b "$W/admin.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -H "Accept: text/html" $B/login

echo "=== T14 管理员建号 → 期望 200 且无 password_hash ==="
curl -s -b "$W/admin.txt" -w "\n  HTTP %{http_code}\n" -X POST $B/api/admin/users \
  -H "Content-Type: application/json" \
  -d '{"username":"lisi","password":"Test@2026","display_name":"李四","role":"employee","employee_no":"E002"}'

echo "=== T15 重复用户名建号 → 期望 40202 中文 ==="
curl -s -b "$W/admin.txt" -w "\n  HTTP %{http_code}\n" -X POST $B/api/admin/users \
  -H "Content-Type: application/json" \
  -d '{"username":"lisi","password":"Test@2026","role":"employee"}'

echo "=== T16 弱密码 → 期望 40201 ==="
curl -s -b "$W/admin.txt" -w "\n  HTTP %{http_code}\n" -X POST $B/api/admin/users \
  -H "Content-Type: application/json" \
  -d '{"username":"wangwu","password":"123","role":"employee"}'

echo "=== T17 新账号可登录 → 期望 302 ==="
curl -s -o /dev/null -c "$W/lisi.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=lisi&password=Test@2026"

echo "=== T18 篡改 Cookie → 期望 401 ==="
curl -s -b "$W/admin.txt" -w "\n  HTTP %{http_code}\n" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJoYWNrIiwiaWQiOjE3OTM0MDAwMDAsImV4cCI6OTk5OTk5OTk5OX0.zzzz" \
  $B/api/admin/users
