# -*- coding: utf-8 -*-
"""
韩国 DART 财报全文采集模块
来源: 韩国金融监督院 FSS 官方 OpenAPI (opendart.fss.or.kr)

接口:
  - 公司清单:      GET  /api/corpCode.xml      (ZIP, 含 CORPCODE.xml)
  - 公司市场分类:  GET  /api/company.json       (corp_cls: Y=KOSPI K=KOSDAQ N=KONEX E=기타)
  - 披露清单:      GET  /api/list.json          (按公司+日期+披露类型, 返回 rcept_no)
  - 全文:          GET  /api/document.xml       (ZIP, 含 사업보고서 全文 XML)

披露类型 pblntf_detail_ty: A001=사업보고서(年报) A002=반기보고서(半年) A003=분기보고서(季报)

产品形态: 整本年报「叙事全文」→ markdown(不再是结构化三张表)。document.xml 返回的
XML 是 HTML 风格(DART 自有 schema): P=段落 TITLE=标题 SECTION-1/2/3=章节 TABLE=表格。
"""

import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

DART_BASE = "https://opendart.fss.or.kr/api"

# 披露类型 -> list.json 的 pblntf_detail_ty (A003 同时覆盖 Q1/Q3, 靠标题月份区分)
PBLNTF_DETAIL_TY = {"annual": "A001", "semiannual": "A002", "q1": "A003", "q3": "A003"}

# 市场分类 corp_cls -> 交易所(对齐 FMP/Yahoo 后缀: KSC=KOSPI .KS, KOE=KOSDAQ .KQ, KNX=KONEX .KN); 丢弃 기타(E)
CLS_TO_EXCHANGE = {"Y": "ksc", "K": "koe", "N": "knx"}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DataSinking/0.1; +https://datasink.ing)"}

# 公司清单缓存(避免每次全量重新下载 corpCode.xml + 逐公司查 corp_cls, 国内访问韩国慢)
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dart_companies_cache.json")
CACHE_TTL = 604800  # 7 天(公司清单 + corp_cls 变更极慢)


def get_api_key():
    k = os.environ.get("DART_API_KEY", "").strip()
    if not k:
        print("[错误] 请设置环境变量 DART_API_KEY (去 https://opendart.fss.or.kr 免费注册获取)")
        raise SystemExit(1)
    return k


def _get_json(url, params, retries=3, delay=2):
    """GET + JSON, 带重试(处理瞬时网络错误)"""
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=90)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            print(f"  [warn] 请求重试 {i + 1}/{retries}: {type(e).__name__}")
            time.sleep(delay * (i + 1))
    raise RuntimeError("unreachable")


def _get_bytes(url, params, retries=3, delay=2):
    """GET + 原始字节(用于 document.xml 返回 ZIP), 带重试"""
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=120)
            resp.raise_for_status()
            return resp.content
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            print(f"  [warn] 请求重试 {i + 1}/{retries}: {type(e).__name__}")
            time.sleep(delay * (i + 1))
    raise RuntimeError("unreachable")


# ---------- 公司清单 ----------
def _read_companies_cache():
    """读取公司清单缓存: TTL 内且为新格式(含 corp_cls)才返回列表, 否则 None(触发重建)。"""
    if os.path.exists(CACHE_PATH):
        if time.time() - os.path.getmtime(CACHE_PATH) < CACHE_TTL:
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and data and "corp_cls" in data[0]:
                    return data
            except Exception:
                pass
    return None


def _write_companies_cache(companies):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(companies, f, ensure_ascii=False)
    except Exception:
        pass


def fetch_corp_cls(api_key, corp_code):
    """company.json (기업개황) 查询公司市场分类 corp_cls: Y=KOSPI, K=KOSDAQ, N=KONEX, E=기타。
    失败返回空串(该公司的市场分类无法确定, 后续直接丢弃)。
    """
    try:
        data = _get_json(f"{DART_BASE}/company.json", {"crtfc_key": api_key, "corp_code": corp_code})
        if data.get("status") == "000":
            return (data.get("corp_cls") or "").strip()
    except Exception:
        pass
    return ""


