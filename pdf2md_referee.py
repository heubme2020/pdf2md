# -*- coding: utf-8 -*-
"""PDF→Markdown 的**外部裁判** —— 拿监管机构的 XBRL 真值，量我们的解析器。

    python pdf2md_referee.py S100W29V              # 单份（含对照）
    python pdf2md_referee.py --batch --limit 100    # 批跑（可断点续跑）
    python pdf2md_referee.py --report               # 只汇总已有结果

## 分层（每层只依赖下一层，换市场/换来源时只动最下面）

    ① 匹配层      numbers_in / numeric_hit      —— **与真值来源无关**，纯数值比较
    ② 真值来源    XbrlSource（EDINET iXBRL）     —— 换 FMP / 人工断言只换这一层
    ③ 检查项      check_value_recall / check_structure
    ④ 对照       每次评估自带"应当 100%"的对照，**达不到就不该信结论**
    ⑤ 批跑与报告 可续跑、按市场分层、失败归因分类

## 两条互补的轴（这是裁判的核心，不是可有可无的细节）

    · **值召回**   数字在 md 里出现了吗？         → 对应"丢没丢内容"
    · **配对率**   它跟正确的科目名在同一行吗？   → 对应"挂对没有"

日股实测：值召回 95.2%、配对率 83.4%。**那 11.8 个点的差距就是"结构丢失"，
它直接解释 A1 通过率 13.9% / A11 闭合率 0.257** —— 数字都在，但对不上号。

## ⚠️ 走过的弯路（都写在这里，免得下次重踩）

地基曾经是**子串匹配**，于是被迫跟数字的**表示形式**较劲，连踩四个坑：
  · `599.37` 剥掉小数点变 `59937`，文档里是 `918.59` —— 小数天生对不上
  · `1,298,700` 反推显示值要 /1000 再四舍五入成 `1299`，文档印的是 `1298`（截断）
  · 短数字（`110`、`1`）不得不用长度门槛挡，而门槛又误杀真值
  · 对照组只到 62%，我连改三轮都以为"是 PDF 烂"，其实是**测法烂**
换成「两边都解析成浮点数按数值比」之后，千分位/全角/△/小数/截断/四舍五入**全部消失**，
对照组直接到 100%。

**教训：一个测法如果必须靠"长度门槛"才不误报，说明它的地基选错了。**
"""
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

ARCHIVE = Path(os.environ.get("ARCHIVE_DIR", "Z:/datasinking-data/raw"))
STATE = BASE / "referee_results.jsonl"
# 「可测」的数值门槛：报告币种下 >= 100 万。一条规则同时排掉每股收益与百分比 ——
# 它们不是金额，量的不是我们关心的东西，而且作为搜索目标会大量碰巧命中。
MIN_VALUE = 1e6


# ============================================================ ① 匹配层

_FW = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
_NUM_RE = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d])")


def _ascii(s):
    return (s or "").translate(_FW)


def norm(text):
    """归一化空白与千分位 —— 用于**科目名**的匹配（科目名里也有全角/空格）。"""
    return re.sub(r"[\s　,]", "", _ascii(text))


def numbers_in(text):
    """把文本里的数全部解析成带符号浮点数。

    符号：`△`/`▲`/全角减号/前导 `-`/`(…)` 都算负 —— 中日财报里「△131」= -131，
    漏了符号会让正负号错位（DQC 0015 那类错误）。
    """
    out, s = [], _ascii(text)
    for m in _NUM_RE.finditer(s):
        try:
            v = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        head = s[max(0, m.start() - 2):m.start()]
        if any(c in head for c in "△▲-−－("):
            v = -v
        out.append(v)
    return out


