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
    raise ValueError(f"不支持的交易所: {exchange}")
