"""医保项目目录治理后端运行入口。

路由总览：
  GET  /health
  目录    GET  /catalogs[?scope=&province=]
          GET  /catalogs/{id}/items?date=
          GET  /catalogs/{id}/items/{code}/versions
  映射    GET  /mappings?province=&date=
          GET  /mappings/{group_id}
  治理    POST /proposals                         提交映射建议
          GET  /proposals[?status=&province=]
          GET  /proposals/{id}
          POST /proposals/{id}/reviews           业务/财务审核
          POST /proposals/{id}/publish           发布待生效版本
          POST /mappings/{id}/versions/{v}/withdraw
          POST /admin/activate?as_of=YYYY-MM-DD  跨日跑批
          GET  /admin/consistency?as_of=
  结算    POST /settlements
          GET  /settlements[?bill_id=&status=]
          GET  /settlements/{id}                 含全部目录/映射版本与冲正
          POST /settlements/{id}/reversals
          GET  /reversals/{id}
  队列    GET  /queue[?status=]
          POST /queue/{id}/annotations
  影子    POST /shadow/replay                     回放内置或请求体账单
          GET  /shadow/replay                     回放内置账单
  审计    GET  /audit?limit=

所有写操作的操作人取 X-Actor 请求头（缺省为 system）。
"""

import argparse
import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import catalog as catalog_mod
import shadow as shadow_mod
from catalog import (
    BUSINESS_REVIEW,
    FINANCE_REVIEW,
    DomainError,
    Registry,
    REVIEW_APPROVE,
    REVIEW_REJECT,
)
from seed_data import seed
from settlement import SettlementEngine

SERVICE_ID = "medical-catalog"
SERVICE_NAME = "医保项目目录衔接"

DEFAULT_DATA = "data/registry.json"


def health_payload():
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Application:
    """无状态应用层：持有注册中心与结算引擎，Handler 只做 HTTP 编解码。"""

    def __init__(self, data_path=DEFAULT_DATA, today=None):
        self.registry = Registry(
            path=data_path,
            clock=today or date.today,
        )
        self.engine = SettlementEngine(self.registry)

    def ensure_seeded(self):
        if not self.registry.list_catalogs():
            seed(self.registry)
        return self


def _err_status(exc):
    code = getattr(exc, "code", "DOMAIN_ERROR")
    return {
        "NOT_FOUND": 404,
        "VALIDATION_ERROR": 400,
        "CONFLICT": 409,
    }.get(code, 400)


