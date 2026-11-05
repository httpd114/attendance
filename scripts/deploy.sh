#!/usr/bin/env bash
# ====================================================================
# 单实例启动脚本（生产请改用进程管理器 / 容器编排托管）
# --------------------------------------------------------------------
#   CHECK_ONLY=1 bash scripts/deploy.sh   只做上线前预检，不启动服务
#   SKIP_BUILD=1 bash scripts/deploy.sh   跳过构建自检直接启动
#
# ⚠️ 本脚本不会替你保管密钥。生产环境用密钥管理服务注入环境变量，
#    不要把 LLM_API_KEY 写进命令行历史或提交到版本库。
# ====================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

HOST_VAL="${HOST:-127.0.0.1}"
PORT_VAL="${PORT:-8000}"
DB_PATH_VAL="${DB_PATH:-$ROOT/data/chat.db}"

echo "=== 上线前预检 ==="

# 1) 必需密钥：宁可拒绝启动，也不能带着开发默认值上线。
#    （当前代码尚未内置该强校验，因此由脚本先兜住，见 docs/上线前检查清单 A4）
missing=0
for var in LLM_BASE_URL LLM_API_KEY LLM_MODEL JWT_SECRET AGENT_GATE_SECRET; do
  if [ -z "${!var:-}" ]; then
    echo "  [缺失] 环境变量 $var 未设置"
    missing=1
  fi
done
if [ -n "${JWT_SECRET:-}" ] && [ "$JWT_SECRET" = "dev-only-change-me-in-production" ]; then
  echo "  [不安全] JWT_SECRET 仍是开发默认值"
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  echo "预检未通过：拒绝启动。请补齐密钥后重试。"
  exit 1
fi
echo "  [OK] 必需密钥已配置"

# 2) 目录与资源
mkdir -p "$ROOT/data"
for p in "$ROOT/src/attendance_agent/static" "$ROOT/src/attendance_agent/templates" \
         "$ROOT/data/报销制度.md"; do
  if [ ! -e "$p" ]; then
    echo "  [缺失] $p"
    exit 1
  fi
done
echo "  [OK] 静态资源与制度文档就位"

# 3) 启动前对现有数据库做一致性备份（含 WAL，不能用 cp 直接拷）
if [ -f "$DB_PATH_VAL" ]; then
  BACKUP="$ROOT/data/chat.db.bak.$(date +%Y%m%d%H%M%S)"
  "$PYTHON" - "$DB_PATH_VAL" "$BACKUP" <<'PYEOF'
import sqlite3
import sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(src)
out = sqlite3.connect(dst)
with out:
    con.backup(out)          # 在线一致性备份，WAL 内容一并落盘
out.close()
con.close()
print(f"  [OK] 数据库已备份：{dst}")
PYEOF
else
  echo "  [OK] 首次启动，将由应用自动建表"
fi

# 4) 构建自检
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  PYTHON="$PYTHON" bash scripts/build.sh
else
  echo "  [跳过] 构建自检（SKIP_BUILD=1）"
fi

if [ "${CHECK_ONLY:-0}" = "1" ]; then
  echo "预检通过（CHECK_ONLY=1，未启动服务）。"
  exit 0
fi

# 5) 启动。生产应由反向代理终止 TLS，应用只监听本机。
echo "=== 启动服务：http://$HOST_VAL:$PORT_VAL ==="
exec "$PYTHON" -m uvicorn attendance_agent.main:app \
  --host "$HOST_VAL" --port "$PORT_VAL"
