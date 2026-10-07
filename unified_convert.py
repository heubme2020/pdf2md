# -*- coding: utf-8 -*-
"""统一转换入口 —— 按交易所分发到正确的转换器。

Part 2「几个交易所统一一个代码」的落地。四地四种原件格式收敛成两个入口：

    convert(exchange, 路径, meta)           —— A股 PDF 路径 + 离线基线/重转（读文件字节再调 convert_data）
    convert_data(exchange, 字节/文本, meta)  —— 采集线（crawl_*.py 手里是 fetch 到的字节/文本，不必先落盘）

    日本 jpx        → EDINET iXBRL ZIP 字节  → `edinet_html.ixbrl_to_md`
    台湾 twse/tpex  → MOPS iXBRL HTML 字节   → `edinet_html.ixbrl_to_md`
    韩国 ksc/koe/knx → DART XML 文本         → `dart_fetch.dart_xml_to_md`
    A股 sse/szse/bj → 巨潮 PDF 路径          → `pdf_to_md.convert`

输出统一格式（frontmatter + md）。离线基线/指标线（pdf2md_baseline.py）和 VPS 采集线
（crawl_*.py）都从这里走，保证「线上怎么转、离线怎么转」是同一份代码、同一套口径。

    from unified_convert import convert, convert_data
    md = convert("jpx", "Z:/datasinking-data/raw/jp_xbrl/7203/S100XXX.zip", meta={...})
    md = convert_data("twse", html_bytes, meta={...})
"""
import edinet_html
from pdf_to_md import convert as _pdf_convert
# 日本 iXBRL ZIP → 正文各章 HTML 的章节提取，与 crawl_edinet 共用同一份。
# 已移到叶子模块 edinet_xbrl（不 import 任何爬虫），避免「爬虫 import unified_convert」时的循环 import。
from edinet_xbrl import chapter_html as _jp_chapter_html

PDF_EXCHANGES = {"sse", "szse", "bj"}
TW_EXCHANGES = {"twse", "tpex"}
KR_EXCHANGES = {"ksc", "koe", "knx"}


def convert(exchange, source_path, meta=None):
    """按交易所把一份原件（**文件路径**）转成 md。

    source_path 语义按交易所：
      · sse/szse/bj → PDF 文件路径（pdf_to_md 直接吃路径，不读字节）
      · jpx/twse/tpex/ksc/koe/knx → 原件文件路径（读字节后走 convert_data）
    """
    if exchange in PDF_EXCHANGES:
        return _pdf_convert(source_path, None, meta=meta)
    with open(source_path, "rb") as f:
        return convert_data(exchange, f.read(), meta)


def convert_data(exchange, data, meta=None):
    """按交易所把原件**字节/文本**转成 md。采集线（crawl_*.py）走这个 —— 手里就是
    fetch 到的字节/文本，不必先落盘再读回来。

    data 语义按交易所（bytes 或 str 都接受，内部按需 decode）：
      · jpx        → EDINET iXBRL ZIP 字节
      · twse/tpex  → MOPS iXBRL HTML 字节
      · ksc/koe/knx → DART XML 文本（fetch_document_xml 已 decode）
    """
    if exchange == "jpx":
        return edinet_html.ixbrl_to_md(_jp_chapter_html(data), meta)
    if exchange in TW_EXCHANGES:
        html = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        return edinet_html.ixbrl_to_md(html, meta)
    if exchange in KR_EXCHANGES:
        from dart_fetch import dart_xml_to_md
        xml = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        return dart_xml_to_md(xml, meta)
    if exchange == "lse":
        return _uk_zip_to_md(data, meta)
    if exchange == "nse":
        xml = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        return _in_xbrl_to_md(xml, meta)
    raise ValueError(f"不支持的交易所: {exchange}")


def _in_xbrl_to_md(xml_text, meta=None):
    """印度 NSE XBRL → 结构化财务 MD（简单版：抽 facts 概念→值，过滤元数据噪声）。

    NSE XBRL 是纯 XBRL，facts 形如 <ind-as:Revenue contextRef=...>123</ind-as:Revenue>。
    简单版按概念聚合值、按出现次数降序；标签映射/表格化以后补（这是「结构化财务」不是「叙事全文」）。
    """
    import re
    _NOISE = {"identifier", "explicitMember", "footnote", "segment", "scenario"}
    facts = re.findall(r"<([\w-]+):([A-Za-z][\w]*) [^>]*>([^<]+)</\1:\2>", xml_text)
    agg = {}
    for _p, concept, val in facts:
        if concept in _NOISE:
            continue
        agg.setdefault(concept, []).append(val.strip())
    lines = []
    for concept, vals in sorted(agg.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- **{concept}**: " + " / ".join(dict.fromkeys(vals))[:200])
    body = "\n".join(lines)
    if not meta:
        return body
    from pdf_to_md import _build_frontmatter
    fm = _build_frontmatter(meta)
    return (fm + "\n" + body) if fm else body


def _uk_zip_to_md(zip_bytes, meta=None):
    """英国 ESEF ZIP → MD（v14：表格+文本结构化）。

    ESEF 是 iXBRL（Workiva 生成），无 <h1-h4>，标题在 div/p、表格在 <table>。
    v13 基础版是「每 </div> 一行」平铺；v14 改成：<table>→markdown 表格，其余→段落，
    剥 ix: 标签保文本。数字逐字（不 OCR、不改写）。
    """
    import io
    import re
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    xhtmls = [n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
    if not xhtmls:
        return ""
    main = max(xhtmls, key=lambda n: len(zf.read(n)))
    html = zf.read(main).decode("utf-8", "replace")
    return _uk_esef_to_md(html)


def _uk_esef_to_md(html):
    """英国 ESEF XHTML → MD（v14）。"""
    import re
    # 0) 只取 body，丢掉 head（style/meta/title）
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    body = m.group(1) if m else html

    # 1) table 占位 → markdown
    tables = []

    def _tbl(mt):
        tables.append(mt.group(0))
        return f"\n@@TBL{len(tables) - 1}@@\n"
    body = re.sub(r"<table[^>]*>.*?</table>", _tbl, body, flags=re.S | re.I)

    def _table_to_md(tb):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tb, flags=re.S | re.I)
        md_rows = []
        for r in rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S | re.I)
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
            md_rows.append(cells)
        md_rows = [r for r in md_rows if any(c for c in r)]
        if not md_rows:
            return ""
        ncol = max(len(r) for r in md_rows)
        lines = ["| " + " | ".join((r + [""] * ncol)[:ncol]) + " |" for r in md_rows]
        return "\n".join([lines[0], "| " + " | ".join(["---"] * ncol) + " |"] + lines[1:])

    for i, tb in enumerate(tables):
        body = body.replace(f"@@TBL{i}@@", "\n" + _table_to_md(tb) + "\n")

    # 2) 剥 ix: 标签、div/p/tr → 换行、删其余标签
    body = re.sub(r"</?(?:ix|ixt|ixn):[^>]+>", "", body)
    body = re.sub(r"</(?:p|div|tr|h[1-6])>", "\n", body, flags=re.I)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = (body.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
    # 3) 折叠空行、去空白行
    out = [l.rstrip() for l in body.split("\n") if l.strip()]
    return "\n".join(out)
