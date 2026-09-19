"""HTTP 接口契约测试：在真实端口上演练完整治理链路。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seed_data import build_seeded_service, HISTORICAL_BILLS
from service import Handler


class HttpFixture:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.governance_service = build_seeded_service()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = HttpFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fx.stop()

    @property
    def api(self):
        return self.fx

    def test_health(self):
        status, body = self.api.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "medical-catalog")

    def test_seed_catalogs_listed(self):
        status, body = self.api.call("GET", "/catalogs/versions?level=national")
        self.assertEqual(status, 200)
        versions = [v["version"] for v in body["versions"]]
        self.assertIn("NAT-2025", versions)
        self.assertIn("NAT-2026", versions)

    def test_shadow_replay_seed_bills_reports_coverage_and_difference(self):
        status, run = self.api.call("POST", "/shadow/replay",
                                    {"use_seed_bills": True, "label": "契约影子"})
        self.assertEqual(status, 200)
        self.assertEqual(run["total_lines"], len(HISTORICAL_BILLS) + 2)  # 两单多明细
        self.assertGreater(run["gap_lines"], 0)
        self.assertTrue(0 < run["coverage_rate"] < 1)
        # 每行都能追溯版本，覆盖行必须给映射版本
        covered = [l for l in run["lines"] if l["status"] == "covered"]
        self.assertTrue(covered)
        for line in covered:
            self.assertIsNotNone(line["versions"]["national_catalog"])
            self.assertIsNotNone(line["versions"]["mapping"])

    def test_settlement_traceability_and_queue_flow(self):
        # 2025 年正常 + 省增补未决，一张单混合两种结果
        status, claim = self.api.call("POST", "/settlements", {
            "claim_id": "API-SET-1", "insured_region": "HB", "care_region": "HB",
            "service_date": "2025-06-01",
            "lines": [
                {"line_id": "ok", "provincial_code": "HB0701", "charged_amount": "35.00"},
                {"line_id": "gap", "provincial_code": "HB0999", "charged_amount": "55.00"},
            ],
        })
        self.assertEqual(status, 200)
        self.assertEqual(claim["status"], "manual_review")
        ok_line = next(l for l in claim["lines"] if l["line_id"] == "ok")
        self.assertEqual(ok_line["versions"]["mapping"], "MAP-HB-2025-1")
        self.assertEqual(ok_line["national_projects"][0]["code"], "N7001")

        # 未决行进人工队列
        status, queue = self.api.call("GET", "/queue?status=open")
        item = next(i for i in queue["items"] if i["claim_id"] == "API-SET-1")
        self.assertEqual(item["reason"], "UNMAPPED_PROVINCIAL_CODE")

        # 人工给出 map 口径
        status, closed = self.api.call("POST", f"/queue/{item['queue_id']}/resolve", {
            "decision": "map", "operator": "会审组", "note": "暂按普通针刺管理",
            "national_codes": ["NB001"], "effective_from": "2025-01-01",
        })
        self.assertEqual(status, 200)
        manual_id = closed["resolution"]["manual_mapping_version_id"]

        # 重新提交后按人工版本结算，查询接口返回具体映射版本
        status, claim2 = self.api.call("POST", "/settlements", {
            "claim_id": "API-SET-2", "insured_region": "HB", "care_region": "HB",
            "service_date": "2025-06-01",
            "lines": [{"provincial_code": "HB0999", "charged_amount": "55.00"}],
        })
        self.assertEqual(claim2["status"], "settled")
        self.assertEqual(claim2["lines"][0]["versions"]["mapping"], manual_id)
        status, fetched = self.api.call("GET", "/settlements/API-SET-2")
        self.assertEqual(status, 200)
        self.assertIn(manual_id, fetched["version_summary"]["mapping_versions"])

    def test_proposal_requires_evidence_and_double_review(self):
        status, err = self.api.call("POST", "/proposals", {
            "region": "HB", "national_version": "NAT-2025",
            "intended_from": "2025-01-01",
            "groups": [{"links": [{"national_code": "N7001", "provincial_code": "HB0701"}]}],
        })
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "bad_request")

        status, proposal = self.api.call("POST", "/proposals", {
            "proposer": "接口测试专家", "region": "HB",
            "national_version": "NAT-2025",
            "title": "接口测试提案",
            "intended_from": "2025-01-01", "intended_to": "2025-12-31",
            "evidence": ["测试依据文号 001"],
            "groups": [{"links": [{"national_code": "NA001", "provincial_code": "HBA001"}]}],
        })
        self.assertEqual(status, 201)
        pid = proposal["proposal_id"]
        # 只过业务审核不能发布
        self.api.call("POST", f"/proposals/{pid}/reviews",
                      {"kind": "business", "decision": "approved", "reviewer": "业务"})
        status, err = self.api.call("POST", f"/proposals/{pid}/publish-mapping",
                                    {"version": "dup-1", "effective_from": "2025-06-01"})
        self.assertEqual(status, 409)
        # 发布区间与现有映射编码冲突（HBA001 已在 MAP-HB-2025-1 覆盖）
        self.api.call("POST", f"/proposals/{pid}/reviews",
                      {"kind": "finance", "decision": "approved", "reviewer": "财务"})
        status, err = self.api.call("POST", f"/proposals/{pid}/publish-mapping",
                                    {"version": "dup-1", "effective_from": "2025-06-01"})
        self.assertEqual(status, 409)

    def test_withdraw_pending_mapping_and_cannot_withdraw_active(self):
        status, body = self.api.call("POST", "/mappings/MAP-HB-2027-PENDING/withdraw", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "withdrawn")
        status, err = self.api.call("POST", "/mappings/MAP-HB-2025-1/withdraw", {})
        self.assertEqual(status, 409)

    def test_reversal_only_path_to_fix_settled_claim(self):
        self.api.call("POST", "/settlements", {
            "claim_id": "API-REV-1", "insured_region": "BJ", "care_region": "HB",
            "service_date": "2025-10-01",
            "lines": [{"provincial_code": "HB0201", "charged_amount": "200.00"}],
        })
        status, correction = self.api.call("POST", "/settlements/API-REV-1/reversal",
                                           {"reason": "重复申报", "operator": "经办赵"})
        self.assertEqual(status, 201)
        self.assertEqual(correction["reversed_baseline_amount"], "-180.00")
        _, claim = self.api.call("GET", "/settlements/API-REV-1")
        self.assertEqual(claim["total_baseline_amount"], "180.00")  # 原单不变
        self.assertEqual(claim["reversal_status"], "reversed")
        # 再次冲正被拒绝
        status, err = self.api.call("POST", "/settlements/API-REV-1/reversal",
                                    {"reason": "再次", "operator": "赵"})
        self.assertEqual(status, 409)

    def test_cross_day_batch_picks_versions_per_line_date(self):
        status, batch = self.api.call("POST", "/batches", {
            "label": "跨日",
            "requests": [
                {"claim_id": "API-B1", "insured_region": "HB", "care_region": "HB",
                 "service_date": "2025-12-31",
                 "lines": [{"provincial_code": "HB0301", "charged_amount": "15.00"}]},
                {"claim_id": "API-B2", "insured_region": "HB", "care_region": "HB",
                 "service_date": "2026-01-01",
                 "lines": [{"provincial_code": "HB0301", "charged_amount": "15.00"}]},
            ],
        })
        self.assertEqual(status, 200)
        self.assertEqual(batch["settled"], 2)
        results = {r["claim_id"]: r for r in batch["results"]}
        self.assertEqual(results["API-B1"]["lines"][0]["baseline_amount"], "12.00")
        self.assertEqual(results["API-B2"]["lines"][0]["baseline_amount"], "10.00")
        self.assertEqual(
            results["API-B1"]["lines"][0]["versions"]["national_catalog"],
            "NATIONAL@NAT-2025")
        self.assertEqual(
            results["API-B2"]["lines"][0]["versions"]["national_catalog"],
            "NATIONAL@NAT-2026")

    def test_batch_rolls_back_on_invalid_request(self):
        status, err = self.api.call("POST", "/batches", {"requests": [
            {"claim_id": "API-BAD1", "insured_region": "HB", "care_region": "HB",
             "service_date": "2025-06-01",
             "lines": [{"provincial_code": "HB0701", "charged_amount": "1.00"}]},
            {"insured_region": "HB"},
        ]})
        self.assertEqual(status, 400)
        status, _ = self.api.call("GET", "/settlements/API-BAD1")
        self.assertEqual(status, 404)

    def test_unknown_route_404(self):
        status, _ = self.api.call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_audit_log_records_governance_actions(self):
        self.api.call("POST", "/shadow/replay", {"use_seed_bills": True, "label": "审计影子"})
        status, body = self.api.call("GET", "/audit")
        self.assertEqual(status, 200)
        actions = {e["action"] for e in body["events"]}
        self.assertIn("shadow_replay", actions)
        self.assertIn("mapping_published", actions)


if __name__ == "__main__":
    unittest.main()
