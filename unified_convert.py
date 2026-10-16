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
        from dart_fetch import dart_xml_to_md, _decode_dart_xml
        xml = _decode_dart_xml(data) if isinstance(data, bytes) else data
        return dart_xml_to_md(xml, meta)
    if exchange == "lse":
        return _uk_to_md(data, meta)
    if exchange == "nse":
        xml = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        return _in_xbrl_to_md(xml, meta)
    if exchange == "br":
        xml = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        return br_xml_to_md(xml, meta)
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


def _uk_to_md(data, meta=None):
    """英国 ESEF → MD（v14）。两种形态：
      · ZIP 包（Workiva 打包多份 XHTML）→ 取最大 XHTML 成员
      · 单文件 XHTML（DirectUpload 直传，如 AIM 小公司）→ 直接转
    入口由 convert_data 的 lse 分支调用，data 恒为 bytes。"""
    if isinstance(data, bytes) and data[:2] == b"PK":
        return _uk_zip_to_md(data, meta)
    html = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return _uk_esef_to_md(html)


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
    # 0) 先提取 CSS 字号映射（在 head 的 <style> 里，取 body 前必须先拿，否则丢了）
    font_map = _uk_font_map(html)
    # 0.5) 只取 body，丢掉 head（style/meta/title）
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    body = m.group(1) if m else html

    # 0.5) 整块剥掉 XBRL 元数据壳（ix:header 里装着 ix:references/ix:resources →
    #       contexts/units/dimensions，即 LEI 码 + 期间日期 + ifrs-full:XXXMember）。
    #       v14 之前只剥 ix: 标签、留着文本，导致全文开头一大片这类噪声（1.36M 字里大部分是它）。
    body = re.sub(r"<ix:header\b[^>]*>.*?</ix:header>", "", body, flags=re.S | re.I)
    body = re.sub(r"<ix:hidden\b[^>]*>.*?</ix:hidden>", "", body, flags=re.S | re.I)

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

    # 2) 剥 ix: 标签，再按格式识别标题（pdf2htmlEX vs Workiva）
    body = re.sub(r"</?(?:ix|ixt|ixn):[^>]+>", "", body)
    if 'class="t m0' in body:
        body = _uk_divs_to_md(body)      # pdf2htmlEX：div fs/ff 字号
    elif '<strong' in body and 'charStyle' not in body:
        body = _uk_strong_to_md(body)    # other 格式：strong 加粗标记标题
    else:
        body = _uk_class_to_md(body, font_map)   # Workiva / S class：CSS class 字号
    # 全量解 HTML 实体：旧版只手写了 6 个（&nbsp;/&amp;/&lt;/&gt;/&#39;/&quot;），
    # 漏掉 &#8217;（'）、&#233;（é）、&#57535; 等数字实体 → 正文残留一堆实体/乱码。
    import html as _html
    body = _html.unescape(body).replace("\xa0", " ")
    # 3) 折叠空行、去空白行
    out = [l.rstrip() for l in body.split("\n") if l.strip()]
    return "\n".join(out)


