"""目录治理领域层测试。"""

import unittest
from datetime import date
from decimal import Decimal

from catalog_domain import (
    CatalogGovernanceService,
    DomainError,
    MANY_TO_ONE,
    NATIONAL_CATALOG,
    ONE_TO_MANY,
    ONE_TO_ONE,
    REASON_NO_MAPPING_VERSION,
    REASON_UNMAPPED_CODE,
    SERVICE_CATEGORIES,
    StateConflictError,
)


def project(code, name="项目", category="心血管", price="100.00", **kw):
    return {"code": code, "name": name, "category": category, "unit": "次",
            "price": price, **kw}


class Fixture:
    """构造国家两版 + 湖北两版目录的最小夹具。"""

    def __init__(self):
        self.svc = CatalogGovernanceService()
        self.svc.create_catalog_version(
            level="national", region=None, version="N25",
            effective_from="2025-01-01", effective_to="2025-12-31",
            projects=[project("N1", "心电", price="30"), project("N2", "CT", price="180")],
        )
        self.svc.publish_catalog_version(NATIONAL_CATALOG, "N25")
        self.svc.create_catalog_version(
            level="national", region=None, version="N26",
            effective_from="2026-01-01", effective_to=None,
            projects=[project("N1", "心电", price="35"),
                      project("N2A", "肺容量", category="呼吸", price="45"),
                      project("N2B", "肺通气", category="呼吸", price="50")],
        )
        self.svc.publish_catalog_version(NATIONAL_CATALOG, "N26")
        self.svc.create_catalog_version(
            level="provincial", region="HB", version="P25",
            effective_from="2025-01-01", effective_to="2025-12-31",
            projects=[project("P1", "省心电", price="28"),
                      project("P2", "省CT", price="175"),
                      project("PX", "省增补", category="中医", price="50")],
        )
        self.svc.publish_catalog_version("PROV-HB", "P25")
        self.svc.create_catalog_version(
            level="provincial", region="HB", version="P26",
            effective_from="2026-01-01", effective_to=None,
            projects=[project("P1", "省心电", price="35"),
                      project("P3", "省肺打包", category="呼吸", price="95"),
                      project("PX", "省增补", category="中医", price="50")],
        )
        self.svc.publish_catalog_version("PROV-HB", "P26")

    def submit_approve_publish(self, groups, *, national="N25", frm="2025-01-01",
                               to="2025-12-31", version="v1", evidence=("依据文件",)):
        p = self.svc.submit_proposal({
            "proposer": "专家组", "national_version": national, "region": "HB",
            "intended_from": frm, "intended_to": to,
            "evidence": list(evidence), "groups": groups,
        })
        self.svc.review_proposal(p["proposal_id"], kind="business", decision="approved",
                                 reviewer="业务")
        self.svc.review_proposal(p["proposal_id"], kind="finance", decision="approved",
                                 reviewer="财务")
        return self.svc.publish_mapping(p["proposal_id"], version=version,
                                        effective_from=frm, effective_to=to)

    @staticmethod
    def link(pairs, note=""):
        return {"links": [{"national_code": n, "provincial_code": p} for n, p in pairs],
                "note": note}


class CatalogVersionTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def test_thirteen_categories_are_seed_scope(self):
        self.assertEqual(len(SERVICE_CATEGORIES), 13)

    def test_unknown_category_rejected(self):
        with self.assertRaises(DomainError):
            self.fx.svc.create_catalog_version(
                level="national", region=None, version="N99",
                effective_from="2099-01-01", effective_to=None,
                projects=[project("X", category="不存在的类别")],
            )

    def test_overlapping_published_versions_rejected(self):
        self.fx.svc.create_catalog_version(
            level="national", region=None, version="N27",
            effective_from="2027-01-01", effective_to=None,
            projects=[project("N1", price="40")])
        # N26 自 2026-01-01 起长期有效，N27 与之重叠
        with self.assertRaises(StateConflictError):
            self.fx.svc.publish_catalog_version(NATIONAL_CATALOG, "N27")

    def test_adjacent_year_versions_coexist(self):
        # 夹具中 P25 止于 2025-12-31、P26 始于 2026-01-01，相邻不重叠。
        r25 = self.fx.svc.resolve_line(care_region="HB",
                                       service_date=date(2025, 12, 31),
                                       provincial_code="P1")
        r26 = self.fx.svc.resolve_line(care_region="HB",
                                       service_date=date(2026, 1, 1),
                                       provincial_code="P1")
        self.assertEqual(r25["versions"]["provincial_catalog"], "PROV-HB@P25")
        self.assertEqual(r26["versions"]["provincial_catalog"], "PROV-HB@P26")

    def test_draft_version_never_participates_in_resolution(self):
        self.fx.svc.create_catalog_version(
            level="provincial", region="BJ", version="BJ-DRAFT",
            effective_from="2025-01-01", effective_to=None,
            projects=[project("B1")])
        result = self.fx.svc.resolve_line(
            care_region="BJ", service_date=date(2025, 6, 1), provincial_code="B1")
        self.assertEqual(result["status"], "unresolved")

    def test_only_not_yet_effective_version_can_be_withdrawn(self):
        self.fx.svc.create_catalog_version(
            level="provincial", region="SH", version="SH27",
            effective_from="2027-01-01", effective_to=None,
            projects=[project("S1")])
        self.fx.svc.publish_catalog_version("PROV-SH", "SH27")
        withdrawn = self.fx.svc.withdraw_catalog_version("PROV-SH", "SH27")
        self.assertEqual(withdrawn["status"], "withdrawn")
        # 已生效的不能撤回
        with self.assertRaises(StateConflictError):
            self.fx.svc.withdraw_catalog_version("PROV-HB", "P25")


class ProposalReviewTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def _proposal(self, **over):
        payload = {
            "proposer": "专家", "national_version": "N25", "region": "HB",
            "intended_from": "2025-01-01", "intended_to": "2025-12-31",
            "evidence": ["国家文件"],
            "groups": [self.fx.link([("N1", "P1")])],
        }
        payload.update(over)
        return self.fx.svc.submit_proposal(payload)

    def test_evidence_is_mandatory(self):
        with self.assertRaises(DomainError):
            self._proposal(evidence=[])
        with self.assertRaises(DomainError):
            self._proposal(evidence=["  "])

    def test_many_to_many_must_be_split(self):
        with self.assertRaises(DomainError):
            self._proposal(groups=[self.fx.link([("N1", "P1"), ("N2", "P2")])])

    def test_codes_must_exist_in_both_catalogs(self):
        with self.assertRaises(DomainError):
            self._proposal(groups=[self.fx.link([("NOPE", "P1")])])
        with self.assertRaises(DomainError):
            self._proposal(groups=[self.fx.link([("N1", "NOPE")])])

    def test_provincial_code_cannot_appear_in_two_groups(self):
        with self.assertRaises(DomainError):
            self._proposal(groups=[
                self.fx.link([("N1", "P1")]),
                self.fx.link([("N2", "P1")]),
            ])

    def test_cardinality_conflicts_are_listed_not_blocking(self):
        p = self._proposal(
            national_version="N26",
            intended_from="2026-01-01", intended_to="2026-12-31",
            groups=[self.fx.link([("N2A", "P3"), ("N2B", "P3")], "打包")],
        )
        self.assertTrue(any("多对一" in c for c in p["conflicts"]))
        self.assertEqual(p["status"], "submitted")

    def test_both_reviews_required_to_publish(self):
        p = self._proposal()
        self.fx.svc.review_proposal(p["proposal_id"], kind="business",
                                    decision="approved", reviewer="业务")
        with self.assertRaises(StateConflictError):
            self.fx.svc.publish_mapping(p["proposal_id"], version="x",
                                        effective_from="2025-01-01")
        self.fx.svc.review_proposal(p["proposal_id"], kind="finance",
                                    decision="approved", reviewer="财务")
        mv = self.fx.svc.publish_mapping(p["proposal_id"], version="x",
                                         effective_from="2025-01-01")
        self.assertEqual(mv["status"], "published")

    def test_either_rejection_blocks_publication(self):
        p = self._proposal()
        self.fx.svc.review_proposal(p["proposal_id"], kind="business",
                                    decision="approved", reviewer="业务")
        self.fx.svc.review_proposal(p["proposal_id"], kind="finance",
                                    decision="rejected", reviewer="财务",
                                    comment="当量不符")
        self.assertEqual(self.fx.svc.get_proposal(p["proposal_id"])["status"], "rejected")
        with self.assertRaises(StateConflictError):
            self.fx.svc.publish_mapping(p["proposal_id"], version="y",
                                        effective_from="2025-01-01")

    def test_same_code_overlapping_mapping_rejected_across_proposals(self):
        self.fx.submit_approve_publish([self.fx.link([("N1", "P1")])], version="a")
        with self.assertRaises(StateConflictError):
            self.fx.submit_approve_publish([self.fx.link([("N1", "P1")])], version="b")

    def test_cross_national_version_transition_is_allowed_for_changeover(self):
        # 2025 版映射 P1->N1；2026 新版国家换版后，P1 可重新映射，区间允许相邻年度。
        self.fx.submit_approve_publish([self.fx.link([("N1", "P1")])], version="a")
        self.fx.submit_approve_publish(
            [self.fx.link([("N1", "P1")])],
            national="N26", frm="2026-01-01", to=None, version="b")
        r25 = self.fx.svc.resolve_line(care_region="HB", service_date=date(2025, 12, 31),
                                      provincial_code="P1")
        r26 = self.fx.svc.resolve_line(care_region="HB", service_date=date(2026, 1, 1),
                                      provincial_code="P1")
        self.assertEqual(r25["versions"]["mapping"], "MAP-HB-a")
        self.assertEqual(r26["versions"]["mapping"], "MAP-HB-b")
        self.assertEqual(r25["national_projects"][0]["price"], "30.00")
        self.assertEqual(r26["national_projects"][0]["price"], "35.00")


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        # 2025：1:1 与 1:N（P1/P2 都挂 N1 演示一对多）
        self.fx.submit_approve_publish([
            self.fx.link([("N1", "P1"), ("N1", "P2")]),
        ], version="25")
        # 2026：N:1 打包 P3 -> N2A+N2B；P1 未映射（缺口）
        self.fx.submit_approve_publish(
            [self.fx.link([("N2A", "P3"), ("N2B", "P3")], "省打包")],
            national="N26", frm="2026-01-01", to=None, version="26")

    def _settle(self, day, code, amount="100.00", qty=1, claim="C1"):
        return self.fx.svc.settle({
            "claim_id": claim, "insured_region": "BJ", "care_region": "HB",
            "service_date": day,
            "lines": [{"provincial_code": code, "charged_amount": amount,
                       "quantity": qty}],
        })

    def test_service_date_selects_unique_versions(self):
        old = self._settle("2025-06-01", "P1", "33.00", claim="OLD")
        new = self._settle("2026-06-01", "P3", "100.00", claim="NEW")
        line_old = old["lines"][0]
        self.assertEqual(line_old["versions"]["national_catalog"], "NATIONAL@N25")
        self.assertEqual(line_old["versions"]["provincial_catalog"], "PROV-HB@P25")
        self.assertEqual(line_old["versions"]["mapping"], "MAP-HB-25")
        line_new = new["lines"][0]
        self.assertEqual(line_new["versions"]["national_catalog"], "NATIONAL@N26")
        self.assertEqual(line_new["versions"]["provincial_catalog"], "PROV-HB@P26")
        self.assertEqual(line_new["versions"]["mapping"], "MAP-HB-26")

    def test_one_to_many_relation_is_flagged(self):
        r = self._settle("2025-06-01", "P2", claim="O2M")
        self.assertEqual(r["lines"][0]["relation"], ONE_TO_MANY)
        self.assertFalse(r["lines"][0]["bundle"])

    def test_many_to_one_bundle_baseline_sums_national_prices(self):
        r = self._settle("2026-06-01", "P3", "100.00", qty=2, claim="N21")
        line = r["lines"][0]
        self.assertEqual(line["relation"], MANY_TO_ONE)
        self.assertTrue(line["bundle"])
        self.assertEqual(line["baseline_amount"], "190.00")  # (45+50)*2
        self.assertEqual(line["difference_amount"], "-90.00")  # 可为负

    def test_unmapped_code_goes_to_queue_not_guessed(self):
        r = self._settle("2025-06-01", "PX", "50.00", claim="GAP")
        self.assertEqual(r["status"], "manual_review")
        self.assertEqual(r["lines"][0]["reason"], REASON_UNMAPPED_CODE)
        self.assertIsNone(r["total_difference_amount"])
        queue = self.fx.svc.list_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["provincial_code"], "PX")
        self.assertIsNotNone(queue[0]["versions"]["national_catalog"])

    def test_missing_mapping_version_goes_to_queue(self):
        # BJ 在 2026 没有任何映射版本
        self.fx.svc.create_catalog_version(
            level="provincial", region="BJ", version="BJ26",
            effective_from="2026-01-01", effective_to=None,
            projects=[project("B1")])
        self.fx.svc.publish_catalog_version("PROV-BJ", "BJ26")
        r = self.fx.svc.settle({
            "claim_id": "XPROV", "insured_region": "HB", "care_region": "BJ",
            "service_date": "2026-03-01",
            "lines": [{"provincial_code": "B1", "charged_amount": "10.00"}],
        })
        self.assertEqual(r["lines"][0]["reason"], REASON_NO_MAPPING_VERSION)

    def test_query_returns_versions_not_just_amount(self):
        self._settle("2025-06-01", "P1", "33.00", claim="TRACE")
        fetched = self.fx.svc.get_settlement("TRACE")
        self.assertEqual(fetched["version_summary"]["mapping_versions"], ["MAP-HB-25"])
        self.assertEqual(len(fetched["version_summary"]["national_catalog_versions"]), 1)
        self.assertEqual(fetched["lines"][0]["national_projects"][0]["code"], "N1")

    def test_line_level_service_date_overrides_header_for_cross_day_stay(self):
        r = self.fx.svc.settle({
            "claim_id": "CROSS", "insured_region": "BJ", "care_region": "HB",
            "service_date": "2025-12-31",
            "lines": [
                {"line_id": "a", "service_date": "2025-12-31", "provincial_code": "P1",
                 "charged_amount": "33.00"},
                {"line_id": "b", "service_date": "2026-01-01", "provincial_code": "P3",
                 "charged_amount": "100.00"},
            ],
        })
        self.assertEqual(r["lines"][0]["versions"]["national_catalog"], "NATIONAL@N25")
        self.assertEqual(r["lines"][1]["versions"]["national_catalog"], "NATIONAL@N26")
        self.assertEqual(r["status"], "settled")


class ShadowAndCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.fx.submit_approve_publish([self.fx.link([("N1", "P1")])], version="25")

    def test_shadow_does_not_write_settlements_or_queue(self):
        bills = [{
            "claim_id": "S1", "insured_region": "HB", "care_region": "HB",
            "service_date": "2025-06-01",
            "lines": [
                {"provincial_code": "P1", "charged_amount": "40.00"},
                {"provincial_code": "PX", "charged_amount": "50.00"},
            ],
        }]
        run = self.fx.svc.shadow_replay(bills, label="演练")
        self.assertEqual(run["mode"], "shadow")
        self.assertEqual(run["total_lines"], 2)
        self.assertEqual(run["covered_lines"], 1)
        self.assertEqual(run["coverage_rate"], 0.5)
        # 差异只统计覆盖行（40-30），缺口 50 单列
        self.assertEqual(run["total_difference_amount"], "10.00")
        self.assertEqual(run["gap_charged_amount"], "50.00")
        self.assertEqual(self.fx.svc.list_settlements(), [])
        self.assertEqual(self.fx.svc.list_queue(), [])
        fetched = self.fx.svc.get_shadow_run(run["run_id"])
        self.assertEqual(fetched["lines"][0]["versions"]["mapping"], "MAP-HB-25")

    def test_reversal_keeps_original_immutable_and_prevents_double_reversal(self):
        r = self.fx.svc.settle({
            "claim_id": "REV", "insured_region": "HB", "care_region": "HB",
            "service_date": "2025-06-01",
            "lines": [{"provincial_code": "P1", "charged_amount": "40.00"}],
        })
        original_baseline = r["total_baseline_amount"]
        cor = self.fx.svc.reverse_settlement("REV", reason="科室录错", operator="赵")
        self.assertEqual(cor["reversed_baseline_amount"], "-30.00")
        fetched = self.fx.svc.get_settlement("REV")
        self.assertEqual(fetched["total_baseline_amount"], original_baseline)
        self.assertEqual(fetched["reversal_status"], "reversed")
        self.assertEqual(fetched["corrections"][0]["correction_id"],
                         cor["correction_id"])
        with self.assertRaises(StateConflictError):
            self.fx.svc.reverse_settlement("REV", reason="再来一次", operator="赵")
        with self.assertRaises(DomainError):
            self.fx.svc.reverse_settlement("REV", reason="  ", operator="赵")

    def test_manual_adjudication_is_scoped_to_national_version(self):
        # PX 在两版目录中都没有正式映射
        r = self.fx.svc.settle({
            "claim_id": "MAN1", "insured_region": "HB", "care_region": "HB",
            "service_date": "2025-06-01",
            "lines": [{"provincial_code": "PX", "charged_amount": "50.00"}],
        })
        qid = r["lines"][0]["queue_id"]
        closed = self.fx.svc.resolve_queue_item(
            qid, decision="map", operator="会审组", note="暂按心电管理",
            national_codes=["N1"], effective_from="2025-01-01")
        manual_id = closed["resolution"]["manual_mapping_version_id"]
        again = self.fx.svc.settle({
            "claim_id": "MAN2", "insured_region": "HB", "care_region": "HB",
            "service_date": "2025-06-02",
            "lines": [{"provincial_code": "PX", "charged_amount": "50.00"}],
        })
        self.assertEqual(again["status"], "settled")
        self.assertEqual(again["lines"][0]["versions"]["mapping"], manual_id)
        self.assertEqual(again["lines"][0]["resolution_basis"],
                         "manual_adjudication")
        # 换国家版本后口径不自动延续
        future = self.fx.svc.settle({
            "claim_id": "MAN3", "insured_region": "HB", "care_region": "HB",
            "service_date": "2026-06-02",
            "lines": [{"provincial_code": "PX", "charged_amount": "50.00"}],
        })
        self.assertEqual(future["status"], "manual_review")
        # 已关闭队列不能重复处理
        with self.assertRaises(StateConflictError):
            self.fx.svc.resolve_queue_item(
                qid, decision="reject", operator="x", note="n")


class BatchAtomicityTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.fx.submit_approve_publish([self.fx.link([("N1", "P1")])], version="25")
        self.requests = [
            {"claim_id": f"B{i}", "insured_region": "HB", "care_region": "HB",
             "service_date": "2025-06-01",
             "lines": [{"provincial_code": "P1", "charged_amount": "40.00"}]}
            for i in range(5)
        ]

    def test_batch_succeeds_atomically(self):
        batch = self.fx.svc.run_batch(self.requests, label="跨日跑批")
        self.assertEqual(batch["total"], 5)
        self.assertEqual(batch["settled"], 5)
        self.assertEqual(len(self.fx.svc.list_settlements()), 5)

    def test_batch_failure_leaves_no_trace(self):
        bad = self.requests + [{"insured_region": "HB"}]  # 缺字段
        with self.assertRaises(DomainError):
            self.fx.svc.run_batch(bad, label="必失败批次")
        for i in range(5):
            with self.assertRaises(Exception):
                self.fx.svc.get_settlement(f"B{i}")
        self.assertEqual(self.fx.svc.list_settlements(), [])
        self.assertEqual(self.fx.svc.list_queue(), [])

    def test_fail_after_injection_rolls_back_whole_batch(self):
        with self.assertRaises(DomainError):
            self.fx.svc.run_batch(self.requests, label="注入失败", fail_after=3)
        self.assertEqual(self.fx.svc.list_settlements(), [])


if __name__ == "__main__":
    unittest.main()
