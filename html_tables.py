# -*- coding: utf-8 -*-
"""把 HTML 解析成**规整的二维表格网格** —— 正确处理 rowspan / colspan / 嵌套表。

## 为什么必须有这个

裁判要做「科目-数值配对」比对，就需要从 iXBRL 里取**权威的表格结构**。
第一版用的是简化版：「每行第一个非空单元格当科目名，行内所有数字当数值」。

**在真实财报表格上它会配错。** 财报表格有：
  · **多层表头**（「当中間連結会計期間」「前連結会計年度」跨列）
  · **跨行科目**（一个科目名 rowspan=2，对应两行数值）
  · **合并单元格**（合計行 colspan）
实测抽出来过科目名叫 `1,127` 的荒谬配对，而它直接导致裁判的**对照组都到不了 100%** ——
连"应该 100%"的标准都达不到，后面所有数字都不可信。

## 网格规则

把每个单元格铺进矩阵，rowspan/colspan 覆盖到的地方填**合并后的同一份文本**，
这样任何一个 (行, 列) 坐标都能取到它实际归属的单元格内容 —— 这正是"科目名与数值同行"
这个判断所需要的形式。

## 与表格结构指标（A2/A11/TEDS）的关系

这个模块**只做结构还原**，不做判定。A2（列对齐）、A11（表内勾稽）、
以及和 iXBRL 真值的比对都建在它上面。**一个坏了的结构还原，会让上面所有指标一起骗人。**
"""
import re
from html.parser import HTMLParser

# 全角→半角（数字/小数点/逗号），用于数值解析
_FW = str.maketrans("０１２３４５６７８９．，", "0123456789.,")


def parse_number(text, scale=None, sign=None):
    """把单元格里的显示文本解析成数值。

    ⚠️ iXBRL 的 `scale` 是 10 的幂（`scale="6"` 表示这个数是以百万为单位显示的），
    `sign="-"` 表示取负（显示值本身是绝对值）。**漏掉任一个都会差 10^6 或差正负号。**
    """
    if text is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(text).translate(_FW))
    if not s or s in ("-", ".", "-."):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if scale:
        try:
            v *= 10 ** int(scale)
        except (TypeError, ValueError):
            pass
    return -v if sign == "-" else v


class _Cell:
    __slots__ = ("text", "facts", "rowspan", "colspan")

    def __init__(self, rowspan=1, colspan=1):
        self.text = ""
        # ⚠️ 每条事实**必须同时留 `text`（文档里印的那个串）和 `value`（应用 scale 后的数）**。
        #    只留 value 会出真错：大株主表里文档印的是「1,298」（千株，scale=3），
        #    从 value=1,298,700 反推要除以 1000 → 四舍五入成 1299，**跟文档对不上**，
        #    于是那一整张表在比对里全被判成"未命中"。实测这一条就造成 164 组假失败。
        self.facts = []          # [{"value": float, "text": 原文}, ...]
        self.rowspan = rowspan
        self.colspan = colspan

    def __repr__(self):
        return f"Cell({self.text[:14]!r}, rs={self.rowspan}, cs={self.colspan}, n={len(self.facts)})"


