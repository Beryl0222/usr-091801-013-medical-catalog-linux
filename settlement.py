"""结算引擎与冲正。

结算请求按“就医发生时点 + 参保地 + 就医地”解析唯一适用的目录与映射版本：
- 账单使用就医地省级编码；
- 国家目录给出国家参照价；
- 参保地项目的支付限制决定可支付参照额；
- 1:N 按子项权重把国家父项目价格分摊到每条子项账单；
- N:1 一条省级捆绑行对应多个国家子项目，权重仅用于把账单金额拆分展示。

任何无法解析的行都进入人工队列，绝不猜测。结算记录一经生成不可修改，
生效后发现错误只能追加冲正记录（原始记录保留，冲正记录携带重算结果与差额）。
"""

from __future__ import annotations

import copy
from decimal import Decimal

from catalog import (
    ACTIVE,
    PENDING,
    d,
    iso_day,
    money,
    new_id,
    parse_date,
    utcnow_iso,
)

ENGINE_VERSION = "settlement-1.0"

LIVE = "LIVE"
SHADOW = "SHADOW"

SETTLED = "SETTLED"
MANUAL = "MANUAL_REVIEW"
REVERSED = "REVERSED"

QUEUE_OPEN = "OPEN"
QUEUE_ANNOTATED = "ANNOTATED"

REASON_NO_MAPPING = "MAPPING_UNRESOLVED"
REASON_ITEM_MISSING = "ITEM_VERSION_MISSING"
REASON_AMBIGUOUS = "AMBIGUOUS_MAPPING"


def _q(value):
    return d(value).quantize(Decimal("0.01"))


def _sum(records, key):
    total = Decimal("0")
    for record in records:
        value = record.get(key)
        if value is not None:
            total += d(value)
    return money(total) if records else None


