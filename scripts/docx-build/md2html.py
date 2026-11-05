#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 peer-benchmark-proposal.md 转成打印友好的 HTML，供 html-to-doc 转换为 docx。"""
import os
import re
import html as htmllib

SRC = r"D:\shijian\attendance\peer-benchmark-proposal.md"
OUT = r"D:\shijian\attendance\output\docx-build\peer-benchmark.html"

CSS = """
<style>
  body { font-family: "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif;
         font-size: 10.5pt; line-height: 1.7; color: #1a1a1a; }
  h1 { font-size: 20pt; font-weight: 700; color: #12395B; margin: 0 0 14pt 0;
       border-bottom: 2px solid #12395B; padding-bottom: 8pt; }
  h2 { font-size: 15pt; font-weight: 700; color: #12395B; margin: 20pt 0 8pt 0;
       border-left: 4px solid #12395B; padding-left: 8pt; }
  h3 { font-size: 12.5pt; font-weight: 700; color: #1F5C8B; margin: 14pt 0 6pt 0; }
  h4 { font-size: 11pt; font-weight: 700; color: #333; margin: 10pt 0 4pt 0; }
  p  { margin: 0 0 8pt 0; text-align: justify; }
  table { border-collapse: collapse; width: 100%; margin: 10pt 0 14pt 0; font-size: 9pt; }
  th { background: #12395B; color: #ffffff; font-weight: 700;
       border: 1px solid #12395B; padding: 5pt 6pt; text-align: left; }
  td { border: 1px solid #B8C4CE; padding: 5pt 6pt; vertical-align: top; }
  tr:nth-child(even) td { background: #F2F6F9; }
  blockquote { margin: 8pt 0 12pt 0; padding: 8pt 12pt;
               background: #F5F7F9; border-left: 3px solid #7A9BB8; color: #33475B; }
  blockquote p { margin: 0 0 4pt 0; }
  pre { background: #F4F6F8; border: 1px solid #D8E0E6; border-radius: 3px;
        padding: 8pt 10pt; font-family: "Consolas", "Courier New", monospace;
        font-size: 8.5pt; line-height: 1.45; white-space: pre-wrap; margin: 8pt 0 12pt 0; }
  code { font-family: "Consolas", "Courier New", monospace; font-size: 9pt;
         background: #EEF2F5; padding: 1pt 3pt; }
  pre code { background: none; padding: 0; }
  ul, ol { margin: 6pt 0 10pt 0; padding-left: 20pt; }
  li { margin: 0 0 4pt 0; }
  hr { border: none; border-top: 1px solid #C9D4DC; margin: 16pt 0; }
  strong { font-weight: 700; }
  em { font-style: italic; color: #666; }
</style>
"""


def inline(text: str) -> str:
    """行内标记：先转义，再还原 code / bold / italic。"""
    text = htmllib.escape(text, quote=False)
    # 行内代码优先，避免其中的 * 被当作强调
    code_spans = []

    def stash(m):
        code_spans.append(m.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    for i, c in enumerate(code_spans):
        text = text.replace(f"\x00CODE{i}\x00", f"<code>{htmllib.escape(c, quote=False)}</code>")
    return text


def render(md: str) -> str:
    lines = md.split("\n")
    body = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # 代码块
        if line.strip().startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            body.append("<pre><code>" + htmllib.escape("\n".join(buf), quote=False) + "</code></pre>")
            continue

        # 分隔线
        if re.fullmatch(r"\s*---+\s*", line):
            body.append("<hr/>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            body.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = []
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(("th", header))
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(("td", cells))
                i += 1
            tbl = ["<table>"]
            for idx, (tag, cells) in enumerate(rows):
                t = "th" if idx == 0 else "td"
                tbl.append("<tr>" + "".join(f"<{t}>{inline(c)}</{t}>" for c in cells) + "</tr>")
            tbl.append("</table>")
            body.append("\n".join(tbl))
            continue

        # 引用块
        if line.strip().startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = []
            for b in buf:
                if b.strip():
                    inner.append(f"<p>{inline(b.strip())}</p>")
            body.append("<blockquote>" + "".join(inner) + "</blockquote>")
            continue

        # 列表
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            body.append("<ul>" + "".join(f"<li>{inline(t.strip())}</li>" for t in items) + "</ul>")
            continue

        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            body.append("<ol>" + "".join(f"<li>{inline(t.strip())}</li>" for t in items) + "</ol>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 普通段落（合并连续行）
        buf = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^\s*(#{1,6}\s|>|[-*]\s|\d+\.\s|```|\||---)", lines[i]
        ):
            buf.append(lines[i].strip())
            i += 1
        body.append(f"<p>{inline(' '.join(buf))}</p>")

    return "\n".join(body)


def main():
    with open(SRC, encoding="utf-8") as f:
        md = f.read()
    content = render(md)
    doc = (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/>'
        "<title>派遣岗位需求单版本交接与人工复核 · 同行方案</title>"
        f"{CSS}</head><body>\n{content}\n</body></html>"
    )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"OK -> {OUT} ({len(doc)} chars)")


if __name__ == "__main__":
    main()