def numeric_hit(nums, fact, rel=1e-6):
    """这条事实的数值在 `nums` 里吗？返回命中的形式，否则 None。

    按 10 的幂试 0/3/6/8（円/千円/百万円/億円），并用相对容差 —— 四舍五入与截断都能容忍。

    ⚠️ **原文不带符号**：iXBRL 把负号放在 `sign` 属性里，文档呈现为「△13347」，
    而 `numbers_in` 会正确解析成 -13347 → 正的找不到。所以两个符号都试。
    这一条就是结构化对照组最后 2% 残差的全部原因。
    """
    txt = fact.get("text") if isinstance(fact, dict) else None
    v = fact.get("value") if isinstance(fact, dict) else fact
    if txt:
        for n in numbers_in(txt):
            for cand in (n, -n):
                if any(abs(cand - x) <= max(abs(cand) * rel, 1e-9) for x in nums):
                    return "原文"
    if v is None:
        return None
    for k in (0, 3, 6, 8):
        target = abs(v) / (10 ** k)
        if target < 1:
            continue
        tol = max(target * rel, 1e-9)
        if any(abs(x - target) <= tol for x in nums):
            return f"1e{k}"
    return None


def testable(fact):
    """够不够"可测"：按**数值量级**判，不按字符长度判。"""
    v = fact.get("value") if isinstance(fact, dict) else fact
    return v is not None and abs(v) >= MIN_VALUE


# ============================================================ ② 真值来源

def load_key():
    for line in open(BASE / ".env", encoding="utf-8"):
        if line.startswith("EDINET_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


class XbrlSource:
    """EDINET iXBRL 真值来源。

    `type=1` 的 ZIP 里同时有「叙事正文 honbun HTML」和「内联 XBRL 标签」——
    所以叙事和数字**是同一份文件**，期间/币种/合并口径天然对齐，不需要任何映射。
    （这正是拿 FMP 当裁判时最痛的地方，这里全都不存在。）

    **带磁盘缓存**：真值只跟文档有关、跟被测的解析器变体无关。
    不做缓存的话，比 N 个变体就要把同一批 XBRL 拉 N 遍 —— 而网络正是这里的大头
    （每份约 1 分钟，3 个变体就是 5 小时）。
    """

    CACHE = BASE / "_xbrl_cache"

    def __init__(self, api_key, use_cache=True):
        self.key = api_key
        self.use_cache = use_cache

    def _zip_bytes(self, doc_id):
        """取 type=1 的 ZIP **原始字节**。缓存的就是它。

        ⚠️ **缓存原始数据，不缓存派生结果。**（2026-09-28 改）
        原来的版本缓存的是「解析出来的配对表」—— 那样一旦改了 `html_tables`（解析器），
        缓存不会失效，**会继续吐用旧解析器算出来的"真值"，而且不报警**。
        这和本项目反复踩的坑是同一类：看起来对、实际是旧的。

        存原始 ZIP 之后，**原理上不可能过期** —— 派生结果每次现算（本地解析 <1 秒），
        省掉的仍然是重复下载（一次 ~1 分钟）。
        """
        p = self.CACHE / f"{doc_id}.zip"
        if self.use_cache and p.exists():
            return p.read_bytes()
        b = self._fetch_zip_bytes(doc_id)
        if self.use_cache:
            self.CACHE.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".zip.tmp")
            tmp.write_bytes(b)
            tmp.replace(p)                  # 原子替换
        return b

    def _fetch_zip_bytes(self, doc_id, tries=4):
        import requests
        last = None
        for i in range(tries):
            try:
                r = requests.get(f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}",
                                 params={"type": "1"},
                                 headers={"Ocp-Apim-Subscription-Key": self.key}, timeout=180)
                r.raise_for_status()
                return r.content
            except Exception as e:  # noqa: BLE001
                last = e
                if i < tries - 1:
                    time.sleep(3 * (i + 1))
        raise last

    def _zip(self, doc_id):
        return zipfile.ZipFile(io.BytesIO(self._zip_bytes(doc_id)))

    def _html(self, doc_id, only_honbun=False):
        zf = self._zip(doc_id)
        names = [n for n in sorted(zf.namelist()) if n.lower().endswith((".htm", ".html"))]
        if only_honbun:
            names = [n for n in names if "honbun" in n.lower()]
        return "".join(zf.read(n).decode("utf-8", "replace") for n in names)

    def facts(self, doc_id):
        """数值事实表（用于值召回）—— 也从同一个缓存 ZIP 派生。"""
        import edinet_xbrl as X
        return [f for f in X.parse_zip_facts(self._zip_bytes(doc_id), doc_id)
                if f["kind"] == "num" and f["value"] is not None]

    def labeled_facts(self, doc_id):
        """权威的 `(科目名, 数值)` 配对 —— 从表格结构里读，不是猜的。

        **每次现算**（从缓存的原始 ZIP 派生），所以改了 `html_tables` 立刻生效。
        """
        from html_tables import labeled_rows, parse_tables
        out = []
        for t in parse_tables(self._html(doc_id)):
            out.extend(labeled_rows(t))
        return out

    def control_text(self, doc_id):
        """对照组：**折成一行的原始正文**。

        它和真值同源，所以配对率必须 ≈100% —— 达不到就说明是测法坏了，不是被测量的错。
        """
        html = self._html(doc_id)
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        t = re.sub(r"(?is)<[^>]+>", " ", t)
        return " ".join(t.split())


