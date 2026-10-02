# -*- coding: utf-8 -*-
"""A 股章节校验 —— 模板来自**证监会准则原文**，不是从我们自己的语料里统计的。

## 为什么 A 股要这个，日/台/韩不要

    日 / 台 / 韩：文档**声明**自己的结构  →  我们【读】它（iXBRL 锚点 / h1）
    A 股        ：监管**规定**章节清单    →  我们【对】它

A 股 没有公开 XBRL（实测巨潮公告记录的 `adjunctType` 就是 `PDF`），没有那份"标准答案文件"。
但《公开发行证券的公司信息披露内容与格式准则》把章节列死了。

⚠️ **白名单必须来自监管原文，不能从语料统计** —— 从语料统计是**循环的**
（语料里有什么就算什么合法），那量出来的还是我们自己的解析结果。

## ⚠️ 必须按版本切片 —— 准则改过好几版

| 年报准则 | 节数 | 适用于期末年 |
|---|---|---|
| 2025 版（公告〔2025〕3 号，**2025-07-01 施行**） | **8** | 2025+ |
| 2021 版（公告〔2021〕15 号） | 10 | 2021–2024 |
| 2017 版 | 12 | 2018–2020 |
| 2016 版 | 12 | 2016–2017 |
| 2015 版 | 11 | 2015 |
| 2014 版 | 12 | 2013–2014（含「内部控制」） |
| 2012 版 | 11 | ≤2012 |

**分界点由真实披露验证过**：茅台 2024 年报 10 节（2021 版）/ 2025 年报 **8 节**（2025 版）。
用单一模板会让 2025-07-01 之后的报告整体错位。

## ⚠️ 三个必须容忍的真实不一致（都实测过，不是推测）

1. **第一节名字不统一**：规则写「重要提示、目录和释义」，**茅台实际写「第一节 释义」**，
   万科 A 则与规则一字不差。同一规则、同年、同所，两种写法。
2. **「和 / 与」不一致**：规则写「环境**和**社会责任」，茅台实际写「环境**与**社会责任」。
3. **A+H 公司整份不遵守准则**：万科 A 年报用 H 股模板，有「致股东」「企业管治报告」
   「监事会报告」—— **这些词准则里根本不存在**。这类不能判成"错"，要单独归类。

所以比对前要**归一化**，且匹配用「包含」而非全等。
"""
import re

# ── 各版本的年报正文节名（**逐字抄自 CSRC 原文**，不是改写）────────────────
ANNUAL = {
    "2025": ["重要提示、目录和释义", "公司简介和主要财务指标", "管理层讨论与分析",
             "公司治理、环境和社会", "重要事项", "股份变动及股东情况",
             "债券相关情况", "财务报告"],
    "2021": ["重要提示、目录和释义", "公司简介和主要财务指标", "管理层讨论与分析",
             "公司治理", "环境和社会责任", "重要事项", "股份变动及股东情况",
             "优先股相关情况", "债券相关情况", "财务报告"],
    "2017": ["重要提示、目录和释义", "公司简介和主要财务指标", "公司业务概要",
             "经营情况讨论与分析", "重要事项", "股份变动及股东情况", "优先股相关情况",
             "董事、监事、高级管理人员和员工情况", "公司治理", "公司债券相关情况",
             "财务报告", "备查文件目录"],
    "2015": ["重要提示、目录和释义", "公司简介和主要财务指标", "公司业务概要",
             "管理层讨论与分析", "重要事项", "股份变动及股东情况", "优先股相关情况",
             "董事、监事、高级管理人员和员工情况", "公司治理", "财务报告", "备查文件目录"],
    "2014": ["重要提示、目录和释义", "公司简介", "会计数据和财务指标摘要", "董事会报告",
             "重要事项", "股份变动及股东情况", "优先股相关情况",
             "董事、监事、高级管理人员和员工情况", "公司治理", "内部控制",
             "财务报告", "备查文件目录"],
    "2012": ["重要提示、目录和释义", "公司简介", "会计数据和财务指标摘要", "董事会报告",
             "重要事项", "股份变动及股东情况",
             "董事、监事、高级管理人员和员工情况", "公司治理", "内部控制",
             "财务报告", "备查文件目录"],
}
ANNUAL["2016"] = ANNUAL["2017"]