def _uk_divs_to_md(body):
    """英国 ESEF 的 pdf2htmlEX 绝对定位 div → MD：按 fs/ff 识别标题 + 正文段落合并。

    英国 XHTML 是 pdf2htmlEX 输出，每行文本一个 `<div class="t m0 x.. h.. y.. ff.. fs.. ...">`，
    绝对定位。`fs` 是字号编号（每个文件独立），`ff` 是字体编号。
    标题 = fs < 正文 fs，或 fs==正文但 ff 不同（字号同但字体不同，如正文 fs0 的文件）。
    正文按 y 坐标排序，y 间距大的断开成新段落，小的合并成一段（原每行一个 div）。
    """
    import re
    from collections import Counter
    div_re = re.compile(r'<div class="t ([^"]*)"[^>]*>(.*?)</div>', re.S)

    def _fs(cls):
        m = re.search(r"fs([0-9a-f]+)", cls)
        return int(m.group(1), 16) if m else None

    def _ff(cls):
        m = re.search(r"ff([0-9a-f]+)", cls)
        return int(m.group(1), 16) if m else None

    def _y(cls):
        m = re.search(r"y([0-9a-f]+)", cls)
        return int(m.group(1), 16) if m else 0

    items = []
    for m in div_re.finditer(body):
        cls, inner = m.group(1), m.group(2)
        txt = re.sub(r"<[^>]+>", "", inner).strip()
        if not txt:
            continue
        items.append((_y(cls), _fs(cls), _ff(cls), txt))
    if not items:
        return body
    fs_list = [fs for _, fs, _, _ in items if fs is not None]
    if not fs_list:
        return body
    body_fs = Counter(fs_list).most_common(1)[0][0]
    ff_list = [ff for _, _, ff, _ in items if ff is not None]
    body_ff = Counter(ff_list).most_common(1)[0][0] if ff_list else None
    # 页眉/页脚是每页重复的（同一文本出现几十次），真标题只出现 1-2 次 —— 用出现次数排除。
    txt_count = Counter(txt for _, _, _, txt in items)

    items.sort(key=lambda t: t[0])
    lines = []
    para = []
    prev_y = None
    for y, fs, ff, txt in items:
        is_head = (fs is not None and fs < body_fs) or (
            fs == body_fs and body_ff is not None and ff is not None and ff != body_ff
        )
        is_head = is_head and txt_count[txt] <= 2
        if is_head and len(txt) <= 80 and not txt.isdigit():
            if para:
                lines.append(" ".join(para))
                para = []
            lines.append("## " + txt)
        else:
            gap = (y - prev_y) if prev_y is not None else 999
            if gap > 20:  # y 间距大 → 新段落
                if para:
                    lines.append(" ".join(para))
                    para = []
            para.append(txt)
        prev_y = y
    if para:
        lines.append(" ".join(para))
    out = "\n".join(lines)
    # 清理残留标签（非 t class 的 div/p/tr → 换行，其余剥掉）
    out = re.sub(r"</?(?:div|p|tr|h[1-6])[^>]*>", "\n", out, flags=re.I)
    out = re.sub(r"<[^>]+>", "", out)
    return out


def _uk_font_map(html):
    """从 XHTML 的 <style> 提取所有 `.class { font-size: Xpx/pt }` → 字号。
    兼容三种英国格式的字号 class：charStyle-xxx（Workiva）、Sx（Credit Agricole ivk 库）等。"""
    import re
    css = re.search(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)
    out = {}
    if not css:
        return out
    for m in re.finditer(r"\.([\w-]+)\s*\{([^}]*)\}", css.group(1)):
        fs = re.search(r"font-size:\s*([\d.]+)\s*(?:px|pt)", m.group(2))
        if fs:
            out[m.group(1)] = float(fs.group(1))
    return out


def _uk_class_to_md(body, font_map):
    """英国 Workiva / S-class 格式 → MD：div/span 的 class 里取最大字号，字号 > 正文字号的识别为标题。

    英国 ESEF 有 pdf2htmlEX（div fs，走 _uk_divs_to_md）和另外两种（Workiva 的 charStyle、
    Credit Agricole 的 Sx class）—— 后两者的字号都在 CSS 的 `.class { font-size }`，
    这里统一按「div 里出现的最大字号」判断标题。
    """
    import re
    from collections import Counter
    divs = re.findall(r'<div class="([^"]*)"[^>]*>(.*?)</div>', body, re.S)
    items = []
    for cls, inner in divs:
        txt = re.sub(r"<[^>]+>", "", inner).strip()
        if not txt:
            continue
        fs = 0.0
        for tok in cls.split():
            fs = max(fs, font_map.get(tok, 0.0))
        for sp in re.findall(r'class="([^"]*)"', inner):  # span 的字号 class（Workiva）
            for tok in sp.split():
                fs = max(fs, font_map.get(tok, 0.0))
        items.append((fs, txt))
    if not items:
        return body
    body_fs = Counter(fs for fs, _ in items).most_common(1)[0][0]
    lines = []
    for fs, txt in items:
        if fs > body_fs and len(txt) <= 80:
            lines.append(f"## {txt}")
        else:
            lines.append(txt)
    return "\n".join(lines)