# ============================================================ ③ 检查项

def check_value_recall(md, facts):
    """值召回：数字在 md 里出现了吗。"""
    nums = numbers_in(md)
    tested = hits = excluded = 0
    misses = []
    for f in facts:
        if not testable(f):
            excluded += 1
            continue
        tested += 1
        if numeric_hit(nums, f):
            hits += 1
        else:
            misses.append(f)
    return {"tested": tested, "hits": hits, "excluded": excluded,
            "rate": (hits / tested) if tested else None,   # 同上：0 条 ≠ 0%
            "misses": [{"concept": f["concept"], "value": f["value"]} for f in misses[:100]]}


def check_structure(md, labeled):
    """配对率：数值跟**正确的科目名在同一行**吗。**这是决定 A1/A11 的那一层。**

    失败必须**归因**，因为修法完全不同：
      · `label_missing`   —— 科目名在 md 里找不到（文本/章节丢失）
      · `value_absent`    —— 科目名在，但这个数**整篇都找不到**（数字被粘连/截断/丢了）
      · `value_elsewhere` —— 科目名在、数也在，但**不在同一行**（串行/错位）
    """
    lines = [(norm(l), numbers_in(l)) for l in md.split("\n") if l.strip()]
    all_nums = [n for _, ns in lines for n in ns]
    tested = hits = excluded = 0
    label_missing, value_absent, value_elsewhere = [], [], []
    for r in labeled:
        key = r["label_key"]
        facts = [f for f in r["facts"] if testable(f)]
        if not facts:
            excluded += 1
            continue
        tested += 1
        cand = [ns for ln, ns in lines if key in ln]
        if not cand:
            label_missing.append(r)
        elif any(numeric_hit(ns, f) for ns in cand for f in facts):
            hits += 1
        elif any(numeric_hit(all_nums, f) for f in facts):
            value_elsewhere.append(r)        # 数在，但不在该科目所在的行
        else:
            value_absent.append(r)           # 数整篇都没有
    return {"tested": tested, "hits": hits, "excluded": excluded,
            # ⚠️ `tested == 0` 必须返回 **None**，不能返回 0.0 ——
            #    否则「没有数据」会被报成「全军覆没」。实测有 4 份文档因为抽不出权威配对，
            #    配对率和**对照组**同时显示 0.0%，把尾部统计整个搞脏。
            #    这和「skip 不计入通过」是同一条纪律：**测不了 ≠ 测出来是零**。
            "rate": (hits / tested) if tested else None,
            "label_missing": len(label_missing),
            "value_absent": len(value_absent),
            "value_elsewhere": len(value_elsewhere),
            "samples": [r["label"] for r in (label_missing + value_absent + value_elsewhere)[:10]]}


# ============================================================ ④ 单文档评估

