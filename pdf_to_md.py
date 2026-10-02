# -*- coding: utf-8 -*-
"""
完整公告 PDF → Markdown 转换器

原理:
  年报/季报的排版信号:
    - 章节标题(第一节/第二节...) : 黑体 + 大字号(>=12pt)
    - 正文                      : 宋体 ~10.6pt
    - 表格内文字                : <=9pt (小字号)
    - 页眉/页脚                 : 顶部/底部固定位置, 过滤
  表格用 pymupdf find_tables 检测(文本策略, 支持三线表), 渲染为 markdown 表格。

相比旧版的可读性改进:
  1. 标题误判修复: 长句(>40字)或以句末标点结尾的文字不再当标题
  2. 正文段落合并: 消除 PDF 换行把一句话拆成两半的断句问题
  3. 同行文本合并: 表格散落的单元格按「同一行」用空格对齐

LLM 友好增强:
  - 开头 YAML frontmatter: 注入 stock_code/stock_name/report_period/doc_type/title,
    下游 LLM 直接拿到文档身份与报告期, 无需从正文猜。
  - 每张表格前补表题/单位行(caption): 明确「这张表是什么、数值单位是什么」,
    避免 LLM 把「万元」当「元」读成 10000 倍。
"""

import re

import fitz  # pymupdf

from table_detect import cluster_rows, detect_hlines, extract_table, table_blocks

# 华康 DF 系字体(台湾 MOPS 常见)会每页刷 "MuPDF error: unknown cid font type",
# 全量采集时能把日志刷到 GB 级 —— 关掉底层告警, 业务层已用 classify_md 判正文可用性。
try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:  # noqa: BLE001
    pass

# 解析器版本号: 每次改转换逻辑 +1, 爬虫据此识别旧版本数据并重转
#
# ⚠️ 这个数字有两个已知问题（2026-09-27 查明，**尚未修** —— 等指标体系落地后一并处理）：
#
# 1) v9 是**歧义版本号**：磁盘上这份代码同时含 `_detect_tables_lines` 和
#    `_detect_tables_text`(:307) 的合并调用 —— 即 `待办清单.md` 判定「为 1.5% 通过率
#    引入 6.3% 错误、1.78x 重复，不划算」的那版 v9b。它和纯线条版 v9 共用数字 9，
#    于是 `bench_quality_v9_jp.json` 与 `bench_quality_v9b_jp.json` 在库里无法区分，
#    线上任何一份 md 也答不出「它是哪个算法产的」。
#
# 2) 这个号码**只管到 A 股**：日台韩在各自 crawler 里另有一个独立数字
#    （crawl_twse.py:46=3 / crawl_edinet.py:30=2 / crawl_dart.py:33=2），
#    改 pdf_to_md 不会自动重转它们，必须手动 bump。
#
# ⚠️ bump 这个数字 = **触发 A 股全量重转**（33 万份，见 Z 盘存档规模）。
#    所以不要顺手改它 —— 必须先有指标证明改动是净胜。
# v10（2026-09-28）：**表格提取不再丢字** —— `table_detect.extract_table` 里
#   原来有一句 `if ci is None: continue`，把"落不进任何列"的文本**静默丢弃**。
#   日股实测：整列标签被丢掉（`負債合計` → `債合計`），数字被截断（`73,0`）、粘连（`13,0508,824`）。
#   修法：① 列边界扩展到覆盖块内**全部**文字；② 落在外面的词归**最近的列**，绝不丢弃。
#   裁判（EDINET XBRL 真值）判定 **净胜**：配对率 88.4%→91.7%、串行错位 8.5%→4.7%、逐份 +80/-0。
#
# ⚠️ v9 是**空号**：实测磁盘上 v9 与线上 v8 的产出**逐字节相同**（9/9 份），
#    所以 v9 从没带来过行为变化。真正的改动从 v10 算起。
PARSER_VERSION = 10  # v10: 表格提取不丢字（列边界覆盖全部文字 + 落列外归最近列）

