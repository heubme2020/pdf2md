# pdf2md —— 财报全文 → Markdown

把 A股 / 日本 / 台湾 / 韩国 / 美股 的财报原件（PDF / iXBRL / XML）转成**逐字准确、结构清晰**的 Markdown，供 LLM / RAG 直接读取。

核心原则：**有 XBRL 的市场走干净路径（直接读结构，零解析歧义），没有 XBRL 的走 PDF 反推结构**；数字一律**逐字转录**，不做视觉模型式的「改写」。

---

## 升级前后指标对比（v11 → v12）

### 日本（EDINET iXBRL 裁判，889 份年报）

| 指标 | 升级前 v11 | 升级后 v12 | 变化 |
|---|---|---|---|
| **配对率**（科目名+数值同格） | 92.3% | **94.8%** | **+2.5pp** |
| **值召回**（数值抽取完整） | 97.0% | **99.2%** | **+2.2pp** |
| 错位率 value_elsewhere | 4.8% | **3.7%** | -1.1pp |
| 丢值率 value_absent | 2.3% | **0.9%** | -1.4pp |

### A股（FMP 裁判，800 份年报）

| 指标 | 升级前 | 升级后 |
|---|---|---|
| 配对率 | 100% | 100%（已到顶） |

### 章节切片（客户反馈「章节切片错误很多」）

- 假标题（正文句子被误标为 `###` 标题）：**归零**，真标题全保留。

---

## 这版的核心改动（v12）

1. **三线表无横线分行**（`table_detect._row_boundaries`）
   日本利润表「连续科目之间没有横线」（只有小计前有线），旧版按横线分行会把「売上高」「売上原価」胶合成一个 cell；新版按**词 y 空隙**补行界。这是日本值召回 +2.2pp 的主要来源。

2. **假标题字号判断**（`pdf_to_md` h3 加 `size ≥ 12`）
   A股 标题和正文**都是宋体**，区别在字号（真标题 12、正文 9、清单项 11）。旧版只看正则+长度、不看字号，把「二、公司全体董事出席董事会会议。」这类正文句子标成标题 → 假章节。字号判断比之前试过的「长度≤20」稳（不误丢长真标题）。

3. **SEC `$` 符号合并**（`edinet_html._merge_signs`）
   美股 iXBRL（Workiva 生成）把美元符号 `$` 单独放一个 `<td>`、数字放下一个，旧版拆成两列导致值匹配失败；新版把纯符号格并回数值格。

4. **章节本体统一**（`section_ontology.py` + `gen_focus_points.py`）
   中日韩台美一套规范概念（mda / financial_statements / notes / audit_report …），各市场标题映射到同一个 concept；加新交易所只加一张映射表。

5. **评测四过滤器**（`fmp_eval.py`）
   存档垃圾 / FMP 自洽性 / 重述断崖（Q3→Q4 跳变）/ 标签口径，把真值源的噪声从「解析器质量」里剥干净。

---

## 文件结构

```
pdf_to_md.py        A股 PDF → MD（线条法 + find_tables，三线表分行、假标题修复）
edinet_html.py      日/台/美 iXBRL → MD（表格还原 + $ 合并）
html_tables.py      HTML 表格 → 网格（rowspan/colspan、△负数）
table_detect.py     三线表/线条检测（含 _row_boundaries 词 y 分行）
unified_convert.py  按交易所分发的统一入口（四地四种原件 → 两个函数）
converter_version.py 统一版本号（改转换器就 bump）
edinet_xbrl.py      EDINET iXBRL ZIP 章节提取
dart_fetch.py       韩国 DART XML → MD
section_ontology.py 跨市场章节本体（单一真源）
gen_focus_points.py 由 section_ontology 生成 worker 的关注点映射

# —— 评测（真值裁判）——
pdf2md_referee.py   日/台 XBRL 裁判（配对率 / 值召回 / 失败归因）
pdf2md_metrics.py   A1 会计恒等式 / A11 表内闭合 / A15 阅读顺序
fmp_eval.py         A股/韩 FMP 裁判（四过滤器）
fmp_source.py       FMP 真值来源
ashare_sections.py  A股章节校验（CSRC 模板）
```

## 用法

```python
from unified_convert import convert

# A股 PDF
md = convert("sse", "600519_2025-12-31_annual_xxx.pdf",
             meta={"stock_code": "600519", "report_period": "2025-12-31", ...})

# 日本 EDINET iXBRL ZIP
md = convert("jpx", "7203/S100XXXX.zip", meta={...})

# 台湾 MOPS iXBRL HTML / 韩国 DART XML 同理
```

## 依赖

- Python 3.x
- PyMuPDF（PDF 文本层 + find_tables）
- 可选：rclone（推 R2 用）、paramiko（连 VPS 用）

## 设计原则

- **逐字转录**：数字从文本层/结构逐字取，绝不用视觉模型「改写」——财报数字错一个就废。
- **有 XBRL 走干净路径、无 XBRL 走 PDF**：日本/台湾/美股有 iXBRL 直接读结构；只有 A股 走 PDF 反推。
- **为实值丢外观永远错**：宁可留外观垃圾（如表格多空列），不可丢实值。

---

## License / 许可证声明

**本仓库代码采用 AGPL-3.0**（与它依赖的 PyMuPDF 一致）。

⚠️ **本库依赖 AGPL-3.0 的 PyMuPDF**（`pdf_to_md.py` 用到的 `fitz` / `find_tables` 就是它）。

- PyMuPDF 采用**双授权**：AGPL-3.0（开源）或 Artifex 商业授权（付费）。
- AGPL-3.0 的「网络条款」（§13）要求：如果把基于 PyMuPDF 的软件**作为网络服务（SaaS）运行**，须向网络用户提供**整个应用**的源码。
- 本仓库本身已按 AGPL-3.0 开源，但**不代表你的使用方式自动合规**。

**如果你要商用 / 跑成公开服务**，请自行：

1. 确认 AGPL §13 对你具体服务形态的适用性（**建议咨询律师，以上不是法律意见**）；
2. 或购买 Artifex 商业授权；
3. 或改用授权更宽松的替代（如 Docling = MIT、MinerU = Apache）。