def _uk_strong_to_md(body):
    """英国 other 格式（table/strong，无 t m0/charStyle/S-class）→ MD：strong 里的文本 → 标题。

    这类文件的标题用 <strong> 加粗标记（封面、目录、章节标题），正文在 table/td 里。
    """
    import re

    def _repl(m):
        txt = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return f"## {txt}\n" if txt else ""

    body = re.sub(r"<strong[^>]*>(.*?)</strong>", _repl, body, flags=re.S)
    body = re.sub(r"</?(?:div|p|tr|td|h[1-6])[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    return body


# ---- 巴西 CVM DFP XML → MD ----
_BR_REPORTS = [
    ("BalancoPatrimonialAtivo", "Balanço Patrimonial — Ativo"),
    ("BalancoPatrimonialPassivo", "Balanço Patrimonial — Passivo"),
    ("DemonstracaoResultado", "Demonstração do Resultado"),
    ("DemonstracaoResultadoAbrangente", "Demonstração do Resultado Abrangente"),
    ("DemonstracaoFluxoCaixa", "Demonstração dos Fluxos de Caixa"),
    ("DemonstracaoMutacoesPatrimonioLiquido", "Demonstração das Mutações do Patrimônio Líquido"),
    ("DemonstracaoValorAdicionado", "Demonstração do Valor Adicionado"),
]


def _fmt_num(s):
    """金额加千分位（CVM 的 EscalaMoeda=2 通常是千，原样转千分位让数字可读）。"""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return f"{int(s):,}"
    except ValueError:
        return s


def _contas_to_md(body):
    """一组 <Conta> → markdown 表格，值列**动态识别**：
    主要报表的值标签是 UltimoExercicio/Penultimo/Antenpenultimo（三年），
    DMPL 权益变动表的值标签是 CapitalSocialIntegralizado/ReservasLucro 等权益科目。"""
    import re
    contas = re.findall(r"<Conta>(.*?)</Conta>", body, re.S)
    if not contas:
        return ""
    _FIXED = {"ContaFixa", "CodigoConta", "DescricaoConta"}
    cols = []
    for c in contas:
        for t in re.findall(r"<(\w+)>", c):
            if t not in _FIXED and t not in cols:
                cols.append(t)
    header = ["Código", "Conta"] + cols
    lines = ["| " + " | ".join(header) + " |",
             "|" + "---|" * len(header)]
    for c in contas:
        code = re.search(r"<CodigoConta>(.*?)</CodigoConta>", c)
        desc = re.search(r"<DescricaoConta>(.*?)</DescricaoConta>", c)
        row = [code.group(1) if code else "", desc.group(1) if desc else ""]
        for t in cols:
            v = re.search(r"<" + t + r">(.*?)</" + t + r">", c)
            row.append(_fmt_num(v.group(1) if v else ""))
        # 描述空 + 所有值空 = CVM 的账户占位符，跳过
        if not row[1].strip() and not any(x.strip() for x in row[2:]):
            continue
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def br_xml_to_md(xml_text, meta=None):
    """巴西 CVM DFP XML → MD：个体/合并两套报表，每套是「账户-值」表格。"""
    import re
    out = []
    company = re.search(r"<RazaoSocialEmpresa>(.*?)</RazaoSocialEmpresa>", xml_text)
    period = re.search(r"<DataReferencia>(.*?)</DataReferencia>", xml_text)
    esc = re.search(r"<EscalaMoeda>(.*?)</EscalaMoeda>", xml_text)
    head = []
    if company:
        head.append(f"Empresa: {company.group(1).strip()}")
    if period:
        head.append(f"Data de referência: {period.group(1).strip()}")
    if esc:
        head.append(f"Escala moeda: {esc.group(1).strip()} (1=unidade, 2=milhar, 3=milhão)")
    if head:
        out.append("> " + " ｜ ".join(head))

    for grp, label in (("DfIndividuais", "Demonstrações Individuais"),
                       ("DfConsolidadas", "Demonstrações Consolidadas")):
        m = re.search(r"<" + grp + r">(.*?)</" + grp + r">", xml_text, re.S)
        if not m:
            continue
        body = m.group(1)
        out.append(f"# {label}")
        for tag, title in _BR_REPORTS:
            rm = re.search(r"<" + tag + r">(.*?)</" + tag + r">", body, re.S)
            if not rm:
                continue
            tbl = _contas_to_md(rm.group(1))
            if tbl:
                out.append(f"\n## {title}\n\n{tbl}")
    return "\n".join(out)
