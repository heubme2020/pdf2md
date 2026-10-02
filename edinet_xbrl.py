# -*- coding: utf-8 -*-
"""从 EDINET 的 iXBRL 里抽出**结构化事实**，当 PDF 解析的裁判。

## 为什么这是裁判，而不是另一个数据源

同一份有価証券報告書，EDINET 同时给 `type=1`（iXBRL，含全部正文）和 `type=2`（PDF）。
两者的 docID 相同 —— 所以**期间、币种、合并/母公司口径天然对齐**，不需要任何映射。
这正是用 FMP 时最痛的地方（symbol 映射、丰田 3 月决算、单位缩放），在这里全都不存在。

## ⚠️ 最大的坑：contextRef

裸读 `NetSales` 这种标签是**错的** —— 必须解析 `contextRef` 才能分清：
  · **連結** vs **個別**（合并 vs 母公司）
  · **当期** vs **前期** vs **前々期**
不解析就一定会把"上期"当成"本期"，这是最典型的错法（`去回答_合辑` 里那条日文回答
自己也点了这个：「contextRef/unitRefを解決できず、連結と個別、当期と前期の取り違えが最大の穴」）。

所以这个模块**只输出带完整上下文的事实**，不做任何"取第一个"的偷懒。

## iXBRL 的两个关键属性

  · `ix:nonFraction` —— 数值事实；`scale` 是 10 的幂次（`scale="6"` = 百万円）
    `sign="-"` 表示该显示值为负；`format` 决定逗号/括号的解析方式
  · `ix:nonNumeric` —— 文本事实（公司名、日期等）

用法：
    from edinet_xbrl import fetch_ixbrl_facts
    facts = fetch_ixbrl_facts(doc_id, api_key)
"""
import io
import re
import sys
import zipfile
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

# 「連結/個別」的判定线索 —— contextRef 的 id 里通常带这些词
_CONSOLIDATED = re.compile(r"^(?!.*(?:NonConsolidated|Individual|個別|Nonconsolidated)).*"
                           r"(Consolidated|連結|consolidated)", re.I)
_NONCONSOLIDATED = re.compile(r"(NonConsolidated|Individual|個別|Nonconsolidated)", re.I)


def fetch_ixbrl_zip(doc_id, api_key, timeout=180):
    """取 type=1 的 ZIP 原始字节。"""
    import requests
    r = requests.get(f"{EDINET_BASE}/documents/{doc_id}", params={"type": "1"},
                     headers={"Ocp-Apim-Subscription-Key": api_key}, timeout=timeout)
    r.raise_for_status()
    return r.content