# 半年报：2025/2021 版与同期年报**逐字相同**；2017 版比同期年报少「公司治理」（唯一结构差）
SEMIANNUAL = {"2025": ANNUAL["2025"], "2021": ANNUAL["2021"],
              "2017": [s for s in ANNUAL["2017"] if s != "公司治理"]}

# ⚠️ **季报不能用准则清单。** 《编报规则第13号》正文只有 3 节，且「财务报表」在
#    第三章「附录」里、不是"节"。实际披露是 4 节（深主板部分），或**根本不用「第X节」**
#    （沪市 / 创业板实测 0 个）。这里只是把准则那 3 节留着做参考，**不参与判定**。
Q_REPORT_PATTERN = ["重要提示", "公司基本情况", "重要事项"]

# 期末年 → 用哪一版（年报/半年报）
_BY_YEAR = [(2025, "2025"), (2021, "2021"), (2018, "2017"), (2016, "2016"),
            (2015, "2015"), (2013, "2014"), (0, "2012")]

# 这些词**只在模板外发行人（多为 A+H）的披露里**出现 —— 命中就归为「模板外」，
# **不报错**。实测来自万科 A 年报（H 股风格）。
_OFF_TEMPLATE = ("致股东", "企业管治", "监事会报告", "董事会报告")

SEC_LINE = re.compile(r"^#{0,3}\s*(第[一二三四五六七八九十百\d]+[节章節])\s*([^\n]{0,40})")
TOC_LINE = re.compile(r"[.．·]{4,}|\d{1,4}\s*$")


def _norm(s):
    """归一化：去空白/标点，**和↔与统一**，「、目录和」这类可省部分去掉。

    实测的三种不一致（见模块头）全靠这一步吸收：
      「环境和社会责任」vs「环境与社会责任」  → 和↔与
      「重要提示、目录和释义」vs「释义」      → 去「、目录和」
    """
    s = re.sub(r"[\s　]|\.{2,}|[·．]{2,}", "", s or "")
    s = s.replace("与", "和")
    s = s.replace("、目录和", "")
    s = s.replace("目录和", "")
    return s


def _pick_version(doc_type, period):
    """按**披露时点**选版本。年报/半年报按期末年；实际分界点是披露日 2025-07-01
    （2025 准则 2025-07-01 施行）—— 期末年 2024 的年报在 2025-04 披露，仍走 2021 版，
    所以按**期末年**切就够了，不必再算披露日。"""
    y = int(str(period)[:4]) if period and str(period)[:4].isdigit() else 0
    for lo, ver in _BY_YEAR:
        if y >= lo:
            return ver
    return "2012"


def scan_sections(md):
    """扫 md 正文，返回 `[(节号, 名字), …]`。**目录行排除** —— 它们也含「第X节」但不算边界。

    ⚠️ **不要求章节行被标成标题**：实测季报的章节名大量留在正文里没被抽成 `##`。
    """
    out = []
    for line in md.split("\n"):
        if not line.strip() or TOC_LINE.search(line):
            continue
        m = SEC_LINE.match(line)
        if m:
            out.append((m.group(1), re.sub(r"\s+", "", m.group(2))[:30]))
    return out


