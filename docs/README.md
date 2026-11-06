# 项目文档交付索引 v1.1

整理日期：2026-11-06 ｜ 交付目录：`docs/`（v1.1 起文件平铺，取消 `deliverables-v1.0/` 子目录）

## 交付清单

| # | 需求 | 文件 | 说明 |
|---|---|---|---|
| 1 | 六人访谈记录 | `六人访谈记录.md` / `.json` / `.csv` | 6 人、25 轮问答、36 个事实编号；三格式同源生成。CSV 为 25 行 × 13 列，UTF-8 BOM，Excel 直接打开不乱码 |
| 2 | 基本简报等信息（4 个文件） | `项目基本简报-4文件.zip` | 解压得到 `项目基本简报/` 下 4 个 md；生成时已实际解压并做 SHA-256 一致性校验。4 个简报的独立文件也在本目录（`01-` ~ `04-`） |
| 3(1) | 典型事件采访记录 | `典型事件采访记录.txt` | 6 轮逐字稿（要求至少 2~3 轮），具体量化，纯文本可直接下载 |
| 3(2) | AI 整理纪要 | `典型事件AI整理纪要.docx` | AI 对逐字稿做时间线、量化三级分级、根因归纳与事实编号映射；含 4 处人工确认点 |
| 4 | 行业分析 | `行业分析文档.md` / `行业分析文档.docx` | 规模结构、政策合规、标杆企业、可比技术实践、与本项目差距、16 条来源清单 |
| 5 | 改造前后流程图 | `流程图说明.md`、`流程图-01-改造前-现状与典型事件.png` / `.svg`、`流程图-02-改造后-AI数智化.png` / `.svg` | As-Is 13 步 / 5 断点 / 典型事件时间线，To-Be 断点关闭方案与 AI-01~AI-10 落点；配说明文档与 `make_flowcharts.py` 生成脚本 |

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

生成时曾把 4 个文件解压到 `results/解压验证/` 并与原稿逐一做 SHA-256 比对，结果全部一致；v1.1 平铺整理后该验证副本已移除，如需复核可重新执行上述 `unzip` 后比对。

## 复现命令

在**项目根目录**执行：

```bash
# 重新生成两张流程图（SVG + PNG；PNG 需本机 Chrome / Edge，也可用 CHROME_PATH 指定）
python docs/make_flowcharts.py

# 重新生成访谈三格式
python docs/build_interviews.py

# 重新生成两份 docx 的中间 HTML（复用已验证的商务报告版式引擎）
python docs/build_html.py

# HTML → DOCX（需 workbuddy-docx 的 convert_doc.py，在 docs/ 目录下执行）
python <plugin>/skills/html-to-doc/scripts/convert_doc.py \
  --html 典型事件AI整理纪要.html \
  --output 典型事件AI整理纪要.docx \
  --require-page-furniture --no-verify
```

> `build_html.py` 复用的排版引擎位于 `output/上线交付运行手册-v1.0/html/build_html.py`。该目录属流水线工作区、被 `.gitignore` 排除、未随仓库分发；在全新克隆中需先恢复该引擎才能复现 docx 转换，脚本会在缺失时明确报错。

## 性质声明（所有文件通用）

- 六人访谈、典型事件采访、单号、人数与耗时均源自 `discovery-report.md` 的**合成演练访谈**，受访者与事件为演练创作，不代表任何真实企业经营事实。
- 「80 个岗位需求、重复澄清 22%」是源报告明确标注的**虚构基线**，所有文档均禁止将其作为 ROI 输入或验收目标；两张流程图中的 36 / 93 / 154 分钟等数字均为 B 级演练设定值，仅限流程示例。
- 行业分析中的规模、政策与上市公司数据为**公开信息**（核验日期 2026-11-06，来源分级 A/B/C/D 见正文第十一章），与项目演练数据严格分开；行业报告存在 CR3、机构数、服务人次等口径冲突，文档并列保留、未强行统一。
- 两份 docx 与两张流程图均为 AI 辅助整理 / 分析稿，标注「待验证」「未复核」的条目在客户书面确认前不构成开发承诺或验收依据；To-Be 图描述的是**目标态设计**，其中 AI-07 尚未接入主链路、AI-09 / AI-10 属二期。
- 工作区**无现成的可下载压缩包**，本 zip 与 4 个简报是依据现有材料与四来源框架（Buyer / Brief / Industry / Operator）整理生成；书面 Brief 原文与版本在源报告中仍标注为「待补」，不得由本包替代。

## 目录说明

v1.1 起全部交付文件平铺在 `docs/` 根目录，无子目录：

| 类别 | 文件 |
|---|---|
| 交付正文 | `六人访谈记录.*`、`典型事件采访记录.txt`、`典型事件AI整理纪要.docx`、`行业分析文档.md` / `.docx`、`01-` ~ `04-` 简报、`项目基本简报-4文件.zip` |
| 流程图 | `流程图说明.md`、两张 `.png` / `.svg` |
| 生成脚本 | `make_flowcharts.py`、`build_interviews.py`、`build_html.py` |
| 中间产物 | `典型事件AI整理纪要.html`、`行业分析文档.html`、`典型事件AI整理纪要.md`、`html_build_report.json` |
