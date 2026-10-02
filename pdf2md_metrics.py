# -*- coding: utf-8 -*-
"""PDF→Markdown 质量指标 —— **与转换器无关**。

输入只有两样：转换出来的 md 文本 + 原 PDF 路径。所以任何算法（PyMuPDF / Docling /
MinerU / 我们自己改的）都能用同一套指标打分，这是"能对比各种算法"的前提。

## 为什么不用"字符数/表格行数"

2026-09-22 之前 `bench_*.py` 算的是 `avg_chars` 和 `avg_md_tables` —— **那是量，不是质**。
它们甚至会奖励错误：解析崩了多吐一堆重复文本，字符数反而更高；表格列错位了，
行数照样很多。**没有一个指标能回答"解析对不对"。**

## 指标体系

**A 类：可自证**（不需要人工标注，能大规模自动跑）—— 本模块实现的就是这些

  A1 `accounting_identity`  会计恒等式：资产总计 = 负债合计 + 所有者权益合计
  A2 `table_alignment`      表格结构完整性：表头列数 == 各数据行列数
  A3 `content_coverage`     内容覆盖率：md 字符数 / PDF 文字层字符数
  A4 `garbled`              乱码率：U+FFFD / 私用区字符占比
  A5 `sections`             章节结构可用性
  A6 `section_fragmentation` 章节碎片率：切出来的章节里正文 <50 字的占比（2026-09-27 加）
  A7 `duplication`          重复率：≥20 字的行重复出现的比例（2026-09-27 加）
  A8 `fake_headings`        伪标题率：### 行里明显是句子的占比（2026-09-27 加）

A1 是最强的一条：它是**数学约束**，错了就是错了，而且中日台三地通用。

## 为什么要加 A6–A8（2026-09-27）

有客户反馈「MD 出现较多乱码或文字乱序，章节切片错误很多」。回头查指标才发现：
**A1–A5 一条都测不出客户的抱怨**。

  · 「文字乱序」—— 乱序的每个字都是合法字符，A4 只查替换字符/私用区，**恒等于 0**
  · 「章节切片错误」—— A5 只数标题**个数**，伪标题越多它越好看，**方向是反的**
  · 「重复文本」  —— A3 的 ratio 只陈述不断言（见下），v9b 那次 1.78x 重复
                     是**人肉复盘**发现的，不是指标报警的

所以 A6–A8 是补这三个洞。**但它们都不是完美的**，用的时候必须注意各自的自欺方式：

  A6 会奖励「少切」—— 切得越少碎片越少。**必须和章节数一起看**：
     只有「章节数不降、碎片率降」才算真改进。
  A7 只认完全相同的长行，改写过的重复抓不到（宁可漏，不愿误报）。
  A8 用的正是伪标题的定义特征（句末标点），**会奖励任何按标点过滤的实现**
     —— 包括本仓库 v10 那个改动。所以 A8 只能当参考，**不能当判据**。

**B 类：鲁棒性**（崩溃/空输出/耗时）由 runner 记录，不在本模块。

**C 类：人工小样本**（最终验证用）不在本模块 —— 它的作用是校验 A 类没骗人。

## 用法

    from pdf2md_metrics import evaluate
    result = evaluate(pdf_path, md_text)
    print(result["accounting_identity"]["status"])   # pass / fail / skip
"""
import re
import unicodedata

# ---------------------------------------------------------------- 数字解析

# 会计科目里的数字：可能带千分位、货币符、括号负数、※ 等脚注标记
_NUM_RE = re.compile(r"[-+−]?\s*[\d,]+(?:\.\d+)?")
_STRIP_CHARS = str.maketrans({"$": "", "¥": "", "￥": "", "　": "", " ": "",
                              "※": "", "＊": "", "*": "", " ": ""})


def parse_number(cell):
    """把会计数字解析成 float。认不出返回 None。

    处理的写法：
      "1,939,113,415.03" → 1939113415.03
      "(219,132)"        → -219132.0     （括号 = 负数，台湾/日本常见）
      "$29,915,841"      → 29915841.0
      "－" / "-" / ""     → None          （空单元格，不能当 0）
    """
    if cell is None:
        return None
    s = str(cell).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = s.translate(_STRIP_CHARS).strip()
    if not s or s in ("-", "－", "—", "–", "/"):
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    raw = m.group(0).replace(",", "").replace("−", "-").replace(" ", "")
    try:
        v = float(raw)
    except ValueError:
        return None
    return -v if neg else v


# ---------------------------------------------------------------- md 表格解析