class Handler(BaseHTTPRequestHandler):
    app: Application = None  # 由服务器注入

    # ----- HTTP 基础 -----

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc):
        self._send_json(
            {"error": exc.code, "message": exc.message, "details": exc.details},
            status=_err_status(exc),
        )

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            raise catalog_mod.ValidationError("请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise catalog_mod.ValidationError("请求体必须是 JSON 对象")
        return payload

    @property
    def actor(self):
        return self.headers.get("X-Actor", "system")

    def log_message(self, *_args):
        return

    # ----- 路由 -----

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        # 健康检查不依赖应用状态，保证最小可运行
        if method == "GET" and path == "/health":
            self._send_json(health_payload())
            return
        app = self.app
        if app is None:
            # 未挂载业务应用：除健康检查外不暴露任何路由
            self._send_json(
                {"error": "NOT_FOUND", "message": f"路由不存在: {path}",
                 "details": {}}, status=404)
            return
        reg, eng = app.registry, app.engine
        try:
            if method == "GET" and path == "/catalogs":
                self._send_json(reg.list_catalogs(query.get("scope"),
                                                  query.get("province"))); return
            match = re.fullmatch(r"/catalogs/([^/]+)/items/([^/]+)/versions", path)
            if method == "GET" and match:
                self._send_json(reg.item_versions(match.group(1), match.group(2))); return
            match = re.fullmatch(r"/catalogs/([^/]+)/items", path)
            if method == "GET" and match:
                self._send_json(reg.effective_items(
                    match.group(1), query.get("date") or reg.clock())); return
            if method == "GET" and path == "/mappings":
                self._send_json(reg.effective_mappings(
                    query["province"], query.get("date") or reg.clock())); return
            match = re.fullmatch(r"/mappings/([^/]+)", path)
            if method == "GET" and match:
                self._send_json(reg.get_group(match.group(1))); return
            if path == "/proposals":
                if method == "POST":
                    body = self._read_json()
                    result = reg.submit_proposal(
                        province=body["province"],
                        direction=body["direction"],
                        members=body["members"],
                        evidence=body["evidence"],
                        conflict_acknowledgements=body.get("conflict_acknowledgements"),
                        weights=body.get("weights"),
                        note=body.get("note", ""),
                        actor=self.actor,
                    )
                    self._send_json(result, 201); return
                self._send_json(reg.list_proposals(query.get("status"),
                                                   query.get("province"))); return
            match = re.fullmatch(r"/proposals/([^/]+)", path)
            if method == "GET" and match:
                self._send_json(reg.get_proposal(match.group(1))); return
            match = re.fullmatch(r"/proposals/([^/]+)/reviews", path)
            if method == "POST" and match:
                body = self._read_json()
                kind = body.get("kind", "").upper()
                if kind not in (BUSINESS_REVIEW, FINANCE_REVIEW):
                    raise catalog_mod.ValidationError(
                        "kind 必须为 BUSINESS 或 FINANCE")
                decision = body.get("decision", "").upper()
                if decision not in (REVIEW_APPROVE, REVIEW_REJECT):
                    raise catalog_mod.ValidationError(
                        "decision 必须为 APPROVE 或 REJECT")
                result = reg.review_proposal(
                    match.group(1), kind, decision, self.actor,
                    body.get("comment", ""))
                self._send_json(result); return
            match = re.fullmatch(r"/proposals/([^/]+)/publish", path)
            if method == "POST" and match:
                body = self._read_json()
                result = reg.publish_proposal(
                    match.group(1), body["effective_from"], self.actor,
                    body.get("effective_until"))
                self._send_json(result); return
            match = re.fullmatch(r"/mappings/([^/]+)/versions/(\d+)/withdraw", path)
            if method == "POST" and match:
                body = self._read_json()
                result = reg.withdraw_version(
                    match.group(1), int(match.group(2)), self.actor,
                    body.get("reason", ""))
                self._send_json(result); return
            if method == "POST" and path == "/admin/activate":
                body = self._read_json()
                self._send_json(reg.activate_due(
                    body.get("as_of") or query.get("as_of"), self.actor)); return
            if method == "GET" and path == "/admin/consistency":
                self._send_json(reg.consistency_check(
                    query.get("as_of") or reg.clock())); return
            if path == "/settlements":
                if method == "POST":
                    result = eng.settle(self._read_json(), actor=self.actor)
                    self._send_json(result, 201); return
                self._send_json(eng.list_settlements(query.get("bill_id"),
                                                     query.get("status"))); return
            match = re.fullmatch(r"/settlements/([^/]+)", path)
            if method == "GET" and match:
                self._send_json(eng.get_settlement(match.group(1))); return
            match = re.fullmatch(r"/settlements/([^/]+)/reversals", path)
            if method == "POST" and match:
                body = self._read_json()
                result = eng.reverse(match.group(1), body.get("reason", ""),
                                     self.actor, body.get("corrected_lines", []))
                self._send_json(result, 201); return
            match = re.fullmatch(r"/reversals/([^/]+)", path)
            if method == "GET" and match:
                self._send_json(eng.get_reversal(match.group(1))); return
            if method == "GET" and path == "/queue":
                self._send_json(eng.list_queue(query.get("status"))); return
            match = re.fullmatch(r"/queue/([^/]+)/annotations", path)
            if method == "POST" and match:
                body = self._read_json()
                result = eng.annotate_queue(match.group(1), self.actor,
                                            body.get("note", ""),
                                            body.get("decision", "ANNOTATED"))
                self._send_json(result); return
            if path == "/shadow/replay":
                if method == "GET":
                    self._send_json(shadow_mod.replay(reg)); return
                body = self._read_json()
                bills = body.get("bills")
                self._send_json(shadow_mod.replay(
                    reg, bills=bills if bills is not None else None)); return
            if method == "GET" and path == "/audit":
                self._send_json(reg.audit_tail(int(query.get("limit", 100)))); return
            self._send_json(
                {"error": "NOT_FOUND", "message": f"路由不存在: {path}",
                 "details": {}}, status=404)
        except DomainError as exc:
            self._send_error(exc)
        except KeyError as exc:
            self._send_error(catalog_mod.ValidationError(
                f"请求缺少必填字段: {exc.args[0]}"))


def build_server(port, data_path, seed_if_empty=True, today=None):
    app = Application(data_path=data_path, today=today)
    if seed_if_empty:
        app.ensure_seeded()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.app = app  # type: ignore[attr-defined]
    Handler.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=DEFAULT_DATA, help="持久化文件路径")
    parser.add_argument("--check", action="store_true", help="基础自检")
    parser.add_argument("--seed", action="store_true",
                        help="初始化种子目录与映射后退出")
    parser.add_argument("--reset", action="store_true",
                        help="与 --seed 配合：清空既有数据后重建")
    parser.add_argument("--shadow", action="store_true",
                        help="影子回放内置历史账单并打印报告")
    parser.add_argument("--no-autoseed", action="store_true",
                        help="启动时若数据文件为空也不自动写入种子")
    args = parser.parse_args()

    import os
    if args.check:
        assert health_payload()["status"] == "ok"
        assert len(catalog_mod.CATALOG_CATEGORIES) == 13
        print("基础检查通过（十三类服务已登记）")
        return

    if args.reset and os.path.exists(args.data):
        os.unlink(args.data)

    app = Application(data_path=args.data)
    if args.seed:
        if app.registry.list_catalogs() and not args.reset:
            print("数据已存在；如需重建请加 --reset")
        else:
            seed(app.registry)
            print(f"种子数据已写入 {args.data}")

    if args.shadow:
        app.ensure_seeded()
        report = shadow_mod.replay(app.registry)
        print(shadow_mod.format_text(report))
        return

    if not args.no_autoseed:
        app.ensure_seeded()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    Handler.app = app
    print(f"{SERVICE_NAME} 监听 :{args.port}（数据 {args.data}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
