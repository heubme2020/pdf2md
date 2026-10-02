# -*- coding: utf-8 -*-
"""从 section_ontology.py 生成 worker/src/index.ts 的 FOCUS_POINTS 片段（单一真源）。

用法：python gen_focus_points.py > 片段.ts
把片段贴进 worker/src/index.ts 的 `const FOCUS_POINTS = ...`（注意：改完要 deploy，见记忆 worker-deploy-auth）。

本脚本把 section_ontology.MAPPING 的**市场级 superset** 展开到 exchange 级，
再叠加 EXCHANGE_OVERRIDES 里的 exchange 级差异（bj⊂ashare、knx⊂kr，抄自 worker 现役）。
"""
import json
import section_ontology as so

# concept → (worker focus-point id, aliases) —— 只覆盖 worker 部署的那 6 个关注点
FOCUS = {
    "mda":                  ("mdna",       ["md&a", "mda", "management discussion", "management's discussion",
                                            "经营讨论", "管理层讨论", "运营讨论", "经营情况讨论"]),
    "financial_statements": ("statements", ["financial statements", "financial statement", "三大表", "财务报表"]),
    "notes":                ("notes",      ["notes to the financial statements", "notes", "附注", "附註"]),
    "audit_report":         ("audit",      ["audit report", "auditor's report", "independent auditor", "审计报告", "감사보고서"]),
    "risk":                 ("risk",       ["risk factors", "risk", "风险", "風險", "리스크"]),
    "governance":           ("governance", ["corporate governance", "governance", "公司治理", "企業統治", "지배구조"]),
}

# market → 覆盖的 exchange
MARKET_EXCHANGES = {
    "ashare": ["sse", "szse", "bj"],
    "jpx":    ["jpx"],
    "twse":   ["twse", "tpex"],
    "kr":     ["ksc", "koe", "knx"],
}

# exchange 级差异（bj 是 ashare 的子集、knx 是 kr 的子集）—— 抄自 worker 现役 FOCUS_POINTS
EXCHANGE_OVERRIDES = {
    "bj": {
        "mda":                  ["管理层讨论与分析", "经营情况讨论与分析"],
        "financial_statements": ["财务报告", "财务报表"],
        "audit_report":         ["审计报告"],
        "risk":                 ["风险因素"],
        "governance":           ["公司治理"],
    },
    "knx": {
        "mda":                  ["경영진의 논의", "사업의 내용"],
        "financial_statements": ["재무제표"],
        "notes":                ["주석"],
        "audit_report":         ["감사보고서"],
        "risk":                 ["위험요소"],
        "governance":           ["지배구조"],
    },
}


def keywords_for(exchange, market, concept):
    """该 exchange 下 concept 的最终关键词（先查 override，再回落到市场 superset）。"""
    return EXCHANGE_OVERRIDES.get(exchange, {}).get(concept) or so.MAPPING.get(market, {}).get(concept, [])


def emit_ts():
    out = ["const FOCUS_POINTS: { id: string; aliases: string[]; keywords: Record<string, string[]> }[] = ["]
    for concept, (fpid, aliases) in FOCUS.items():
        out.append("  {")
        out.append(f'    id: "{fpid}",')
        out.append(f'    aliases: {json.dumps(aliases, ensure_ascii=False)},')
        out.append("    keywords: {")
        for mkt, exchanges in MARKET_EXCHANGES.items():
            for ex in exchanges:
                kws = keywords_for(ex, mkt, concept)
                out.append(f'      {ex}: {json.dumps(kws, ensure_ascii=False)},')
        out.append("    },")
        out.append("  },")
    out.append("];")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(emit_ts())
