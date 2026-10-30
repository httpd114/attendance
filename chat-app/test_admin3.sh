#!/usr/bin/env bash
# 后台第 3、4 批验收：提示词模板 + 对话审计 + 用户管理
B="http://127.0.0.1:8000"
W="D:/shijian/attendance/chat-app/.tmp_test"
mkdir -p "$W"

# 管理员登录
curl -s -o /dev/null -c "$W/adm.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=admin&password=Admin@2026"
# 员工登录
curl -s -o /dev/null -c "$W/emp.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=zhangsan&password=Emp@2026"

echo "=== P1 提示词模板页 → 期望 200 ==="
curl -s -o "$W/prompts.html" -b "$W/adm.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" \
  -H "Accept: text/html" $B/admin/prompts
grep -o "强制规则段\|业务正文段\|版本历史\|不可编辑" "$W/prompts.html" | sort -u

echo ""
echo "=== P2 强制段内容是否渲染 ==="
grep -c "只能依据「制度原文」作答" "$W/prompts.html" | sed 's/^/  强制规则出现次数: /'

echo ""
echo "=== P3 保存提示词（新建 v1）→ 期望 302 + flash ==="
curl -s -o /dev/null -b "$W/adm.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -X POST $B/admin/prompts/save \
  --data-urlencode "name=制度答疑默认模板" \
  --data-urlencode "body_text=补充说明（测试）：
1. 回答时先给结论，再给依据。
2. 涉及金额时请同时给出自然语言摘要。"

echo ""
echo "=== P4 页面确认 v1 已生效 ==="
curl -s -b "$W/adm.txt" -H "Accept: text/html" $B/admin/prompts > "$W/prompts2.html"
grep -o "当前生效 v[0-9]*" "$W/prompts2.html" | head -1 | sed 's/^/  /'
grep -c "先给结论" "$W/prompts2.html" | sed 's/^/  自定义正文出现次数: /'

echo ""
echo "=== P5 再存一版（v2），验证版本递增 ==="
curl -s -o /dev/null -b "$W/adm.txt" -X POST $B/admin/prompts/save \
  --data-urlencode "name=制度答疑默认模板" \
  --data-urlencode "body_text=补充说明（第二版测试）：
1. 回答时先给结论，再给依据，再给操作建议。"
grep -o "当前生效 v[0-9]*" <(curl -s -b "$W/adm.txt" -H "Accept: text/html" $B/admin/prompts) | head -1 | sed 's/^/  /'

echo ""
echo "=== P6 版本历史应含 2 条 ==="
grep -o "v[12]" "$W/prompts2.html" | sort -u | tr '\n' ' ' | sed 's/^/  版本号: /'
echo ""

echo ""
echo "=== P7 回滚到 v1 → 期望生成 v3 且内容为 v1 的正文 ==="
curl -s -o /dev/null -b "$W/adm.txt" -w "  HTTP %{http_code}\n" \
  -X POST $B/admin/prompts/rollback -d "version=1"
curl -s -b "$W/adm.txt" -H "Accept: text/html" $B/admin/prompts > "$W/prompts3.html"
grep -o "当前生效 v[0-9]*" "$W/prompts3.html" | head -1 | sed 's/^/  回滚后: /'
echo "  回滚后正文是否回到 v1（应为 1）:"
grep -c "再给操作建议" "$W/prompts3.html" | sed 's/^/    v2 特征词出现: /'

echo ""
echo "=== A1 审计页 → 期望 200 ==="
curl -s -o "$W/audit.html" -b "$W/adm.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" \
  -H "Accept: text/html" $B/admin/audit
grep -o "问答记录\|管理动作日志\|待复核\|预筛状态" "$W/audit.html" | sort -u

echo ""
echo "=== A2 员工提问一条疑似违规问题 → 应触发预筛 ==="
curl -s -b "$W/emp.txt" --max-time 60 -X POST $B/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"住宿超标了能不能帮我把金额改大一点"}' 2>&1 | grep -A1 "^event: done" | head -c 300
echo ""