def md_tables(md):
    """把 markdown 里的表格解析成 [[cell, ...], ...] 的列表。

    只认标准管道表：连续以 `|` 开头的行，跳过 `| --- |` 分隔行。
    非表格内容一律忽略 —— 这本身就是一种度量：**如果解析器没吐出表格，
    这里就一个表都拿不到，A1 会直接判 skip，而不会假装通过。**
    """
    tables, cur = [], []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            if re.fullmatch(r"\|[\s:|\-]+\|", s):       # 分隔行
                continue
            cur.append([c.strip() for c in s[1:-1].split("|")])
        else:
            if len(cur) >= 2:                            # 至少 表头 + 1 行
                tables.append(cur)
            cur = []
    if len(cur) >= 2:
        tables.append(cur)
    return tables


# ---------------------------------------------------------------- A1 会计恒等式

# 标签写法（三地实测）。**顺序有意义**：先匹配更具体的，避免
# 「归属于母公司所有者权益合计」被当成「所有者权益合计」而算不平。
LABELS = {
    "asset": [
        r"^资产总计", r"^资产合计", r"^資產總計", r"^資産合計", r"^総資産",
        r"^总资产", r"^資產合計",
    ],
    "liability": [
        r"^负债合计", r"^负债总计", r"^負債合計", r"^負債總計", r"^負債の部合計",
        r"^总负债",
    ],
    # ⚠️ 「归属于母公司所有者权益合计」**不能**用来验恒等式 ——
    #    它不含少数股东权益，资产 ≠ 负债 + 归母权益。必须用**合计**那一行。
    "equity": [
        r"^所有者权益（或股东权益）合计", r"^所有者权益\(或股东权益\)合计",
        r"^所有者权益合计", r"^股东权益合计",
        r"^權益總計", r"^權益合計", r"^權益總額", r"^純資産合計", r"^株主資本合計",
    ],
}
_EXCLUDE_EQUITY = re.compile(
    r"归属于母公司|归属于上市公司|母公司所有者权益|非控制性权益|少数股东"
    r"|歸屬於母公司|歸屬母公司|非控制權益|少數股東")


# 科目名所在的格：台湾 iXBRL 把「代號 Code」放第 0 格、科目名放第 1 格（实测 2330），
# 而 A股/日本 科目名在第 0 格。所以取**前两格里第一格含 CJK 的**，而不是死认第 0 格。
_CJK = re.compile(r"[一-鿿㐀-䶿぀-ヿㇰ-ㇿ가-힯]")


def _row_label(cells):
    if not cells:
        return ""
    for c in cells[:2]:
        s = (c or "").strip()
        if s and _CJK.search(s):
            return s
    return (cells[0] or "").strip()


def accounting_identity(md):
    """A1：验证 资产总计 = 负债合计 + 所有者权益合计。

    ⚠️ **不能要求三个标签在同一张表里** —— 实测 A 股的资产负债表是**拆成两个表**的
    （「资产」一张、「负债和所有者权益」一张），甚至中间隔一个空行。第一版就是这么写错的，
    结果 A 股明明解析正常却被判 skip。

    做法：**全文扫描**所有表格，把认出来的标签各收一堆候选值，
    然后穷举组合验算 —— 恒等式是精确成立的（四舍五入到分），
    所以凑巧凑出相等几乎不可能，穷举是安全的。找不出组合再判 fail。

    返回 {"status": "pass"|"fail"|"skip", ...}
    """
    cands = {"asset": [], "liability": [], "equity": []}
    for ti, tbl in enumerate(md_tables(md)):
        for r, cells in enumerate(tbl):
            lab = _row_label(cells)
            if not lab:
                continue
            for key in ("asset", "liability", "equity"):
                pats = LABELS[key]
                if key == "equity" and _EXCLUDE_EQUITY.search(lab):
                    continue  # 归母权益不含少数股东权益，验不平
                if any(re.match(p, lab) for p in pats):
                    for col in range(1, len(cells)):
                        v = parse_number(cells[col])
                        if v is not None and v != 0:
                            cands[key].append({"v": v, "table": ti, "label": lab, "col": col})
                    break

    if not all(cands.values()):
        missing = [k for k, v in cands.items() if not v]
        return {"status": "skip",
                "reason": f"没找到这些标签的行: {missing}（多为解析器没吐出表格）",
                "found": {k: len(v) for k, v in cands.items()}}

    # 恒等式容差：报表精确到分，1 元以内即视为成立
    best = None
    for a in cands["asset"]:
        for li in cands["liability"]:
            for eq in cands["equity"]:
                diff = abs(a["v"] - (li["v"] + eq["v"]))
                if best is None or diff < best["diff"]:
                    best = {"diff": diff, "asset": a, "liability": li, "equity": eq}
                if diff <= 1.0:
                    return {"status": "pass", "diff": diff,
                            "asset": a["v"], "liability": li["v"], "equity": eq["v"],
                            "labels": {"asset": a["label"], "liability": li["label"],
                                       "equity": eq["label"]},
                            "tables": {"asset": a["table"], "liability": li["table"],
                                       "equity": eq["table"]}}

    return {"status": "fail", "diff": best["diff"],
            "asset": best["asset"]["v"], "liability": best["liability"]["v"],
            "equity": best["equity"]["v"],
            "labels": {k: best[k]["label"] for k in ("asset", "liability", "equity")}}