def load_companies(api_key, use_cache=True, with_class=True):
    """下载并解析全部公司清单, 逐公司查 corp_cls 区分 KOSPI/KOSDAQ/KONEX, 返回
    [{corp_code, corp_name, stock_code, corp_cls, exchange}]。
    只保留上市市场 KOSPI(Y) + KOSDAQ(K) + KONEX(N), 丢弃 기타(E); stock_code 空的公司也丢弃。
    结果缓存到本地 JSON(TTL 7 天)。

    `with_class=False`：**跳过市场分类**，直接返回 [{corp_code, corp_name, stock_code}]。
    为什么要这个开关：分类是**逐公司一次 API 请求**（约 3988 次 + 0.2s 限速），
    首次跑要十几分钟；而**只下载原件根本不需要知道它是 KOSPI 还是 KOSDAQ**。
    （2026-09-22 `archive_fetch.py` 就卡在这 —— 本地缓存的旧格式没有 corp_cls，
    被判失效后触发了全量重建。）
    这条路径**不写缓存**，避免把不带 corp_cls 的清单写进正式缓存反而毒化生产。
    """
    if use_cache:
        cached = _read_companies_cache()
        if cached is not None:
            return cached

    resp = requests.get(
        f"{DART_BASE}/corpCode.xml", params={"crtfc_key": api_key}, headers=HEADERS, timeout=180
    )
    resp.raise_for_status()
    data = resp.content
    if data[:2] == b"PK":  # ZIP magic
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml_bytes = z.read(z.namelist()[0])
    else:
        xml_bytes = data

    root = ET.fromstring(xml_bytes)
    companies = []
    for item in root.findall("list"):
        code = (item.findtext("corp_code") or "").strip()
        name = (item.findtext("corp_name") or "").strip()
        stock = (item.findtext("stock_code") or "").strip()
        if stock:  # 只保留上市公司
            companies.append({"corp_code": code, "corp_name": name, "stock_code": stock})

    if not with_class:
        print(f"  [corp_cls] 跳过市场分类（with_class=False），直接返回 {len(companies)} 家")
        return companies

    result = []
    for i, c in enumerate(companies):
        cls = fetch_corp_cls(api_key, c["corp_code"])
        exchange = CLS_TO_EXCHANGE.get(cls)
        if exchange:
            c["corp_cls"] = cls
            c["exchange"] = exchange
            result.append(c)
        if (i + 1) % 200 == 0:
            print(f"  [corp_cls] {i + 1}/{len(companies)} ...")
        time.sleep(0.2)  # 限速

    print(f"  [corp_cls] 完成: 保留 {len(result)}/{len(companies)} 家(KOSPI + KOSDAQ + KONEX)")
    _write_companies_cache(result)
    return result


# ---------- 披露清单 + 全文 ----------
def fetch_disclosures(api_key, corp_code, bgn_de, end_de, detail_ty=None):
    """list.json 查披露清单, 返回 [{report_nm, rcept_no, rcept_dt}]。
    detail_ty 为空则返回全部披露; 否则按 A001/A002/A003 过滤。
    """
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_no": "1",
        "page_count": "100",
    }
    if detail_ty:
        params["pblntf_detail_ty"] = detail_ty
    data = _get_json(f"{DART_BASE}/list.json", params)
    if data.get("status") != "000":
        return []
    return [
        {
            "report_nm": (d.get("report_nm") or "").strip(),
            "rcept_no": d.get("rcept_no") or "",
            "rcept_dt": d.get("rcept_dt") or "",
        }
        for d in (data.get("list") or [])
        if d.get("rcept_no")
    ]


def fetch_document_xml(api_key, rcept_no):
    """document.xml → ZIP → 主 XML 全文字符串(取 ZIP 里不带 _ 后缀的主文件)。"""
    data = _get_bytes(f"{DART_BASE}/document.xml", {"crtfc_key": api_key, "rcept_no": rcept_no})
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            # 主文件 = rcept_no.xml; 其它 _00760/_00761 是附件明细表
            main = next((n for n in names if n == f"{rcept_no}.xml"), names[0])
            return z.read(main).decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def classify_disclosure(report_nm):
    """report_nm → doc_type(annual/semiannual/q1/q3), 非目标返回 None。"""
    if "사업보고서" in report_nm or "事業報告書" in report_nm:
        return "annual"
    if "반기보고서" in report_nm or "半期報告書" in report_nm:
        return "semiannual"
    if "분기보고서" in report_nm or "四半期報告書" in report_nm:
        m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
        if m:
            return "q1" if int(m.group(2)) == 3 else "q3"
        return "q1"
    return None


def report_period_from_nm(report_nm):
    """report_nm 里的 (YYYY.MM) → 报告期 YYYY-MM-DD; 无法解析返回 None。"""
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    day = {12: 31, 6: 30, 3: 31, 9: 30}.get(mo)
    return f"{y}-{mo:02d}-{day:02d}" if day else None


