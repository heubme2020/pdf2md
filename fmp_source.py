# -*- coding: utf-8 -*-
"""FMP 真值来源 —— 给**没有 XBRL 的市场**当裁判（A 股、韩股、以及日台的交叉验证）。

## 为什么需要它

`XbrlSource`（EDINET iXBRL）只覆盖日本。**A 股没有公开的 XBRL 路径** ——
而 A 股是客户最可能用的市场。没有真值源，A 股的每一次改动都是**盲改**
（这正是 v9b 那次的处境：只有一个内生的会计恒等式当依据）。

FMP 覆盖四个亚洲市场（实测 `financial-statement-symbol-list`）：
    `.SS`/`.SZ` 4533 只｜`.T` 4026｜`.TW`/`.TWO` 2209｜`.KS`/`.KQ` 较少

## 口径已标定（`_calibrate_fmp.py`，用 FMP 自己的 CSV 跑的）

四个市场**同一套映射**（恒等式落在 [0.99,1.01] 的比例）：

| 我们 md 里的科目 | FMP 字段 | 依据 |
|---|---|---|
| 资产总计 | `totalAssets` | — |
| **负债合计** | `totalLiabilities` | **不含**少数股东权益 —— A股「负债+归母权益」只有 47.9% 成立，补上少数股东后 97.6% |
| **所有者权益合计** | `totalEquity` | = 归母 + 少数股东（对应我们 `LABELS` 里刻意排除「归属于母公司」的那条） |

⚠️ **FMP 自身内部一致性只有 95.4%–99.6%**，不是 100%。拿它当裁判时命中率上限就在那里，
剩下的 1–5% 是**裁判自己的噪声**，不能算成我们的错。

## 与 XbrlSource 的关键差别（决定了它更弱）

| | XbrlSource | FmpSource |
|---|---|---|
| 对齐 | **同一份文件**（docID），期间/口径天然一致 | 第三方数据商，**要自己对齐** |
| 覆盖 | 全文（含叙事章节的表格） | **只有三大表** |
| 单位 | 随文档（千円/百万円） | 绝对额，要试 10^0/3/6/8 |
| 噪声 | 0 | 1–5% |

**所以 FMP 只能验"三大表抽得对不对"，验不了叙事章节 —— 而客户抱怨的"章节切片"恰恰在那里。**
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
FMP_BASE = "https://financialmodelingprep.com/stable"
# A 股的交易所代码 → FMP 后缀。北交所 FMP 未覆盖（标 None，调用方跳过）。
EXCHANGE_SUFFIX = {
    "sse": "SS", "szse": "SZ", "bj": None,
    "jpx": "T", "ksc": "KS", "koe": "KQ", "knx": None,
    "twse": "TW", "tpex": "TWO",
}

# FMP 字段 → 我们 md 里可能出现的科目名（**同义词都要给**：A股 报表里
# 「资产总计」和「资产合计」两种写法都常见，少一个就会把命中的判成未命中）
FIELD_LABELS = {
    "totalAssets":             ["资产总计", "资产合计", "资产总额"],
    "totalLiabilities":        ["负债合计", "负债总计", "负债总额"],
    "totalEquity":             ["所有者权益合计", "股东权益合计",
                                "所有者权益（或股东权益）合计", "所有者权益总额", "股东权益总额"],
    "totalCurrentAssets":      ["流动资产合计"],
    "totalCurrentLiabilities": ["流动负债合计"],
    "totalNonCurrentAssets":   ["非流动资产合计"],
    "totalNonCurrentLiabilities": ["非流动负债合计"],
    "cashAndCashEquivalents":  ["货币资金"],
    "inventory":               ["存货"],
    "totalDebt":               ["负债合计"],      # 弱同义，可选
}
# 三大表的恒等式字段优先 —— 它们才是 A1/A11 的核心
CORE_FIELDS = ["totalAssets", "totalLiabilities", "totalEquity",
               "totalCurrentAssets", "totalCurrentLiabilities"]

# 各市场我们的 md 里可能出现的科目名（FMP 字段 → 该市场科目名）。
# zh=A股简体、ja=日本、zh_tw=台湾繁体、ko=韩国。统一指标时按市场取。
LABELS_BY_MARKET = {
    "zh": FIELD_LABELS,
    "ja": {
        "totalAssets":             ["資産合計", "資産総額", "総資産"],
        "totalLiabilities":        ["負債合計", "負債総額"],
        "totalEquity":             ["純資産合計", "純資産"],
        "totalCurrentAssets":      ["流動資産合計", "流動資産総額"],
        "totalCurrentLiabilities": ["流動負債合計", "流動負債総額"],
    },
    "zh_tw": {
        "totalAssets":             ["資產總計", "資產合計", "資產總額"],
        "totalLiabilities":        ["負債總計", "負債合計", "負債總額"],
        "totalEquity":             ["權益總計", "權益合計", "股東權益總計", "股東權益合計", "權益總額"],
        "totalCurrentAssets":      ["流動資產合計", "流動資產總計"],
        "totalCurrentLiabilities": ["流動負債合計", "流動負債總計"],
    },
    "ko": {
        "totalAssets":             ["자산총계", "자산합계"],
        "totalLiabilities":        ["부채총계", "부채합계"],
        "totalEquity":             ["자본총계", "자본합계"],
        "totalCurrentAssets":      ["유동자산", "유동자산합계"],
        "totalCurrentLiabilities": ["유동부채", "유동부채합계"],
    },
}


QUANT_DIR = r"C:\Users\heubme2024\Desktop\quant"


def load_fmp_key():
    """FMP key 在 quant 项目的 `config_local.py` 里，变量名 `FMP_TOKEN`。

    ⚠️ **不要用正则去解析 `write_stock_data.py`**（第一版就是这么写的，一直读不到）——
    那边只有一句 `TOKEN = FMP_TOKEN` 的**转发**，key 本体不在那个文件里：
        write_stock_data.py:42   from config_local import MYSQL_DSN, FMP_TOKEN
        config_local.py:15       FMP_TOKEN = <key>
    直接 import，不解析文本、不打印值。
    """
    if QUANT_DIR not in sys.path:
        sys.path.insert(0, QUANT_DIR)
    try:
        from config_local import FMP_TOKEN  # noqa: PLC0415
        if FMP_TOKEN:
            return FMP_TOKEN
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("FMP_API_KEY", "")


def fmp_symbol(stock_code, exchange):
    suf = EXCHANGE_SUFFIX.get((exchange or "").lower())
    return f"{stock_code}.{suf}" if suf else None


class FmpSource:
    """FMP 真值来源。带磁盘缓存（真值只跟股票+期间有关，跟被测变体无关）。"""

    CACHE = BASE / "_fmp_cache"

    def __init__(self, api_key=None, use_cache=True):
        self.key = api_key or load_fmp_key()
        self.use_cache = use_cache

    def _get(self, path, params, tries=4):
        """带重试的 GET —— 跨境链路偶发 SSL 抖动（本项目老毛病）。"""
        url = f"{FMP_BASE}/{path}?" + "&".join(f"{k}={v}" for k, v in params.items()) \
              + f"&apikey={self.key}"
        last = None
        for i in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001
                last = e
                if i < tries - 1:
                    time.sleep(2 * (i + 1))
        raise last

    def statements(self, symbol, period="annual", limit=8):
        """取三表。返回 {表名: [行, ...]}。"""
        cp = self.CACHE / f"{symbol}.{period}.json"
        if self.use_cache and cp.exists():
            try:
                return json.loads(cp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        out = {}
        failed = []
        for name in ("balance-sheet-statement", "income-statement", "cash-flow-statement"):
            try:
                out[name] = self._get(name, {"symbol": symbol, "period": period,
                                             "limit": limit}) or []
            except Exception as e:  # noqa: BLE001
                out[name] = []
                out[f"{name}_error"] = f"{type(e).__name__}: {e}"
                failed.append(name)
        # ⚠️ **失败绝不写缓存。** 第一版没这条：key 还没读到的那次把空结果缓存了下来，
        #    之后每次读缓存都拿到空 —— 看起来像"FMP 没数据"，实际是**失败被固化**了。
        #    凡是"取不到"和"取到空"分不清的缓存，都会制造这种假象。
        if self.use_cache and not failed:
            self.CACHE.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out

    def quarterly_balance(self, symbol, limit=40):
        """季度资产负债表（本地缓存）。用于 Q3→Q4 重述断崖检测。"""
        cp = self.CACHE / f"{symbol}.quarter_bs.json"
        if self.use_cache and cp.exists():
            try:
                return json.loads(cp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        rows = self._get("balance-sheet-statement",
                         {"symbol": symbol, "period": "quarter", "limit": limit}) or []
        if self.use_cache:
            self.CACHE.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    def q3_q4_jump(self, symbol, year):
        """目标年 Q3→Q4 totalAssets 相对跳变。返回 (jump, q3, q4)；缺 Q3/Q4 返回 (None, None, None)。

        FMP 在**公司重述年报时只改 Q4/年末那一行、不动中间 Q1–Q3**，所以 Q3→Q4 出现真实
        资产负债表不可能有的断崖（000800 +300%、002168 -37%）——断崖年 = FMP 是重述值，
        不该拿来和我们原始披露的 PDF 比。
        """
        q3 = q4 = None
        for r in self.quarterly_balance(symbol):
            d = str(r.get("date", ""))[:10]
            if not d.startswith(str(year)):
                continue
            p = r.get("period", "")
            ta = r.get("totalAssets")
            if not isinstance(ta, (int, float)):
                continue
            if p == "Q3":
                q3 = ta
            elif p == "Q4":
                q4 = ta
        if not q3 or not q4:
            return None, q3, q4
        return (q4 - q3) / q3, q3, q4

    def labeled_facts(self, stock_code, exchange, report_period=None, period="annual"):
        """`(科目名, 数值)` 配对 —— 形状和 `XbrlSource.labeled_facts` 一致，
        所以 `check_structure` 不用改就能用。

        `report_period`：我们文档的期末（如 `2025-12-31`）。给了就挑那一期，
        否则取最新一期。**期间对齐是这里最容易出错的地方** —— 对不上会稳定错一期。
        """
        sym = fmp_symbol(stock_code, exchange)
        if not sym:
            return []
        st = self.statements(sym, period)
        rows = st.get("balance-sheet-statement") or []
        if not rows:
            return []

        if report_period:
            want = str(report_period)[:10]
            hit = [r for r in rows if str(r.get("date", ""))[:10] == want]
            if not hit:
                # 财年错位（日股 3 月决算常见）→ 不硬凑，返回空，让调用方判"对不齐"
                return []
            rec = hit[0]
        else:
            rec = rows[0]

        out = []
        for field in CORE_FIELDS:
            v = rec.get(field)
            if isinstance(v, (int, float)) and abs(v) >= 1e6:
                for lab in FIELD_LABELS.get(field, []):
                    out.append({"label": lab, "label_key": lab,
                                "facts": [{"value": float(v), "text": None}],
                                "field": field})
        return out

    def control_text(self, stock_code, exchange, report_period=None):
        """对照组：把真值本身铺成一段"应当 100%"的文本。

        ⚠️ 这个对照**比 XBRL 那边弱**：XBRL 的对照来自**原始文档**（真的验了"文档里有"），
        而这里只是把数值回写成文本 —— 它只验「匹配层没坏」，**验不了「真值确实来自那份文档」**。
        所以 FMP 这条的结论要更保守地读。
        """
        rows = self.labeled_facts(stock_code, exchange, report_period)
        parts = []
        for r in rows:
            for f in r["facts"]:
                parts.append(f"{r['label']} {f['value']:,.0f}")
        return "\n".join(parts)


if __name__ == "__main__":
    src = FmpSource()
    print("FMP key:", "已读到" if src.key else "**没读到**")
    for code, ex in (("600519", "sse"), ("000001", "szse"), ("300750", "szse")):
        try:
            rows = src.labeled_facts(code, ex, None)
            print(f"\n  {fmp_symbol(code, ex)}: {len(rows)} 组")
            for r in rows[:5]:
                print(f"     {r['label']:16s} {r['facts'][0]['value']:>20,.0f}  ({r['field']})")
        except Exception as e:  # noqa: BLE001
            print(f"  {code}.{ex}: ❌ {type(e).__name__}: {str(e)[:80]}")
