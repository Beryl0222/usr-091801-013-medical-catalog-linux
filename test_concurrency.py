"""并发一致性测试：跑批与治理操作同时进行时不得出现撕裂状态。"""

import threading
import unittest
from datetime import date

from catalog_domain import CatalogGovernanceService


def build_service():
    svc = CatalogGovernanceService()
    svc.create_catalog_version(
        level="national", region=None, version="N25",
        effective_from="2025-01-01", effective_to="2025-12-31",
        projects=[{"code": "N1", "name": "心电", "category": "心血管",
                   "unit": "次", "price": "30.00"}],
    )
    svc.publish_catalog_version("NATIONAL", "N25")
    svc.create_catalog_version(
        level="provincial", region="HB", version="P25",
        effective_from="2025-01-01", effective_to=None,
        projects=[{"code": "P1", "name": "省心电", "category": "心血管",
                   "unit": "次", "price": "28.00"}],
    )
    svc.publish_catalog_version("PROV-HB", "P25")
    p = svc.submit_proposal({
        "proposer": "专家", "national_version": "N25", "region": "HB",
        "intended_from": "2025-01-01", "intended_to": None,
        "evidence": ["文号"],
        "groups": [{"links": [{"national_code": "N1", "provincial_code": "P1"}]}],
    })
    svc.review_proposal(p["proposal_id"], kind="business", decision="approved",
                        reviewer="业务")
    svc.review_proposal(p["proposal_id"], kind="finance", decision="approved",
                        reviewer="财务")
    svc.publish_mapping(p["proposal_id"], version="v1",
                        effective_from="2025-01-01", effective_to=None)
    return svc, p["proposal_id"]


class ConcurrencyTest(unittest.TestCase):
    def test_batches_and_settlements_never_lose_or_duplicate_claims(self):
        svc, _ = build_service()
        errors: list[Exception] = []

        def batch(worker_id: int):
            try:
                for b in range(20):
                    reqs = [
                        {
                            "claim_id": f"W{worker_id}-B{b}-{i}",
                            "insured_region": "HB", "care_region": "HB",
                            "service_date": "2025-06-01",
                            "lines": [{"provincial_code": "P1",
                                       "charged_amount": "33.00"}],
                        }
                        for i in range(5)
                    ]
                    svc.run_batch(reqs, label=f"w{worker_id}-b{b}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def singles(worker_id: int):
            try:
                for i in range(100):
                    svc.settle({
                        "claim_id": f"S{worker_id}-{i}",
                        "insured_region": "HB", "care_region": "HB",
                        "service_date": "2025-06-01",
                        "lines": [{"provincial_code": "P1",
                                   "charged_amount": "33.00"}],
                    })
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=batch, args=(w,)) for w in range(4)]
        threads += [threading.Thread(target=singles, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        settlements = svc.list_settlements()
        # 4 worker * 20 批 * 5 单 + 4 worker * 100 单
        self.assertEqual(len(settlements), 4 * 20 * 5 + 4 * 100)
        claim_ids = [s["claim_id"] for s in settlements]
        self.assertEqual(len(claim_ids), len(set(claim_ids)))  # 无重复
        self.assertTrue(all(s["status"] == "settled" for s in settlements))
        # 每单都可按 ID 取回且带版本
        for cid in claim_ids[:50]:
            detail = svc.get_settlement(cid)
            self.assertEqual(
                detail["lines"][0]["versions"]["national_catalog"], "NATIONAL@N25")
            self.assertEqual(detail["lines"][0]["versions"]["mapping"], "MAP-HB-v1")

    def test_batch_remains_atomic_while_concurrent_reads_happen(self):
        svc, _ = build_service()
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def failing_batch():
            try:
                barrier.wait()
                reqs = [
                    {"claim_id": f"FB-{i}", "insured_region": "HB",
                     "care_region": "HB", "service_date": "2025-06-01",
                     "lines": [{"provincial_code": "P1", "charged_amount": "1.00"}]}
                    for i in range(100)
                ]
                reqs.append({"insured_region": "HB"})  # 必然失败
                with self.assertRaises(Exception):
                    svc.run_batch(reqs, label="失败批")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader(worker_id: int):
            try:
                barrier.wait()
                for _ in range(200):
                    svc.list_settlements()
                    svc.list_queue()
                    svc.resolve_line(care_region="HB", service_date=date(2025, 6, 1),
                                     provincial_code="P1")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=failing_batch)]
        threads += [threading.Thread(target=reader, args=(w,)) for w in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        # 失败批次没有留下任何 FB-* 单据
        self.assertEqual(
            [s for s in svc.list_settlements() if s["claim_id"].startswith("FB-")],
            [])

    def test_withdraw_competing_future_versions_only_one_wins(self):
        svc, _ = build_service()
        # 两版互不重叠的待生效目录，都允许发布；随后并发撤回。
        ids = []
        windows = (("2030-01-01", "2030-12-31"), ("2031-01-01", None))
        for i, (frm, to) in enumerate(windows):
            svc.create_catalog_version(
                level="provincial", region="TJ", version=f"TJ{i}",
                effective_from=frm, effective_to=to,
                projects=[{"code": f"T{i}", "name": "t", "category": "诊断",
                           "unit": "次", "price": "1.00"}])
            svc.publish_catalog_version("PROV-TJ", f"TJ{i}")
            ids.append(f"TJ{i}")

        results: list[str] = []

        def withdraw(version: str):
            try:
                svc.withdraw_catalog_version("PROV-TJ", version)
                results.append("ok")
            except Exception:  # noqa: BLE001
                results.append("conflict")

        threads = [threading.Thread(target=withdraw, args=(v,)) for v in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sorted(results), ["ok", "ok"])
        for v in ids:
            self.assertEqual(
                svc.get_catalog_version("PROV-TJ", v)["status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
