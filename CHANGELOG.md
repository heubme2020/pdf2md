# Changelog

## v16 — 2026-10-13: Korea EUC-KR decode + SEC `$`/headings + A-share containment dedup

数据质量自测框架（31 题 × 每所 7 股全量审计）揪出的一批转换 bug，按根因修：

1. **韩国 DART 老报告乱码**（`dart_fetch._decode_dart_xml` + `unified_convert`）
   DART `document.xml` 的 XML 声明永远写 `utf-8`，但老报告正文实际是 **EUC-KR**。按 utf-8 解码会解出 5.9 万 U+FFFD（韩文全丢）。修法：按「哪个解码 FFFD 更少」自检，utf-8 解出大量 FFFD 就退回 cp949。老报告 FFFD 5.9 万 → 0，新报告（真 UTF-8）不受影响。

2. **美股 SEC `$` 单独占格致列错位**（`edinet_html._merge_signs`）
   Workiva 把 `$` 单独放一个 td、数字放下一个。原 `_merge_signs` 并回时少一列、右补齐让带 `$` 行整体左移一列，`table_footing` 求和对不上（AAPL Cash 2024 值落错列）。修法：`$数值` 并回 `$` 原列（=表头起始列）、数字原列留空，保持列数。

3. **美股 SEC 标题退化 `PartI/1/13`**（`edinet_html._toc_titles`）
   SEC 目录里同一锚点有 `Item 1.` → `Business` → 页码 `1` 三个链接，原 `out[anchor]=text` 后写覆盖取到最后一个（页码），`re.sub(r"\s+","")` 把 "Part I" 剥成 "PartI"。修法：跳过纯数字页码、first-wins、折叠空白（对台湾单链接/中文零影响）。

4. **美股 OTC 无章节兜底**（`convert_local._sec_toc_fallback`）
   OTC/小盘文件没有 `<a href>` 目录（TOC 是 `<table>`），标题一个都抽不出。兜底：从转出的目录表认「Item N. / PART I/II/III/IV」行补成 `##` 标题。

5. **A股跨页表重复**（`pdf_to_md._detect_tables`）
   文本法把同一张表拆成「1 张多列大表 + 多张 2 列碎片」，碎片 bbox 小、过不了 50% 面积重叠阈值，全被保留 → 表头重复。修法：加 `_bbox_contains`「完全包含 + 列数更少」去重。

## v15 — 2026-10-11: A-share table dedup fix + FMP 3-statement truth

### What changed

1. **A-share three-line table dedup fix** (`pdf_to_md._detect_tables`)
   `find_tables(strategy="lines")` over-splits A-share three-line tables (horizontal rules only) into 9 columns and truncates wrapped labels ("…的影响" split off). The merge now prefers the handwritten line detector when the boxed table has **>2 more columns** than it (over-split), while still preferring `find_tables(lines)` for HuaKang boxed tables (Japan/Taiwan, where the handwritten detector smears text).

2. **Generic cross-page table merge** (`pdf_to_md.convert`)
   A table block ending at the page bottom and continuing at the next page top (same column count) is merged into one table instead of two fragments.

3. **FMP truth extended to all three statements** (`fmp_source`)
   Adds income-statement (`revenue`/`operatingIncome`/`incomeBeforeTax`/`netIncome`) and cash-flow (`operatingCashFlow`/`netCashProvidedByInvestingActivities`/`netCashProvidedByFinancingActivities`) fields, with a `FIELD_STATEMENT` mapping to pick each field from its own statement. `labeled_facts` now reads all three statements.

### Verification

Frozen test set (52 A-share reports, `_cashflow_eval.py`), cash-flow **C1 identity** (期末 = 期初 + 经营 + 投资 + 筹资 + 汇率): **51.6% → 71.1%**. Japan FMP pairing unchanged (60% before = after), confirming no regression on the iXBRL path.

---

## v14 — 2026-10-07: UK ESEF (new market)

The UK files annual reports in **ESEF iXBRL** — an XHTML wrapper whose layout is a PDF-style text layer (no `<h1>`/`<table>`, text placed by x/y coordinates + font size). v14 adds a structured converter for it.