class _TableParser(HTMLParser):
    """把 HTML 解成表格列表；每个表格是二维网格。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []          # [[ [Cell,...], ... ], ...]
        self._tstack = []         # 嵌套表：每层是一组行
        self._row = None
        self._cell = None
        self._ix = None           # 正在读的 ix:nonFraction 属性
        self._ixbuf = None        # 它的内层文本

    # ---------- 工具 ----------
    def _rows(self):
        return self._tstack[-1] if self._tstack else None

    @staticmethod
    def _int(attrs, key, default=1):
        try:
            return max(1, int(str(dict(attrs).get(key) or default).strip()))
        except (TypeError, ValueError):
            return default

    # ---------- 标签 ----------
    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "table":
            self._tstack.append([])
        elif t == "tr" and self._tstack:
            self._row = []
        elif t in ("td", "th") and self._row is not None:
            self._cell = _Cell(self._int(attrs, "rowspan"), self._int(attrs, "colspan"))
        elif t == "ix:nonfraction":
            self._ix = dict(attrs)
            self._ixbuf = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data
        if self._ixbuf is not None:
            self._ixbuf.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "ix:nonfraction":
            if self._ix is not None and self._cell is not None:
                raw = " ".join("".join(self._ixbuf or []).split())
                v = parse_number(raw, self._ix.get("scale"), self._ix.get("sign"))
                if v is not None:
                    self._cell.facts.append({"value": v, "text": raw})
            self._ix, self._ixbuf = None, None
        elif t in ("td", "th"):
            if self._cell is not None and self._row is not None:
                self._cell.text = " ".join(self._cell.text.split())
                self._row.append(self._cell)
            self._cell = None
        elif t == "tr":
            if self._row is not None and self._tstack:
                self._tstack[-1].append(self._row)
            self._row = None
        elif t == "table":
            if not self._tstack:
                return
            rows = self._tstack.pop()
            self.tables.append(rows)


def build_grid(rows):
    """把「带 span 的行」铺成矩形矩阵。**这是整个模块的核心。**

    规则：rowspan/colspan 覆盖到的格子都记成**同一个单元格的文本**，
    这样 (行, 列) 坐标总能取到它实际归属的内容 —— "科目名与数值同行"才判得准。

    返回 [[Cell, ...], ...]；行长度按该表最大宽度补齐。
    """
    grid = []
    occupied = {}                       # (行, 列) -> Cell（被上面的 rowspan 占住的）
    for r, cells in enumerate(rows):
        if r >= len(grid):
            grid.append([])
        c = 0
        for cell in cells:
            while occupied.pop((r, c), None) is not None:
                c += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    rr, cc = r + dr, c + dc
                    while rr >= len(grid):
                        grid.append([])
                    while cc >= len(grid[rr]):
                        grid[rr].append(None)
                    grid[rr][cc] = cell if (dr == 0 and dc == 0) else cell
                    if dr > 0:
                        occupied[(rr, cc)] = cell
            c += cell.colspan
        while occupied.pop((r, c), None) is not None:
            while c >= len(grid[r]):
                grid[r].append(None)
            c += 1
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row.extend([None] * (width - len(row)))
    return grid


def parse_tables(html):
    """HTML → [{"grid": [[Cell,...]], "rows": 原始行数, "cols": 宽度}, ...]"""
    p = _TableParser()
    p.feed(html)
    p.close()
    out = []
    for rows in p.tables:
        if not rows:
            continue
        g = build_grid(rows)
        out.append({"grid": g, "rows": len(g), "cols": len(g[0]) if g else 0})
    return out


def labeled_rows(table, min_cols=2):
    """从网格里抽 `(科目名, [数值...])` —— **用整行而不是"第一个单元格"**。

    规则：一行的**第一个非空、且不含数值的单元格**当科目名；
    该行其余单元格里的 iXBRL 数值全部收进来。

    ⚠️ 为什么要求科目名"不含数值"：财报表里有大量以数字开头的行（期次、年份），
    第一版没这个限制，于是抽出过 `1,127` 当科目名。
    """
    out = []
    for row in table["grid"]:
        cells = [c for c in row if c is not None]
        if len(cells) < min_cols:
            continue
        label = ""
        for i, c in enumerate(cells):
            txt = (c.text or "").strip()
            if not txt:
                continue
            # 科目名：不能是纯数字、不能含 iXBRL 数值
            if c.facts or re.fullmatch(r"[\d,.\-（）()％%]+", txt):
                break
            label = txt
            rest = cells[i + 1:]
            break
        else:
            continue
        if not label or len(label) < 2:
            continue
        facts = [f for c in cells for f in c.facts]
        if facts:
            out.append({"label": label, "facts": facts,
                        "values": [f["value"] for f in facts],
                        "label_key": re.sub(r"[\s　|]", "", label.translate(_FW))})
    return out


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
                   if n.lower().endswith((".htm", ".html")))

    tables = parse_tables(html)
    print(f"{doc}｜解析出 {len(tables)} 张表")
    big = sorted(tables, key=lambda t: -(t["rows"] * t["cols"]))[:3]
    for t in big:
        print(f"\n  表 {t['rows']}×{t['cols']} —— 前 8 行：")
        for row in t["grid"][:8]:
            cells = []
            for c in row:
                s = (c.text or "")[:12] if c else ""
                if c and c.facts:
                    s += f"⟨{c.facts[0]:,.0f}⟩" if abs(c.facts[0]) < 1e9 else f"⟨{c.facts[0]:.2e}⟩"
                cells.append(s)
            print("   | " + " | ".join(cells))
    lr = [r for t in tables for r in labeled_rows(t)]
    print(f"\n总共抽出 {len(lr)} 组「科目-数值」")
    for r in lr[:10]:
        print(f"   {r['label'][:30]:32s} {[f'{v:,.0f}' for v in r['values'][:3]]}")
