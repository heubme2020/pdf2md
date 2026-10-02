# -*- coding: utf-8 -*-
"""EDINET iXBRL/HTML → Markdown（**替代 `edinet_fetch.html_to_md`**）。

## 为什么要重写

现成的 `html_to_md`（`edinet_fetch.py:207`）在真文档上**只保住 10% 的表格结构**（[实测]，
用 XBRL 真值裁判量的）：3.2M 字符 HTML 吐成 108k 字符，**表格全被拍平**。
值召回 100% 而配对率 10% —— **数字一个没丢，但全部失去了归属**。
而归属正是 A1/A11 要的东西。

## 做法：按表格切片

    HTML ──切──▶ [文本段] [表格] [文本段] [表格] …
                    │        │
                    │        └─▶ html_tables 还原网格 → 真 Markdown 表
                    └─▶ HTMLParser 抽标题层级 / 段落

**关键在表格那一路**：`html_tables` 正确铺平 rowspan/colspan、解析 `△` 负数和 iXBRL 的
`scale`，所以出来的单元格内容和归属都是对的。文本段那一路只需要保留 h1–h6 的层级
（EDINET 的章节结构就写在里面：`第一部【企業情報】` → `第１【企業の概況】`）。

## 与「解析 PDF」的根本区别

PDF 那条路是**从版面反推结构**（所以会串行、会错位、要猜标题）。
这条路是**直接读结构** —— 层级、表格、单元格都是文档自带的。
"""
import re
from html.parser import HTMLParser

from html_tables import parse_tables

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_HEAD_RE = re.compile(r"^h([1-6])$")

# 台湾 MOPS 的章节结构载体（2026-09-29 实测）：
#   它**没有 `<h1>`–`<h6>`**（实测全为 0），而是用
#     <div id="BalanceSheet">  语义锚点  +  目录 <a href="#BalanceSheet">資產負債表</a>
#   比日本的"靠字号猜标题"更可靠 —— 层级是文档自己声明的。
#   这里把目录读成 `{锚点 → 标题}`，遇到对应 div 就发一个 `##`。
_ANCHOR_LINK_RE = re.compile(r'<a\s[^>]*href="#([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_SECTION_DIV_RE = re.compile(r'<div\s[^>]*id="([^"]+)"', re.I)


def _toc_titles(html):
    """从目录里抽 `{锚点: 标题}`。只认 `<a href="#X">`，别的链接忽略。"""
    out = {}
    for m in _ANCHOR_LINK_RE.finditer(html):
        anchor = m.group(1).strip()
        # 标题可能在嵌套的 span 里（`<span class="zh">資產負債表</span><span class="en">...`）——
        # **取中文那份**，没有就把整段文本剥标签。英文那份不取（正文是中文）。
        inner = m.group(2)
        zh = re.search(r'class="zh"[^>]*>(.*?)</span>', inner, re.S | re.I)
        text = zh.group(1) if zh else re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", "", text)
        if anchor and text and len(text) <= 40:
            out[anchor] = text
    return out
# 这些标签的内容是"机器可读的壳"，不是给人看的正文，丢掉
#   ix:header/ix:hidden/ix:references/ix:resources = inline XBRL 的元数据壳
#   （<ix:header> 里装 xbrli:context / xbrli:unit / xbrldi:*Member 等维度定义 ——
#    台湾 MOPS 的 iXBRL 把它们整段放在正文前，不丢掉就会漏成一大片
#    `2024-01-01` / `OrdinaryShareMember` 这类垃圾段落；EDINET 虽把 header 拆成独立文件、
#    但 body 里的 <ix:hidden> 隐藏事实同样该丢，两市场都适用）。
_DROP_TAGS = {"script", "style", "head", "meta", "link",
              "ix:header", "ix:hidden", "ix:references", "ix:resources"}


class _TextWalker(HTMLParser):
    """把一段（不含表格的）HTML 抽成 markdown 文本：标题按 h1–h6、其余按段落。

    `toc`：`{锚点: 标题}` —— 台湾 iXBRL 用它出章节（见 `_toc_titles`）。
    """

    def __init__(self, toc=None):
        super().__init__(convert_charrefs=True)
        self.toc = toc or {}
        self.out = []
        self._buf = []
        self._head = None
        self._drop = 0
        self._block = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in _DROP_TAGS:
            self._drop += 1
        elif t == "br":
            self._buf.append("\n")
        elif _HEAD_RE.match(t):
            self._flush()
            self._head = int(t[1])
        elif t == "div":
            # 台湾：`<div id="BalanceSheet">` → 按目录里的标题发一个 ##
            for k, v in attrs:
                if k.lower() == "id" and v in self.toc:
                    self._flush()
                    self.out.append("## " + self.toc[v])
                    break
            self._block = True
        elif t in ("p", "li", "tr", "td", "th"):
            self._block = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in _DROP_TAGS:
            self._drop = max(0, self._drop - 1)
        elif _HEAD_RE.match(t):
            self._flush()
            self._head = None

    def handle_data(self, data):
        if self._drop:
            return
        self._buf.append(data)

    def _flush(self):
        text = re.sub(r"[ \t　]+", " ", "".join(self._buf))
        text = re.sub(r"\n{2,}", "\n", text).strip()
        self._buf = []
        if not text:
            return
        if self._head:
            self.out.append("#" * self._head + " " + text.replace("\n", " "))
        else:
            self.out.append(text)

    def close(self):
        super().close()
        self._flush()


