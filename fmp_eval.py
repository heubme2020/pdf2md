# -*- coding: utf-8 -*-
"""统一 FMP 真值评测 —— 跨市场（A股 / 日本 / 台湾 / 韩国），同一套指标。

    python fmp_eval.py --market ashare --build 300   # 建样本
    python fmp_eval.py --market ashare --run          # 跑（可续跑）
    python fmp_eval.py --market jp     --build 300
    python fmp_eval.py --market jp     --run
    python fmp_eval.py --market ashare --report

## 为什么能统一

FMP 覆盖 A股(.SS/.SZ) / 日本(.T) / 台湾(.TW/.TWO) / 韩国(.KS/.KQ)，
四个市场的**同一套 FMP 字段**（totalAssets/totalLiabilities/totalEquity/...），
只有「我们 md 里的科目名」随市场不同 —— 那个差异收在 `fmp_source.LABELS_BY_MARKET`。

## 三个过滤器（2026-09-30 数据说话，见记忆 pdf2md-gpu-baseline）

1. **存档垃圾**：PDF 无文字层（扫描件）/ 无资产负债表关键字（非年报）→ skip，不跑转换。
2. **重述断崖**：FMP 在重述年报时只改 Q4 行，Q3→Q4 断崖 >30% → skip「FMP重述」（解析器是对的）。
3. **标签口径**：证券/金融类用「所有者权益总额」「资产总额」等，已并入 LABELS_BY_MARKET。

## 各市场样本来源

- ashare：`raw/ashare/{code}/{period}_{type}_{id}.pdf`，代码在目录名里。
- jp：`raw/jp/{period}_{type}_{docID}.pdf`（无代码），靠 `_jp_meta_cache.json` 的 docID→代码映射。
- tw：`raw/tw/{code}/...`（存量 297 份多为 2006 旧季报 + 编码乱码，暂不接 FMP）。
- kr：无 PDF 存档（FMP 有数据但我们没 PDF），跳过。
"""
import argparse
import glob
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from fmp_source import FmpSource, LABELS_BY_MARKET, CORE_FIELDS, fmp_symbol  # noqa: E402
from pdf2md_referee import numbers_in, numeric_hit, norm  # noqa: E402

ARCHIVES = {
    "ashare": "Z:/datasinking-data/raw/ashare",
    "jp": "Z:/datasinking-data/raw/jp",
    "tw": "Z:/datasinking-data/raw/tw",
}
# 市场 → FMP 交易所代码（fmp_symbol 的 exchange 参数）
MARKET_EXCHANGE = {"ashare": None, "jp": "jpx", "tw": "twse", "kr": "ksc"}
# 市场 → fmp_source.LABELS_BY_MARKET 的键
MARKET_LABELS = {"ashare": "zh", "jp": "ja", "tw": "zh_tw", "kr": "ko"}
EXCHANGE_BY_PREFIX = {"6": "sse", "0": "szse", "3": "szse", "8": "bj", "4": "bj"}
MIN_VALUE = 1e6
ANNUAL_MIN_YEAR = 2019


# ---------------------------------------------------------------- 样本
def _parse_ashare(path):
    code = Path(path).parent.name
    m = re.match(r"(\d{4}-\d{2}-\d{2})_([a-z]+)_(\d+)\.pdf$", Path(path).name)
    if not m:
        return None
    ex = EXCHANGE_BY_PREFIX.get(code[0])
    if not ex:
        return None
    return {"stock_code": code, "exchange": ex, "report_period": m.group(1),
            "doc_type": m.group(2), "pdf": path}


def build_ashare(n, seed=42):
    files = glob.glob(f"{ARCHIVES['ashare']}/*/*.pdf")
    rnd = random.Random(seed)
    rnd.shuffle(files)
    out = []
    for f in files:
        m = _parse_ashare(f)
        if not m or m["doc_type"] != "annual" or int(m["report_period"][:4]) < ANNUAL_MIN_YEAR:
            continue
        out.append(m)
        if len(out) >= n:
            break
    return out


