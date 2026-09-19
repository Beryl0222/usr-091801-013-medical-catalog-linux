"""目录治理后端的领域与接口测试。

覆盖：
- 目录项目版本与按就医时点取唯一版本；
- 映射建议的依据/冲突/双审/发布/撤回/接续；
- 跨日跑批的整批原子性（冲突全批中止、状态不变）；
- 结算的 1:N / N:1 分摊、跨省支付限额、未决入队、版本可追溯；
- 冲正只追加、原记录不可变；影子回放不写正式数据；
- 多线程并发发布/跑批/结算下的事务一致性；
- HTTP 契约。
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from catalog import (
    ACTIVE,
    BUSINESS_REVIEW,
    BUSINESS_APPROVED,
    ConflictError,
    FINANCE_REVIEW,
    ONE_TO_MANY,
    ONE_TO_ONE,
    PENDING,
    Registry,
    REVIEW_APPROVE,
    REVIEW_REJECT,
    ValidationError,
    WITHDRAWN,
)
from seed_data import seed
from service import Application, Handler
from settlement import (
    MANUAL,
    SETTLED,
    SettlementEngine,
)
import shadow


TODAY = date(2026, 9, 19)


def build_registry():
    return Registry(clock=lambda: TODAY)


def seeded():
    return seed(build_registry())


def make_proposal(reg, province, direction, national, provinces,
                  weights=None, actor="expert-张"):
    """提交建议，自动完成冲突确认。返回建议记录。"""
    members = [{"side": "NATIONAL", "code": c} for c in national]
    members += [{"side": "PROVINCE", "code": c} for c in provinces]
    evidence = [{"type": "DOCUMENT", "reference": "医保办[2026]1号",
                 "summary": "价格项目衔接专家论证纪要"}]
    try:
        return reg.submit_proposal(province, direction, members, evidence,
                                   weights=weights, actor=actor)
    except ConflictError as exc:
        conflicts = exc.details["conflicts"]
        acks = [{"key": c["key"], "note": "专家确认粒度调整并接续旧版本"}
                for c in conflicts]
        return reg.submit_proposal(province, direction, members, evidence,
                                   conflict_acknowledgements=acks,
                                   weights=weights, actor=actor)


def approve_and_publish(reg, pid, effective_from, actor="publisher-李"):
    reg.review_proposal(pid, BUSINESS_REVIEW, REVIEW_APPROVE, "business-王")
    reg.review_proposal(pid, FINANCE_REVIEW, REVIEW_APPROVE, "finance-赵")
    return reg.publish_proposal(pid, effective_from, actor=actor)


def add_test_items(reg):
    """登记仅供测试用的国家/沪/浙项目，避免与种子映射纠缠。"""
    reg.add_item("NATIONAL", "N-T001", "测试护理项甲", "护理", unit="日",
                 price="100.00", effective_from="2026-01-01")
    reg.add_item("NATIONAL", "N-T002", "测试护理项乙", "护理", unit="日",
                 price="200.00", effective_from="2026-01-01")
    reg.add_item("PROV-SH", "SH-T001", "沪测试护理甲", "护理", unit="日",
                 price="95.00", payment_limit="90.00",
                 effective_from="2026-01-01")
    reg.add_item("PROV-SH", "SH-T002", "沪测试护理乙", "护理", unit="日",
                 price="190.00", payment_limit="180.00",
                 effective_from="2026-01-01")


class CatalogVersionTest(unittest.TestCase):
    def test_item_versions_are_half_open_and_point_in_time_unique(self):
        reg = seeded()
        reg.add_item("NATIONAL", "N-NK001", "普通门诊诊查费(修订)", "内科",
                     unit="次", price="26.00", effective_from="2026-10-01")
        v_old = reg.effective_item("NATIONAL", "N-NK001", date(2026, 9, 30))
        v_boundary = reg.effective_item("NATIONAL", "N-NK001", date(2026, 10, 1))
        self.assertEqual(v_old["price"], "25.00")
        self.assertEqual(v_boundary["price"], "26.00")
        # 旧版本区间在新版本起点闭合
        self.assertEqual(v_old["effective_until"], "2026-10-01")
        self.assertIsNone(v_boundary["effective_until"])
        # 生效早于首个版本：查不到，而不是抛错或猜测
        self.assertIsNone(reg.effective_item("NATIONAL", "N-NK001",
                                             date(2025, 1, 1)))

    def test_overlapping_item_version_rejected(self):
        reg = seeded()
        # 追溯日期的新版本不允许：会重写历史费用解释
        with self.assertRaises(ConflictError):
            reg.add_item("NATIONAL", "N-NK001", "追溯版本", "内科",
                         price="99.00", effective_from="2026-02-01")
        # 未来但起点不晚于现版本起点也不允许
        with self.assertRaises(ConflictError):
            reg.add_item("NATIONAL", "N-NK001", "重复版本", "内科",
                         price="99.00", effective_from="2020-01-01")

    def test_category_must_be_one_of_thirteen(self):
        reg = seeded()
        with self.assertRaises(ValidationError):
            reg.add_item("NATIONAL", "N-X1", "杂项", "不属于十三类",
                         price="1.00", effective_from="2026-01-01")


class ProposalWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.reg = seeded()

    def test_evidence_is_mandatory(self):
        members = [{"side": "NATIONAL", "code": "N-HL002"},
                   {"side": "PROVINCE", "code": "ZJ-HL003"}]
        with self.assertRaises(ValidationError):
            self.reg.submit_proposal("ZJ", ONE_TO_ONE, members, evidence=[])
        with self.assertRaises(ValidationError):
            self.reg.submit_proposal("ZJ", ONE_TO_ONE, members,
                                     evidence=[{"type": "NOTE", "summary": ""}])

    def test_conflict_must_be_listed_and_acknowledged(self):
        # SH-HL002 已映射 N-HL002，改挂 N-HL001 必须被检出
        members = [{"side": "NATIONAL", "code": "N-HL001"},
                   {"side": "PROVINCE", "code": "SH-HL002"}]
        evidence = [{"type": "DOCUMENT", "reference": "x", "summary": "s"}]
        with self.assertRaises(ConflictError) as caught:
            self.reg.submit_proposal("SH", ONE_TO_ONE, members, evidence)
        conflicts = caught.exception.details["conflicts"]
        self.assertTrue(conflicts)
        self.assertEqual(conflicts[0]["type"], "OVERLAPPING_MEMBER")
        missing = caught.exception.details["missing_acknowledgements"]
        self.assertEqual(set(missing), {c["key"] for c in conflicts})
        # 逐条确认后允许提交，建议中永久保留冲突清单
        proposal = self.reg.submit_proposal(
            "SH", ONE_TO_ONE, members, evidence,
            conflict_acknowledgements=[{"key": c["key"], "note": "确认"}
                                       for c in conflicts])
        self.assertEqual(len(proposal["conflicts"]), len(conflicts))

    def test_direction_must_match_member_shape_and_category(self):
        evidence = [{"type": "DOCUMENT", "reference": "x", "summary": "s"}]
        with self.assertRaises(ValidationError):
            # 1:N 需要至少两个省级子项
            self.reg.submit_proposal(
                "ZJ", ONE_TO_MANY,
                [{"side": "NATIONAL", "code": "N-KF001"},
                 {"side": "PROVINCE", "code": "SH-KF001"}], evidence)
        with self.assertRaises(ValidationError):
            # 跨类别映射
            self.reg.submit_proposal(
                "SH", ONE_TO_ONE,
                [{"side": "NATIONAL", "code": "N-KF001"},
                 {"side": "PROVINCE", "code": "SH-ZY001"}], evidence)

    def test_review_order_and_double_review(self):
        p = make_proposal(self.reg, "ZJ", ONE_TO_ONE,
                          ["N-HL002"], ["ZJ-HL003"])
        # 财务不能先审
        with self.assertRaises(ConflictError):
            self.reg.review_proposal(p["id"], FINANCE_REVIEW,
                                     REVIEW_APPROVE, "f")
        self.reg.review_proposal(p["id"], BUSINESS_REVIEW,
                                 REVIEW_APPROVE, "b")
        self.assertEqual(self.reg.get_proposal(p["id"])["status"],
                         BUSINESS_APPROVED)
        # 业务不能复审
        with self.assertRaises(ConflictError):
            self.reg.review_proposal(p["id"], BUSINESS_REVIEW,
                                     REVIEW_APPROVE, "b2")
        self.reg.review_proposal(p["id"], FINANCE_REVIEW,
                                 REVIEW_REJECT, "f", comment="限额存疑")
        # 被拒后不能发布
        with self.assertRaises(ConflictError):
            self.reg.publish_proposal(p["id"], "2026-10-01")

    def test_only_double_approved_can_publish(self):
        p = make_proposal(self.reg, "ZJ", ONE_TO_ONE,
                          ["N-HL002"], ["ZJ-HL003"])
        with self.assertRaises(ConflictError):
            self.reg.publish_proposal(p["id"], "2026-10-01")

    def test_withdraw_reopens_predecessor_and_pending_not_usable(self):
        eng = SettlementEngine(self.reg)
        p = make_proposal(self.reg, "SH", ONE_TO_ONE,
                          ["N-HL001"], ["SH-HL002"])
        pub = approve_and_publish(self.reg, p["id"], "2026-10-01")
        gid, ver = pub["group_id"], pub["version"]

        def settle(day, bill_id):
            return eng.settle({
                "bill_id": bill_id, "service_date": day,
                "provider_province": "SH", "insured_province": "SH",
                "lines": [{"province_code": "SH-HL002", "quantity": 1,
                           "amount": "14.00"}]})

        # 生效日前一天仍走旧映射
        before = settle("2026-09-30", "B0")
        self.assertEqual(before["status"], SETTLED)
        self.assertEqual(before["lines"][0]["resolution"][0]["national_code"],
                         "N-HL002")
        # 生效日当天但尚未跑批：旧版本区间已闭合、新版本未激活，
        # 必须进入人工队列，且不能使用待生效版本猜测
        gap = settle("2026-10-01", "B1")
        self.assertEqual(gap["status"], MANUAL)
        self.assertEqual(gap["unresolved"][0]["pending_candidates"][0]
                         ["effective_from"], "2026-10-01")
        # 撤回：旧版本重新 ACTIVE 且区间开放，同日账单恢复可结算
        self.reg.withdraw_version(gid, ver, "publisher-李", "依据材料待补")
        group = self.reg.get_group(gid)
        self.assertEqual(group["versions"][1]["status"], WITHDRAWN)
        self.assertEqual(group["versions"][0]["status"], ACTIVE)
        self.assertIsNone(group["versions"][0]["effective_until"])
        recovered = settle("2026-10-01", "B2")
        self.assertEqual(recovered["status"], SETTLED)
        self.assertEqual(recovered["lines"][0]["resolution"][0]["national_code"],
                         "N-HL002")
        # 撤回后可重新发布新版本接续
        p2 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-HL001"], ["SH-HL002"])
        approve_and_publish(self.reg, p2["id"], "2026-10-01")
        self.assertEqual(len(self.reg.get_group(gid)["versions"]), 3)
        # 已生效版本不允许撤回
        with self.assertRaises(ConflictError):
            self.reg.withdraw_version(gid, 1, "x", "尝试撤回已生效版本")


class ActivationBatchTest(unittest.TestCase):
    def setUp(self):
        self.reg = seeded()
        add_test_items(self.reg)

    def test_batch_activates_all_due_and_marks_superseded(self):
        p1 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-T001"], ["SH-T001"])
        pub1 = approve_and_publish(self.reg, p1["id"], "2026-10-01")
        p2 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-T002"], ["SH-T002"])
        approve_and_publish(self.reg, p2["id"], "2026-11-01")
        # 10-01 跑批只激活到期者
        r1 = self.reg.activate_due(date(2026, 10, 1))
        self.assertEqual(len(r1["activated"]), 1)
        group1 = self.reg.get_group(pub1["group_id"])
        self.assertEqual(group1["versions"][-1]["status"], ACTIVE)
        # 11-01 跑批前一致性提示有待激活
        warn = self.reg.consistency_check(date(2026, 11, 1))
        self.assertTrue(any(i["type"] == "DUE_NOT_ACTIVATED"
                            for i in warn["issues"]))
        r2 = self.reg.activate_due(date(2026, 11, 1))
        self.assertEqual(len(r2["activated"]), 1)
        self.assertTrue(self.reg.consistency_check(date(2026, 11, 1))["ok"])

    def test_pending_overlap_is_blocked_at_publish_time(self):
        # 同一编码在存在待生效版本期间不允许再发布交叉版本
        p1 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-T001"], ["SH-T001"])
        approve_and_publish(self.reg, p1["id"], "2026-11-01")
        p2 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-T002"], ["SH-T001"])
        self.reg.review_proposal(p2["id"], BUSINESS_REVIEW,
                                 REVIEW_APPROVE, "b")
        self.reg.review_proposal(p2["id"], FINANCE_REVIEW,
                                 REVIEW_APPROVE, "f")
        with self.assertRaises(ConflictError):
            self.reg.publish_proposal(p2["id"], "2026-12-01")

    def test_conflicting_batch_aborts_atomically(self):
        # 正常发布一条 11-01 生效的映射并激活
        p1 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-T001"], ["SH-T001"])
        pub1 = approve_and_publish(self.reg, p1["id"], "2026-11-01")
        self.reg.activate_due(date(2026, 11, 1))
        # 构造一条“旧系统导入”的非法待生效版本：不同分组、12-01 生效，
        # 但省级编码与仍生效的 pub1 冲突（正常 API 不可能产生此状态）
        state = self.reg.snapshot()
        bogus_id = "map_corrupt_import"
        state["mappings"][bogus_id] = {
            "id": bogus_id, "province": "SH", "direction": ONE_TO_ONE,
            "versions": [{
                "version": 1, "status": PENDING, "direction": ONE_TO_ONE,
                "members": [{"side": "NATIONAL", "code": "N-T002"},
                            {"side": "PROVINCE", "code": "SH-T001"}],
                "weights": None, "effective_from": "2026-12-01",
                "effective_until": None, "proposal_id": None,
                "supersedes_version": None, "published_by": "legacy-import",
                "published_at": None, "activated_at": None,
            }],
        }
        self.reg.restore(state)
        with self.assertRaises(ConflictError) as caught:
            self.reg.activate_due(date(2026, 12, 1))
        self.assertIn("SH-T001", str(caught.exception))
        # 整批中止：两个版本状态均不变
        bogus = self.reg.get_group(bogus_id)
        self.assertEqual(bogus["versions"][-1]["status"], PENDING)
        good = self.reg.get_group(pub1["group_id"])
        self.assertEqual(good["versions"][-1]["status"], ACTIVE)
        # 一致性检查必须报告错误
        self.assertFalse(self.reg.consistency_check(date(2026, 12, 1))["ok"])
        # 整改：撤回非法版本，发布不重叠的合法版本，跑批成功
        self.reg.withdraw_version(bogus_id, 1, "publisher-李",
                                  "编码占用，整改")
        p3 = make_proposal(self.reg, "SH", ONE_TO_ONE,
                           ["N-T002"], ["SH-T002"])
        approve_and_publish(self.reg, p3["id"], "2026-12-01")
        r = self.reg.activate_due(date(2026, 12, 1))
        self.assertEqual(len(r["activated"]), 1)
        self.assertTrue(self.reg.consistency_check(date(2026, 12, 1))["ok"])


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.reg = seeded()
        self.eng = SettlementEngine(self.reg)

    def test_one_to_many_apportions_national_price(self):
        # 浙江把全身麻醉拆成诱导/维持，国家价 800 按 0.4/0.6 分摊
        req = {"bill_id": "B1", "service_date": "2026-05-01",
               "provider_province": "ZJ", "insured_province": "ZJ",
               "lines": [
                   {"province_code": "ZJ-MZ001", "quantity": 1,
                    "amount": "300.00"},
                   {"province_code": "ZJ-MZ002", "quantity": 1,
                    "amount": "560.00"}]}
        result = self.eng.settle(req)
        self.assertEqual(result["status"], SETTLED)
        expected = [l["national_expected_amount"] for l in result["lines"]]
        self.assertEqual(expected, ["320.00", "480.00"])
        # 每行都能追溯到国家项目版本与同一个映射分组版本
        groups = {r["group_id"] for l in result["lines"] for r in l["resolution"]}
        self.assertEqual(len(groups), 1)
        for line in result["lines"]:
            r = line["resolution"][0]
            self.assertEqual(r["national_code"], "N-MZ001")
            self.assertTrue(r["mapping_version"] >= 1)
            self.assertEqual(r["mapping_status"], ACTIVE)

    def test_many_to_one_bundle_and_cross_province_limits(self):
        # 上海全麻捆绑行，参保地浙江：按浙江拆细映射汇总支付限额
        req = {"bill_id": "B2", "service_date": "2026-03-05",
               "provider_province": "SH", "insured_province": "ZJ",
               "lines": [{"province_code": "SH-MZ001", "quantity": 1,
                          "amount": "980.00"}]}
        result = self.eng.settle(req)
        line = result["lines"][0]
        self.assertEqual(len(line["resolution"]), 2)  # N-MZ001 + N-MZ002
        self.assertEqual(line["national_expected_amount"], "1000.00")
        # 290*0.4 + 520*0.6 + 插管180 = 608
        self.assertEqual(line["insured_limit_amount"], "608.00")
        self.assertEqual(line["payable_reference_amount"], "608.00")
        # 跨省两个目录版本都在结果里
        self.assertEqual(result["provider_catalog_id"], "PROV-SH")
        self.assertEqual(result["insured_catalog_id"], "PROV-ZJ")
        self.assertTrue(result["national_catalog_version"])
        # 参保地匹配明细携带省项目版本
        matches = [m for r in line["resolution"] for m in r["insured_matches"]]
        self.assertTrue(matches)
        self.assertTrue(all(m["insured_item_version"] for m in matches))

    def test_unresolved_goes_to_queue_without_guessing(self):
        req = {"bill_id": "B3", "service_date": "2026-07-02",
               "provider_province": "ZJ", "insured_province": "ZJ",
               "lines": [
                   {"province_code": "ZJ-KF002", "quantity": 1,
                    "amount": "48.00"},
                   {"province_code": "ZJ-HL003", "quantity": 1,
                    "amount": "45.00"}]}
        result = self.eng.settle(req)
        self.assertEqual(result["status"], MANUAL)
        self.assertTrue(result["queue_id"])
        reasons = {u["code"]: u["reason"] for u in result["unresolved"]}
        self.assertEqual(reasons["ZJ-HL003"], "MAPPING_UNRESOLVED")
        queue = self.eng.list_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["id"], result["queue_id"])

    def test_pending_version_cannot_settle_before_effective_date(self):
        p = make_proposal(self.reg, "ZJ", ONE_TO_ONE,
                          ["N-HL002"], ["ZJ-HL003"])
        approve_and_publish(self.reg, p["id"], "2026-10-01")
        req = {"bill_id": "B4", "service_date": "2026-09-30",
               "provider_province": "ZJ", "insured_province": "ZJ",
               "lines": [{"province_code": "ZJ-HL003", "quantity": 1,
                          "amount": "45.00"}]}
        result = self.eng.settle(req)
        self.assertEqual(result["status"], MANUAL)
        # 队列提示中应能看到待生效候选，但没有被使用
        hint = result["unresolved"][0]["pending_candidates"]
        self.assertEqual(len(hint), 1)
        self.assertEqual(hint[0]["effective_from"], "2026-10-01")

    def test_historical_bill_before_baseline_is_unresolved(self):
        # 历史费用按就医发生时点解释：2025 年基线尚未生效
        req = {"bill_id": "B5", "service_date": "2025-12-20",
               "provider_province": "ZJ", "insured_province": "ZJ",
               "lines": [{"province_code": "ZJ-FC001", "quantity": 1,
                          "amount": "1780.00"}]}
        result = self.eng.settle(req)
        self.assertEqual(result["status"], MANUAL)
        self.assertEqual(result["unresolved"][0]["reason"],
                         "MAPPING_UNRESOLVED")

    def test_result_query_returns_every_version(self):
        req = {"bill_id": "B6", "service_date": "2026-05-09",
               "provider_province": "ZJ", "insured_province": "SH",
               "lines": [{"province_code": "ZJ-YK001", "quantity": 1,
                          "amount": "2300.00"}]}
        result = self.eng.settle(req)
        fetched = self.eng.get_settlement(result["id"])
        self.assertEqual(fetched["id"], result["id"])
        self.assertEqual(fetched["engine_version"], result["engine_version"])
        resolution = fetched["lines"][0]["resolution"][0]
        for field in ("national_item_version", "national_catalog_version",
                      "provider_item_version", "provider_catalog_version",
                      "group_id", "mapping_version",
                      "mapping_effective_from"):
            self.assertIn(field, resolution if field.startswith(("national",
                          "group", "mapping")) else fetched["lines"][0])
        self.assertEqual(fetched["national_catalog_id"], "NATIONAL")


class ReversalTest(unittest.TestCase):
    def test_reversal_appends_and_keeps_original_immutable(self):
        reg = seeded()
        eng = SettlementEngine(reg)
        req = {"bill_id": "R1", "service_date": "2026-04-18",
               "provider_province": "ZJ", "insured_province": "ZJ",
               "lines": [{"province_code": "ZJ-FC002", "quantity": 1,
                          "amount": "3100.00"}]}
        original = eng.settle(req)
        rev = eng.reverse(original["id"], "金额录入错误，冲正重算",
                          "cashier-钱",
                          [{"line_no": 1, "billed_amount": "3000.00"}])
        self.assertEqual(rev["deltas"]["billed_amount"], "-100.00")
        # 原记录不可变，冲正挂在查询结果上
        fetched = eng.get_settlement(original["id"])
        self.assertEqual(fetched["totals"]["billed_amount"], "3100.00")
        self.assertEqual(len(fetched["reversals"]), 1)
        self.assertEqual(fetched["reversals"][0]["id"], rev["id"])
        self.assertEqual(
            rev["corrected_result"]["totals"]["billed_amount"], "3000.00")
        # 已生效的映射不能直接改，只能冲正：尝试直接改库结构应不被允许
        with self.assertRaises(Exception):
            reg.withdraw_version(
                rev["corrected_result"]["lines"][0]["resolution"][0]["group_id"],
                1, "x", "生效版本不允许撤回")


class ShadowReplayTest(unittest.TestCase):
    def test_replay_reports_coverage_and_difference_without_persistence(self):
        reg = seeded()
        before = len(reg.snapshot()["settlements"])
        report = shadow.replay(reg)
        self.assertEqual(report["mode"], "SHADOW")
        self.assertEqual(report["bills_total"], 8)
        self.assertEqual(
            report["lines_resolved"] + sum(report["unresolved_reasons"].values()),
            report["lines_total"])
        self.assertTrue(0 < report["line_coverage_rate"] < 1)
        # 差异金额闭合：申报 - 国家参照 = 差异
        from decimal import Decimal
        amounts = report["amounts"]
        self.assertEqual(
            Decimal(amounts["billed"]) - Decimal(amounts["national_expected"]),
            Decimal(amounts["difference_billed_vs_expected"]))
        # 未决账单覆盖：特色项目 / 造口护理 / 基线前历史账单
        unresolved_bills = {b["bill_id"] for b in report["bills"]
                            if b["status"] == MANUAL}
        self.assertEqual(unresolved_bills,
                         {"BILL-2026-0005", "BILL-2026-0006",
                          "BILL-2025-0008"})
        # 每张单都带目录与映射版本
        for bill in report["bills"]:
            self.assertTrue(bill["national_catalog_version"])
        # 影子模式不产生正式结算、不产生人工队列
        self.assertEqual(len(reg.snapshot()["settlements"]), before)
        self.assertEqual(len(reg.snapshot()["queue"]), 0)
        # 文本报告可生成
        text = shadow.format_text(report)
        self.assertIn("影子回放报告", text)

    def test_shadow_settlement_cannot_be_reversed(self):
        reg = seeded()
        eng = SettlementEngine(reg)
        import shadow as shadow_mod
        bills = shadow_mod.load_bills()
        result = eng.settle(bills[0], mode="SHADOW")
        # 影子结算不写入正式库，因此既查不到也不能冲正
        from catalog import NotFound
        with self.assertRaises(NotFound):
            eng.get_settlement(result["id"])
        with self.assertRaises(NotFound):
            eng.reverse(result["id"], "x", "a", [])


class ConcurrencyConsistencyTest(unittest.TestCase):
    def test_concurrent_publish_activate_settle_keeps_consistency(self):
        reg = seeded()
        add_test_items(reg)
        eng = SettlementEngine(reg)
        errors = []

        # 预置两条不同日期的待生效版本
        p1 = make_proposal(reg, "SH", ONE_TO_ONE, ["N-T001"], ["SH-T001"])
        approve_and_publish(reg, p1["id"], "2026-10-01")
        p2 = make_proposal(reg, "SH", ONE_TO_ONE, ["N-T002"], ["SH-T002"])
        approve_and_publish(reg, p2["id"], "2026-11-01")

        barrier = threading.Barrier(4)

        def runner(kind):
            try:
                barrier.wait()
                for _ in range(15):
                    if kind == "activate":
                        for d in (date(2026, 10, 1), date(2026, 11, 1),
                                  date(2026, 12, 1)):
                            try:
                                reg.activate_due(d, actor="batch")
                            except ConflictError:
                                pass
                    elif kind == "settle":
                        for d, code in ((date(2026, 9, 30), "SH-NK001"),
                                        (date(2026, 10, 5), "SH-T001"),
                                        (date(2026, 11, 5), "SH-T002")):
                            result = eng.settle({
                                "bill_id": f"C-{kind}-{threading.get_ident()}-{d}",
                                "service_date": d,
                                "provider_province": "SH",
                                "insured_province": "SH",
                                "lines": [{"province_code": code, "quantity": 1,
                                           "amount": "10.00"}]})
                            # 已决单据绝不允许出现多映射歧义
                            for u in result["unresolved"]:
                                self.assertNotEqual(u["reason"],
                                                    "AMBIGUOUS_MAPPING")
                    elif kind == "consistency":
                        reg.consistency_check(date(2026, 12, 1))
            except Exception as exc:  # 并发期间只允许 DomainError 被正常处理
                errors.append(exc)

        threads = [threading.Thread(target=runner, args=(k,))
                   for k in ("activate", "settle", "settle", "consistency")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        report = reg.consistency_check(date(2026, 12, 1))
        self.assertTrue(report["ok"], report["issues"])


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Application(data_path=None)
        cls.app.ensure_seeded()
        Handler.app = cls.app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _request(self, method, path, body=None, actor="tester",
                 expect_status=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-Actor": actor})
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                payload = json.load(response)
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            status = exc.code
        if expect_status:
            self.assertEqual(status, expect_status, payload)
        return status, payload

    def test_health_and_catalog_listing(self):
        _, payload = self._request("GET", "/health")
        self.assertEqual(payload["status"], "ok")
        _, catalogs = self._request("GET", "/catalogs")
        scopes = {c["scope"] for c in catalogs}
        self.assertEqual(scopes, {"NATIONAL", "PROVINCE"})

    def test_full_governance_and_settlement_flow_over_http(self):
        members = [{"side": "NATIONAL", "code": "N-HL001"},
                   {"side": "PROVINCE", "code": "SH-HL002"}]
        evidence = [{"type": "DOCUMENT", "reference": "沪医保[2026]12号",
                     "summary": "一级护理项目改挂论证"}]
        # 第一次提交：409，返回冲突清单
        status, err = self._request("POST", "/proposals",
                                    {"province": "SH", "direction": ONE_TO_ONE,
                                     "members": members, "evidence": evidence},
                                    expect_status=409)
        acks = [{"key": c["key"], "note": "逐条确认接续"}
                for c in err["details"]["conflicts"]]
        status, proposal = self._request(
            "POST", "/proposals",
            {"province": "SH", "direction": ONE_TO_ONE, "members": members,
             "evidence": evidence, "conflict_acknowledgements": acks},
            expect_status=201)
        pid = proposal["id"]
        # 财务先审 409
        self._request("POST", f"/proposals/{pid}/reviews",
                      {"kind": FINANCE_REVIEW, "decision": REVIEW_APPROVE},
                      expect_status=409)
        self._request("POST", f"/proposals/{pid}/reviews",
                      {"kind": BUSINESS_REVIEW, "decision": REVIEW_APPROVE})
        self._request("POST", f"/proposals/{pid}/reviews",
                      {"kind": FINANCE_REVIEW, "decision": REVIEW_APPROVE,
                       "comment": "限额测算通过"})
        _, pub = self._request("POST", f"/proposals/{pid}/publish",
                               {"effective_from": "2026-10-01"})
        gid, ver = pub["group_id"], pub["version"]
        self.assertEqual(pub["status"], PENDING)
        # 生效前结算仍是旧映射
        bill = {"bill_id": "HTTP-1", "service_date": "2026-09-30",
                "provider_province": "SH", "insured_province": "SH",
                "lines": [{"province_code": "SH-HL002", "quantity": 1,
                           "amount": "14.00"}]}
        _, before = self._request("POST", "/settlements", bill, expect_status=201)
        self.assertEqual(before["lines"][0]["resolution"][0]["national_code"],
                         "N-HL002")
        # 撤回再发布新版本，跑批激活
        self._request("POST",
                      f"/mappings/{gid}/versions/{ver}/withdraw",
                      {"reason": "材料补录中"})
        status, proposal2 = self._request(
            "POST", "/proposals",
            {"province": "SH", "direction": ONE_TO_ONE, "members": members,
             "evidence": evidence, "conflict_acknowledgements": acks},
            expect_status=201)
        self._request("POST", f"/proposals/{proposal2['id']}/reviews",
                      {"kind": BUSINESS_REVIEW, "decision": REVIEW_APPROVE})
        self._request("POST", f"/proposals/{proposal2['id']}/reviews",
                      {"kind": FINANCE_REVIEW, "decision": REVIEW_APPROVE})
        self._request("POST", f"/proposals/{proposal2['id']}/publish",
                      {"effective_from": "2026-10-01"})
        self._request("POST", "/admin/activate", {"as_of": "2026-10-01"})
        # 生效后结算走新映射
        bill["bill_id"] = "HTTP-2"
        bill["service_date"] = "2026-10-01"
        _, after = self._request("POST", "/settlements", bill, expect_status=201)
        resolution = after["lines"][0]["resolution"][0]
        self.assertEqual(resolution["national_code"], "N-HL001")
        self.assertEqual(resolution["mapping_version"], 3)
        # 顶层返回结算时的目录当前版本，行内返回项目入库时的目录版本快照
        self.assertGreaterEqual(after["national_catalog_version"],
                                resolution["national_catalog_version"])
        self.assertTrue(resolution["national_item_version"])
        # 查询接口必须返回具体目录与映射版本，而非只有金额
        _, fetched = self._request("GET", f"/settlements/{after['id']}")
        self.assertEqual(fetched["national_catalog_id"], "NATIONAL")
        self.assertTrue(fetched["provider_catalog_version"])
        self.assertEqual(fetched["lines"][0]["resolution"][0]["group_id"], gid)
        # 冲正
        _, rev = self._request(
            "POST", f"/settlements/{after['id']}/reversals",
            {"reason": "录入错误", "corrected_lines": [
                {"line_no": 1, "billed_amount": "15.00"}]}, expect_status=201)
        self.assertEqual(rev["deltas"]["billed_amount"], "1.00")
        # 影子回放与一致性、审计
        _, report = self._request("GET", "/shadow/replay")
        self.assertIn("line_coverage_rate", report)
        _, consistency = self._request(
            "GET", "/admin/consistency?as_of=2026-10-01")
        self.assertTrue(consistency["ok"])
        _, audit = self._request("GET", "/audit?limit=20")
        self.assertTrue(any(a["action"] == "MAPPINGS_ACTIVATED" for a in audit))

    def test_unknown_route_and_validation_errors(self):
        self._request("GET", "/unknown", expect_status=404)
        self._request("POST", "/settlements", {"bill_id": "X"},
                      expect_status=400)


if __name__ == "__main__":
    unittest.main()