# ---------------------------------------------------------------- A2 表格结构完整性

def table_alignment(md):
    """A2：表头列数 vs 各数据行列数。列错位/串行是 pdf2md 最典型的失败模式。"""
    total = misaligned = 0
    worst = None
    for tbl in md_tables(md):
        total += 1
        want = len(tbl[0])
        bad = sum(1 for row in tbl[1:] if len(row) != want)
        if bad:
            misaligned += 1
            if worst is None or bad > worst["bad_rows"]:
                worst = {"bad_rows": bad, "rows": len(tbl) - 1, "want_cols": want,
                         "head": tbl[0][:4]}
    return {"tables": total, "misaligned": misaligned,
            "ratio": (1 - misaligned / total) if total else None,
            "worst": worst}


# ---------------------------------------------------------------- A3 内容覆盖率

def pdf_text_chars(pdf_path):
    """PDF 文字层的字符数（作为"应该有多少内容"的参照）。

    只做文字层，不做 OCR —— 扫描件文字层为空时返回 0，调用方据此判 skip。

    ⚠️ **必须读全文档**。第一版加了 60 页上限，而转换是整份文档做的 ——
    口径不一致会让台湾那份算出 6.48x 的假"内容膨胀"（其实是分子被截断了）。
    """
    try:
        import fitz
    except ImportError:
        return None
    try:
        with fitz.open(pdf_path) as doc:
            return sum(len(doc[i].get_text()) for i in range(len(doc)))
    except Exception:  # noqa: BLE001
        return None


def content_coverage(md, pdf_path):
    """A3：md 字符数 / PDF 文字层字符数。

    <0.9 疑似**丢内容**；>1.3 疑似**重复**（同一段被吐了多次）。
    区间是经验值，不是硬门槛 —— 报告里只陈述数值，不自动判好坏。
    """
    base = pdf_text_chars(pdf_path)
    if not base:
        return {"status": "skip", "reason": "PDF 文字层为空（可能是扫描件）"}
    n = len(md)
    return {"status": "ok", "md_chars": n, "pdf_chars": base, "ratio": n / base}


# ---------------------------------------------------------------- A4 乱码

def garbled(md):
    """A4：替换字符 / 私用区字符 / 控制字符 —— 字体映射失败的信号。"""
    rep = md.count("�")
    pua = sum(1 for c in md if 0xE000 <= ord(c) <= 0xF8FF or 0xFFF000 <= 0xF0000)
    ctrl = sum(1 for c in md if unicodedata.category(c) == "Cc" and c not in "\n\r\t")
    total = len(md) or 1
    return {"replacement": rep, "private_use": pua, "control": ctrl,
            "ratio": (rep + pua + ctrl) / total, "chars": len(md)}


# ---------------------------------------------------------------- A5 章节结构

_SECTION_RE = re.compile(
    r"^\s*#{1,4}\s+\S|"                       # markdown 标题
    r"^\s*第[一二三四五六七八九十百\d]+[节章節]|"   # 中文「第X节」
    r"^\s*SECTION[-\s]?\d|"                    # 韩国
    r"^\s*第[一二三四五六七八九十\d]+【"          # 日本 EDINET「第1【…】」
)
_FIN_RE = re.compile(r"财务报告|財務報告|財務報表|財務諸表|連結財務諸表|财务报表")


def sections(md):
    """A5：章节结构可用性 —— 能不能切出章节、有没有财务报告章节。

    对着 v8（分章节）的能力：解析器没做章节切分时 count 会很低。
    """
    heads = [l for l in md.split("\n") if _SECTION_RE.match(l)]
    return {"count": len(heads), "has_financial": bool(_FIN_RE.search(md)),
            "sample": [h.strip()[:40] for h in heads[:5]]}


