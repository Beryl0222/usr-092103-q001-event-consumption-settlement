"""HTTP JSON 接口（标准库实现，无第三方依赖）。

鉴权约定（联调用请求头表达调用方身份）：
- X-Actor-Role: operator | spectator | merchant
- X-Actor-Id:   游客 ID 或商户 ID（operator 可省略）
数据边界在读侧强制；写接口仅 operator 可调。

幂等：命令请求体可带 client_request_id，或用 Idempotency-Key 请求头。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .app import App
from .errors import SettlementError
from .views import MERCHANT, OPERATOR, SPECTATOR, Principal

WRITE_OPERATOR_ONLY = True


def _die(handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": code, "message": message}, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "SettlementAPI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静
        return

    # ---- 基础设施 --------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def _principal(self) -> Principal:
        role = self.headers.get("X-Actor-Role", OPERATOR)
        pid = self.headers.get("X-Actor-Id")
        return Principal(role, pid)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_operator(self) -> Principal:
        p = self._principal()
        if WRITE_OPERATOR_ONLY and p.role != OPERATOR:
            raise PermissionError("写接口仅运营角色可调用")
        return p

    def _call(self, fn: Callable[..., dict], body: dict, skip: set[str] | None = None) -> dict:
        skip = skip or set()
        kwargs = {k: v for k, v in body.items() if k not in skip}
        if "client_request_id" not in kwargs:
            key = self.headers.get("Idempotency-Key")
            if key:
                kwargs["client_request_id"] = key
        return fn(**kwargs)

    # ---- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            principal = self._principal()
            v = self.app.views
            if path == "/health":
                self._write(200, {"status": "ok"})
            elif m := re.fullmatch(r"/bundles/([^/]+)", path):
                self._write(200, v.spectator_bundle(principal, m.group(1)))
            elif m := re.fullmatch(r"/bundles/([^/]+)/refunds", path):
                self._write(200, v.spectator_refund_progress(principal, m.group(1)))
            elif path == "/merchant/redemptions":
                edition = self._query("edition_id")
                self._write(200, v.merchant_redemptions(principal, edition))
            elif path == "/merchant/settlements":
                self._write(200, v.merchant_settlements(principal))
            elif path == "/merchant/contracts":
                edition = self._query("edition_id")
                self._write(200, v.merchant_contract_versions(principal, edition))
            elif m := re.fullmatch(r"/operator/batches/([^/]+)", path):
                self._write(200, v.batch_detail(principal, m.group(1)))
            elif m := re.fullmatch(r"/operator/lines/([^/]+)/trace", path):
                self._write(200, v.settlement_trace(principal, m.group(1)))
            elif path == "/operator/events":
                self._write(200, v.audit_events(
                    principal,
                    aggregate_id=self._query("aggregate_id"),
                    event_type=self._query("event_type")))
            else:
                _die(self, 404, "NOT_FOUND", f"无此路径：{path}")
        except (SettlementError, PermissionError, ValueError, TypeError) as exc:
            self._error(exc)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            _die(self, 500, "INTERNAL_ERROR", str(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._read_json()
            s = self.app.service

            if path == "/editions":
                self._require_operator()
                res = self._call(s.register_edition, body)
            elif path == "/merchants":
                self._require_operator()
                res = self._call(s.register_merchant, body)
            elif m := re.fullmatch(r"/contracts/([^/]+)/versions", path):
                self._require_operator()
                res = s.effective_contract_version(m.group(1), **{
                    k: v for k, v in body.items()
                    if k in ("merchant_id", "edition_id", "terms", "effective_from", "version_no",
                             "note", "client_request_id")})
            elif m := re.fullmatch(r"/bundles/([^/]+)", path):
                self._require_operator()
                res = s.issue_bundle(m.group(1), **{
                    k: v for k, v in body.items()
                    if k in ("spectator_id", "edition_id", "items", "client_request_id")})
            elif path == "/redemptions":
                self._require_operator()
                res = self._call(s.record_redemption, body)
            elif path == "/refunds":
                self._require_operator()
                res = self._call(s.request_refund, body)
            elif m := re.fullmatch(r"/refunds/([^/]+)/confirm", path):
                self._require_operator()
                res = s.confirm_refund(m.group(1), **{
                    k: v for k, v in body.items()
                    if k in ("note", "client_request_id", "at")})
            elif m := re.fullmatch(r"/editions/([^/]+)/reschedule", path):
                self._require_operator()
                res = s.reschedule_edition(m.group(1), **body)
            elif path == "/batches":
                self._require_operator()
                res = self._call(s.open_batch, body)
            elif m := re.fullmatch(r"/batches/([^/]+)/freeze", path):
                self._require_operator()
                res = s.freeze_batch(m.group(1), **{
                    k: v for k, v in body.items()
                    if k in ("client_request_id", "at")})
            elif m := re.fullmatch(r"/batches/([^/]+)/pay", path):
                self._require_operator()
                res = s.mark_batch_paid(m.group(1), **{
                    k: v for k, v in body.items()
                    if k in ("client_request_id", "at")})
            elif m := re.fullmatch(r"/batches/([^/]+)/adjustments", path):
                self._require_operator()
                res = s.post_correction(m.group(1), **body)
            else:
                _die(self, 404, "NOT_FOUND", f"无此路径：{path}")
                return
            self._write(200, res)
        except (SettlementError, PermissionError, ValueError, TypeError) as exc:
            self._error(exc)
        except Exception as exc:  # 兜底：任何未预期错误返回 500，不断连
            import traceback
            traceback.print_exc()
            _die(self, 500, "INTERNAL_ERROR", str(exc))

    def _query(self, name: str) -> str | None:
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return qs.get(name, [None])[0]

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            _die(self, 403, "ACCESS_DENIED", str(exc))
        else:
            mapping = {
                "NotFound": (404, "NOT_FOUND"),
                "AccessDenied": (403, "ACCESS_DENIED"),
                "DuplicateRedemption": (409, "DUPLICATE_REDEMPTION"),
                "Conflict": (409, "CONFLICT"),
                "InsufficientBalance": (409, "INSUFFICIENT_BALANCE"),
                "IllegalState": (422, "ILLEGAL_STATE"),
                "WindowClosed": (422, "WINDOW_CLOSED"),
                "ContractError": (422, "CONTRACT_ERROR"),
                "ValueError": (400, "BAD_REQUEST"),
                "TypeError": (400, "BAD_REQUEST"),
            }
            status, code = mapping.get(type(exc).__name__, (400, "BAD_REQUEST"))
            _die(self, status, code, str(exc))


def build_server(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.app = app  # type: ignore[attr-defined]
    return server


def serve(app: App, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = build_server(app, host, port)
    print(f"赛事跨业消费清算 API 监听 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