def _parse_number(raw, scale, sign, fmt=None):
    """把 iXBRL 里的显示串解析成数。

    ⚠️ 三个必须处理的转换（漏一个就会差 10^6 或差正负号）：
      1. 去掉逗号、全角数字、货币符号
      2. `scale` 是 10 的幂 —— scale=6 表示这个数是以「百万」为单位显示的
      3. `sign="-"` 表示取负（iXBRL 规范：显示值是绝对值，符号在属性里）
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("-", "－", "—", "–", "△", "▲"):
        return None
    # 全角数字 → 半角
    s = s.translate(str.maketrans("０１２３４５６７８９．，", "0123456789.,"))
    s = s.replace(",", "").replace(" ", "").replace("　", "")
    s = s.replace("¥", "").replace("￥", "").replace("$", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith(("△", "▲", "-", "−", "－")):
        neg, s = True, s[1:]
    m = re.match(r"^\d+(?:\.\d+)?$", s)
    if not m:
        return None
    v = float(s)
    if scale:
        try:
            v *= 10 ** int(scale)
        except (TypeError, ValueError):
            pass
    if sign == "-":
        neg = not neg
    return -v if neg else v


def parse_ixbrl_facts(html, doc_id=""):
    """从一个 iXBRL HTML 里抽出所有数值/文本事实。

    返回 [{concept, context_ref, unit_ref, value, scale, sign, is_consolidated,
           text, period_ref}, ...]
    """
    facts = []
    # ix:nonFraction —— 数值事实。属性顺序不固定，所以逐个属性抓
    for m in re.finditer(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>", html, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        a = dict(re.findall(r'(\w[\w:-]*)\s*=\s*"([^"]*)"', attrs))
        text = re.sub(r"<[^>]+>", "", inner).strip()
        name = (a.get("name") or "").split(":")[-1]
        if not name:
            continue
        ctx = a.get("contextRef", "")
        facts.append({
            "concept": name,
            "context_ref": ctx,
            "unit_ref": a.get("unitRef", ""),
            "value": _parse_number(text, a.get("scale"), a.get("sign"), a.get("format")),
            "scale": a.get("scale"), "sign": a.get("sign"),
            "is_consolidated": bool(_CONSOLIDATED.search(ctx)),
            "is_nonconsolidated": bool(_NONCONSOLIDATED.search(ctx)),
            "text": text, "doc_id": doc_id, "kind": "num",
        })
    # ix:nonNumeric —— 文本事实（公司名/日期等）
    for m in re.finditer(r"<ix:nonNumeric\b([^>]*)>(.*?)</ix:nonNumeric>", html, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        a = dict(re.findall(r'(\w[\w:-]*)\s*=\s*"([^"]*)"', attrs))
        text = re.sub(r"<[^>]+>", "", inner).strip()
        facts.append({
            "concept": (a.get("name") or "").split(":")[-1],
            "context_ref": a.get("contextRef", ""),
            "value": None, "text": text, "doc_id": doc_id, "kind": "text",
            "is_consolidated": bool(_CONSOLIDATED.search(a.get("contextRef", ""))),
            "is_nonconsolidated": bool(_NONCONSOLIDATED.search(a.get("contextRef", ""))),
        })
    return facts


def parse_contexts(html):
    """解析 `<xbrli:context>` 定义 —— **合并/個別、当期/前期 的真正来源。**

    ⚠️ 为什么必须解析它，不能用 contextRef 的 id 猜：
    实测 `S100W29V` 的 contextRef id 形如 `Prior1YearInstant`，**只写期间、不写合并与否**。
    合并/個別是**维度（dimension）**，写在 context 定义里的 `<xbrldi:explicitMember>`，
    轴通常是 `jpdei_cor:ConsolidatedOrNonConsolidatedAxis`。
    第一版靠正则从 id 里猜，结果「連結 0 / 個別 435 / 未判定 704」—— 猜错了绝大多数。
    """
    ctx = {}
    for m in re.finditer(r"<xbrli:context\b[^>]*\bid\s*=\s*\"([^\"]+)\"(.*?)</xbrli:context>",
                         html, re.S | re.I):
        cid, body = m.group(1), m.group(2)
        inst = re.search(r"<xbrli:instant>\s*([\d-]+)", body, re.I)
        start = re.search(r"<xbrli:startDate>\s*([\d-]+)", body, re.I)
        end = re.search(r"<xbrli:endDate>\s*([\d-]+)", body, re.I)
        dims = {}
        for dm in re.finditer(r"<xbrldi:explicitMember\b[^>]*\bdimension\s*=\s*\"([^\"]+)\"[^>]*>"
                              r"\s*([^<]+?)\s*</xbrldi:explicitMember>", body, re.S | re.I):
            dims[dm.group(1).split(":")[-1]] = dm.group(2).strip().split(":")[-1]
        ctx[cid] = {
            "instant": inst.group(1) if inst else None,
            "start": start.group(1) if start else None,
            "end": end.group(1) if end else None,
            "dims": dims,
        }
    return ctx


def _scope_of(c):
    """从 context 定义里判定 合并 / 個別。

    ⚠️ **EDINET 的约定：合并 = 没有该维度（默认值），個別才显式标 `NonConsolidatedMember`。**
    实测 `S100W29V` 里 `Assets` 用的四个 contextRef：
        CurrentYearInstant                          ← 合并·当期（无维度）
        CurrentYearInstant_NonConsolidatedMember    ← 個別·当期
        Prior1YearInstant / Prior1YearInstant_NonConsolidatedMember 同理
    而 `ConsolidatedMember` 这个字样在整份文档里出现 **0 次**（`grep` 到的 678 次全是
    `NonConsolidatedMember` 的子串）。

    第一版把"无维度"判成 unknown，于是**合并口径一条都取不到** —— 这正是
    `去回答_合辑` 那条日文回答里说的「連結と個別の取り違えが最大の穴」。
    """
    if not c:
        return "unknown"
    blob = " ".join(c["dims"].values()) + " " + " ".join(c["dims"].keys())
    if re.search(r"NonConsolidated|Individual|個別", blob, re.I):
        return "individual"
    # 没有個別维度 → 按 EDINET 约定就是合并
    return "consolidated"


def get_facts(doc_id, api_key):
    """取一份文档的全部事实（**会联网**）。"""
    return parse_zip_facts(fetch_ixbrl_zip(doc_id, api_key), doc_id)


def parse_zip_facts(zip_bytes, doc_id=""):
    """从 ZIP **字节**解析事实 —— 与联网取数分离。

    为什么拆开：调用方要缓存**原始字节**（改了 HTML 解析器能自动重解析，不会拿到过期结果），
    所以解析必须能脱离 `fetch` 单独调用。
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = [n for n in zf.namelist() if n.lower().endswith((".htm", ".html"))]

    contexts = {}
    pages = []
    for n in sorted(names):
        try:
            html = zf.read(n).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        pages.append((n, html))
        contexts.update(parse_contexts(html))

    facts = []
    for n, html in pages:
        for f in parse_ixbrl_facts(html, doc_id):
            c = contexts.get(f.get("context_ref") or "")
            f["ctx_instant"] = c["instant"] if c else None
            f["ctx_end"] = c["end"] if c else None
            f["dims"] = c["dims"] if c else {}
            f["scope"] = _scope_of(c)          # consolidated / individual / unknown
            f["file"] = n.split("/")[-1][:40]
            facts.append(f)
    return facts


