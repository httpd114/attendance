#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 Markdown → 打印友好 HTML（支持表格/代码/引用/列表/标题）。"""
import os
import re
import sys
import html as htmllib

CSS = """
<style>
  body { font-family: "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif;
         font-size: 10.5pt; line-height: 1.68; color: #1a1a1a; }
  h1 { font-size: 19pt; font-weight: 700; color: #12395B; margin: 0 0 6pt 0;
       border-bottom: 2px solid #12395B; padding-bottom: 8pt; }
  h2 { font-size: 14pt; font-weight: 700; color: #12395B; margin: 18pt 0 7pt 0;
       border-left: 4px solid #12395B; padding-left: 8pt; }
  h3 { font-size: 12pt; font-weight: 700; color: #1F5C8B; margin: 12pt 0 5pt 0; }
  p  { margin: 0 0 7pt 0; text-align: justify; }
  table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt 0; font-size: 9pt; }
  th { background: #12395B; color: #ffffff; font-weight: 700;
       border: 1px solid #12395B; padding: 5pt 6pt; text-align: left; }
  td { border: 1px solid #B8C4CE; padding: 5pt 6pt; vertical-align: top; }
  tr:nth-child(even) td { background: #F4F7FA; }
  blockquote { margin: 8pt 0 11pt 0; padding: 8pt 12pt;
               background: #F5F7F9; border-left: 3px solid #7A9BB8; color: #33475B; }
  blockquote p { margin: 0 0 4pt 0; }
  pre { background: #F4F6F8; border: 1px solid #D8E0E6; padding: 8pt 10pt;
        font-family: "Consolas", monospace; font-size: 8.5pt;
        white-space: pre-wrap; margin: 8pt 0 12pt 0; }
  code { font-family: "Consolas", monospace; font-size: 9pt; background: #EEF2F5; padding: 1pt 3pt; }
  ul, ol { margin: 5pt 0 10pt 0; padding-left: 20pt; }
  li { margin: 0 0 3pt 0; }
  hr { border: none; border-top: 1px solid #C9D4DC; margin: 14pt 0; }
  em { font-style: italic; color: #666; }
</style>
"""


def inline(text):
    text = htmllib.escape(text, quote=False)
    spans = []

    def stash(m):
        spans.append(m.group(1))
        return f"\x00C{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    for i, c in enumerate(spans):
        text = text.replace(f"\x00C{i}\x00", f"<code>{htmllib.escape(c, quote=False)}</code>")
    return text


def render(md):
    lines = md.split("\n")
    body, i, n = [], 0, len(md.split("\n"))
    while i < n:
        line = lines[i]

        if line.strip().startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            body.append("<pre>" + htmllib.escape("\n".join(buf), quote=False) + "</pre>")
            continue

        if re.fullmatch(r"\s*---+\s*", line):
            body.append("<hr/>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            body.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        if line.strip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = [("th", [c.strip() for c in line.strip().strip("|").split("|")])]
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                rows.append(("td", [c.strip() for c in lines[i].strip().strip("|").split("|")]))
                i += 1
            tbl = ["<table>"]
            for idx, (_t, cells) in enumerate(rows):
                t = "th" if idx == 0 else "td"
                tbl.append("<tr>" + "".join(f"<{t}>{inline(c)}</{t}>" for c in cells) + "</tr>")
            tbl.append("</table>")
            body.append("\n".join(tbl))
            continue

        if line.strip().startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = [f"<p>{inline(b.strip())}</p>" for b in buf if b.strip()]
            body.append("<blockquote>" + "".join(inner) + "</blockquote>")
            continue

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

        if not line.strip():
            i += 1
            continue

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
    src, out, title = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(src, encoding="utf-8") as f:
        md = f.read()
    doc = (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/>'
        f"<title>{htmllib.escape(title)}</title>{CSS}</head><body>\n"
        f"{render(md)}\n</body></html>"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"OK -> {out} ({len(doc)} chars)")


if __name__ == "__main__":
    main()