# 各市场官方披露源 —— 写进 md 的 frontmatter，让**署名随数据走**
# （用户把 md 转走/贴到别处时，来源信息跟着走，而不只是留在网站上）。
# 与 worker/src/index.ts 的 EXCHANGE_SOURCE、website/lib/sources.ts 保持一致。
# 台湾 MOPS 属政府资料开放授权（要求显名）、韩国 DART 与日本 EDINET 规约亦要求标注来源。
SOURCE_BY_EXCHANGE = {
    "sse": "巨潮资讯网", "szse": "巨潮资讯网", "bj": "巨潮资讯网",
    "ksc": "DART", "koe": "DART", "knx": "DART",
    "jpx": "EDINET",
    "twse": "公開資訊觀測站 MOPS", "tpex": "公開資訊觀測站 MOPS",
    "hkex": "HKEXnews", "sgx": "SGX",
}


def source_of(exchange):
    """交易所代码 -> 官方披露源（未知返回 None，frontmatter 里就不写这行）"""
    return SOURCE_BY_EXCHANGE.get(exchange)

# 章节标题: 字号阈值
HEADING_SIZE = 12.0
# 页眉/页脚过滤
HEADER_Y = 55

# 子标题编号模式: 一、 （一） 1、 (数字序号只用顿号, 避免把 "13.45" 等数值当标题)
SUBHEAD_RE = re.compile(
    r"^[\s　]*([一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|[0-9]+、)\s*\S"
)

# 日文章节标题模式: 第X【...(如 第１【企業の概況】 / 第一部【企業情報】)
JP_SECTION_RE = re.compile(r"^第[^【]{1,5}【")

# A股年报章节标题模式: 第X节/第X章(如 第一节释义 / 第十节财务报告), 标准格式不靠字体
CN_SECTION_RE = re.compile(r"^第[一二三四五六七八九十0-9]+[节章]")

# 相邻行合并的行距阈值(pt): 小于此值视为同一段落换行
LINE_GAP = 22.0

# 表格标题(单位行)识别阈值(pt): 表格上方的文本与其间距小于此值视为该表的表题/单位
CAPTION_GAP = 30.0


def _span_text(block):
    """合并文本块的所有 span 文本"""
    parts = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            parts.append(span["text"])
    return "".join(parts).strip()


def _max_size(block):
    return max((s.get("size", 0) for line in block.get("lines", []) for s in line.get("spans", [])), default=0)


def _is_page_number(text):
    """判断是否为页码: 纯数字 或 'N / M' 格式"""
    t = text.strip()
    if not t:
        return False
    if t.isdigit():
        return True
    return bool(re.fullmatch(r"\d+\s*/\s*\d+", t))


def _is_heading(text, spans):
    """判断文本块是否为章节标题: 黑体 + 大字号 + 短句 + 非标点结尾"""
    if len(text) > 40:  # 长句(如董事会声明)不是标题
        return False
    if text[-1] in "。；，、．.,，;":  # 句末标点结尾的不是标题
        return False
    if JP_SECTION_RE.match(text.strip()) or CN_SECTION_RE.match(text.strip()):
        # 日文「第X【…】」/ A股「第X节」章节标题, 标准格式不管字体直接认
        return True
    for s in spans:
        font = s.get("font", "")
        if s.get("size", 0) >= HEADING_SIZE and (
            "黑" in font or "Hei" in font or "hei" in font
            or "Goth" in font or "ゴシック" in font or "ゴ" in font  # 日文标题用 Gothic
        ):
            return True
    return False


def _in_tables(bbox, table_bboxes):
    """判断文本块是否落在某个表格区域内"""
    for tb in table_bboxes:
        if bbox[1] >= tb[1] - 2 and bbox[3] <= tb[3] + 2 and bbox[0] >= tb[0] - 2 and bbox[2] <= tb[2] + 2:
            return True
    return False


