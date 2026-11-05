#!/usr/bin/env bash
# 后台第 3、4 批验收：提示词模板 + 对话审计 + 用户管理
B="http://127.0.0.1:8000"
W="$(cd "$(dirname "$0")" && pwd)/.tmp_test"
PYTHON="${PYTHON:-python}"   # 可用 PYTHON=/path/to/python 覆盖
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
# 从版本历史表格里抓"vN"标记（表格中每行一个），排除标题里的"版本历史"
grep -o "v[0-9]\+" "$W/prompts2.html" | sort -u | tr '\n' ' ' | sed 's/^/  版本号: /'
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
# ⭐ 统计卡片里的"待复核"数值才是标记数，
#    直接 grep "待复核" 会把标题、筛选项、标签一起数进去（之前误报为 4）。
echo "  待复核标记数（应仍为 1）:"
curl -s -b "$W/adm.txt" -H "Accept: text/html" "$B/admin/audit?flag_status=suspected" > "$W/audit3.html"
grep -A3 'stat-label">待复核' "$W/audit3.html" | grep -o '[0-9]\+' | head -1 | sed 's/^/    /'
echo "  正常问题是否被误标:"
grep -c "市内交通费报销标准是多少" "$W/audit3.html" | sed 's/^/    出现在待复核列表次数: /'

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
# ⚠️ 中文字段必须用 --data-urlencode，不能用 -d。
#    Git Bash 下 `curl -d "display_name=李四"` 发出的是系统 ANSI 码页(GBK)
#    字节，服务端按 latin-1 解读 → 存进去是乱码。
#    这是**测试脚本**的编码问题，不是服务端缺陷——
#    浏览器发的就是标准 UTF-8，用 --data-urlencode 验证已确认存储正确。
#
# 用户名加时间戳：保证每次跑都是新账号，不会撞上上一轮遗留的同名账号
# （撞上会返回 409，导致后面的登录测试误报为失败）。
U_NAME="lisi_$(date +%H%M%S)"
echo "  本轮测试用户名: $U_NAME"
curl -s -o /dev/null -b "$W/adm.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -X POST $B/admin/users/create \
  --data-urlencode "username=$U_NAME" \
  --data-urlencode "password=Test@2026" \
  --data-urlencode "display_name=李四" \
  --data-urlencode "role=employee" \
  --data-urlencode "employee_no=E1002"

echo ""
echo "=== U2b 中文名存储校验（hex 应为 UTF-8，不能是 latin-1 二次编码）==="
curl -s -b "$W/adm.txt" $B/api/admin/users | U_NAME="$U_NAME" "$PYTHON" -c "
import sys, json, os
users = json.load(sys.stdin)['users']
target = os.environ.get('U_NAME', '')
for u in users:
    if u['username'] == target:
        h = u['display_name'].encode('utf-8').hex()
        expect = '李四'.encode('utf-8').hex()
        print(f'  display_name = {u[\"display_name\"]}  hex = {h}')
        print(f'  期望 hex = {expect}')
        print('  结论:', '✓ 编码正确' if h == expect else '✗ 编码错误')
        break
else:
    print('  未找到账号', target)
"

echo ""
echo "=== U3 新账号应出现在列表中 ==="
curl -s -b "$W/adm.txt" -H "Accept: text/html" $B/admin/users > "$W/users2.html"
grep -o "$U_NAME\|李四\|E1002" "$W/users2.html" | sort -u | tr '\n' ' ' | sed 's/^/  /'
echo ""

echo ""
echo "=== U4 新账号可以登录 ==="
curl -s -o /dev/null -c "$W/new.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=$U_NAME&password=Test@2026" \
  -w "  HTTP %{http_code} -> %{redirect_url}\n"

echo ""
echo "=== U5 停用新账号后，其令牌应立即失效（访问 /api/sessions → 403）==="
LISI_ID=$(curl -s -b "$W/adm.txt" $B/api/admin/users | U_NAME="$U_NAME" "$PYTHON" -c "
import sys, json, os
for u in json.load(sys.stdin)['users']:
    if u['username'] == os.environ['U_NAME']:
        print(u['id']); break
")
echo "  新账号 user_id = $LISI_ID"
curl -s -o /dev/null -b "$W/adm.txt" -X POST $B/admin/users/$LISI_ID/toggle
curl -s -b "$W/new.txt" -w "\n  HTTP %{http_code}\n" $B/api/sessions/c_testtest1234/messages

echo ""
echo "=== P8 员工无权访问后台页面 → 302 回首页 ==="
for p in /admin /admin/knowledge /admin/prompts /admin/audit /admin/users; do
  curl -s -o /dev/null -b "$W/emp.txt" -w "  $p → %{http_code} %{redirect_url}\n" \
    -H "Accept: text/html" $B$p
done

echo ""
echo "=== 完成 ==="