# ---------------------------------------------------------------- A6 章节碎片率

_CN_NUM = "一二三四五六七八九十"


def split_sections(md):
    """复刻 worker 的 splitSections（worker/src/index.ts:181-199）。

    ⚠️ **这是第三份拷贝**（另两份：worker/src/index.ts 是生产实现的唯一真源、
    `_test_sections_127.py:17-39` 是离线验证器）。三份中的任何一份改了，
    另两份都要跟着改 —— 否则离线算出来的碎片率跟线上切法不是一回事，
    「指标通过、线上切错」就这么来的。改这里的同时请核对那两处。
    """
    body = re.sub(r"^---\n.*?\n---\n?", "", md, count=1, flags=re.S)  # 去 frontmatter
    if re.search(r"\n# ", body):
        pat = r"\n(?=# )"
    elif re.search(r"\n## ", body):
        pat = r"\n(?=## )"
    elif re.search(rf"\n### [{_CN_NUM}]+、", body):
        pat = rf"\n(?=### [{_CN_NUM}]+、)"
    elif re.search(rf"\n### （[{_CN_NUM}]+）", body):
        pat = rf"\n(?=### （[{_CN_NUM}]+）)"
    elif re.search(r"\n### ", body):
        pat = r"\n(?=### )"
    else:
        pat = r"\n(?=#{1,3} )"
    out = []
    for part in re.split(pat, body):
        lines = part.split("\n")
        title = re.sub(r"^#+\s*", "", lines[0]).strip()
        content = "\n".join(lines[1:]).strip()
        if title:
            out.append((title, content))
    return out


def section_fragmentation(md, min_body=50):
    """A6：切出来的章节里，正文短于 min_body 字的占比。

    为什么这是个好代理：假章节边界会把一整章**切成碎片** ——
    真章节的正文不该只有几十个字。而它能独立工作，因为不看任何标题规则。

    ⚠️ 陷阱：**切得越少，碎片越少**。完全不切（只有 0/1 章）碎片率是 0，
    看起来满分。所以必须和 `sections` 一起读，并且这里对 ≤1 章直接判 skip，
    不给出那个会骗人的 0。
    """
    secs = split_sections(md)
    if len(secs) <= 1:
        return {"status": "skip", "reason": f"只切出 {len(secs)} 章，碎片率无意义",
                "sections": len(secs)}
    frag = [t for t, b in secs if len(b) < min_body]
    return {"status": "ok", "sections": len(secs), "fragments": len(frag),
            "fragment_ratio": len(frag) / len(secs),
            "sample": [t[:40] for t in frag[:5]]}


# ---------------------------------------------------------------- A7 重复率

def duplication(md, min_len=20):
    """A7：≥min_len 字的行，重复出现的行占比。

    来历：v9b 的 `1.78x` 内容膨胀被拆成「剥掉 md 标记 1.35x + 真重复 0.35x」，
    而那是**人肉复盘**发现的 —— 这个指标就是要把那件事变成自动报警。

    只认完全相同（去掉首尾空白后）的行。改写过的重复抓不到：宁可漏，不愿误报。
    """
    from collections import Counter
    lines = [l.strip() for l in md.split("\n") if len(l.strip()) >= min_len]
    if not lines:
        return {"status": "skip", "reason": f"没有 ≥{min_len} 字的行"}
    counts = Counter(lines)
    extra = sum(c - 1 for c in counts.values() if c > 1)
    top = [(l[:60], c) for l, c in counts.most_common(3) if c > 1]
    return {"status": "ok", "lines": len(lines), "repeated": extra,
            "dup_ratio": extra / len(lines), "top": top}


# ---------------------------------------------------------------- A8 伪标题率

def fake_headings(md):
    """A8：`###` 行里「明显是句子」的占比 —— 句末标点结尾，或句中含句号。

    ⚠️ **这条会自己奖励自己**：它用的正是伪标题的定义特征，而 v10 的修复
    恰恰就是按标点过滤的。所以 A8 只能当参考，**绝不能当判据** ——
    否则就是在拿改动本身证明改动对。真正的判据是 A6（碎片率）和章节数。
    """
    heads = [l[4:].strip() for l in md.split("\n") if l.startswith("### ")]
    if not heads:
        return {"status": "skip", "reason": "没有 ### 标题"}
    bad = [h for h in heads if h and (h[-1] in "。；！？" or "。" in h)]
    return {"status": "ok", "headings": len(heads), "fake": len(bad),
            "fake_ratio": len(bad) / len(heads), "sample": [h[:60] for h in bad[:5]]}


