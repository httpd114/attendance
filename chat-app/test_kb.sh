#!/usr/bin/env bash
# 知识库多文档管理测试（第 2 批验收）
B="http://127.0.0.1:8000"
W="D:/shijian/attendance/chat-app/.tmp_test"
mkdir -p "$W"

# 管理员登录
curl -s -o /dev/null -c "$W/admin.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=admin&password=Admin@2026"

echo "=== K1 知识库页面 → 期望 200 ==="
curl -s -o "$W/kb.html" -b "$W/admin.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" \
  -H "Accept: text/html" $B/admin/knowledge
grep -o "制度知识库\|上传制度文档\|已上传文档" "$W/kb.html" | sort -u

echo ""
echo "=== K2 后台首页 → 期望 200 ==="
curl -s -o "$W/home.html" -b "$W/admin.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" \
  -H "Accept: text/html" $B/admin
grep -o "制度文档\|检索切片\|待复核问答\|今日问答" "$W/home.html" | sort -u

echo ""
echo "=== K3 文档列表 API → 期望 200 ==="
curl -s -b "$W/admin.txt" -w "\n  HTTP %{http_code}\n" $B/api/admin/kb/documents

echo ""
echo "=== K4 上传 Markdown 文档 → 期望 302 + flash ==="
cat > "$W/差旅补充规定.md" <<'EOF'
# 八、补充规定（2026年10月新增）

## 8.1 高铁票改签

高铁票因行程变更改签的，改签费由员工承担，但一次行程内的改签费不超过 200 元。
改签后需在原车票报销单上附改签单，未附改签单的按原票金额报销。

## 8.2 市内交通

市内交通费实报实销，需提供正规发票或电子行程单。单次超过 100 元的，
需在报销单备注栏注明事由。

## 8.3 超时住宿

因航班取消导致延期的住宿，按出差地协议酒店标准报销，不受出差天数上限限制，
但需提供航空公司出具的航班取消证明。
EOF
curl -s -o /dev/null -b "$W/admin.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -X POST $B/admin/knowledge/upload \
  -F "file=@$W/差旅补充规定.md"

echo ""
echo "=== K5 上传后文档列表（应含 2 份）==="
curl -s -b "$W/admin.txt" $B/api/admin/kb/documents | head -c 900
echo ""

echo ""
echo "=== K6 检索新文档内容（高铁改签）→ 应命中补充规定 ==="
curl -s -b "$W/admin.txt" --max-time 60 -X POST $B/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"高铁票改签费最多能报多少？"}' 2>&1 | grep -A1 "^event: citations" | tail -1 | head -c 700
echo ""

echo ""
echo "=== K7 切片预览页 → 期望 200 ==="
DOCID=$(curl -s -b "$W/admin.txt" $B/api/admin/kb/documents | "C:/Users/456/.workbuddy/binaries/python/envs/default/Scripts/python.exe" -c "
import json,sys
d=json.load(sys.stdin)
for x in d['documents']:
    if '补充' in x['filename']: print(x['id']); break
")
echo "  补充规定文档 ID: $DOCID"
curl -s -o "$W/chunks.html" -b "$W/admin.txt" -w "  HTTP %{http_code}  size=%{size_download}\n" \
  -H "Accept: text/html" "$B/admin/knowledge/documents/$DOCID/chunks"
grep -o "高铁票改签\|市内交通\|超时住宿" "$W/chunks.html" | sort -u

echo ""
echo "=== K8 手动重建索引 → 期望 302 ==="
curl -s -o /dev/null -b "$W/admin.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -X POST $B/admin/knowledge/reindex

echo ""
echo "=== K9 员工访问知识库页 → 期望 302 跳登录/首页（不得暴露后台）==="
curl -s -o /dev/null -c "$W/emp.txt" -H "Accept: text/html" -X POST $B/login \
  -d "tenant_code=default&username=zhangsan&password=Emp@2026"
curl -s -o /dev/null -b "$W/emp.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -H "Accept: text/html" $B/admin/knowledge

echo ""
echo "=== K10 删除补充规定 → 期望 302 ==="
curl -s -o /dev/null -b "$W/admin.txt" -w "  HTTP %{http_code}  Location: %{redirect_url}\n" \
  -X POST $B/admin/knowledge/documents/$DOCID/delete

echo ""
echo "=== K11 删除后检索（应不再命中补充规定）==="
curl -s -b "$W/admin.txt" --max-time 60 -X POST $B/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"高铁票改签费最多能报多少？"}' 2>&1 | grep -A1 "^event: citations" | tail -1 | head -c 700
echo ""