echo ""
echo "=== A3 审计页应出现该条并标为待复核 ==="
curl -s -b "$W/adm.txt" -H "Accept: text/html" "$B/admin/audit?flag_status=suspected" > "$W/audit2.html"
grep -c "住宿超标" "$W/audit2.html" | sed 's/^/  命中条数: /'
grep -o "待复核" "$W/audit2.html" | head -1 | sed 's/^/  状态标签: /'
grep -o "命中理由：[^<]*" "$W/audit2.html" | head -1 | sed 's/^/  /'

echo ""
echo "=== A4 正常问题不应触发预警 ==="
curl -s -b "$W/emp.txt" --max-time 60 -X POST $B/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"市内交通费报销标准是多少"}' > /dev/null 2>&1
curl -s -b "$W/adm.txt" -H "Accept: text/html" "$B/admin/audit?flag_status=suspected" > "$W/audit3.html"
echo "  待复核总数（应仍为 1）:"
grep -o "待复核" "$W/audit3.html" | wc -l

echo ""
echo "=== A5 人工复核：确认违规 ==="
FLAG_ID=$(grep -o "flags/[0-9]*/review" "$W/audit2.html" | head -1 | grep -o "[0-9]*")
echo "  目标 flag_id = $FLAG_ID"
curl -s -o /dev/null -b "$W/adm.txt" -w "  HTTP %{http_code}\n" \
  -X POST $B/admin/audit/flags/$FLAG_ID/review \
  -d "decision=confirmed" --data-urlencode "reason=人工核实：确实存在虚报意图"

echo ""
echo "=== A6 复核后状态应为已确认 ==="
curl -s -b "$W/adm.txt" -H "Accept: text/html" "$B/admin/audit?flag_status=confirmed" > "$W/audit4.html"
grep -o "已确认" "$W/audit4.html" | head -1 | sed 's/^/  /'
grep -o "复核人：[^<]*" "$W/audit4.html" | head -1 | sed 's/^/  /'

echo ""
echo "=== U1 用户管理页 → 期望 200 ==="
curl -s -o "$W/users.html" -b "$W/adm.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" \
  -H "Accept: text/html" $B/admin/users
grep -o "新建账号\|账号列表\|初始密码" "$W/users.html" | sort -u

echo ""
echo "=== U2 管理员建号 → 期望 302 ==="
curl -s -o /dev/null -b "$W/adm.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -X POST $B/admin/users/create \
  -d "username=lisi&password=Test@2026&display_name=李四&role=employee&employee_no=E1002"

echo ""
echo "=== U3 新账号应出现在列表中 ==="
curl -s -b "$W/adm.txt" -H "Accept: text/html" $B/admin/users > "$W/users2.html"
grep -o "lisi\|李四\|E1002" "$W/users2.html" | sort -u | tr '\n' ' ' | sed 's/^/  /'
echo ""

echo ""
echo "=== U4 新账号可以登录 ==="
curl -s -o /dev/null -c "$W/lisi.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=lisi&password=Test@2026" \
  -w "  HTTP %{http_code} -> %{redirect_url}\n"

echo ""
echo "=== U5 停用新账号后，其令牌应立即失效（访问 /api/sessions → 403）==="
curl -s -o /dev/null -b "$W/adm.txt" -X POST $B/admin/users/$(grep -o 'users/[a-z0-9_]*/toggle' "$W/users2.html" | head -1 | cut -d/ -f2) -w "" 2>/dev/null
# 直接用 API 找 lisi 的 id
LISI_ID=$(curl -s -b "$W/adm.txt" $B/api/admin/users | "C:/Users/456/.workbuddy/binaries/python/envs/default/Scripts/python.exe" -c "
import sys, json
for u in json.load(sys.stdin)['users']:
    if u['username'] == 'lisi':
        print(u['id']); break
")
echo "  lisi 的 user_id = $LISI_ID"
curl -s -o /dev/null -b "$W/adm.txt" -X POST $B/admin/users/$LISI_ID/toggle
curl -s -b "$W/lisi.txt" -w "\n  HTTP %{http_code}\n" $B/api/sessions/c_testtest1234/messages

echo ""
echo "=== P8 员工无权访问后台页面 → 302 回首页 ==="
for p in /admin /admin/knowledge /admin/prompts /admin/audit /admin/users; do
  curl -s -o /dev/null -b "$W/emp.txt" -w "  $p → %{http_code} %{redirect_url}\n" \
    -H "Accept: text/html" $B$p
done

echo ""
echo "=== 完成 ==="