# 变体 → 解析器的开关组合。
#   order: pdf_to_md.ORDER_STRATEGY（阅读顺序）  col: table_detect.COLUMN_STRATEGY（列边界）
# ⚠️ 变体名**不捕获代码版本** —— `y` 是加丢字修复之前的基线，`fix1` 之后。
#    所以跨版本比较要看**同一次代码状态下**跑的两个变体（如 fix1 vs words）。
VARIANT_SWITCH = {
    "y":       {"order": "y",       "col": "hlines"},
    "fix1":    {"order": "y",       "col": "hlines"},   # 含丢字修复后的基线
    "natural": {"order": "natural", "col": "hlines"},
    "words":   {"order": "y",       "col": "words"},
}


def _apply_variant(variant):
    import pdf_to_md as P
    import table_detect as T
    sw = VARIANT_SWITCH.get(variant, {"order": "y", "col": "hlines"})
    if hasattr(P, "ORDER_STRATEGY"):
        P.ORDER_STRATEGY = sw["order"]
    if hasattr(T, "COLUMN_STRATEGY"):
        T.COLUMN_STRATEGY = sw["col"]
    return sw


def evaluate_document(doc_id, pdf_path, src, with_control=True, variant="y",
                      with_ixbrl=True):
    """一份文档的完整评估，含对照组。

    `variant` 切换解析器开关组合（阅读顺序 + 列边界），见 `VARIANT_SWITCH`。
    """
    import pdf_to_md as P
    t0 = time.time()
    _apply_variant(variant)
    out = {"doc_id": doc_id, "pdf": str(pdf_path), "variant": variant,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        md_pdf = P.convert(str(pdf_path), None)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["md_chars"] = len(md_pdf)

    rows = src.labeled_facts(doc_id)
    out["n_labeled"] = len(rows)
    out["structure"] = check_structure(md_pdf, rows)

    # 值召回要拿"全部数值事实"比，不只是表里的
    try:
        facts = src.facts(doc_id)
        out["n_facts"] = len(facts)
        out["value_recall"] = check_value_recall(md_pdf, facts)
    except Exception as e:  # noqa: BLE001
        out["value_recall_error"] = f"{type(e).__name__}: {e}"

    if with_control:
        ctl = src.control_text(doc_id)
        out["control"] = {
            "structure": check_structure(ctl, rows)["rate"],
            "value_recall": (check_value_recall(ctl, facts)["rate"]
                             if out.get("n_facts") else None),
        }

    # ⭐ **同一份文档、两条来源的并排对照**：PDF→md vs iXBRL→md。
    #    这才是"日股该不该换源"的直接判据 —— 不是看哪个"看起来好"，
    #    而是同一把尺子量出来的配对率。
    if with_ixbrl:
        try:
            import edinet_html
            md_ix = edinet_html.ixbrl_to_md(src._html(doc_id))
            out["ixbrl_md_chars"] = len(md_ix)
            out["ixbrl_structure"] = check_structure(md_ix, rows)
            if out.get("n_facts"):
                out["ixbrl_value_recall"] = check_value_recall(md_ix, facts)
        except Exception as e:  # noqa: BLE001
            out["ixbrl_error"] = f"{type(e).__name__}: {e}"
    # ⚠️ **输入匹配性检查** —— 与"测法对不对"是**两件事**，必须分开。
    # 实测：`S100PS7X` 是一份 2 页的訂正報告書（封面），PDF 文字层只有 870 字符，
    # 而 EDINET 的 XBRL 里带着完整财报数据 → 配对率 0.2%。
    # **它的对照组仍是 100%** —— 对照组只能证明"测法没错"，证明不了
    # "这份 PDF 本来就该包含那些内容"。不分开，就会把文档类型不匹配算成解析器的错。
    # 判据用**比例**而不是绝对字符数：正常文档每 1 组权威配对约摊到 300+ 字符的 md，
    # 而实测的坏样本（`S100JFE9` 6,790 字符 / 495 组、`S100N4TX` 1,576 / 194）
    # 只有十几到几十字符 —— 那说明 PDF 几乎没吐出内容，不是解析错。
    if out.get("n_labeled", 0) >= 50 and out.get("md_chars", 0) < 150 * out["n_labeled"]:
        out["suspect_input_mismatch"] = True
    # 另一种"测不了"：文档的表根本没打 XBRL 标签（实测 `S100UG8B` 96 张表、
    # 含标签单元格 0 个 —— 瑞士法郎计价的外国公司报表）。**这不是失败，是无法测量。**
    if out.get("n_labeled", 0) == 0:
        out["not_measurable"] = True
    out["secs"] = round(time.time() - t0, 1)
    return out


# ============================================================ ⑤ 批跑与报告

def find_pdf(doc_id):
    for pat in (f"{ARCHIVE}/jp/**/*{doc_id}*.pdf", f"{ARCHIVE}/jp/*{doc_id}*.pdf"):
        g = list(Path().glob(pat)) if not pat.startswith("Z") else []
        import glob as _g
        g = _g.glob(pat, recursive=True)
        if g:
            return g[0]
    return None


def sample_doc_ids(n, seed=42):
    """从日股存档文件名里抽 docID（文件名自带 docID：`20250331_annual_S100W29V.pdf`）。"""
    import glob
    import random
    files = glob.glob(f"{ARCHIVE}/jp/*.pdf")
    rnd = random.Random(seed)
    rnd.shuffle(files)
    out = []
    for f in files:
        base = os.path.basename(f)
        # ⚠️ **排除訂正報告書（amendment）**：它的 PDF 往往只是 2 页封面，
        #    而 XBRL 里带着完整财报 —— PDF 与 XBRL 覆盖的内容不是同一批，
        #    「配对率」对这类文档没有意义（实测会掉到 0.2%，把分布整个带偏）。
        #    裁判的前提是「同一份文档的两种格式」，这个前提不成立就不能比。
        if "_amendment_" in base or "_amendment." in base:
            continue
        m = re.search(r"(S[0-9A-Z]{6,})\.pdf$", base)
        if m:
            out.append((m.group(1), f))
        if len(out) >= n:
            break
    return out


def report(records):
    for mk in (None, "jp"):
        allrs = [r for r in records if r.get("structure") and (mk is None or r.get("market", "jp") == mk)]
        # ⚠️ 把「有效」和「无数据」分开 —— 混在一起会让分布和尾部统计都不准。
        #    判据用 `tested > 0`（而不是 `rate is not None`），因为**旧记录里存的是 0.0**
        #    —— 那是旧代码把"测不了"写成了"测出来是零"。用 tested 判对新旧都成立。
        # ⚠️ 「输入不匹配」的判据在**报告层**也要算一遍 —— 因为**旧记录里没有这个字段**
        #    （那批是用改动前的代码跑的）。只在写入时算，旧数据就漏判，分布会被污染。
        for r in allrs:
            if "suspect_input_mismatch" not in r:
                r["suspect_input_mismatch"] = (r.get("n_labeled", 0) >= 50
                                               and r.get("md_chars", 0) < 150 * r.get("n_labeled", 0))
        # 「有效」= 有可测配对、且 PDF 确实吐出了内容（不是输入不匹配）
        rs = [r for r in allrs if r["structure"].get("tested", 0) > 0
              and not r.get("suspect_input_mismatch")]
        novalid = [r for r in allrs if r not in rs]
        if not allrs:
            continue
        print(f"\n{'='*76}\n  【{mk or '全部'}】有效 {len(rs)} 份"
              + (f"｜无数据/失败 {len(novalid)} 份" if novalid else "") + f"\n{'='*76}")
        if novalid:
            print("  ⚠️ 无数据/失败（**不计入分布**，否则会把'测不了'算成'测出来是零'）: "
                  + ", ".join(f"{r['doc_id']}({r.get('error', '抽不出权威配对')[:24]})" for r in novalid[:5]))
        if not rs:
            continue
        for key, label in (("value_recall", "值召回"), ("structure", "配对率")):
            vals = [r[key]["rate"] for r in rs if r.get(key) and r[key].get("rate") is not None]
            if not vals:
                continue
            vals.sort()
            tested = sum(r[key]["tested"] for r in rs if r.get(key))
            excl = sum(r[key]["excluded"] for r in rs if r.get(key))
            print(f"  {label:8s} 中位 {vals[len(vals)//2]:.1%}  "
                  f"p10 {vals[len(vals)//10]:.1%}  p90 {vals[9*len(vals)//10]:.1%}  "
                  f"min {vals[0]:.1%}｜合计测 {tested} 条、排除 {excl} 条")
        # ⚠️ 归因要**按"占全部被测配对的比"**算，不能按"占全部失败的比" ——
        #    后者会被一份彻底坏掉的文档主导（实测一份坏文档独占 86% 的失败），
        #    聚合出来的"主要失败模式"其实是那一份的失败模式。
        mismatch = [r for r in rs if r.get("suspect_input_mismatch")]
        agg = Counter()
        all_tested = sum((r.get("structure") or {}).get("tested", 0) for r in rs) or 1
        for r in rs:
            for k in ("label_missing", "value_absent", "value_elsewhere"):
                agg[k] += r["structure"].get(k, 0)
        print(f"  失败归因（占 {all_tested} 组被测配对）: " + "｜".join(
            f"{k} {v} ({v/all_tested:.1%})" for k, v in agg.most_common()))

        # 尾部：多少份低于门槛 —— 这才是"最差的那批长什么样"
        for thr in (0.5, 0.8):
            bad = [r for r in rs if r["structure"]["rate"] < thr]
            if bad:
                print(f"  ⚠️ 配对率 <{thr:.0%} 的有 {len(bad)}/{len(rs)} 份: "
                      + ", ".join(f"{r['doc_id']}({r['structure']['rate']:.1%})" for r in bad[:6]))
        if mismatch:
            print(f"  ⚠️ 疑似「输入不匹配」（PDF 几乎没吐出内容）: {len(mismatch)} 份 — "
                  + ", ".join(f"{r['doc_id']}(md {r.get('md_chars', 0)}字)" for r in mismatch[:5])
                  + "\n     这些**不算解析器的错**。")
        nm = [r for r in allrs if r.get("not_measurable")]
        if nm:
            print(f"  ⚪ 无法测量（该文档的表没打 XBRL 标签）: {len(nm)} 份 — "
                  + ", ".join(r["doc_id"] for r in nm[:5]))
        ctls = [r["control"]["structure"] for r in rs
                if r.get("control") and r["control"].get("structure") is not None]
        if ctls:
            lo = sorted(ctls)[0]
            print(f"  对照组配对率 中位 {sorted(ctls)[len(ctls)//2]:.1%}｜最低 {lo:.1%}"
                  + ("  ✅" if lo >= 0.95 else "  ❌ 低于 95% —— 结论不可信"))


def state_path(variant):
    """每个变体一份结果文件 —— 才能对着**同一批文档**比。"""
    return BASE / ("referee_results.jsonl" if variant == "y"
                   else f"referee_results_{variant}.jsonl")


def load_state(variant):
    p = state_path(variant)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()]