def build_jp(n, seed=42):
    meta = json.loads(Path(BASE / "_jp_meta_cache.json").read_text(encoding="utf-8"))
    rnd = random.Random(seed)
    rnd.shuffle(meta)
    out = []
    for m in meta:
        if m.get("doc_type") != "annual":
            continue
        per = m.get("report_period", "")
        if not per or int(per[:4]) < ANNUAL_MIN_YEAR:
            continue
        docid = m.get("announcement_id")
        if not docid:
            continue
        compact = per.replace("-", "")[:8]
        pdf = f"{ARCHIVES['jp']}/{compact}_annual_{docid}.pdf"
        if not os.path.exists(pdf):
            continue
        out.append({"stock_code": m["stock_code"], "exchange": "jpx",
                    "report_period": per, "doc_type": "annual", "pdf": pdf})
        if len(out) >= n:
            break
    return out


def build_sample(market, n, seed=42):
    if market == "jp":
        return build_jp(n, seed)
    return build_ashare(n, seed)


# ---------------------------------------------------------------- 过滤器
def precheck(pdf):
    """快速判定存档是否可用（不跑转换）。返回 (ok, reason)。"""
    import fitz
    try:
        doc = fitz.open(pdf)
    except Exception as e:  # noqa: BLE001
        return False, f"打开失败 {e}"
    full = "".join(p.get_text() for p in doc)
    doc.close()
    if len(full.strip()) < 50:
        return False, "扫描件（无文字层）"
    if not re.search(r"资产总计|资产合计|资产总额|資産合計|資產總計|자산총계", full):
        return False, "非年报（无资产负债表）"
    return True, ""


def score_doc(md, truth_rows, labels):
    """一个字段一行，命中任一可接受科目名即算命中。返回受控统计。labels = 该市场科目名表。"""
    lines = [(norm(l), numbers_in(l)) for l in md.split("\n") if l.strip()]
    allnums = [n for _, ns in lines for n in ns]
    tested = hits = 0
    label_missing = value_elsewhere = value_absent = 0
    for label_group, val in truth_rows:
        keys = [norm(k) for k in label_group]
        cand = [ns for ln, ns in lines if any(k in ln for k in keys)]
        tested += 1
        if not cand:
            label_missing += 1
        elif any(numeric_hit(ns, {"value": val, "text": None}) for ns in cand):
            hits += 1
        elif numeric_hit(allnums, {"value": val, "text": None}):
            value_elsewhere += 1
        else:
            value_absent += 1
    return {"tested": tested, "hits": hits,
            "rate": (hits / tested) if tested else None,
            "label_missing": label_missing, "value_elsewhere": value_elsewhere,
            "value_absent": value_absent}


# ---------------------------------------------------------------- 主流程
def sample_file(market):
    return BASE / f"fmp_eval_sample_{market}.json"


def state_file(market):
    return BASE / f"fmp_eval_results_{market}.jsonl"