def _merge_signs(cells):
    """把「货币符号单独成格」并回数值格：SEC(Workiva) 把 `$` 单独放一个 td、数字放下一个，
    于是 `$` 和 `35,934` 被拆成两列，值匹配会失败。这里 `$`/`¥` 符号格并回下一格。
    ⚠️ 只并「纯符号格」—— 不动日本/台湾的 `△`（它可能和数字同格或另有语义）。"""
    out, i = [], 0
    while i < len(cells):
        c = cells[i]
        if c.strip() in ("$", "¥") and i + 1 < len(cells) and cells[i + 1].strip():
            out.append(c + cells[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return out


def _grid_to_md(grid):
    """网格 → Markdown 管道表。

    ⚠️ **合并单元格要去重**：`html_tables.build_grid` 为了让「任意 (行,列) 坐标都能取到
    它归属的单元格内容」，把 colspan/rowspan 覆盖到的每个位置都填了**同一个 Cell 对象**。
    直接打印就会出现 `| 回次 | 回次 |`。这里用**对象身份**（`is`）判断是不是同一格 ——
    比"值相同就合并"安全，因为相邻两个真单元格完全可能都是 `0`。
    """
    rows = []
    for row in grid:
        cells, prev = [], None
        for c in row:
            if c is prev:                 # 同一格横跨过来的 → 留空
                cells.append("")
            else:
                cells.append(((c.text if c else "") or "").replace("|", "\\|").strip())
            prev = c
        if any(cells):
            rows.append(_merge_signs(cells))
    if len(rows) < 2:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def table_to_md(table_html):
    """一段 `<table>…</table>` → Markdown。**这是本模块存在的理由。**"""
    mds = []
    for t in parse_tables(table_html):
        s = _grid_to_md(t["grid"])
        if s:
            mds.append(s)
    return "\n\n".join(mds)


def ixbrl_to_md(html, meta=None, min_table_rows=2):
    """iXBRL/HTML → Markdown。**日本 EDINET 与台湾 MOPS 共用这一个**（实测两者都吃得下）。

    `meta` 非空时在最前面写 YAML frontmatter（与 PDF 线保持一致的格式）。

    章节层级有两个来源，**互不干扰**：
      · 日本：`<h1>`–`<h6>`                                （`_TextWalker` 的 `_HEAD_RE` 分支）
      · 台湾：`<div id="…">` + 目录                          （`_toc_titles` + div 分支）
    实测量过：台湾的 iXBRL 里 `<h1>`–`<h6>` 出现 **0 次**，全靠锚点。
    """
    toc = _toc_titles(html)
    parts = []
    pos = 0
    for m in _TABLE_RE.finditer(html):
        seg = html[pos:m.start()]
        parts.extend(_text_parts(seg, toc))
        tmd = table_to_md(m.group(0))
        if tmd:
            parts.append(tmd)
        pos = m.end()
    parts.extend(_text_parts(html[pos:], toc))

    body = "\n\n".join(p for p in parts if p and p.strip())
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if not meta:
        return body
    from pdf_to_md import _build_frontmatter     # 复用 PDF 线那套，格式保持一致
    fm = _build_frontmatter(meta)
    return (fm + "\n" + body) if fm else body


def _text_parts(segment_html, toc=None):
    if not segment_html or not segment_html.strip():
        return []
    w = _TextWalker(toc)
    try:
        w.feed(segment_html)
        w.close()
    except Exception:  # noqa: BLE001
        return []
    return [x for x in w.out if x.strip()]


if __name__ == "__main__":
    import io
    import sys
    import zipfile

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.path.insert(0, ".")
    import edinet_xbrl as X

    key = ""
    for line in open(".env", encoding="utf-8"):
        if line.startswith("EDINET_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
    doc = sys.argv[1] if len(sys.argv) > 1 else "S100W29V"
    zf = zipfile.ZipFile(io.BytesIO(X.fetch_ixbrl_zip(doc, key)))
    html = "".join(zf.read(n).decode("utf-8", "replace") for n in sorted(zf.namelist())
                   if "honbun" in n.lower())
    md = ixbrl_to_md(html, {"exchange": "jpx", "stock_code": "6937"})
    print(f"HTML {len(html):,} 字符 → md {len(md):,} 字符")
    print(f"  ## {md.count(chr(10)+'## ')} ｜ ### {md.count(chr(10)+'### ')}"
          f"｜#### {md.count(chr(10)+'#### ')}｜管道表 {md.count('| --- |')}")
    print("\n—— 前 800 字 ——")
    print(md[:800])