- `_uk_esef_to_md` (`unified_convert.py`): `<table>` → Markdown tables, everything else → paragraphs, `ix:` tags stripped, `<body>` only.
- Replaces the v13 "one line per `</div>`" flat dump (0 tables + `<style>` CSS leakage) with structured tables + paragraphs.
- Numbers stay verbatim (no OCR, no rewrite).

---

## v13 — 2026-10-07: frameless balance-sheet extraction

The big fix for the biggest failure mode: Japan/Taiwan balance sheets use **whitespace column alignment instead of ruled lines**, so the old line-based detector couldn't find the totals at all — A1 accounting identity passed on only **2%** of Japan/Taiwan reports.

### What changed

1. **Frameless balance-sheet line parser** (`pdf_to_md._parse_balance_sheet`)
   Parses `科目代码 + 科目名 + 金额` line-by-line instead of relying on table detection. Pre-scans the balance sheet across pages and replaces the fragmented `find_tables(text)` output with a clean grid.

2. **Boxed-table detection merged in** (`pdf_to_md._detect_tables_boxed`)
   `find_tables(strategy="lines")` handles boxed tables (fund-lending / endorsement / schedules) that the handwritten detector smeared into one blob — the root cause of the TSMC garbling. Merged + deduplicated with the handwritten line detector (boxed wins on overlap).

3. **Hardened total-row extraction** (`pdf_to_md._find_total`)
   - Label matching switched from anchored `^…$` to `search` + CJK negative-lookbehind (`(?<![一-龥])`) — matches labels on the same line as an account code (`0.77 2xxx 負債總計`) while still excluding subtotals (`流動負債合計`) and the grand total (`負債及權益總計`).
   - Account-code fallback (`1xxx`/`2xxx`/`3xxx`) recovers a total when `get_text()` separates the label from its amount by >100 lines.

### Verification

Full benchmark (127 reports/market, sampled from a full archive; results in `bench_quality_v13.json`):

| Market | A1 pass rate (v12 → v13) |
|---|---|
| A-share | 62.2% (three-line tables, unaffected; sample variance) |
| **Japan** | **2% → 86.6%** |
| Taiwan | 2% → 10.2% (whole archive: 47/127 HuaKang-garbled + mostly quarterly reports) |

On the curated Taiwan annual-report set, A1 self-consistency is **87% → 98%** (47/48; the one failure is a HuaKang-font garbled page, not a parsing bug).

### Known limits

- `2637` (TW) uses letter-form account codes (`11XX` / `2XXX`) inline with labels — a non-standard format the line parser still skips (no longer emits *wrong* data, just doesn't extract it).
- HuaKang-font garbled pages (`DFKaiShu` "Estd-BF" private glyph IDs) are a font-level problem, out of scope for the parser.

---

## v12 — 2026-10-02: three-line table row split, fake-heading fix

1. **Three-line table row boundaries without horizontal rules** (`table_detect._row_boundaries`)
   Japan income statements have no ruled line between consecutive items (only before subtotals). The old rule-based row split glued items together; the new word-y-gap split recovers them. Main source of Japan value-recall +2.2pp.

2. **Fake-heading font-size check** (`pdf_to_md` h3, `size ≥ 12`)
   A-share headings and body text are the same typeface, distinguished only by font size. The old regex+length check mislabeled body sentences as headings; size check fixes it.

3. **SEC `$` sign merge** (`edinet_html._merge_signs`)
   US iXBRL (Workiva) puts the `$` in its own `<td>` and the number in the next — the old split produced two columns and failed value matching.

4. **Unified section ontology** (`section_ontology.py` + `gen_focus_points.py`)
   One cross-market concept set (mda / financial_statements / notes / audit_report …) with a per-market heading mapping.

5. **Evaluation four filters** (`fmp_eval.py`)
   Archive garbage / FMP self-consistency / restatement cliff (Q3→Q4 jump) / label scope — strips ground-truth noise from parser quality.

### Metrics (v11 → v12)

| Metric | Japan (XBRL referee, 889 reports) | A-share (FMP referee, 800 reports) |
|---|---|---|
| Pairing rate | 92.3% → **94.8%** | 100% (topped out) |
| Value recall | 97.0% → **99.2%** | — |
| Misalignment | 4.8% → 3.7% | — |
| Missing value | 2.3% → 0.9% | — |