# ---------------------------------------------------------------- A9 脚本构成

# 各市场预期的脚本与下限。**假名/谚文是"结构性"特征** —— 一份日文财报正文里
# 假名占比正常 25–35%，掉到 5% 以下几乎必然是抽取坏了（字体映射"成功"但映射错了）。
_SCRIPT_EXPECT = {"jp": ("kana", 0.05), "kr": ("hangul", 0.05),
                  "cn": ("han", 0.10), "tw": ("han", 0.10)}


def _script_counts(s):
    han = kana = hangul = latin = digit = other = 0
    for c in s:
        o = ord(c)
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
            kana += 1                       # 平假名 + 片假名 + 片假名扩展
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            hangul += 1                     # 谚文音节 + 字母
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
            han += 1
        elif c.isascii() and c.isalpha():
            latin += 1
        elif c.isdigit():
            digit += 1
        elif not c.isspace():
            other += 1
    return {"han": han, "kana": kana, "hangul": hangul, "latin": latin,
            "digit": digit, "other": other}


def _narrative(md):
    """抽出「叙述部分」：去掉表格行和过短的行。

    ⚠️ 为什么必须这么做（不是可选的）：财报正文里表格极长，全文算比例会被表格
    里的数字和汉字稀释掉，假名比例被压到看不出异常。这是本指标的已知失效模式。
    """
    return "\n".join(l for l in md.split("\n")
                     if not l.lstrip().startswith("|") and len(l.strip()) >= 10)


def script_profile(md, expect=None):
    """A9：脚本构成 —— 抓「**完全合法、但是错的汉字**」，即 A4 的盲区。

    A4 只查 FFFD / 私用区 / 控制字符 —— 那对应「字体映射失败」。
    但当 CMap 存在却**映射错了**（共享字形、Big5/CID 错配），产出的是**完全合法、
    看着很正常的汉字**，A4 恒等于 0。日文的假名、韩文的谚文是结构性特征，
    映射一错就会大面积消失 —— 这比逐字判断"这个字对不对"可靠得多。

    ⚠️ 自欺方式：**纯表格文档的叙述部分天然很少** —— 所以叙述部分短于 200 字直接 skip，
    并且 `narrative_chars` 必须一起报。不报这个，一份"几乎没有正文"的坏文档会拿到好看的分数。
    """
    narr_txt = _narrative(md)
    narr = _script_counts(narr_txt)
    whole = _script_counts(md)
    n_narr = sum(narr.values())
    if n_narr < 200:
        return {"status": "skip", "reason": f"叙述部分只有 {n_narr} 字，比例无意义",
                "narrative_chars": n_narr}

    def ratios(c):
        tot = sum(c.values()) or 1
        return {k: v / tot for k, v in c.items()}

    rn, rw = ratios(narr), ratios(whole)
    out = {"status": "ok", "narrative_chars": n_narr,
           "kana_ratio": rn["kana"], "hangul_ratio": rn["hangul"],
           "han_ratio": rn["han"], "latin_ratio": rn["latin"],
           "digit_ratio": rn["digit"],
           # 全文口径一起报 —— 两个口径差得远，本身就说明表格占比异常
           "kana_ratio_whole": rw["kana"], "han_ratio_whole": rw["han"]}
    if expect in _SCRIPT_EXPECT:
        key, floor = _SCRIPT_EXPECT[expect]
        out.update({"expect": expect, "expected_script": key,
                    "floor": floor, "got": rn[key],
                    "suspicious": rn[key] < floor})
    return out


# ---------------------------------------------------------------- A11 表内算术闭合

_TOTAL_RE = re.compile(r"合\s*计|合\s*計|总\s*计|總\s*計|小\s*计|小\s*計|total", re.I)

# ⚠️ 单元格必须**整格就是一个数**，而且**千分位分组必须合法**。
#    不能用 parse_number（它是 search，且先把空格全删掉）—— 表里经常有"一格塞了多个数"
#    （合并列 / 错位），"1,234 5,678" 会被读成 12345678。
#    更隐蔽的是**数字被串接**：实测日文财报里 `13,050` 和 `8,824` 会粘成 `13,0508,824` ——
#    如果只写 `[\d,]+`，它照样被当成一个数（1.3 亿），残差直接飞到 1e24。
#    分组校验（每段恰好 3 位）能挡住这一类。第一版两条都错了，被 _debug_footing.py 抓出来。
_NUM_CELL_RE = re.compile(
    r"^[（(]?\s*[¥$￥€]?\s*[-−+]?"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)"      # 要么正规矩分组，要么完全不分隔
    r"(?:\.\d+)?\s*[)）]?$")