def check(md, doc_type, report_period=None):
    """校验一份 md 的章节结构。

    返回 `{status, version, expected, found, missing, extra, off_template, ratio}`
      · `status`: ok（有模板可比）/ skip（季报等无可用模板）
      · `ratio`  = 命中的节数 / 应有节数
      · `extra`  = 抽到但模板里没有的节名（可能是伪标题，也可能是模板外写法）
      · `off_template` = True 时说明命中了 H 股风格词，**这类不报错**

    ⚠️ 季报**不给分数**：准则 3 节、实际 4 节或 0 节，**准则清单不可用**。
       强行用会造出一个骗人的数字 —— 宁可不给。
    """
    if doc_type == "amendment":
        return {"status": "skip", "reason": "修订公告没有固定章节结构"}
    if doc_type in ("q1", "q3"):
        return {"status": "skip", "reason": "季报无可用模板（准则 3 节 vs 实际 4 节/0 节）"}

    ver = _pick_version(doc_type, report_period)
    tpl = (SEMIANNUAL if doc_type == "semiannual" else ANNUAL).get(ver)
    if not tpl:
        return {"status": "skip", "reason": f"没有 {doc_type}/{ver} 的模板"}

    seen = scan_sections(md)
    if not seen:
        return {"status": "ok", "version": ver, "expected": len(tpl),
                "found": 0, "missing": list(tpl), "extra": [], "off_template": False,
                "ratio": 0.0}

    names = [_norm(n) for _, n in seen]
    joined = "".join(names)
    if any(w in joined for w in _OFF_TEMPLATE):
        return {"status": "skip", "version": ver,
                "reason": "模板外发行人（疑似 A+H，H 股风格章节名）",
                "off_template": True, "sections": [n for _, n in seen][:12]}

    tpl_n = [_norm(t) for t in tpl]
    missing = [t for t, tn in zip(tpl, tpl_n)
               if not any(tn in n or n in tn for n in names)]
    found = len(tpl) - len(missing)
    # extra：抽到的名字里，既不匹配任何模板节、也不是"第X节"编号本身的
    extra = [n for _, n in seen
             if len(n) > 3 and not any(tn in _norm(n) or _norm(n) in tn for tn in tpl_n)]
    return {"status": "ok", "version": ver, "expected": len(tpl), "found": found,
            "missing": missing, "extra": extra[:6], "off_template": False,
            "ratio": found / len(tpl), "sections": [f"{a}{b}" for a, b in seen][:14]}


if __name__ == "__main__":
    import glob
    import json
    import random
    import statistics
    import sys
    from collections import Counter
    from pathlib import Path

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    meta = {m["announcement_id"]: m for m in
            json.loads((Path(__file__).resolve().parent / "_ashare_meta_cache.json").read_text(encoding="utf-8"))}
    files = glob.glob("Z:/datasinking-data/data/*/md/*/*.md")
    random.seed(11)
    random.shuffle(files)
    agg, skipped = {}, Counter()
    for f in files:
        m = meta.get(Path(f).name[:-3])
        if not m:
            continue
        r = check(Path(f).read_text(encoding="utf-8", errors="replace"),
                  m.get("doc_type") or "?", m.get("report_period"))
        if r.get("status") != "ok":
            skipped[(m.get("doc_type"), r.get("reason", "")[:26])] += 1
            continue
        agg.setdefault((m.get("doc_type"), r["version"]), []).append(r)
        if sum(len(v) for v in agg.values()) >= N:
            break

    print(f"章节完整率（模板来自 CSRC 原文，按版本切片）\n")
    for (dt, ver), rs in sorted(agg.items(), key=lambda x: -len(x[1])):
        ratios = sorted(r["ratio"] for r in rs)
        full = sum(1 for r in rs if r["ratio"] == 1.0)
        miss = Counter(x for r in rs for x in r["missing"])
        print(f"  {dt:11s} {ver}准则  {len(rs):>4d} 份｜完整率 中位 {statistics.median(ratios):>4.0%}"
              f"｜**全节齐 {full:>3d} ({full/len(rs):>3.0%})**")
        if miss:
            print(f"        缺得最多: {dict(miss.most_common(3))}")
    print()
    for (dt, why), n in skipped.most_common(6):
        print(f"  跳过 {dt}：{why}  （{n} 份）")
