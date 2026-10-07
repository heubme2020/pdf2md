# -*- coding: utf-8 -*-
"""PDF→Markdown 算法基准跑批 —— 用 `pdf2md_metrics.py` 的指标给各算法打分。

**为什么要有这个**：2026-09-22 之前 `bench_*.py` 只看"耗时/字符数/表格行数"，
那些是**量**不是**质**——解析崩了多吐重复文本，字符数反而更高。没有质量指标，
"调最好的算法"就是盲调。这个脚本把质量指标跑成基线，任何新算法都能拿同一把尺子量。

## 用法

    python pdf2md_bench.py                      # 三个市场各跑 20 份，当前 PyMuPDF 转换器
    python pdf2md_bench.py --limit 0            # 不限量（全部 611 份）
    python pdf2md_bench.py --markets ashare     # 只跑 A 股
    python pdf2md_bench.py --workers 4          # 并行（PDF 解析是 CPU 密集）
    python pdf2md_bench.py --converter pymupdf --out bench_quality_pymupdf.json

## 加新算法

在 `CONVERTERS` 里加一项即可：一个 `(pdf_path, out_path) -> None` 的函数。
（Docling / MinerU 要装几 GB 依赖，按需再加。）

## 怎么读结果

- `pass` = 会计恒等式**验算通过** → 数字是对的（最强证据）
- `fail` = 找到了数字但**验不平** → 抽错了或串行了
- `skip` = **连标签都没找到** → 财报竟然提不出资产负债表，等于解析失败
  ⚠️ 所以"有效率"要按 `pass / 总数` 算，**skip 不能算通过**。
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
BENCH = BASE / "bench_pdfs"
MARKETS = ["ashare", "jp", "tw"]

# 原件存档目录。--from-archive 时从这里抽样；用 ARCHIVE_DIR 环境变量指向你自己的财报 PDF 归档。
# 注意韩国是 DART 的 XML，**不能喂给 pdf2md**，所以不在可抽样市场里。
ARCHIVE = Path(os.environ.get("ARCHIVE_DIR", "./raw"))
ARCHIVE_MARKETS = ["ashare", "jp", "tw"]


def sample_from_archive(market, n, seed=0):
    """从 Z 盘存档里随机抽 n 份 PDF。

    为什么要这个：`bench_pdfs/` 是**固定的 611 份**，适合"换算法时比同一批"；
    但要看**真实语料**上的表现（尤其 VPS 上没有的样本），就得从全量存档里抽。
    """
    import random
    root = ARCHIVE / market
    if not root.is_dir():
        return []
    all_pdfs = list(root.rglob("*.pdf"))
    if not all_pdfs:
        return []
    rnd = random.Random(seed)
    rnd.shuffle(all_pdfs)
    return [str(p) for p in all_pdfs[:n]]


# ---------------------------------------------------------------- 转换器

def conv_pymupdf(pdf_path, out_path):
    """VPS 线：当前的转换器（pdf_to_md，线条法 + PyMuPDF）。"""
    import pdf_to_md
    pdf_to_md.convert(pdf_path, out_path)


# Docling 的 DocumentConverter 初始化很贵（要加载模型），每个进程只建一次
_DOCLING = None


def conv_docling(pdf_path, out_path):
    """本地线候选 A：Docling（IBM，深度学习版面分析 + 表格识别）。

    第一次调用会下载模型（几百 MB），之后走本地缓存。
    """
    global _DOCLING
    if _DOCLING is None:
        from docling.document_converter import DocumentConverter
        _DOCLING = DocumentConverter()
    res = _DOCLING.convert(pdf_path)
    Path(out_path).write_text(res.document.export_to_markdown(), encoding="utf-8")


CONVERTERS = {
    "pymupdf": conv_pymupdf,   # VPS 线（基线）
    "docling": conv_docling,         # 本地线候选
}


# ---------------------------------------------------------------- 单份处理

def run_one(args):
    """在子进程里跑一份 PDF：转换 + 算指标。返回 dict。"""
    pdf_path, converter_name, tmpdir = args
    from pdf2md_metrics import evaluate

    out_md = os.path.join(tmpdir, Path(pdf_path).stem + ".md")
    rec = {"pdf": os.path.basename(pdf_path), "path": pdf_path,
           "converter": converter_name}
    t0 = time.time()
    try:
        CONVERTERS[converter_name](pdf_path, out_md)
        md = Path(out_md).read_text(encoding="utf-8", errors="replace")
        rec["ok"] = True
    except Exception as e:  # noqa: BLE001
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["trace"] = traceback.format_exc()[-400:]
        md = ""
    rec["secs"] = round(time.time() - t0, 2)

    if rec["ok"]:
        rec["metrics"] = evaluate(pdf_path, md)
    else:
        rec["metrics"] = None
    try:
        os.remove(out_md)
    except OSError:
        pass
    return rec


# ---------------------------------------------------------------- 汇总

def _pct(n, d):
    return f"{100.0 * n / d:.1f}%" if d else "—"


def summarize(records):
    n = len(records)
    ok = [r for r in records if r["ok"]]
    out = {"total": n, "converted_ok": len(ok), "crash_rate": 1 - len(ok) / n if n else None}

    if not ok:
        return out

    ai = [r["metrics"]["accounting_identity"]["status"] for r in ok]
    passed = ai.count("pass")
    failed = ai.count("fail")
    skipped = ai.count("skip")

    tabs = [r["metrics"]["table_alignment"] for r in ok]
    tot_tables = sum(t["tables"] for t in tabs)
    bad_tables = sum(t["misaligned"] for t in tabs)

    cov = [r["metrics"]["content_coverage"]["ratio"]
           for r in ok if r["metrics"]["content_coverage"].get("status") == "ok"]
    gar = [r["metrics"]["garbled"]["ratio"] for r in ok]
    secs = [r["metrics"]["sections"]["count"] for r in ok]

    out.update({
        # ⚠️ 有效率按 pass/总数 —— skip 是"连标签都没找到"，对财报而言等于解析失败
        "identity_pass": passed, "identity_fail": failed, "identity_skip": skipped,
        "identity_pass_rate": passed / len(ok),
        "tables_total": tot_tables, "tables_misaligned": bad_tables,
        "table_alignment_rate": (1 - bad_tables / tot_tables) if tot_tables else None,
        "coverage_median": statistics.median(cov) if cov else None,
        "coverage_min": min(cov) if cov else None,
        "coverage_max": max(cov) if cov else None,
        "garbled_median": statistics.median(gar) if gar else None,
        "garbled_papers": sum(1 for g in gar if g > 0),
        "sections_median": statistics.median(secs) if secs else None,
        "seconds_total": round(sum(r["secs"] for r in ok), 1),
        "seconds_median": statistics.median([r["secs"] for r in ok]),
    })
    return out


def show(label, s):
    print(f"\n{'='*64}\n  {label}\n{'='*64}")
    print(f"  样本 {s['total']} ｜ 转换成功 {s['converted_ok']} ｜ 崩溃率 {_pct(s.get('crash_rate', 0)*s['total'], s['total'])}")
    if not s.get("identity_pass_rate") and s.get("identity_pass_rate") != 0:
        return
    print(f"\n  A1 会计恒等式（最强的质量证据）")
    print(f"     ✅ 验算通过 {s['identity_pass']:>3}  ({_pct(s['identity_pass'], s['converted_ok'])})")
    print(f"     ❌ 验不平   {s['identity_fail']:>3}  ({_pct(s['identity_fail'], s['converted_ok'])})")
    print(f"     ⏭  提不出   {s['identity_skip']:>3}  ({_pct(s['identity_skip'], s['converted_ok'])})")
    print(f"  A2 表格对齐率   {_pct(s['tables_total']-s['tables_misaligned'], s['tables_total'])}"
          f"   ({s['tables_total']-s['tables_misaligned']}/{s['tables_total']} 张表对齐)")
    if s.get("coverage_median"):
        print(f"  A3 内容覆盖率   中位 {s['coverage_median']:.2f}x   "
              f"(范围 {s['coverage_min']:.2f}–{s['coverage_max']:.2f}x；<0.9 疑丢内容，>1.3 疑重复)")
    print(f"  A4 乱码         中位 {s['garbled_median']:.5f}   {s['garbled_papers']} 份有乱码字符")
    print(f"  A5 章节数       中位 {s['sections_median']:.0f}")
    print(f"  ⏱  耗时         中位 {s['seconds_median']:.1f}s/份，合计 {s['seconds_total']:.0f}s")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--converter", default="pymupdf", choices=sorted(CONVERTERS))
    ap.add_argument("--markets", default=",".join(MARKETS))
    ap.add_argument("--limit", type=int, default=20, help="每个市场跑几份；0 = 不限")
    ap.add_argument("--workers", type=int, default=4, help="并行进程数（PDF 解析是 CPU 密集）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--from-archive", action="store_true",
                    help=f"从原件存档抽样（默认 {ARCHIVE}），而不是 bench_pdfs/")
    ap.add_argument("--seed", type=int, default=0, help="存档抽样的随机种子（固定种子 = 可复现）")
    args = ap.parse_args()

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    jobs, by_market = [], {}
    for m in markets:
        if args.from_archive:
            if m not in ARCHIVE_MARKETS:
                print(f"  [跳过] {m}: 存档里没有可用的 PDF（韩国是 XML）")
                continue
            n = args.limit or 50
            pdfs = sample_from_archive(m, n, seed=args.seed)
        else:
            pdfs = sorted(str(p) for p in (BENCH / m).glob("*.pdf"))
            if args.limit:
                pdfs = pdfs[:args.limit]
        by_market[m] = len(pdfs)
        for p in pdfs:
            jobs.append((p, args.converter, tempfile.gettempdir()))

    if not jobs:
        src = f"存档 {ARCHIVE}" if args.from_archive else "bench_pdfs/"
        sys.exit(f"{src} 下没有找到 PDF（看了这些市场: {markets}）")

    print(f"转换器: {args.converter} ｜ 市场: {', '.join(f'{m}({c})' for m, c in by_market.items())}")
    print(f"共 {len(jobs)} 份，{args.workers} 进程并行\n")

    records, done = [], 0
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, j): j for j in jobs}
        for f in as_completed(futs):
            rec = f.result()
            records.append(rec)
            done += 1
            flag = "ok " if rec["ok"] else "ERR"
            ai = (rec["metrics"] or {}).get("accounting_identity", {}).get("status", "-")
            print(f"  [{done:>3}/{len(jobs)}] {flag} {rec['pdf'][:38]:<40} {ai:<5} {rec['secs']:>6.1f}s")

    print(f"\n总耗时 {time.time()-t0:.0f}s")

    results = {"converter": args.converter, "records": records, "summary": {}}
    for m in markets:
        sel = [r for r in records if os.sep + m + os.sep in r["path"]]
        s = summarize(sel)
        results["summary"][m] = s
        show(f"{m}  ({by_market[m]} 份)", s)
    overall = summarize(records)
    results["summary"]["ALL"] = overall
    show("全部合计", overall)

    out = args.out or str(BASE / f"bench_quality_{args.converter}.json")
    Path(out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果已存: {out}")


if __name__ == "__main__":
    main()