def compare(variants):
    """同一样本上并排比对多个变体 —— **逐份配对**，只看两边都有效的文档。"""
    data = {v: {r["doc_id"]: r for r in load_state(v)
                if (r.get("structure") or {}).get("tested", 0) > 0
                and not r.get("suspect_input_mismatch")} for v in variants}
    common = set.intersection(*(set(d) for d in data.values())) if data else set()
    if not common:
        sys.exit("没有共同样本可比")
    print(f"\n{'='*78}\n  阅读顺序策略对照｜共同样本 {len(common)} 份\n{'='*78}")
    order = sorted(common)
    base = variants[0]
    print(f"  {'变体':10s} {'配对率中位':>10s} {'值召回中位':>10s} {'串行错位占比':>12s} {'改善(逐份)':>12s}")
    for v in variants:
        rs = [data[v][d] for d in order]
        pr = sorted(r["structure"]["rate"] for r in rs)
        vr = sorted(r["value_recall"]["rate"] for r in rs if r.get("value_recall"))
        ve = sum(r["structure"].get("value_elsewhere", 0) for r in rs)
        tt = sum(r["structure"]["tested"] for r in rs) or 1
        if v == base:
            tag = "—"
        else:
            win = sum(1 for d in order
                      if data[v][d]["structure"]["rate"] > data[base][d]["structure"]["rate"])
            lose = sum(1 for d in order
                       if data[v][d]["structure"]["rate"] < data[base][d]["structure"]["rate"])
            tag = f"+{win}/-{lose}"
        print(f"  {v:10s} {pr[len(pr)//2]:>10.1%} {vr[len(vr)//2]:>10.1%} "
              f"{ve/tt:>12.1%} {tag:>12s}")
    print("\n  「+赢/-输」是**逐份**比 —— 聚合均值会掩盖此消彼长。")