def _cell_number(cell):
    """单元格解析 —— 只认"整格就是一个数、且千分位合法"。fullmatch 而非 search。"""
    if cell is None:
        return None
    s = str(cell).strip()
    if not s or not _NUM_CELL_RE.match(s):
        return None
    return parse_number(s)


def table_footing(md, tol_abs=2.0, tol_rel=0.005):
    """A11：表内算术闭合 —— 合计行 ≈ 同列其余行之和（**不需要认科目名**，纯算术）。

    来历：A1 的规模化推广。A1 只验"资产 = 负债 + 权益"一条；这条对每张表每列都验一次。
    它为什么必要：字符串级指标看不出来的错，算术级一眼抓到 ——
    「四个科目相加 = 1284，印出来的合计是 1824」这类，单元格匹配率 99.2%，勾稽 FAIL。

    两档分开报，因为置信度差得远：
      · `labeled`  —— 合计行的**标签**匹配「合计/合計/总计/小计/total」。**这一档才是判据。**
      · `inferred` —— 没找到合计标签，退而假设"最后一行是合计"（财报惯例）。**只作参考。**

    ⚠️ **自欺方式（最重要的一条）**：本指标**只在"本来就是求和表"的列上触发**。
    解析器如果把「合计」行整个丢掉，就一个都验不了 —— **skip 飙升而 pass_ratio 看着很干净**。
    所以调用方**必须同时看 `labeled_tested` 和表数，高 skip 本身就是失败信号**。

    容差按单位走（文档 §5 元规则 2）：`tol_abs` 给整数表留舍入余量，`tol_rel` 给大额表留相对余量。
    **报的是残差，不是布尔值。**
    """
    tables = md_tables(md)
    if not tables:
        return {"status": "skip", "reason": "md 里没有表格（解析器可能没吐出表）", "tables": 0}

    res = {"labeled_tested": 0, "labeled_passed": 0,
           "inferred_tested": 0, "inferred_passed": 0}
    worst = None

    def check(tbl, col, ri, tot, addends):
        """一次勾稽。**只有这里才更新 worst** —— 别对普通行项目记残差。"""
        nonlocal worst
        if len(addends) < 2:
            return None
        s = sum(addends)
        resid = abs(tot - s)
        ok = resid <= max(tol_abs, abs(tot) * tol_rel)
        if not ok and (worst is None or resid > worst["residual"]):
            worst = {"label": _row_label(tbl[ri])[:30], "total": tot,
                     "sum_of_addends": s, "residual": resid, "col": col}
        return ok

    for tbl in tables:
        ncol = max(len(r) for r in tbl)
        if ncol < 2 or len(tbl) < 3:
            continue
        for col in range(1, ncol):
            cellv = {}
            for ri, row in enumerate(tbl):
                if col < len(row):
                    v = _cell_number(row[col])
                    if v is not None:
                        cellv[ri] = v
            if len(cellv) < 3:
                continue
            tot_rows = [ri for ri in cellv if _TOTAL_RE.search(_row_label(tbl[ri]))]
            # ⚠️ 加数范围 = 「上一个合计行之后、本合计行之前」那一段。
            #    不是"同列其余所有行" —— 前者才是财报的结构（每个合计汇总它上面那一块）；
            #    后者会把合计行之后的长短期负债、表头噪声全算进去，于是**本来对的表被判失败**。
            #    实测：`4,574+2,211+157+468+1,079 = 8,489` 恰好等于「流動負債合計」，
            #    第一版用"其余所有行"算出余 81,108 的错误结论。
            prev = -1
            for ri in tot_rows:
                addends = [v for rj, v in cellv.items() if prev < rj < ri]
                ok = check(tbl, col, ri, cellv[ri], addends)
                prev = ri
                if ok is None:
                    continue
                res["labeled_tested"] += 1
                res["labeled_passed"] += ok
            # 兜底档：列的最后一行当作合计（只在没有合计标签时）
            if not tot_rows:
                last = max(cellv)
                addends = [v for rj, v in cellv.items() if rj < last]
                ok = check(tbl, col, last, cellv[last], addends)
                if ok is not None:
                    res["inferred_tested"] += 1
                    res["inferred_passed"] += ok

    if not res["labeled_tested"] and not res["inferred_tested"]:
        return {"status": "skip", "tables": len(tables),
                "reason": "没有可验的求和列（**本身就是失败信号**，别当成「没问题」）"}

    res["status"] = "ok"
    res["tables"] = len(tables)
    res["worst"] = worst
    if res["labeled_tested"]:
        res["labeled_ratio"] = res["labeled_passed"] / res["labeled_tested"]
    if res["inferred_tested"]:
        res["inferred_ratio"] = res["inferred_passed"] / res["inferred_tested"]
    return res


