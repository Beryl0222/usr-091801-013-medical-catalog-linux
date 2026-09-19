"""影子模式回放。

用历史脱敏账单驱动结算引擎，但不写入任何正式结算、不产生人工队列，
只产出覆盖率与支付差异报告。报告逐单、逐行携带目录与映射版本，
便于经办人员核对“当时为什么这么算”。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from decimal import Decimal

from catalog import d, money
from settlement import SETTLED, SettlementEngine

DEFAULT_BILLS = os.path.join(os.path.dirname(__file__), "data",
                             "historical_bills.json")


def load_bills(path=DEFAULT_BILLS):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _increment(bucket, key, field, value):
    bucket[key][field] = bucket[key].get(field, Decimal("0")) + d(value)


def replay(registry, bills=None, path=DEFAULT_BILLS):
    bills = bills if bills is not None else load_bills(path)
    engine = SettlementEngine(registry)
    report = {
        "mode": "SHADOW",
        "bills_total": len(bills),
        "bills_fully_covered": 0,
        "bills_with_unresolved": 0,
        "lines_total": 0,
        "lines_resolved": 0,
        "line_coverage_rate": None,
        "bill_coverage_rate": None,
        "amounts": {
            "billed": "0.00",
            "national_expected": "0.00",
            "payable_reference": "0.00",
            "difference_billed_vs_expected": "0.00",
        },
        "by_provider_province": {},
        "by_category": {},
        "unresolved_reasons": defaultdict(int),
        "bills": [],
    }

    billed_total = expected_total = payable_total = Decimal("0")
    by_prov = defaultdict(lambda: {"bills": 0, "lines": 0, "resolved": 0,
                                   "billed": Decimal("0"),
                                   "national_expected": Decimal("0")})
    by_cat = defaultdict(lambda: {"lines": 0, "billed": Decimal("0"),
                                  "national_expected": Decimal("0")})

    for bill in bills:
        result = engine.settle(bill, mode="SHADOW", actor="shadow-replay")
        fully = result["status"] == SETTLED
        report["bills_fully_covered" if fully else "bills_with_unresolved"] += 1
        province_bucket = by_prov[result["provider_province"]]
        province_bucket["bills"] += 1

        resolved_lines = 0
        for line in result["lines"]:
            report["lines_total"] += 1
            province_bucket["lines"] += 1
            line_billed = d(line.get("billed_amount") or 0)
            billed_total += line_billed
            province_bucket["billed"] += line_billed
            if line.get("resolution"):
                resolved_lines += 1
                report["lines_resolved"] += 1
                province_bucket["resolved"] += 1
                line_expected = d(line.get("national_expected_amount") or 0)
                expected_total += line_expected
                province_bucket["national_expected"] += line_expected
                for res in line["resolution"]:
                    # 用国家项目版本回查类别，避免在结算结果里冗余类别字段
                    item = registry.effective_item(
                        result["national_catalog_id"],
                        res["national_code"], result["service_date"])
                    cat = item["category"] if item else "UNKNOWN"
                    bucket = by_cat[cat]
                    bucket["lines"] += 1
                    amt = d(res.get("national_expected_amount") or 0)
                    bucket["national_expected"] += amt
                    bucket["billed"] += d(res.get("apportioned_billed") or 0)
        for problem in result.get("unresolved", []):
            report["unresolved_reasons"][problem["reason"]] += 1

        totals = result["totals"]
        payable_total += d(totals.get("payable_reference_amount") or 0)

        report["bills"].append({
            "bill_id": result["bill_id"],
            "service_date": result["service_date"],
            "provider_province": result["provider_province"],
            "insured_province": result["insured_province"],
            "case_summary": bill.get("case_summary", ""),
            "status": result["status"],
            "resolved_lines": resolved_lines,
            "total_lines": len(result["lines"]),
            "totals": totals,
            "national_catalog_version": result["national_catalog_version"],
            "provider_catalog_version": result["provider_catalog_version"],
            "insured_catalog_version": result["insured_catalog_version"],
            "mapping_versions": sorted({
                (r["group_id"], r["mapping_version"])
                for line in result["lines"] for r in line.get("resolution", [])
            }),
            "unresolved": result.get("unresolved", []),
        })

    lines_total = report["lines_total"]
    report["line_coverage_rate"] = (
        round(report["lines_resolved"] / lines_total, 4) if lines_total else None)
    bills_total = report["bills_total"]
    report["bill_coverage_rate"] = (
        round(report["bills_fully_covered"] / bills_total, 4)
        if bills_total else None)
    report["amounts"] = {
        "billed": money(billed_total),
        "national_expected": money(expected_total),
        "payable_reference": money(payable_total),
        "difference_billed_vs_expected": money(billed_total - expected_total),
    }
    report["by_provider_province"] = {
        prov: {
            "bills": b["bills"], "lines": b["lines"], "resolved": b["resolved"],
            "line_coverage_rate": round(b["resolved"] / b["lines"], 4)
                if b["lines"] else None,
            "billed": money(b["billed"]),
            "national_expected": money(b["national_expected"]),
            "difference": money(b["billed"] - b["national_expected"]),
        }
        for prov, b in sorted(by_prov.items())
    }
    report["by_category"] = {
        cat: {
            "lines": b["lines"], "billed": money(b["billed"]),
            "national_expected": money(b["national_expected"]),
            "difference": money(b["billed"] - b["national_expected"]),
        }
        for cat, b in sorted(by_cat.items())
    }
    report["unresolved_reasons"] = dict(report["unresolved_reasons"])
    return report


def format_text(report):
    """生成经办人员可读的文本版报告。"""
    lines = [
        "===== 国家医保目录衔接 · 影子回放报告 =====",
        f"回放账单: {report['bills_total']} 张；"
        f"整单覆盖: {report['bills_fully_covered']} 张 "
        f"({pct(report['bill_coverage_rate'])})；"
        f"存在未决: {report['bills_with_unresolved']} 张",
        f"费用行: {report['lines_total']} 行；"
        f"已解析: {report['lines_resolved']} 行 "
        f"({pct(report['line_coverage_rate'])})",
        "",
        "[支付差异（元）]",
        f"  历史申报合计        {report['amounts']['billed']}",
        f"  国家参照价合计      {report['amounts']['national_expected']}",
        f"  参保地支付限额参照  {report['amounts']['payable_reference']}",
        f"  申报-参照 差异      {report['amounts']['difference_billed_vs_expected']}",
        "",
        "[按就医地]",
    ]
    for prov, b in report["by_provider_province"].items():
        lines.append(
            f"  {prov}: 账单{b['bills']} 行覆盖率{pct(b['line_coverage_rate'])} "
            f"申报{b['billed']} 参照{b['national_expected']} 差异{b['difference']}")
    lines.append("")
    lines.append("[按服务类别]")
    for cat, b in report["by_category"].items():
        lines.append(
            f"  {cat}: {b['lines']}行 申报{b['billed']} "
            f"参照{b['national_expected']} 差异{b['difference']}")
    if report["unresolved_reasons"]:
        lines.append("")
        lines.append("[未决原因分布（全部进入人工队列，不做猜测）]")
        for reason, count in sorted(report["unresolved_reasons"].items()):
            lines.append(f"  {reason}: {count}")
    lines.append("")
    lines.append("[逐单结果]")
    for bill in report["bills"]:
        flag = "覆盖" if bill["status"] == "SETTLED" else "未决"
        lines.append(
            f"  {bill['bill_id']} {bill['service_date']} "
            f"{bill['insured_province']}→{bill['provider_province']} [{flag}] "
            f"{bill['resolved_lines']}/{bill['total_lines']}行 "
            f"申报{bill['totals']['billed_amount']} "
            f"参照{bill['totals']['national_expected_amount']}")
        lines.append(f"    目录版本 国家v{bill['national_catalog_version']} "
                     f"就医地v{bill['provider_catalog_version']} "
                     f"参保地v{bill['insured_catalog_version']}")
        versions = ", ".join(f"{g}v{v}" for g, v in bill["mapping_versions"])
        lines.append(f"    映射版本: {versions or '（无）'}")
        for problem in bill["unresolved"]:
            lines.append(f"    未决: 行{problem['line_no']} {problem['code']} "
                         f"{problem['reason']} - {problem['detail']}")
    return "\n".join(lines)


def pct(rate):
    return f"{rate * 100:.1f}%" if rate is not None else "—"
