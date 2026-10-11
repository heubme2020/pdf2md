# -*- coding: utf-8 -*-
"""现金流量表恒等式 C1 自洽评测 ——「表格结构」维度（补 FMP 配对率的盲区）。

C1: 期末现金 = 期初现金 + 经营 + 投资 + 筹资 + 汇率变动。

为什么它能测「表格结构」：跨页表被拆断 / 复杂表头铺错列 → 某一项丢失或错位 → C1 不平。
不需要外部真值，只认文档内部会计恒等式，将来也能套到「没有 FMP 真值」的市场。

用法:
    python _cashflow_eval.py --limit 20     # 快检
    python _cashflow_eval.py                # 全量
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pdf_to_md as P  # noqa: E402

SAMPLE = BASE / "ashare_eval_sample.json"
MANIFEST = BASE / "bench_pdfs" / "testset" / "manifest.json"

# 金额：可带负号/小数/逗号，也允许 "$" 前缀（对齐 _balance_sheet_eval 的宽容度）
AMOUNT = re.compile(r"^\(?-?\$?([\d,]+(?:\.\d+)?)\)?$")


def _to_float(s):
    """金额字符串 → float（兼容负值括号记法「(7,754)」= -7754）。"""
    s = s.replace(",", "").replace("$", "").strip()
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    return float(s)

# (key, 标签正则) —— search 是子串匹配，天然容纳「四、」「加：」「六、」等前缀
ITEMS = [
    ("opening", re.compile(r"(?:期初|年初)现金及现金等价物余额")),
    ("closing", re.compile(r"(?:期末|年末)现金及现金等价物余额")),
    ("op", re.compile(r"经营活动[^|]*?现金流量净额")),  # 允许「(使用)/产生」「/(使用)」变体
    ("inv", re.compile(r"投资活动[^|]*?现金流量净额")),
    ("fin", re.compile(r"筹资活动[^|]*?现金流量净额")),
    ("fx", re.compile(r"汇率变动对现金及现金等价")),  # 放宽：「影响」常被换行截断
]


def extract_cf(md, report_period):
    """从 md 里抽现金流六项 → {key: float or None}。

    只取「合并现金流量表」（碰到「母公司现金流量表」就停），并按 report_period
    的年度选「本期」列（表头 `| 项目 | 附注 | 2024年度 | 2023年度 |` 里带当年年度的列；
    没有表头行则退回默认「本期=第 2 列」）。
    """
    year = str(report_period)[:4]
    out = {key: None for key, _ in ITEMS}
    col_idx = 2  # 默认：项目=0、附注=1、本期=2
    in_merge = False

    for l in md.split("\n"):
        s = l.strip()
        s_ns = s.replace(" ", "")
        if "合并现金流量表" in s_ns:
            in_merge = True
            continue
        if in_merge and "母公司现金流量表" in s_ns:
            break  # 合并表结束，不再往下
        if not in_merge or not s.startswith("|"):
            continue

        cells = [c.replace(" ", "") for c in s.strip("|").split("|")]
        # 表头行：带「项目」「附注」→ 按年度找本期列
        if any(c == "项目" for c in cells) and any(c == "附注" for c in cells):
            for ci, c in enumerate(cells):
                if year in c:
                    col_idx = ci
                    break
            continue
        if len(cells) <= col_idx:
            continue
        for key, pat in ITEMS:
            if out[key] is not None:
                continue
            # 先试单 cell（标准表，用本期列）；取不到再试整行拼接 + 第一个数字
            if any(pat.search(c) for c in cells):
                m = AMOUNT.match(cells[col_idx])
                if m:
                    out[key] = _to_float(m.group(0))
                    continue
            if pat.search("".join(cells)):
                for c in cells:  # 取行内第一个数字（本期）
                    m = AMOUNT.match(c)
                    if m:
                        out[key] = _to_float(m.group(0))
                        break
    return out


def check_c1(items):
    """返回 (ok, 说明) —— C1: 期末 = 期初 + 经营 + 投资 + 筹资 + 汇率。

    fx（汇率变动）可选：很多公司无外币业务、没有这一行 → 缺失按 0。
    """
    need = ["opening", "closing", "op", "inv", "fin"]
    miss = [k for k in need if items.get(k) is None]
    if miss:
        return False, f"缺项: {','.join(miss)}"
    opening, closing = items["opening"], items["closing"]
    fx = items.get("fx") or 0.0  # fx 缺失按 0（无外币业务）
    rhs = opening + items["op"] + items["inv"] + items["fin"] + fx
    tol = max(abs(opening), abs(closing), 1.0) * 0.002  # 0.2% 容差
    if abs(closing - rhs) <= tol:
        return True, ""
    return False, f"不平: 期末{closing:,.0f} vs 期初+五项={rhs:,.0f} (差 {closing - rhs:,.0f})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    # 读冻结测试集 manifest（bench_pdfs/testset/），pdf 路径按 testset 根解析。
    # 兼容两种字段名（老样本用 pdf，grow 新样本用 src）；report_period 缺失时从文件名兜底。
    mani = json.loads(MANIFEST.read_text(encoding="utf-8"))
    samples = []
    for s in mani["samples"].get("ashare", []):
        src = s.get("src") or s.get("pdf")
        if not src:
            continue
        rp = s.get("report_period") or ""
        if not rp:
            m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", Path(src).name)
            rp = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
        samples.append({"stock_code": s.get("stock_code") or "",
                        "exchange": s.get("exchange"),
                        "report_period": rp,
                        "pdf": str(BASE / "bench_pdfs" / src)})
    if a.limit:
        samples = samples[:a.limit]

    ok = fail = missing = 0
    fails = []
    for i, s in enumerate(samples, 1):
        try:
            md = P.convert(s["pdf"], None)
        except Exception as e:  # noqa: BLE001
            missing += 1
            fails.append((s["stock_code"], f"转换失败 {type(e).__name__}"))
            continue
        items = extract_cf(md, s["report_period"])
        good, note = check_c1(items)
        if good:
            ok += 1
        elif note.startswith("缺项"):
            missing += 1
            fails.append((s["stock_code"], note))
        else:
            fail += 1
            fails.append((s["stock_code"], note))
        if i % 20 == 0:
            print(f"  [{i}/{len(samples)}] ok={ok} 不平={fail} 缺项={missing}")

    n = ok + fail + missing
    print(f"\n{'='*66}\n  现金流 C1 自洽｜样本 {n}｜通过 {ok}｜不平 {fail}｜缺项 {missing}"
          f"｜自洽率 {ok/n*100:.1f}%\n{'='*66}")
    if fails:
        print("失败样例（前 15）:")
        for code, note in fails[:15]:
            print(f"  {code}: {note}")


if __name__ == "__main__":
    main()
