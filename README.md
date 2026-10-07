# pdf2md — financial statements to Markdown

Convert full-text financial reports (annual / semi-annual / quarterly) from China, Japan, Taiwan, Korea, the US and the UK into **verbatim, well-structured Markdown**, ready for LLM / RAG consumption.

Core principle: **markets with XBRL take the clean path (read structure directly, zero parsing ambiguity); markets without XBRL reconstruct structure from the PDF**. Numbers are always **transcribed verbatim** — never "rewritten" by a vision model.

---

## v14: UK ESEF (new market)

The UK files annual reports in **ESEF iXBRL** — an XHTML wrapper whose layout is a PDF-style text layer (no `<h1>`/`<table>`, text placed by x/y coordinates + font size). v14 adds `_uk_esef_to_md` in `unified_convert.py`: `<table>` → Markdown tables, everything else → paragraphs, `ix:` tags stripped, `<body>` only. Numbers stay verbatim.

| Change | Where | Effect |
|---|---|---|
| UK ESEF structuring (`<table>`→Markdown table + text paragraphs) | `unified_convert._uk_esef_to_md` | Replaces the v13 "one line per `</div>`" flat dump (0 tables, CSS leakage) with structured tables + paragraphs |

---

## v13: frameless balance-sheet extraction (the big one)

The single hardest problem in PDF→Markdown is the **frameless balance sheet** — Japan/Taiwan reports align columns with whitespace instead of ruled lines, so line-based table detection fails and the accounting identity can't be checked. v13 adds a **line parser** for these sheets:

| Change | Where | Effect |
|---|---|---|
| Frameless balance-sheet line parser (`科目代码 + 科目名 + 金额`) | `pdf_to_md._parse_balance_sheet` | A1 accounting-identity pass rate on Japan/Taiwan goes from **2% → ~90%** |
| Boxed-table detection (`find_tables(lines)`) merged with the handwritten line detector | `pdf_to_md._detect_tables_boxed` | Fixes the garbled boxed tables (fund-lending / endorsement / schedules) that were the root of the TSMC garbling |
| Total-row extraction hardened: `search` + CJK negative-lookbehind + account-code fallback (`1xxx`/`2xxx`/`3xxx`) | `pdf_to_md._find_total` | Recovers `負債總計` / `權益總計` when `get_text()` separates the label from its amount |

See `CHANGELOG.md` for the full release notes.

---

## Metrics (converter-agnostic — measure *quality*, not quantity)

`pdf2md_metrics.py` implements a suite of self-verifying metrics. The strongest is **A1 (accounting identity)**: it's a mathematical constraint (`total assets = total liabilities + total equity`), so it needs **no manual labeling** — wrong is simply wrong.

