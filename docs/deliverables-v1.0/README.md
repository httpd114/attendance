# 项目文档交付索引 v1.0

整理日期：2026-11-06 ｜ 交付目录：`docs/deliverables-v1.0/`

## 交付清单

| # | 需求 | 文件 | 说明 |
|---|---|---|---|
| 1 | 六人访谈记录 | `六人访谈记录.md` / `.json` / `.csv` | 6 人、25 轮问答、36 个事实编号；三格式同源生成。CSV 为 25 行 × 13 列，UTF-8 BOM，Excel 直接打开不乱码 |
| 2 | 基本简报等信息（4 个文件） | `项目基本简报-4文件.zip` | 解压得到 `项目基本简报/` 下 4 个 md；已实际解压并做 SHA-256 一致性校验。单独文件亦在 `briefs/` |
| 3(1) | 典型事件采访记录 | `典型事件采访记录.txt` | 6 轮逐字稿（要求至少 2~3 轮），具体量化，纯文本可直接下载 |
| 3(2) | AI 整理纪要 | `典型事件AI整理纪要.docx` | AI 对逐字稿做时间线、量化三级分级、根因归纳与事实编号映射；含 4 处人工确认点 |
| 4 | 行业分析 | `行业分析文档.md` / `行业分析文档.docx` | 规模结构、政策合规、标杆企业、可比技术实践、与本项目差距、16 条来源清单 |

## 简报包下载与解压缩

zip 内含 4 个文件，无密码、无目录层级外的多余文件：

```text
项目基本简报/
├── 01-项目基本简报.md          （Brief：目标、范围、预算、验收三条硬杠、G0 缺口）
├── 02-业务决策方说明.md        （Buyer：顾明岚，预算 32 人时、决策/否决权矩阵）
├── 03-一线操作说明.md          （Operator：林亦舟，流程、两类时间口径、异常样本）
└── 04-行业与可比实践简报.md    （Industry：公开事实、合规底线、可比案例、适用性差异）
```

Windows PowerShell：

```powershell
Expand-Archive -Path "项目基本简报-4文件.zip" -DestinationPath ".\解压输出" -Force
Get-ChildItem ".\解压输出\项目基本简报" | Select-Object Name, Length
```

Git Bash / macOS / Linux：

```bash
unzip -l 项目基本简报-4文件.zip          # 查看 4 个条目
unzip -o 项目基本简报-4文件.zip -d 解压输出
```

本次已在 `results/解压验证/` 实际解压并把 4 个文件与 `briefs/` 原件做 SHA-256 比对，全部一致。

## 复现命令

```bash
# 重新生成访谈三格式
python sources/build_interviews.py

# 重新生成两份 docx 的中间 HTML（复用已验证的商务报告版式引擎）
python html/build_html.py

# HTML → DOCX（需 workbuddy-docx 的 convert_doc.py）
python <plugin>/skills/html-to-doc/scripts/convert_doc.py \
  --html html/典型事件AI整理纪要.html \
  --output 典型事件AI整理纪要.docx \
  --require-page-furniture --no-verify
```

## 性质声明（所有文件通用）

- 六人访谈、典型事件采访、单号、人数、耗时均源自 `docs/discovery-report.md` 的**合成演练访谈**，受访者与事件为演练创作，不代表任何真实企业经营事实。
- 「80 个岗位需求、重复澄清 22%」是源报告明确标注的**虚构基线**，所有文档均禁止将其作为 ROI 输入或验收目标。
- 行业分析中的规模、政策与上市公司数据为**公开信息**（核验日期 2026-11-06，来源分级 A/B/C/D 见正文第十一章），与项目演练数据严格分开；行业报告存在 CR3、机构数、服务人次等口径冲突，文档并列保留、未强行统一。
- 两份 docx 为 AI 辅助整理 / 分析稿，标注「待验证」「未复核」的条目在客户书面确认前不构成开发承诺或验收依据。
- 工作区**无现成的可下载压缩包**，本 zip 与 4 个简报是依据现有材料与四来源框架（Buyer / Brief / Industry / Operator）整理生成；书面 Brief 原文与版本在源报告中仍标注为「待补」，不得由本包替代。

## 目录说明

| 目录 | 内容 |
|---|---|
| `briefs/` | zip 内 4 个简报的独立文件 |
| `sources/` | 生成脚本与 docx 的 md 源稿 |
| `html/` | 转换用单文件 HTML 与排版脚本 |
| `results/` | 解压验证输出、HTML 生成报告 |