def chapter_html(zip_bytes):
    """从 type=1 的 ZIP 里取**正文各章**的 HTML 拼起来（原 crawl_edinet._chapter_html，2026-09-30 移来共用）。

    ⚠️ **不能按 `honbun` 过滤** —— EDINET 有**两套命名**，实测同一批里都有：
        旧（2017 年）: `0101010_honbun_jpcrp030000-asr-001_…_ixbrl.htm`
        新（近期）  : `0101010_000000.htm`          ← **没有 honbun、没有 _ixbrl 后缀**
    第一版按 `honbun` 过滤，结果**新命名的文档 100% 失败**（金丝雀 12 份里挂了 6 份，
    报「ZIP 里没有 honbun 正文」）。金丝雀的价值就在这儿。

    改成**按章节编码认**：basename 的前 7 位是数字、且不是全零
    （`0000000_header` 是封面，全零正是为了把它排除）。

    ⚠️ 另外一条实测事实：**2013 年前的年报 `type=1` 里是普通 HTML，没有 XBRL 标签。**
    （EDINET 2013 年才引入 iXBRL。实测 2007 年的 `S100CUEY` 有 100 张表、`ix:nonFraction` **0 个**；
    2010 年的 `S100B7XX` 有 582 个标签。）**但照样能转** —— 表格和标题都在 HTML 结构里，
    只是裁判量不了（没有 XBRL 真值）。**两条路都比解析 PDF 强**，所以不必回退。
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = []
    for n in sorted(zf.namelist()):
        base = n.split("/")[-1]
        if not base.lower().endswith((".htm", ".html")):
            continue
        head = base[:7]
        if head.isdigit() and head != "0000000":
            names.append(n)
    if not names:
        raise RuntimeError("ZIP 里没找到章节 HTML（前 7 位为数字的文件）")
    return "".join(zf.read(n).decode("utf-8", "replace") for n in names)


if __name__ == "__main__":
    import os
    key = ""
    for line in open(".env", encoding="utf-8"):
        if line.startswith("EDINET_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
    doc = sys.argv[1] if len(sys.argv) > 1 else "S100W29V"
    fs = get_facts(doc, key)
    nums = [f for f in fs if f["kind"] == "num" and f["value"] is not None]
    print(f"docID {doc}｜事实 {len(fs)} 条（数值 {len(nums)}，文本 {len(fs)-len(nums)}）")
    print(f"  其中 連結 {sum(1 for f in nums if f['is_consolidated'])}"
          f"｜個別 {sum(1 for f in nums if f['is_nonconsolidated'])}"
          f"｜未判定 {sum(1 for f in nums if not f['is_consolidated'] and not f['is_nonconsolidated'])}")
    print("\n  出现最多的科目:")
    for k, n in Counter(f["concept"] for f in nums).most_common(12):
        print(f"    {n:>4}×  {k}")
    print("\n  会计恒等式三件套:")
    for c in ("TotalAssets", "Liabilities", "NetAssets", "Equity", "Assets"):
        for f in nums:
            if f["concept"] == c:
                print(f"    {c:28s} {f['value']:>20,.0f}  scale={f['scale']} ctx={f['context_ref'][:34]}")
                break
