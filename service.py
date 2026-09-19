"""医保项目目录衔接后端的 HTTP 入口（标准库，零第三方依赖）。

路由概览：
  GET  /health
  GET  /catalogs/versions[?level=&region=]
  POST /catalogs/versions
  POST /catalogs/{catalog_id}/versions/{version}/publish
  POST /catalogs/{catalog_id}/versions/{version}/withdraw
  GET  /catalogs/{catalog_id}/versions/{version}
  GET  /proposals[?status=] / GET /proposals/{id}
  POST /proposals / POST /proposals/{id}/reviews / POST /proposals/{id}/publish-mapping
  GET  /mappings[?region=] / POST /mappings/{id}/withdraw
  POST /settlements / GET /settlements / GET /settlements/{claim_id}
  POST /settlements/{claim_id}/reversal
  GET  /queue[?status=open|closed|all] / POST /queue/{queue_id}/resolve
  POST /shadow/replay / GET /shadow/runs / GET /shadow/runs/{run_id}
  POST /batches / GET /batches/{batch_id}
  GET  /audit
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from catalog_domain import (
    DomainError,
    NotFoundError,
    StateConflictError,
)
from seed_data import build_seeded_service, HISTORICAL_BILLS

SERVICE_ID = "medical-catalog"
SERVICE_NAME = "医保项目目录衔接"


def health_payload():
    """构造健康检查响应。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


_DEFAULT_SERVICE = None


def get_default_service():
    """直接实例化 Handler（如基础契约测试）时使用的惰性种子实例。"""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = build_seeded_service()
    return _DEFAULT_SERVICE