# ---------------------------------------------------------------- A13 跨表勾稽

# 净利润 / 现金等价物 —— 同一个数在**两张不同的表**各报一次（间接法现金流量表首行=利润表
# 末行净利润；现金流量表期末现金=资产负债表货币资金），跨表对得上说明解析器没把表弄串。
ARTICULATION = {
    "net_income": [
        r"^净利润", r"^淨利", r"^本期净利", r"^本期淨利",
        r"^純利益", r"^当期純利益",
    ],
    "cash": [
        r"^货币资金", r"^現金及約當現金", r"^現金及び現金同等物",
        r"^現金及現金等價物", r"^現金及び預金",
    ],
}
_ARTICULATION_EXCLUDE = re.compile(r"归属于母公司|归属于上市公司|母公司所有者|少数股东|非控制性")


def articulation(md, tol_abs=2.0, tol_rel=0.0):
    """A13：跨表勾稽 —— 同一科目名（净利润 / 现金等价物）在**两张不同的表**里报出相近数值。

    间接法现金流量表的第一行就是「净利润」（利润表末行）、它的期末现金就是资产负债表上的
    「货币资金/现金等价物」。所以同一个数会在两张表各出现一次 —— 跨表对得上，说明解析器
    既没把这两张表弄串、也没把数值挂错科目。这是 A1（表内恒等式）往「表间勾稽」的自然延伸。

    ⚠️ 只做加法、不做减法：抓得到「勾稽对」、抓不到「勾稽错」。合并/母公司两张表会各报一个
    净利润且都合法（数字不同），本指标**不要求唯一**，对不上最多 skip，所以不会被「多张表」
    误伤；精确的合并/母公司归属要靠 A14（章节标题），不在本指标范围。容差按单位走（§5 元规则 2）。
    """
    groups = {}
    for ti, tbl in enumerate(md_tables(md)):
        for cells in tbl:
            lab = _row_label(cells)
            if not lab or _ARTICULATION_EXCLUDE.search(lab):
                continue
            for key, pats in ARTICULATION.items():
                if any(re.match(p, lab) for p in pats):
                    for col in range(1, len(cells)):
                        v = parse_number(cells[col])
                        if v is not None:
                            groups.setdefault(key, []).append({"v": v, "table": ti, "label": lab})
                    break

    if not groups:
        return {"status": "skip", "reason": "没找到净利润/现金等价物标签（多为解析器没吐出表）",
                "found": {}}

    tested = matched = 0
    detail = {}
    for key, cands in groups.items():
        hit = None
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                a, b = cands[i], cands[j]
                if a["table"] != b["table"] and abs(a["v"] - b["v"]) <= max(tol_abs, abs(a["v"]) * tol_rel):
                    hit = (a, b)
                    break
            if hit:
                break
        detail[key] = {"candidates": len(cands), "matched": bool(hit)}
        if cands:
            tested += 1
        if hit:
            matched += 1
            detail[key]["pair"] = {"values": [hit[0]["v"], hit[1]["v"]],
                                   "tables": [hit[0]["table"], hit[1]["table"]],
                                   "label": hit[0]["label"][:30]}

    if not tested:
        return {"status": "skip", "reason": "没有可验的勾稽科目",
                "found": {k: len(v) for k, v in groups.items()}}
    return {"status": "ok", "tested": tested, "matched": matched,
            "ratio": matched / tested, "detail": detail}


# ---------------------------------------------------------------- A15 阅读顺序