# ---------- DART XML → markdown ----------
def _sanitize_xml(xml_str):
    """修复 DART XML 里的裸 & < 和非法控制字符(否则 ElementTree 报 not well-formed)。
    DART 正文里常见 "< TV 시장점유율 추이 >" 这种裸 < 当装饰括号用, 需转义成 &lt;。"""
    xml_str = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml_str)
    xml_str = re.sub(r"<(?![A-Za-z/!?])", "&lt;", xml_str)  # 裸 <(非标签开头)转义
    xml_str = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_str)
    return xml_str


def _text_content(el):
    """元素及其后代的纯文本拼接(用于 P/SPAN/TH/TD/TE)。"""
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_text_content(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _table_to_md(table_el):
    """DART TABLE → markdown 表格"""
    rows = []
    for tr in table_el.iter():
        if tr.tag.split("}")[-1].upper() != "TR":
            continue
        cells = []
        for cell in tr:
            if cell.tag.split("}")[-1].upper() in ("TH", "TD", "TE"):
                cells.append(_text_content(cell).replace("\n", " ").strip())
        if cells:
            rows.append(cells)
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    for r in rows:
        while len(r) < ncol:
            r.append("")
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


_HEADING_LEVEL = {"SECTION-1": "#", "SECTION-2": "##", "SECTION-3": "###"}
_SKIP_TAGS = {"PGBRK", "COLGROUP", "COL", "IMAGE", "IMG", "IMG-CAPTION", "SUMMARY", "COVER",
              "DOCUMENT-NAME", "COMPANY-NAME", "FORMULA-VERSION", "EXTRACTION"}


def _walk(el, out):
    tag = el.tag.split("}")[-1].upper()
    if tag == "TITLE":
        t = _text_content(el)
        if t:
            out.append(f"\n## {t}")
        return
    if tag in _HEADING_LEVEL:
        level = _HEADING_LEVEL[tag]
        title = None
        for ch in el:
            if ch.tag.split("}")[-1].upper() == "TITLE":
                title = _text_content(ch)
                break
        if title:
            out.append(f"\n{level} {title}")
        for ch in el:
            if ch.tag.split("}")[-1].upper() != "TITLE":
                _walk(ch, out)
        return
    if tag == "P":
        t = _text_content(el)
        if t:
            out.append(t)
        return
    if tag == "TABLE":
        out.append(_table_to_md(el))
        return
    if tag == "SPAN":
        t = _text_content(el)
        if t:
            out.append(t)
        return
    if tag in _SKIP_TAGS:
        return
    for ch in el:
        _walk(ch, out)


def dart_xml_to_md(xml_str, meta=None):
    """DART 사업보고서 全文 XML → markdown(标题/段落/表格)。meta 为可选 frontmatter dict。
    极少数文件 XML 畸形无法解析时, 降级为「剥标签只留文本」, 不整篇丢弃。"""
    xml_str = _sanitize_xml(xml_str)
    try:
        root = ET.fromstring(xml_str)
        body = next((e for e in root.iter() if e.tag.split("}")[-1].upper() == "BODY"), root)
        out = []
        _walk(body, out)
        md = "\n".join(x for x in out if x).strip()
        md = re.sub(r"\n{3,}", "\n\n", md)
    except ET.ParseError:
        md = re.sub(r"<[^>]+>", " ", xml_str)  # 剥掉标签
        md = re.sub(r"\s+", " ", md).strip()
    if meta:
        front = "\n".join(f'{k}: "{v}"' for k, v in meta.items())
        md = f"---\n{front}\n---\n\n{md}"
    return md


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    key = get_api_key()

    companies = load_companies(key)
    target = next((c for c in companies if c["stock_code"] == "005930"), companies[0])
    print(f"测试公司: {target['corp_name']}({target['stock_code']}, {target['exchange']})")

    ds = fetch_disclosures(key, target["corp_code"], "20240101", "20241231", detail_ty="A001")
    print(f"2024 年 사업보고서 {len(ds)} 份:")
    for d in ds:
        print(f"  {d['report_nm']} | rcept_no={d['rcept_no']} | {d['rcept_dt']}")

    if ds:
        xml_str = fetch_document_xml(key, ds[0]["rcept_no"])
        md = dart_xml_to_md(xml_str, {
            "stock_code": target["stock_code"],
            "stock_name": target["corp_name"],
            "report_period": report_period_from_nm(ds[0]["report_nm"]),
            "doc_type": "annual",
            "title": ds[0]["report_nm"],
        })
        print(f"\n--- markdown 长度 {len(md)} 字符, 预览前 1500 字 ---\n{md[:1500]}")
