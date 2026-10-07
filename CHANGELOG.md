# Changelog

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