def main():
    args = sys.argv[1:]

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    variant = opt("--variant", "y")

    if "--compare" in args:
        compare([v.strip() for v in opt("--compare").split(",")])
        return

    if "--report" in args:
        recs = load_state(variant)
        if not recs:
            sys.exit("还没有结果")
        report(recs)
        return

    if "--batch" in args:
        limit = int(opt("--limit", 100))
        sp = state_path(variant)
        done = {r["doc_id"] for r in load_state(variant)}
        pairs = [p for p in sample_doc_ids(limit + len(done)) if p[0] not in done][:limit]
        if not pairs:
            print("没有待跑的（都已做过）")
            report(load_state(variant))
            return
        src = XbrlSource(load_key())
        print(f"批跑 {len(pairs)} 份（已完成 {len(done)}）｜阅读顺序策略 = {variant}")
        with sp.open("a", encoding="utf-8") as fh:
            for i, (doc, pdf) in enumerate(pairs, 1):
                # 单份失败不能拖垮整批 —— 记一条带 error 的结果，继续跑
                try:
                    rec = evaluate_document(doc, pdf, src, variant=variant)
                except Exception as e:  # noqa: BLE001
                    rec = {"doc_id": doc, "pdf": str(pdf), "variant": variant,
                           "error": f"{type(e).__name__}: {e}",
                           "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                st = (rec.get("structure") or {}).get("rate")
                tag = f"配对率 {st:.1%}" if st is not None else f"✗ {rec.get('error', '无结构')[:40]}"
                ctl = (rec.get("control") or {}).get("structure")
                ctl_tag = f"  (对照 {ctl:.0%})" if ctl is not None else ""
                print(f"  [{i}/{len(pairs)}] {doc:12s} {tag}{ctl_tag}", flush=True)
        report(load_state(variant))
        return

    # 单份模式
    doc = args[0] if args and not args[0].startswith("--") else "S100W29V"
    pdf = find_pdf(doc)
    if not pdf:
        sys.exit(f"存档里找不到 {doc}")
    src = XbrlSource(load_key())
    print(f"docID {doc}\nPDF   {pdf}")
    rec = evaluate_document(doc, pdf, src)
    print(f"\n  md {rec.get('md_chars', 0):,} 字符｜权威配对 {rec.get('n_labeled', 0)} 组"
          f"｜数值事实 {rec.get('n_facts', 0)} 条")
    for key, label in (("value_recall", "值召回"), ("structure", "配对率")):
        s = rec.get(key)
        if s:
            print(f"\n  【{label}】测 {s['tested']}｜命中 {s['hits']}｜**{s['rate']:.1%}**"
                  f"｜排除 {s['excluded']}")
            if key == "structure":
                print(f"     失败归因: 科目缺失 {s['label_missing']}｜"
                      f"数值整篇没有 {s['value_absent']}｜数值在但不在同行 {s['value_elsewhere']}")
    if rec.get("control"):
        print(f"\n  对照组: 配对率 {rec['control']['structure']:.1%}"
              f"｜值召回 {(rec['control'].get('value_recall') or 0):.1%}   ← 应≈100%")


if __name__ == "__main__":
    main()