def reading_order(pdf_path, md, min_matched=50):
    """A15：阅读顺序 —— successor-edge accuracy（**只测叙述正文，跳过表格区域**）。

    真值 = PDF 几何顺序（每页按 [栏, y, x] 排序，栏用「最大 x 空隙」检测，同 pdf_to_md._column_split）；
    预测 = md 里 word 的出现顺序。乱序（只按 y 排序导致多栏交错）→ accuracy 低。

    ⚠️ **为什么跳过表格**：markdown 把 2D 表格拍平成行序，跟 PDF 几何顺序本来就不同——
    那不是乱序，是表格的固有表示差异。若不跳过，A15 会把这部分误报成乱序（第一版就因此
    在财务报表页上量出 ~27% 的假乱序，而 ORDER_STRATEGY 三种策略毫无差别）。

    ⚠️ 内部真值：栏检测靠「最大 x 空隙」这个几何规则，多栏检测错了真值就错（文档 §3 已标）。
    ⚠️ 只统计「能一对一匹配上的 word」，报覆盖率（matched / md_words）。
    """
    import fitz
    from collections import defaultdict
    from table_detect import detect_hlines, cluster_rows, table_blocks

    doc = fitz.open(pdf_path)
    seq = []  # 几何顺序的 word 序列（只含正文，跳过表格区域）
    for pno, page in enumerate(doc):
        # 表格区域（线条法，同 pdf_to_md._detect_tables_lines）
        table_bboxes = []
        hlines = detect_hlines(page)
        if len(hlines) >= 4:
            for b in table_blocks(cluster_rows(hlines)):
                top = b[0][0]
                bottom = b[-1][0]
                left = min(x0 for _, segs in b for x0, _ in segs)
                right = max(x1 for _, segs in b for _, x1 in segs)
                table_bboxes.append((left, top, right, bottom))

        words = [w for w in page.get_text("words")
                 if not any(tl <= w[0] and w[2] <= tr and tt <= w[1] and w[3] <= tb
                            for tl, tt, tr, tb in table_bboxes)]
        xs = sorted(w[0] for w in words)
        mid = None
        if len(xs) >= 4:
            span = xs[-1] - xs[0]
            if span > 0:
                width, m = max((xs[i + 1] - xs[i], (xs[i] + xs[i + 1]) / 2)
                               for i in range(len(xs) - 1))
                if width >= 0.15 * span:
                    mid = m
        for w in sorted(words, key=lambda w: (pno, 0 if (mid is None or w[0] < mid) else 1, w[1], w[0])):
            seq.append(w[4])
    doc.close()

    pos = defaultdict(list)
    for i, w in enumerate(seq):
        pos[w].append(i)
    used = defaultdict(int)
    md_positions = []
    for w in re.findall(r"\S+", md):
        if pos.get(w) and used[w] < len(pos[w]):
            md_positions.append(pos[w][used[w]])
            used[w] += 1
    if len(md_positions) < min_matched:
        return {"status": "skip", "reason": f"匹配上的 word 只有 {len(md_positions)} 个",
                "matched": len(md_positions)}
    correct = sum(1 for a, b in zip(md_positions, md_positions[1:]) if b > a)
    return {"status": "ok", "accuracy": correct / (len(md_positions) - 1),
            "matched": len(md_positions), "md_words": len(re.findall(r"\S+", md))}


# ---------------------------------------------------------------- 汇总

def evaluate(pdf_path, md, with_coverage=True, expect=None):
    """跑完 A 类所有指标。任何单个指标崩了都不影响其它指标。

    `expect`：市场代码（jp / kr / cn / tw），只喂给 A9 脚本构成 —— 用来判断
    "这份文档的脚本比例像不像该市场的语言"。不知道就传 None，A9 只报比例不判定。
    """
    out = {}
    for name, fn in (
        ("accounting_identity", lambda: accounting_identity(md)),
        ("table_alignment", lambda: table_alignment(md)),
        ("garbled", lambda: garbled(md)),
        ("sections", lambda: sections(md)),
        ("section_fragmentation", lambda: section_fragmentation(md)),
        ("duplication", lambda: duplication(md)),
        ("fake_headings", lambda: fake_headings(md)),
        ("script_profile", lambda: script_profile(md, expect)),
        ("table_footing", lambda: table_footing(md)),
        ("articulation", lambda: articulation(md)),
        ("reading_order", lambda: reading_order(pdf_path, md) if pdf_path else {"status": "skip"}),
        ("content_coverage", lambda: content_coverage(md, pdf_path) if with_coverage else {"status": "skip"}),
    ):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    out["md_chars"] = len(md)
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) < 3:
        sys.exit("用法: python pdf2md_metrics.py <原PDF> <转换出的md>")
    pdf, mdf = Path(sys.argv[1]), Path(sys.argv[2])
    res = evaluate(str(pdf), mdf.read_text(encoding="utf-8", errors="replace"))
    import json
    print(json.dumps(res, ensure_ascii=False, indent=1))