def run(market, limit=0):
    import pdf_to_md as P

    sf = sample_file(market)
    samples = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else build_sample(market, 300)
    labels = LABELS_BY_MARKET[MARKET_LABELS[market]]
    stf = state_file(market)
    done = set()
    if stf.exists():
        for line in stf.open(encoding="utf-8"):
            if line.strip():
                try:
                    done.add(json.loads(line)["pdf"])
                except Exception:  # noqa: BLE001
                    pass
    todo = [s for s in samples if s["pdf"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"[{market}] 样本 {len(samples)}｜已完成 {len(done)}｜本次 {len(todo)}")

    src = FmpSource(use_cache=True)
    with stf.open("a", encoding="utf-8") as fh:
        for i, s in enumerate(todo, 1):
            rec = dict(s)
            t0 = time.time()
            try:
                ok, why = precheck(s["pdf"])
                if not ok:
                    rec["skip_reason"] = why
                else:
                    ex = s["exchange"]
                    rows = src.labeled_facts(s["stock_code"], ex, s["report_period"])
                    if not rows:
                        rec["skip_reason"] = "FMP 没有该期间（或对不齐）"
                    else:
                        sym = fmp_symbol(s["stock_code"], ex)
                        skip = ""
                        # 过滤器①：FMP 自洽性（totalAssets ≈ totalLiabilities + totalEquity）。
                        # FMP 偶有坏数据（如 9014.T 被收购后恒等式差 65%），跳过不算我们。
                        _v = {r["field"]: r["facts"][0]["value"] for r in rows if r.get("facts")}
                        _ta, _tl, _te = _v.get("totalAssets"), _v.get("totalLiabilities"), _v.get("totalEquity")
                        if all(isinstance(x, (int, float)) for x in (_ta, _tl, _te)) and _ta > 1e6 \
                                and abs(_ta - (_tl + _te)) / _ta > 0.02:
                            skip = "FMP数据不一致(恒等式)"
                        # 过滤器②：重述断崖（FMP 重述年报只改 Q4 行）
                        if not skip:
                            jump, _, _ = src.q3_q4_jump(sym, s["report_period"][:4])
                            if jump is not None and abs(jump) > 0.30:
                                skip = f"FMP重述(Q3→Q4 {jump:+.0%})"
                        if skip:
                            rec["skip_reason"] = skip
                        else:
                            grouped, seen = [], set()
                            for r in rows:
                                if r["field"] in seen or not r.get("facts"):
                                    continue
                                seen.add(r["field"])
                                grouped.append((labels[r["field"]], r["facts"][0]["value"]))
                            md = P.convert(s["pdf"], None)
                            rec["md_chars"] = len(md)
                            rec["score"] = score_doc(md, grouped, labels)
                            rec["n_fields"] = len(grouped)
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"
            rec["secs"] = round(time.time() - t0, 1)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            sc = rec.get("score") or {}
            tag = f"配对率 {sc['rate']:.1%} ({sc['hits']}/{sc['tested']})" if sc.get("rate") is not None \
                else rec.get("skip_reason") or rec.get("error", "无数据")
            print(f"  [{i}/{len(todo)}] {s['stock_code']} {s['report_period']} {tag}", flush=True)
    report(market)


def report(market):
    stf = state_file(market)
    if not stf.exists():
        sys.exit("还没有结果")
    rs = [json.loads(l) for l in stf.open(encoding="utf-8") if l.strip()]
    ok = [r for r in rs if (r.get("score") or {}).get("rate") is not None]
    skips = {}
    for r in rs:
        if (r.get("score") or {}).get("rate") is None:
            skips[r.get("skip_reason") or r.get("error", "?")] = skips.get(r.get("skip_reason") or r.get("error", "?"), 0) + 1
    print(f"\n{'='*72}\n  FMP 真值评测 [{market}]｜共 {len(rs)} 份，有效 {len(ok)} 份\n{'='*72}")
    if skips:
        print("  跳过原因: " + "｜".join(f"{k} {v}" for k, v in sorted(skips.items(), key=lambda x: -x[1])))
    if not ok:
        return
    rates = sorted(r["score"]["rate"] for r in ok)
    print(f"  配对率  中位 {statistics.median(rates):.1%}｜p10 {rates[len(rates)//10]:.1%}"
          f"｜min {rates[0]:.1%}｜max {rates[-1]:.1%}")
    agg = {k: sum(r["score"][k] for r in ok)
           for k in ("label_missing", "value_elsewhere", "value_absent")}
    tot = sum(r["score"]["tested"] for r in ok) or 1
    print(f"  失败归因（占 {tot} 字段）: " + "｜".join(f"{k} {v} ({v/tot:.1%})" for k, v in agg.items()))
    for thr in (0.5, 0.8):
        bad = [r for r in ok if r["score"]["rate"] < thr]
        if bad:
            print(f"  ⚠️ <{thr:.0%} 的 {len(bad)}/{len(ok)} 份: "
                  + ", ".join(f"{r['stock_code']}({r['score']['rate']:.0%})" for r in bad[:8]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="ashare", choices=["ashare", "jp", "tw", "kr"])
    ap.add_argument("--build", type=int, default=0)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.build:
        samples = build_sample(a.market, a.build)
        sample_file(a.market).write_text(json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{a.market}] 样本已冻结 {len(samples)} 份 → {sample_file(a.market).name}")
    elif a.run:
        run(a.market, a.limit)
    else:
        report(a.market)


if __name__ == "__main__":
    main()
