#!/usr/bin/env bash
# ====================================================================
# 本地 / CI 构建自检
# --------------------------------------------------------------------
# 刻意不依赖：运行中的服务、网络、API Key、数据库内容。
# 因此可以在任何干净克隆上直接跑，也算「上一次能跑通」的最小证据。
#
#   bash scripts/build.sh
#   PYTHON=/path/to/python bash scripts/build.sh
# ====================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

pass=0
fail=0
step() {
  local label="$1"; shift
  printf '\n=== %s ===\n' "$label"
  if "$@"; then
    printf '  [OK] %s\n' "$label"
    pass=$((pass + 1))
  else
    printf '  [FAIL] %s\n' "$label" >&2
    fail=$((fail + 1))
  fi
}

step "必需工程文件齐全" bash -c '
  for f in README.md LICENSE CHANGELOG.md CONTRIBUTING.md requirements.txt \
           pyproject.toml .gitignore config/default.yaml \
           docs/design.md docs/api.md src/attendance_agent/__init__.py; do
    test -f "$f" || { echo "缺少 $f"; exit 1; }
  done'

step "全量 Python 语法检查" "$PYTHON" -c '
import py_compile, sys
from pathlib import Path
root = Path(".")
files = (sorted(root.glob("src/**/*.py")) + sorted(root.glob("tests/*.py"))
         + [root/"examples/demo.py", root/"scripts/migrate.py"])
for f in files:
    py_compile.compile(str(f), doraise=True)
print(f"  {len(files)} 个文件语法通过")'

step "运行依赖可用" "$PYTHON" -c '
import importlib.util
needed = ["fastapi", "uvicorn", "jwt", "bcrypt", "pytest", "requests", "numpy"]
missing = [m for m in needed if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit(f"缺少依赖 {missing}，请先执行 pip install -r requirements.txt，"
                     f"或用 PYTHON=/path/to/python 指定已装依赖的解释器")
print("  核心依赖齐全")'

step "路由与页面完整性自检" "$PYTHON" tests/check_routes.py
step "30 条评测用例集自检" "$PYTHON" tests/validate_eval_cases.py
step "核心单测（路径/提示词/脱敏/门禁）" "$PYTHON" -m pytest tests/test_core.py -q
step "Agent 三条轨迹 + 8 条硬门禁单测" "$PYTHON" examples/demo.py

printf '\n==============================================================\n'
printf '  构建自检：%d 通过 / %d 失败\n' "$pass" "$fail"
printf '==============================================================\n'
if [ "$fail" -gt 0 ]; then
  exit 1
fi
printf '全部通过。集成回归（需服务与密钥）：\n'
printf '  bash tests/test_auth.sh && bash tests/test_kb.sh && bash tests/test_admin3.sh\n'