class SettlementEngine:
    def __init__(self, registry):
        self.registry = registry

    # ----- 版本解析 -----

    def _groups_index(self, province, on_day):
        """该省在指定时点的生效分组索引，以及全部待生效候选提示。"""
        active = {}
        for entry in self.registry.effective_mappings(province, on_day):
            for member in entry["members"]:
                if member["side"] != "PROVINCE":
                    continue
                active.setdefault(member["code"], []).append(entry)
        # 待生效候选（含尚未到生效日的）只用于人工队列提示，绝不参与结算
        pending = []
        with self.registry.lock:
            for group in self.registry._state["mappings"].values():
                if group["province"] != province:
                    continue
                for version in group["versions"]:
                    if version["status"] != PENDING:
                        continue
                    for member in version["members"]:
                        if member["side"] == "PROVINCE":
                            pending.append({
                                "group_id": group["id"],
                                "version": version["version"],
                                "effective_from": version["effective_from"],
                                "code": member["code"],
                            })
        return active, pending

    @staticmethod
    def _province_codes(entry):
        return [m["code"] for m in entry["members"] if m["side"] == "PROVINCE"]

    @staticmethod
    def _national_codes(entry):
        return [m["code"] for m in entry["members"] if m["side"] == "NATIONAL"]

    def _weight_for(self, entry, side, code):
        """取成员权重。1:N 权重按省级子项顺序；N:1 按国家子项顺序。"""
        weights = entry.get("weights")
        if not weights:
            return Decimal("1")
        ordered = self._province_codes(entry) if side == "PROVINCE" \
            else self._national_codes(entry)
        idx = ordered.index(code)
        return d(weights[idx])

    def _insured_limit(self, insured_province, national_code, on_day, qty):
        """沿参保地映射查找对应省级项目的支付限制。

        参保地映射的分摊权重是独立的一套分摊（就医地 1:N 权重与参保地权重
        互不连乘），因此这里只乘参保地自身的 share。
        """
        if not insured_province:
            return None
        groups = self.registry.effective_mappings(insured_province, on_day)
        limit_amount = None
        hits = []
        for entry in groups:
            if national_code not in self._national_codes(entry):
                continue
            for prov_code in self._province_codes(entry):
                item = self.registry.effective_item(
                    f"PROV-{insured_province}", prov_code, on_day)
                if not item:
                    continue
                hits.append({"insured_code": prov_code,
                             "insured_item_version": item["version"],
                             "insured_catalog_version": item["catalog_version"],
                             "insured_payment_limit": item.get("payment_limit")})
                if item.get("payment_limit"):
                    share = self._weight_for(entry, "PROVINCE", prov_code)
                    value = d(item["payment_limit"]) * qty * share
                    limit_amount = value if limit_amount is None \
                        else limit_amount + value
        return {"hits": hits, "limit_amount": limit_amount}

    # ----- 单行结算 -----

    def _resolve_line(self, line, line_no, on_day, provider_province,
                      insured_province, pending_candidates):
        qty = d(line.get("quantity", 1))
        billed = d(line["amount"])
        code = line["province_code"]
        provider_catalog = f"PROV-{provider_province}"
        base = {
            "line_no": line_no,
            "provider_code": code,
            "quantity": str(qty),
            "billed_amount": money(billed),
            "resolution": [],
        }
        prov_item = self.registry.effective_item(provider_catalog, code, on_day)
        if prov_item:
            base["provider_name"] = prov_item["name"]
            base["provider_item_version"] = prov_item["version"]
            base["provider_catalog_version"] = prov_item["catalog_version"]

        index, _pending = self._groups_index(provider_province, on_day)
        candidates = index.get(code, [])
        if len(candidates) > 1:
            return None, [{
                "line_no": line_no, "code": code,
                "reason": REASON_AMBIGUOUS,
                "detail": "该编码在就医地同时命中多个生效映射",
                "candidates": [{"group_id": e["group_id"], "version": e["version"]}
                               for e in candidates],
            }]
        entry = candidates[0] if candidates else None
        if entry is None:
            related = [c for c in _pending if c["code"] == code]
            return None, [{
                "line_no": line_no, "code": code,
                "reason": REASON_NO_MAPPING,
                "detail": "就医发生时点无生效映射；待生效版本不可用于猜测结算",
                "pending_candidates": related,
            }]
        if not prov_item:
            return None, [{
                "line_no": line_no, "code": code,
                "reason": REASON_ITEM_MISSING,
                "detail": f"就医地目录 {provider_catalog} 在 {iso_day(on_day)} "
                          f"不存在项目 {code} 的有效版本",
            }]

        national_id = self.registry.national_catalog_id()
        direction = entry["direction"]
        nat_codes = self._national_codes(entry)
        resolutions = []
        expected_total = Decimal("0")
        limit_total = None

        if direction == "1:N":
            parent = nat_codes[0]
            nat_item = self.registry.effective_item(national_id, parent, on_day)
            if not nat_item:
                return None, [{"line_no": line_no, "code": code,
                               "reason": REASON_ITEM_MISSING,
                               "detail": f"国家项目 {parent} 无有效版本"}]
            weight = self._weight_for(entry, "PROVINCE", code)
            expected = d(nat_item["price"]) * qty * weight \
                if nat_item.get("price") else Decimal("0")
            expected_total += expected
            insured = self._insured_limit(
                insured_province, parent, on_day, qty)
            limit_total = insured["limit_amount"] if insured else None
            resolutions.append({
                "national_code": parent,
                "national_name": nat_item["name"],
                "national_item_version": nat_item["version"],
                "national_catalog_version": nat_item["catalog_version"],
                "direction": direction,
                "group_id": entry["group_id"],
                "mapping_version": entry["version"],
                "mapping_status": ACTIVE,
                "mapping_effective_from": entry["effective_from"],
                "mapping_effective_until": entry["effective_until"],
                "weight": str(weight),
                # 1:N：账单行本身就是某一子项的实际收费，不再乘权重；
                # 权重只用于把国家父项目参照价分摊到子项
                "apportioned_billed": money(billed),
                "national_expected_amount": money(expected),
                "insured_matches": insured["hits"] if insured else [],
                "insured_limit_amount": money(insured["limit_amount"])
                    if insured and insured["limit_amount"] is not None else None,
            })
        else:
            # 1:1 与 N:1：账单行上的每个国家项目逐条解析
            for nat_code in nat_codes:
                nat_item = self.registry.effective_item(national_id, nat_code, on_day)
                if not nat_item:
                    return None, [{"line_no": line_no, "code": code,
                                   "reason": REASON_ITEM_MISSING,
                                   "detail": f"国家项目 {nat_code} 无有效版本"}]
                weight = self._weight_for(entry, "NATIONAL", nat_code) \
                    if direction == "N:1" else Decimal("1")
                expected = (d(nat_item["price"]) * qty) if nat_item.get("price") \
                    else Decimal("0")
                expected_total += expected
                insured = self._insured_limit(
                    insured_province, nat_code, on_day, qty)
                if insured and insured["limit_amount"] is not None:
                    limit_total = insured["limit_amount"] \
                        if limit_total is None \
                        else limit_total + insured["limit_amount"]
                resolutions.append({
                    "national_code": nat_code,
                    "national_name": nat_item["name"],
                    "national_item_version": nat_item["version"],
                    "national_catalog_version": nat_item["catalog_version"],
                    "direction": direction,
                    "group_id": entry["group_id"],
                    "mapping_version": entry["version"],
                    "mapping_status": ACTIVE,
                    "mapping_effective_from": entry["effective_from"],
                    "mapping_effective_until": entry["effective_until"],
                    "weight": str(weight),
                    "apportioned_billed": money(billed * weight),
                    "national_expected_amount": money(expected),
                    "insured_matches": insured["hits"] if insured else [],
                    "insured_limit_amount": money(insured["limit_amount"])
                        if insured and insured["limit_amount"] is not None else None,
                })

        expected_total = _q(expected_total)
        payable = min(expected_total, _q(limit_total)) \
            if limit_total is not None else expected_total
        base["resolution"] = resolutions
        base["national_expected_amount"] = money(expected_total)
        base["insured_limit_amount"] = money(limit_total) \
            if limit_total is not None else None
        base["payable_reference_amount"] = money(payable)
        base["difference_amount"] = money(_q(billed) - expected_total)
        return base, []

    # ----- 整单结算 -----

    def settle(self, request, mode=LIVE, actor="system"):
        required = ("bill_id", "service_date", "provider_province", "lines")
        for field in required:
            if field not in request:
                from catalog import ValidationError
                raise ValidationError(f"结算请求缺少字段: {field}")
        on_day = parse_date(request["service_date"])
        provider_province = request["provider_province"]
        insured_province = request.get("insured_province", provider_province)
        if not request["lines"]:
            from catalog import ValidationError
            raise ValidationError("账单至少包含一行费用")

        national_id = self.registry.national_catalog_id()
        lines, unresolved = [], []
        for idx, line in enumerate(request["lines"], start=1):
            resolved, problems = self._resolve_line(
                line, idx, on_day, provider_province, insured_province, [])
            if problems:
                unresolved.extend(problems)
                lines.append({
                    "line_no": idx,
                    "provider_code": line["province_code"],
                    "quantity": str(d(line.get("quantity", 1))),
                    "billed_amount": money(d(line["amount"])),
                    "resolution": [],
                })
            else:
                lines.append(resolved)

        status = SETTLED if not unresolved else MANUAL
        result = {
            "id": new_id("stl"),
            "bill_id": request["bill_id"],
            "mode": mode,
            "status": status,
            "service_date": iso_day(on_day),
            "insured_province": insured_province,
            "provider_province": provider_province,
            "engine_version": ENGINE_VERSION,
            "national_catalog_id": national_id,
            "national_catalog_version":
                self.registry.get_catalog(national_id)["current_version"],
            "provider_catalog_id": f"PROV-{provider_province}",
            "provider_catalog_version":
                self.registry.get_catalog(f"PROV-{provider_province}")["current_version"],
            "insured_catalog_id": f"PROV-{insured_province}",
            "insured_catalog_version":
                self.registry.get_catalog(f"PROV-{insured_province}")["current_version"],
            "lines": lines,
            "unresolved": unresolved,
            "totals": {
                "billed_amount": _sum(lines, "billed_amount"),
                "national_expected_amount": _sum(lines, "national_expected_amount"),
                "payable_reference_amount": _sum(lines, "payable_reference_amount"),
                "difference_amount": _sum(lines, "difference_amount"),
            },
            "created_at": utcnow_iso(),
            "created_by": actor,
        }

        with self.registry.lock:
            if mode == LIVE:
                self.registry._state["settlements"][result["id"]] = result
                if unresolved:
                    queue_id = new_id("que")
                    queue_item = {
                        "id": queue_id,
                        "status": QUEUE_OPEN,
                        "settlement_id": result["id"],
                        "bill_id": request["bill_id"],
                        "service_date": iso_day(on_day),
                        "insured_province": insured_province,
                        "provider_province": provider_province,
                        "reasons": unresolved,
                        "created_at": result["created_at"],
                        "annotations": [],
                    }
                    self.registry._state["queue"][queue_id] = queue_item
                    result["queue_id"] = queue_id
                self.registry._audit_locked(actor, "SETTLEMENT_SETTLED", {
                    "settlement_id": result["id"], "bill_id": request["bill_id"],
                    "status": status, "mode": mode,
                })
                self.registry._persist_locked()
        return result

    def get_settlement(self, settlement_id):
        from catalog import NotFound
        with self.registry.lock:
            record = self.registry._state["settlements"].get(settlement_id)
            if not record:
                raise NotFound(f"结算记录不存在: {settlement_id}")
            out = copy.deepcopy(record)
            out["reversals"] = [
                {"id": r["id"], "reason": r["reason"], "created_at": r["created_at"],
                 "deltas": r["deltas"]}
                for r in self.registry._state["reversals"]
                if r["original_settlement_id"] == settlement_id
            ]
            return out

    def list_settlements(self, bill_id=None, status=None):
        with self.registry.lock:
            out = []
            for record in self.registry._state["settlements"].values():
                if bill_id and record["bill_id"] != bill_id:
                    continue
                if status and record["status"] != status:
                    continue
                out.append({k: v for k, v in record.items()
                            if k in ("id", "bill_id", "status", "mode",
                                     "service_date", "insured_province",
                                     "provider_province", "totals", "queue_id",
                                     "created_at")})
            return out

    # ----- 人工队列 -----

    def list_queue(self, status=None):
        with self.registry.lock:
            out = []
            for item in self.registry._state["queue"].values():
                if status and item["status"] != status:
                    continue
                out.append(copy.deepcopy(item))
            return out

    def annotate_queue(self, queue_id, actor, note, decision="ANNOTATED"):
        from catalog import NotFound, ValidationError
        if not note:
            raise ValidationError("队列处理意见不能为空")
        if decision not in ("ANNOTATED", "ESCALATED", "RESOLVED_EXTERNALLY"):
            raise ValidationError("不支持的队列处理结论")
        with self.registry.lock:
            item = self.registry._state["queue"].get(queue_id)
            if not item:
                raise NotFound(f"人工队列项不存在: {queue_id}")
            annotation = {"actor": actor, "note": note, "decision": decision,
                          "created_at": utcnow_iso()}
            item["annotations"].append(annotation)
            if decision == "RESOLVED_EXTERNALLY":
                item["status"] = "CLOSED"
            elif decision == "ESCALATED":
                item["status"] = "ESCALATED"
            else:
                item["status"] = QUEUE_ANNOTATED
            self.registry._audit_locked(actor, "QUEUE_ANNOTATED",
                                        {"queue_id": queue_id, "decision": decision})
            self.registry._persist_locked()
            return copy.deepcopy(item)

    # ----- 冲正（原始记录不可变）-----

    def reverse(self, settlement_id, reason, actor, corrected_lines):
        """为已生成结算追加冲正记录。

        corrected_lines 按行号给出更正后的数量/金额（错误通常来自录入），
        引擎在同一就医时点重新解析版本，得到冲正后结果与差额；原记录保留。
        """
        from catalog import NotFound, ConflictError, ValidationError
        if not reason or not reason.strip():
            raise ValidationError("冲正必须填写原因")
        with self.registry.lock:
            original = self.registry._state["settlements"].get(settlement_id)
            if not original:
                raise NotFound(f"结算记录不存在: {settlement_id}")
            if original["mode"] != LIVE:
                raise ConflictError("影子结算不允许冲正")
            corrections = {row["line_no"]: row for row in (corrected_lines or [])}
            unknown = sorted(set(corrections) - {l["line_no"] for l in original["lines"]})
            if unknown:
                raise ValidationError("更正行号超出原始账单范围", {"line_nos": unknown})
            request = {
                "bill_id": original["bill_id"],
                "service_date": original["service_date"],
                "provider_province": original["provider_province"],
                "insured_province": original["insured_province"],
                "lines": [],
            }
            for line in original["lines"]:
                fix = corrections.get(line["line_no"], {})
                request["lines"].append({
                    "province_code": line["provider_code"],
                    "quantity": fix.get("quantity", line["quantity"]),
                    "amount": fix.get("billed_amount", line["billed_amount"]),
                })

        # 以 SHADOW 模式重算：复用引擎解析逻辑但不写入正式结算
        corrected = self.settle(request, mode=SHADOW, actor=actor)
        corrected.pop("queue_id", None)
        with self.registry.lock:
            deltas = {}
            for key in ("billed_amount", "national_expected_amount",
                        "payable_reference_amount", "difference_amount"):
                old_v = d(original["totals"].get(key) or 0)
                new_v = d(corrected["totals"].get(key) or 0)
                deltas[key] = money(new_v - old_v)
            record = {
                "id": new_id("rev"),
                "original_settlement_id": settlement_id,
                "bill_id": original["bill_id"],
                "reason": reason,
                "actor": actor,
                "corrected_lines": corrected_lines or [],
                "corrected_result": corrected,
                "deltas": deltas,
                "created_at": corrected["created_at"],
            }
            self.registry._state["reversals"].append(record)
            self.registry._audit_locked(actor, "SETTLEMENT_REVERSED", {
                "settlement_id": settlement_id, "reversal_id": record["id"],
                "reason": reason, "deltas": deltas,
            })
            self.registry._persist_locked()
            return copy.deepcopy(record)

    def get_reversal(self, reversal_id):
        from catalog import NotFound
        with self.registry.lock:
            for record in self.registry._state["reversals"]:
                if record["id"] == reversal_id:
                    return copy.deepcopy(record)
            raise NotFound(f"冲正记录不存在: {reversal_id}")