| # | Metric | What it catches |
|---|---|---|
| A1 | `accounting_identity` | Assets ≠ Liabilities + Equity — the strongest evidence of correctness |
| A2 | `table_alignment` | Column count mismatch across a table (misaligned / interleaved columns) |
| A3 | `content_coverage` | md chars / PDF text-layer chars (< 0.9 dropped content, > 1.3 duplicated) |
| A4 | `garbled` | U+FFFD / private-use / control chars (font-mapping failures) |
| A5 | `sections` | Section structure usability |
| A6 | `section_fragmentation` | False section boundaries (real chapters shouldn't be a few dozen chars) |
| A7 | `duplication` | Repeated ≥20-char lines |
| A8 | `fake_headings` | `###` lines that are really sentences |
| A9 | `script_profile` | Kana/Hangul ratio — catches "legal but wrong" CJK (A4's blind spot) |
| A11 | `table_footing` | Arithmetic closure of subtotal rows |
| A13 | `articulation` | Cross-statement agreement (net income / cash equivalents appear in two tables) |
| A15 | `reading_order` | Successor-edge accuracy vs PDF geometric order |

> **pass / total** is the honest rate — `skip` (label not found) counts as failure for a financial report, because "couldn't extract the balance sheet at all" is exactly the failure mode.

---

## Benchmark (127 reports per market, sampled from a full archive)

Run with:

```bash
python pdf2md_bench.py --from-archive --limit 127 --markets ashare,jp,tw --workers 6
```

Full per-file results: `bench_quality_v13.json`.

| Market | Sample | A1 pass | A1 fail | A1 skip (no balance sheet) | pass rate |
|---|---|---|---|---|---|
| A-share | 127 | 79 | 0 | 48 | 62.2% |
| **Japan** | 127 | **110** | 2 | 15 | **86.6%** |
| Taiwan | 127 | 13 | 2 | 112 | 10.2% |

> **Japan is the headline: 2% → 86.6%** — the frameless balance-sheet parser turns Japan's whitespace-aligned statements from "couldn't extract at all" to "accounting identity verifies".
>
> Taiwan's full archive still has a hard ceiling: 47/127 reports carry HuaKang-font garbled text (A4), and most of the rest are quarterly reports — the 10.2% is the honest whole-archive number, not a curated set.
>
> A-share uses ruled (three-line) tables, unaffected by this change; the 62.2% reflects archive sample variance (skip = reports without a matching balance-sheet label, e.g. financial-sector reports).

Other metrics (all 381 reports): table alignment **99.8%** · content coverage median **1.70×** · garbled **78/381 reports** · median **6** sections · **0 crashes**.

---

## Usage

```python
from unified_convert import convert

# A-share PDF
md = convert("sse", "600519_2025-12-31_annual_xxx.pdf",
             meta={"stock_code": "600519", "report_period": "2025-12-31"})

# Japan EDINET iXBRL ZIP
md = convert("jpx", "7203/S100XXXX.zip", meta={})

# Taiwan MOPS iXBRL HTML / Korea DART XML — same entry point

# UK ESEF ZIP
md = convert("lse", "LEI-period-ESEF-GB-version.zip", meta={})
```

---

## File structure

```
pdf_to_md.py        A-share PDF → MD (line + find_tables detection, frameless balance-sheet parser)
edinet_html.py      Japan/Taiwan/US iXBRL → MD (table restoration + $ sign merge)
html_tables.py      HTML tables → grid (rowspan/colspan, △ negative numbers)
table_detect.py     Ruled-table / line detection (incl. _row_boundaries word-y row split)
unified_convert.py  Unified entry point that dispatches by exchange (incl. UK ESEF)
converter_version.py  Single monotonic version number (bump on any converter change)
edinet_xbrl.py      EDINET iXBRL ZIP chapter extraction
dart_fetch.py       Korea DART XML → MD
section_ontology.py Cross-market section ontology (single source of truth)
gen_focus_points.py Generates the worker's focus-point mapping from section_ontology

# —— evaluation (truth referees) ——
pdf2md_referee.py   Japan/Taiwan XBRL referee (pairing rate / value recall / failure attribution)
pdf2md_metrics.py   A1 accounting identity / A11 table footing / A15 reading order / …
fmp_eval.py         A-share/Korea FMP referee (four noise filters)
fmp_source.py       FMP ground-truth source
ashare_sections.py  A-share section validation (CSRC template)
```

---

## Dependencies

- Python 3.x
- PyMuPDF (PDF text layer + `find_tables`)
- Optional: rclone (R2 push), paramiko (VPS access)

---

## Design principles

- **Verbatim transcription** — numbers come from the text layer / structure character-for-character, never from a vision model's "rewrite". A financial number off by one digit is worthless.
- **XBRL when available, PDF otherwise** — Japan/Taiwan/US have iXBRL (read structure directly); A-share is PDF-only and must reconstruct structure.
- **Never lose a real value for cosmetic cleanliness** — a little visual noise (e.g. extra empty columns) is acceptable; a dropped number is not.

---

## License

**This repository is AGPL-3.0** (matching its PyMuPDF dependency).

⚠️ **This library depends on AGPL-3.0 PyMuPDF** (`fitz` / `find_tables` used by `pdf_to_md.py`).

- PyMuPDF is **dual-licensed**: AGPL-3.0 (open source) or an Artifex commercial license (paid).
- AGPL-3.0's network clause (§13) requires that if you run PyMuPDF-based software **as a network service (SaaS)**, you offer the **entire application's** source to network users.
- This repository is itself AGPL-3.0, but that does **not** mean your use is automatically compliant.

**If you plan to commercialize / run it as a public service**, you should:

1. Confirm how AGPL §13 applies to your specific service shape (**consult a lawyer — the above is not legal advice**);
2. Or buy an Artifex commercial license;
3. Or switch to a more permissive alternative (e.g. Docling = MIT, MinerU = Apache).