class Handler(BaseHTTPRequestHandler):
    server_version = "MedicalCatalog/1.0"

    # 服务实例优先由 server 持有（便于注入测试数据）；
    # 缺省时回退到模块级默认实例，保持基础契约的直接实例化方式。
    @property
    def svc(self):
        service = getattr(self.server, "governance_service", None)
        if service is None:
            service = get_default_service()
            self.server.governance_service = service
        return service

    # -- 框架 ------------------------------------------------------------

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _handle(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            body = self._read_body() if method == "POST" else {}
            # GET 的过滤条件来自 query string；POST 若带同名 query，body 优先。
            params = {**query, **body}
            self._route(method, path, query, params)
        except NotFoundError as exc:
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        except StateConflictError as exc:
            self._send_json(409, {"error": "state_conflict", "message": str(exc)})
        except DomainError as exc:
            self._send_json(400, {"error": "bad_request", "message": str(exc)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, *_args):
        return

    # -- 路由 ------------------------------------------------------------

    def _route(self, method: str, path: str, query: dict, body: dict) -> None:
        parts = [p for p in path.split("/") if p]

        if method == "GET" and path == "/health":
            self._send_json(200, health_payload())
            return

        if parts[:1] == ["catalogs"]:
            return self._route_catalogs(method, parts, body)
        if parts[:1] == ["proposals"]:
            return self._route_proposals(method, parts, body)
        if parts[:1] == ["mappings"]:
            return self._route_mappings(method, parts, body)
        if parts[:1] == ["settlements"]:
            return self._route_settlements(method, parts, body)
        if parts[:1] == ["queue"]:
            return self._route_queue(method, parts, body)
        if parts[:2] == ["shadow", "runs"] or path == "/shadow/replay":
            return self._route_shadow(method, parts, body)
        if parts[:1] == ["batches"]:
            return self._route_batches(method, parts, body)
        if method == "GET" and path == "/audit":
            self._send_json(200, {"events": self.svc.audit_log()})
            return

        self._send_json(404, {"error": "not_found", "message": f"未知路径：{path}"})

    def _route_catalogs(self, method, parts, body):
        # /catalogs/versions
        if parts == ["catalogs", "versions"]:
            if method == "GET":
                self._send_json(
                    200,
                    {"versions": self.svc.list_catalog_versions(
                        level=body.get("level"), region=body.get("region")
                    )},
                )
                return
            if method == "POST":
                result = self.svc.create_catalog_version(
                    level=body["level"],
                    region=body.get("region"),
                    version=body["version"],
                    effective_from=body["effective_from"],
                    effective_to=body.get("effective_to"),
                    projects=body.get("projects", []),
                )
                self._send_json(201, result)
                return
        # /catalogs/{catalog_id}/versions/{version}[/publish|/withdraw]
        if len(parts) >= 4 and parts[1] != "versions" and parts[2] == "versions":
            catalog_id, version = parts[1], parts[3]
            if len(parts) == 4 and method == "GET":
                self._send_json(200, self.svc.get_catalog_version(catalog_id, version))
                return
            if len(parts) == 5 and method == "POST" and parts[4] == "publish":
                self._send_json(200, self.svc.publish_catalog_version(catalog_id, version))
                return
            if len(parts) == 5 and method == "POST" and parts[4] == "withdraw":
                self._send_json(200, self.svc.withdraw_catalog_version(catalog_id, version))
                return
        self._method_or_404(method)

    def _route_proposals(self, method, parts, body):
        if parts == ["proposals"]:
            if method == "GET":
                self._send_json(
                    200, {"proposals": self.svc.list_proposals(status=body.get("status"))}
                )
                return
            if method == "POST":
                self._send_json(201, self.svc.submit_proposal(body))
                return
        if len(parts) == 2 and parts[0] == "proposals" and method == "GET":
            self._send_json(200, self.svc.get_proposal(parts[1]))
            return
        if len(parts) == 3 and parts[0] == "proposals" and method == "POST":
            proposal_id, action = parts[1], parts[2]
            if action == "reviews":
                self._send_json(
                    200,
                    self.svc.review_proposal(
                        proposal_id,
                        kind=body["kind"],
                        decision=body["decision"],
                        reviewer=body.get("reviewer", "未署名审核人"),
                        comment=body.get("comment", ""),
                    ),
                )
                return
            if action == "publish-mapping":
                self._send_json(
                    201,
                    self.svc.publish_mapping(
                        proposal_id,
                        version=body["version"],
                        effective_from=body["effective_from"],
                        effective_to=body.get("effective_to"),
                    ),
                )
                return
        self._method_or_404(method)

    def _route_mappings(self, method, parts, body):
        if parts == ["mappings"]:
            if method == "GET":
                self._send_json(
                    200, {"mappings": self.svc.list_mapping_versions(region=body.get("region"))}
                )
                return
        if len(parts) == 3 and parts[0] == "mappings" and parts[2] == "withdraw":
            if method == "POST":
                self._send_json(200, self.svc.withdraw_mapping_version(parts[1]))
                return
        self._method_or_404(method)

    def _route_settlements(self, method, parts, body):
        if parts == ["settlements"]:
            if method == "POST":
                self._send_json(200, self.svc.settle(body))
                return
            if method == "GET":
                self._send_json(200, {"settlements": self.svc.list_settlements()})
                return
        if len(parts) == 2 and parts[0] == "settlements" and method == "GET":
            self._send_json(200, self.svc.get_settlement(parts[1]))
            return
        if (
            len(parts) == 3
            and parts[0] == "settlements"
            and parts[2] == "reversal"
            and method == "POST"
        ):
            self._send_json(
                201,
                self.svc.reverse_settlement(
                    parts[1],
                    reason=body.get("reason", ""),
                    operator=body.get("operator", "未署名"),
                ),
            )
            return
        self._method_or_404(method)

    def _route_queue(self, method, parts, body):
        if parts == ["queue"] and method == "GET":
            self._send_json(
                200, {"items": self.svc.list_queue(status=body.get("status", "open"))}
            )
            return
        if len(parts) == 3 and parts[0] == "queue" and parts[2] == "resolve" and method == "POST":
            self._send_json(
                200,
                self.svc.resolve_queue_item(
                    parts[1],
                    decision=body["decision"],
                    operator=body.get("operator", "未署名"),
                    note=body.get("note", ""),
                    national_codes=body.get("national_codes"),
                    effective_from=body.get("effective_from"),
                    effective_to=body.get("effective_to"),
                ),
            )
            return
        self._method_or_404(method)

    def _route_shadow(self, method, parts, body):
        if method == "POST" and parts == ["shadow", "replay"]:
            bills = HISTORICAL_BILLS if body.get("use_seed_bills") else body.get("bills", [])
            self._send_json(
                200,
                self.svc.shadow_replay(bills, label=body.get("label", "")),
            )
            return
        if method == "GET" and parts == ["shadow", "runs"]:
            self._send_json(200, {"runs": self.svc.list_shadow_runs()})
            return
        if method == "GET" and len(parts) == 3:
            self._send_json(200, self.svc.get_shadow_run(parts[2]))
            return
        self._method_or_404(method)

    def _route_batches(self, method, parts, body):
        if parts == ["batches"] and method == "POST":
            self._send_json(
                200,
                self.svc.run_batch(
                    body.get("requests", []),
                    label=body.get("label", ""),
                    fail_after=body.get("fail_after"),
                ),
            )
            return
        if len(parts) == 2 and parts[0] == "batches" and method == "GET":
            self._send_json(200, self.svc.get_batch(parts[1]))
            return
        self._method_or_404(method)

    def _method_or_404(self, method):
        self._send_json(404, {"error": "not_found", "message": f"不支持 {method} {self.path}"})


def build_server(host: str, port: int, *, seed: bool = True) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.governance_service = build_seeded_service() if seed else build_empty_for_tests()
    return server


def build_empty_for_tests():
    from catalog_domain import CatalogGovernanceService

    return CatalogGovernanceService()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-seed", action="store_true", help="不装载演示种子数据")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["status"] == "ok"
        # 种子数据必须能完整装载并完成一次影子回放，作为启动自检。
        svc = build_seeded_service()
        run = svc.shadow_replay(HISTORICAL_BILLS, label="启动自检")
        assert run["total_lines"] > 0 and run["coverage_rate"] > 0
        print(
            f"基础检查通过：目录版本 {len(svc.list_catalog_versions())} 个，"
            f"影子回放 {run['covered_lines']}/{run['total_lines']} 行可解释"
        )
        return
    server = build_server(args.host, args.port, seed=not args.no_seed)
    print(f"{SERVICE_NAME} 监听 http://{args.host}:{args.port}（{'含种子数据' if not args.no_seed else '空库'}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
