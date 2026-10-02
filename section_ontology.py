# -*- coding: utf-8 -*-
"""跨市场章节本体（section ontology）—— 把中日韩台财报章节统一到一个「规范概念集」。

## 单一真源

本文件是章节映射的**唯一真源**。worker/src/index.ts 的 `FOCUS_POINTS`（Cloudflare 运行时）
由 `gen_focus_points.py` 从本文件生成；改关键词只改这里，再跑生成器同步 worker。

## 两层（只统一「本体」，不统一「切分机制」）

- 切分机制：A股用正则+CSRC模板「对」、日/台用 iXBRL 锚点/h1「读」—— 机制保留市场差异（客观）。
- 本体（本文件）：把切出来的标题统一映射到 concept —— 这才是要统一的层。

## 结构

- `CONCEPTS`：15 个规范概念（原子节）。其中 6 个是**关注点**（部署在 worker FOCUS_POINTS 里）：
  mda / financial_statements / notes / audit_report / risk / governance。
- `MAPPING`：{concept → {市场 → [关键词]}}，市场级 superset。
  exchange 级差异（bj⊂ashare、knx⊂kr）在 `gen_focus_points.EXCHANGE_OVERRIDES` 里。
- `map_section(market, heading) → concept`：归一化 + 最长关键词胜出。

## 状态（2026-10-01）

- 6 个关注点概念：关键词 = worker 现役 FOCUS_POINTS 的原文（**已吸收**，含 jpx「経営成績/財政状態」等）。
- 台湾：iXBRL（tw_xbrl）实测标题已核对；iXBRL 只有財報层，年報的「公司概況/營運概況」在年報 PDF 里。
- 日本：EDINET iXBRL 章节已核对（第１企業の概況/第２事業の状況/第４経理の状況）。
- 韩国：关键词来自 worker 现役（无 PDF 存档）。
"""
import re

CONCEPTS = {
    "cover":              "封面/目录/释义",
    "company_profile":    "公司简介/概況",
    "business_overview":  "业务概要（A股2017版单独一节；worker 关注点 mdna 里已折叠）",
    "mda":                "管理层讨论与分析（经营情况）—— 关注点",
    "governance":         "公司治理 —— 关注点",
    "esg":                "环境与社会责任",
    "material_events":    "重要事项",
    "shareholders":       "股份变动及股东情况",
    "bonds":              "债券相关情况",
    "financial_report":   "财务报告（父节，含报表+附注+审计）",
    "financial_statements": "财务报表（资产负债表/利润表/现金流量表）—— 关注点",
    "notes":              "财务报表附注 —— 关注点",
    "audit_report":       "审计报告 —— 关注点",
    "risk":               "风险 —— 关注点",
    "other":              "其他/备查文件",
}

# 市场 → concept → 关键词。**6 个关注点概念的词表 = worker 现役 FOCUS_POINTS 的市场级 superset**。
MAPPING = {
    "ashare": {
        "cover":              ["重要提示", "目录", "释义"],
        "company_profile":    ["公司简介", "主要财务指标"],
        "business_overview":  ["公司业务概要"],
        "mda":                ["管理层讨论与分析", "经营情况讨论与分析", "董事会报告", "公司业务概要"],
        "governance":         ["公司治理", "公司治理、环境和社会"],
        "esg":                ["环境和社会", "环境与社会", "社会责任"],
        "material_events":    ["重要事项"],
        "shareholders":       ["股份变动及股东情况", "股份变动", "股东情况"],
        "bonds":              ["债券相关情况", "公司债券相关情况"],
        "financial_report":   ["财务报告"],
        "financial_statements": ["财务报告", "财务报表", "合并资产负债表", "合并利润表", "合并现金流量表"],
        "notes":              ["财务报表附注", "附注"],
        "audit_report":       ["审计报告", "审计意见"],
        "risk":               ["风险因素", "风险"],
    },
    "jpx": {
        "company_profile":    ["企業の概況", "設備の状況"],
        "mda":                ["経営成績", "事業の状況", "経営に関する分析", "財政状態"],
        "material_events":    ["提出会社の状況"],
        "shareholders":       ["株式事務の概要", "大株主の状況"],
        "governance":         ["コーポレート・ガバナンス", "企業統治"],
        "financial_report":   ["経理の状況"],
        "financial_statements": ["財務諸表", "連結貸借対照表", "連結損益計算書", "連結キャッシュ・フロー計算書"],
        "notes":              ["注記", "注記事項"],
        "audit_report":       ["監査報告書", "独立監査人"],
        "risk":               ["事業等のリスク", "リスク"],
    },
    # 台湾：iXBRL（tw_xbrl）实测标题 = 財務報表那份，不是完整年報。
    #   完整年報的「公司概況/營運概況」在另一份年報 PDF 里，暂以常见结构占位。
    "twse": {
        "cover":              ["封面", "目錄", "目次", "財務報表索引"],
        "company_profile":    ["公司概況", "公司沿革"],
        "mda":                ["營運概況", "經營狀況", "營運之檢討與分析"],
        "governance":         ["公司治理", "公司治理情形"],
        "financial_statements": ["資產負債表", "綜合損益表", "現金流量表", "權益變動表", "財務報表"],
        "notes":              ["財務報表附註", "附註"],
        "audit_report":       ["會計師查核報告", "會計師核閱報告"],
        "risk":               ["風險", "風險管理"],
    },
    # 韩国：无 PDF 存档，词表来自 worker 现役 FOCUS_POINTS。
    "kr": {
        "mda":                ["경영진의 논의", "사업의 내용", "이사의 경영진단"],
        "governance":         ["지배구조", "기업지배구조"],
        "financial_statements": ["재무제표", "연결 재무상태표", "연결 손익계산서", "연결 현금흐름표"],
        "notes":              ["주석", "재무제표 주석"],
        "audit_report":       ["감사보고서", "감사의견"],
        "risk":               ["위험요소", "리스크 관리"],
    },
}


def _norm(s):
    """归一化：去空白/点号，和↔与统一。"""
    s = re.sub(r"[\s　]|\.{2,}|[·．]{2,}", "", s or "")
    return s.replace("与", "和")


def map_section(market, heading):
    """把某市场的章节标题映射到规范 concept。命中不了返回 None。

    最长关键词胜出：解决「财务报表附注」应命中 notes 而不是 financial_statements 这类子串冲突。
    """
    pats = MAPPING.get(market)
    if not pats:
        return None
    h = _norm(heading)
    best, best_len = None, 0
    for concept, kws in pats.items():
        for k in kws:
            kn = _norm(k)
            if kn and kn in h and len(kn) > best_len:
                best, best_len = concept, len(kn)
    return best


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    samples = [
        ("ashare", "第三节 管理层讨论与分析", "mda"),
        ("ashare", "第四节 公司治理", "governance"),
        ("ashare", "第八节 财务报告", "financial_report"),
        ("ashare", "十二、财务报表附注", "notes"),
        ("ashare", "合并资产负债表", "financial_statements"),
        ("jpx", "第2【事業の状況】", "mda"),
        ("jpx", "第5【経理の状況】", "financial_report"),
        ("jpx", "監査報告書", "audit_report"),
        ("twse", "資產負債表", "financial_statements"),
        ("twse", "會計師查核報告", "audit_report"),
        ("kr", "재무제표", "financial_statements"),
    ]
    for mkt, heading, want in samples:
        got = map_section(mkt, heading)
        print(f"{'✓' if got == want else '✗'} [{mkt}] {heading!r} -> {got} (期望 {want})")