def _table_to_md(grid):
    """表格数据(二维数组) -> markdown 表格字符串"""
    if not grid:
        return ""
    data = [[(c or "").replace("\n", " ").strip() for c in row] for row in grid]
    data = [row for row in data if any(row)]
    if not data:
        return ""
    ncol = max(len(r) for r in data)
    for r in data:
        while len(r) < ncol:
            r.append("")
    # 去掉全空列(线条法切列常产生多余的空列)
    cols = [i for i in range(ncol) if any(r[i].strip() for r in data)]
    data = [[r[i] for i in cols] for r in data]
    ncol = len(cols)
    if ncol == 0:
        return ""
    lines = []
    header = data[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * ncol) + " |")
    for row in data[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _need_space(a, b):
    """合并两段文本时, 判断是否需要加空格(数字/英文边界)"""
    if not a or not b:
        return False
    return a[-1].isascii() and b[0].isascii()


# 阅读顺序策略 —— **可被外部改写，用于版本对照**（见 pdf2md_referee.py 的变体机制）。
#   "y"       原行为：只按 y0 排序（多栏页会左右交错）
#   "natural" 不排序，用 PyMuPDF 的块顺序
#   "column"  按 x0 找栏间缝，栏内再按 y0
ORDER_STRATEGY = "y"


def _column_split(elements):
    """按 x0 把元素分栏：找最大的那个横向空隙当栏间缝。返回分界 x，判断不出返回 None。

    ⚠️ 必须有「空隙要够大」这一条：单栏文档内部也有 x 差异，不加约束会被误切成两栏，
    结果比原来还糟。阈值取页宽的 15%（经验值，待对照数据定）。
    """
    xs = sorted(e[3] for e in elements if e[3] is not None)
    if len(xs) < 4:
        return None
    span = xs[-1] - xs[0]
    if span <= 0:
        return None
    width, mid = max((xs[i + 1] - xs[i], (xs[i] + xs[i + 1]) / 2)
                     for i in range(len(xs) - 1))
    return mid if width >= 0.15 * span else None


def _merge(elements):
    """同行合并(表格多列) + 段落合并(断句), 标题/表格元素不参与合并"""
    merged = []
    for el in elements:
        if not merged:
            merged.append(el)
            continue
        y0, kind, text = el
        py0, pkind, ptext = merged[-1]
        if pkind != "p" or kind != "p":
            merged.append(el)
            continue
        if abs(y0 - py0) < 5.0:  # 同一行多列: 空格连接
            sep = " " if _need_space(ptext, text) else ""
            merged[-1] = [py0, "p", ptext + sep + text]
        elif y0 - py0 < LINE_GAP and ptext[-1] not in "。！？；：!?;:":
            # 相邻行且上一行无句末标点: 视为同一段落断句, 合并
            sep = " " if _need_space(ptext, text) else ""
            merged[-1] = [y0, "p", ptext + sep + text]
        else:
            merged.append(el)
    return merged


def _yaml_str(v):
    """YAML 字符串转义(双引号), None/空 返回 None"""
    if v is None or str(v) == "":
        return None
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _build_frontmatter(meta):
    """元数据 -> YAML frontmatter 块; 无元数据返回空串

    meta 键: stock_code/stock_name/report_period/announcement_date/doc_type/title/source
    frontmatter 供下游 LLM 直接读取文档身份/报告期/披露日期/单位上下文, 无需再从正文猜。
    """
    if not meta:
        return ""
    keys = [
        ("stock_code", "stock_code"),
        ("stock_name", "stock_name"),
        ("report_period", "report_period"),
        ("announcement_date", "announcement_date"),
        ("doc_type", "doc_type"),
        ("title", "title"),
        ("source", "source"),
    ]
    lines = ["---"]
    for meta_key, yaml_key in keys:
        v = _yaml_str(meta.get(meta_key))
        if v is not None:
            lines.append(f'{yaml_key}: "{v}"')
    if len(lines) == 1:
        return ""
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _is_caption(text):
    """判断文本是否像表题/单位行: 含「单位」, 或短句(<=40字)且非句末标点结尾"""
    if "单位" in text:
        return True
    return len(text) <= 40 and text[-1] not in "。；，、．.,，;:："


# 单位行: 年报财务指标表开头几乎都有「单位：元 / 单位:元 (币种：人民币)」; 日文是「単位：千円」
UNIT_RE = re.compile(r"^\s*(单位|単位)\s*[:：]")


def _mark_units(body):
    """把「单位：元 …」开头的行标成引用块(> 单位：元), 让 LLM 明确数值单位。

    find_tables 对 A 股三线表检出率低, 财务指标表常被展平成一行「单位：元 本期 上期 …」。
    这里按「行首 = 单位：」识别, 把这些单位/表头行显式标注出来, 避免 LLM 把万元当元。
    """
    out_lines = []
    for line in body.split("\n"):
        if UNIT_RE.match(line):
            out_lines.append("> " + line.strip())
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _find_caption(table, elements):
    """表格标题/单位行: 取表格上方紧邻的 caption 文本(给 LLM 提供表名与单位)

    仅当垂直间距 <= CAPTION_GAP 且文本像表题时才算, 避免把正文/残留表内行误当表题。
    """
    best_text, best_y = None, None
    for el in elements:
        kind, payload = el[1], el[2]
        if kind == "table":
            continue
        text = payload if isinstance(payload, str) else ""
        if not text:
            continue
        text = text.strip()
        if not _is_caption(text):
            continue
        gap = table["bbox"][1] - el[0]
        if 0 < gap <= CAPTION_GAP and (best_y is None or gap < best_y):
            best_text, best_y = text, gap
    return best_text


def _detect_tables_lines(page):
    """线条法检测三线表/网格表, 返回 [{bbox: (x0,y0,x1,y1), grid: [[str]]}]"""
    hlines = detect_hlines(page)
    if len(hlines) < 4:
        return []
    rows = cluster_rows(hlines)
    blocks = table_blocks(rows)
    tables = []
    for b in blocks:
        grid = extract_table(page, b)
        if len(grid) < 2 or not grid or len(grid[0]) < 2:
            continue  # 至少 2 行 2 列
        if not any(any(c.strip() for c in row) for row in grid):
            continue  # 无文本 = 装饰线, 不是表
        top = b[0][0]
        bottom = b[-1][0]
        left = min(x0 for _, segs in b for x0, _ in segs)
        right = max(x1 for _, segs in b for _, x1 in segs)
        tables.append({"bbox": (left, top, right, bottom), "grid": grid})
    return tables


def _bbox_overlap(a, b, tol=2.0):
    """两个 bbox 是否明显重叠 —— 用来判断"同一张表被两种策略各检测到一次"。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix = min(ax1, bx1) - max(ax0, bx0)
    iy = min(ay1, by1) - max(ay0, by0)
    if ix <= tol or iy <= tol:
        return False
    inter = ix * iy
    area_a = max(0.0, (ax1 - ax0)) * max(0.0, (ay1 - ay0))
    return area_a > 0 and inter / area_a > 0.5   # 覆盖了线条法那张表的过半面积


def _detect_tables_text(page, min_cols=2, min_rows=2):
    """文本策略检测**无框线**表格（PyMuPDF 的 find_tables）。

    为什么必须有这条：线条法（`table_detect`）只认有横线的表 —— A股三线表那种。
    但**日本 EDINET / 台湾 MOPS 的财报表格往往没有框线**（靠空白对齐），
    线条法一无所获，整段糊成连在一起的文本。

    2026-09-22 实测（日本样本，各取前 8 页）：
        文档A  lines=3  text=8   （5 页线条法漏掉）
        文档B  lines=0  text=8   （全 8 页线条法漏掉）
    → 这正是日台会计恒等式通过率只有 2% 的直接原因。

    ⚠️ **代码里原有注释说「find_tables 在 pymupdf>=1.28 已不可用」是错的**，
    实测 1.28.2 上 `page.find_tables(strategy=...)` 完全可用。

    代价：文本策略会误检（多栏正文可能被当成表），所以调用方要**和线条法合并去重**，
    并且由基准的 A1/A2 指标来验证它到底有没有帮上忙、有没有引入噪声。
    """
    try:
        found = page.find_tables(strategy="text")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in getattr(found, "tables", []) or []:
        try:
            grid = t.extract()
        except Exception:  # noqa: BLE001
            continue
        if not grid or len(grid) < min_rows:
            continue
        # ⚠️ **不能拿第一行判断列数**（第一版就是这么写的，把整张资产负债表筛掉了）：
        # 第 10 页那张真·资产负债表 56x4，首行却是 ['', '', '', 'EDINET提出書類']
        # —— 那是 EDINET 的页眉残留，不是表头，只有 1 个非空格，于是被当成"只有 1 列"拒了。
        # 改成看整张表的列数（find_tables 自己给的），并跳过全空的前导行再判断。
        if getattr(t, "col_count", 0) < min_cols:
            continue
        rows = [[(c or "") for c in row] for row in grid]
        rows = [r for r in rows if any(c.strip() for c in r)]      # 丢掉全空行
        if len(rows) < min_rows:
            continue
        ncols = max((sum(1 for c in r if c.strip()) for r in rows), default=0)
        if ncols < min_cols:
            continue
        out.append({"bbox": tuple(t.bbox), "grid": rows})
    return out


def _detect_tables(page):
    """表格检测：**线条法 + 文本法合并**（不是"找不到才回退"）。

    为什么是合并而不是回退：线条法常常只在**部分页**命中（实测日本文档A 8 页里只中 3 页），
    若"有结果就不看文本法"，剩下 5 页的表格照样全丢。
    合并时把与线条法结果重叠的文本表剔掉，避免同一张表出现两次。

    ⚠️ 2026-10-01 教训：曾改成「逐页回退」（本页线条法有结果就跳过 find_tables），
    结果日本 XBRL 值召回从 96.5% 掉到 73.3%（-23 点）—— 因为附注里**大量无框线表和线表同页**，
    回退把它们全丢了。find_tables 在三线表页的「流|動資産合計」垃圾只是**外观污染**（正确值仍在），
    为外观丢实值不划算，故回滚为合并+去重。真要修垃圾，应该去改 `_bbox_overlap` 的去重，
    或过滤 find_tables 的错切表，而不是整个跳过 find_tables。
    """
    line_tables = _detect_tables_lines(page)
    text_tables = _detect_tables_text(page)
    out = list(line_tables)
    for t in text_tables:
        if any(_bbox_overlap(t["bbox"], lt["bbox"]) for lt in line_tables):
            continue
        out.append(t)
    return out


def convert(pdf_path, out_path, page_range=None, meta=None, table_detector=None):
    """转换整份公告为 markdown

    meta: 可选 {stock_code, stock_name, report_period, announcement_date, doc_type, title},
          用于在 md 开头生成 YAML frontmatter(LLM 友好)。

    table_detector: 可选函数 `page -> [{bbox, grid}]`，替换默认的线条法+文本法表格检测。
          这是 GPU 版（gpu_convert）的接入点 —— 传入 TATR 检测器，其余文字/标题/渲染逻辑复用。
    """
    doc = fitz.open(pdf_path)
    pages = doc if page_range is None else [doc[i] for i in page_range]
    md = []
    detector = table_detector or _detect_tables

    for page in pages:
        page_h = page.rect.height
        # 1. 检测表格(线条法 + 文本法合并；见 _detect_tables 的说明。
        #    ⚠️ 这里原来写着「find_tables 在 pymupdf>=1.28 已不可用」——**那是错的**，
        #    实测 1.28.2 上完全可用，而且正是它救了日台的无框线表格。)
        tables = detector(page)
        table_bboxes = [t["bbox"] for t in tables]

        # 2. 收集文本块(排除表格区域、页眉页脚)
        elements = []  # [y0, kind, text]
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue  # 跳过图像
            bbox = block.get("bbox", (0, 0, 0, 0))
            y0 = bbox[1]
            text = _span_text(block)
            if not text:
                continue
            # 过滤页眉/页脚(但章节标题「第X节/第X【…】」常在页首 y0<HEADER_Y, 不滤)
            if (y0 < HEADER_Y or y0 > page_h - 60) and not (
                JP_SECTION_RE.match(text) or CN_SECTION_RE.match(text)
            ):
                continue
            if _in_tables(bbox, table_bboxes):
                continue
            if y0 > page_h - 75 and _is_page_number(text):
                continue  # 过滤底部页码(如 "1" "2" "1 / 15")
            spans = [s for line in block.get("lines", []) for s in line.get("spans", [])]
            if _is_heading(text, spans):
                kind = "h2"
            # ⚠️ 修复（2026-10-02，原「尚未修」）：h3 之前只看正则+长度**不看字号**，
            #    把正文句子也标成 ###（客户反馈「章节切片错误」的根因）。实测（A股年报）：
            #    真子标题「一、公司信息」字号 12、假标题正文「（一）载有公司负责人…」字号 9、
            #    清单项「1、同时按照…」字号 11 —— 所以加「任一 span 字号 >= HEADING_SIZE」，
            #    只认大字号的真标题。之前试过的 `len<=20` 会误丢「（二）公司优先股股东总数和…」
            #    这类长真标题，字号判断比长度判断稳。
            elif (len(text) <= 40 and SUBHEAD_RE.match(text)
                  and any(s.get("size", 0) >= HEADING_SIZE for s in spans)):
                kind = "h3"
            else:
                kind = "p"
            # 第 4 位是 x0 —— **只有排序用**，排序完立刻剥掉（`_merge` 只吃 3 元组）
            elements.append([y0, kind, text, bbox[0]])

        # 3. 表格作为元素
        for t in tables:
            elements.append([t["bbox"][1], "table", t, t["bbox"][0]])

        # 4. 排序 + 合并 + 渲染
        #
        # ⚠️ **这里是「文字乱序」的根因（2026-09-28 用外部裁判定位）。**
        #    原来只按 y0 排序、从不看 x —— 多栏/表格页上左右栏的块会被按 y 交错拼起来，
        #    于是「数字都在、科目名也都在，但不在同一行」。
        #    实测（日股 98 份，XBRL 真值裁判）：**`value_elsewhere` 占被测配对的 8.5%，
        #    是第二名的 3.4 倍** —— 就是这一行造成的。
        if ORDER_STRATEGY == "natural":
            pass                       # 用 PyMuPDF 给的块顺序（marker 默认做法，声称优于学习模型）
        elif ORDER_STRATEGY == "column":
            mid = _column_split(elements)
            if mid is None:
                elements.sort(key=lambda e: e[0])
            else:
                elements.sort(key=lambda e: (0 if (e[3] or 0) < mid else 1, e[0]))
        else:                          # "y" —— 原行为，只按 y0
            elements.sort(key=lambda e: e[0])

        triples = [e[:3] for e in elements]   # 剥回 3 元组，下游（_merge/_find_caption）不用改
        raw_elements = triples  # 合并前的原始元素, 供 _find_caption 找表题(合并会吞掉短文本)
        merged = _merge(triples)
        for y0, kind, payload in merged:
            if kind == "h2":
                md.append(f"\n## {payload}")
            elif kind == "h3":
                md.append(f"\n### {payload}")
            elif kind == "table":
                # 表格前补表题/单位行(LLM 友好: 明确这张表是什么、数值单位是什么)
                caption = _find_caption(payload, raw_elements)
                if caption:
                    caption = caption.replace("\n", " ").strip()
                    caption = re.sub(r"\s+", " ", caption)
                    if len(caption) > 80:
                        caption = caption[:80] + "…"
                    md.append(f"**{caption}**")
                md.append(_table_to_md(payload["grid"]))
                md.append("")
            else:
                # 普通段落紧凑排列: 段落间单换行, 不再每段强制空行(v5 空行压缩)
                md.append(payload)

    body = "\n".join(md)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = _mark_units(body)
    out = _build_frontmatter(meta) + body
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(out)
    return out


if __name__ == "__main__":
    import sys

    pdf = sys.argv[1] if len(sys.argv) > 1 else "pdf/600519_2023_贵州茅台2023年年度报告.pdf"
    out = pdf.replace(".pdf", ".md")
    print(f"转换: {pdf} -> {out}")
    result = convert(pdf, out)
    print(f"完成, markdown 长度 {len(result)} 字符")
    print("\n=== 预览(前 3000 字符) ===\n")
    print(result[:3000])
