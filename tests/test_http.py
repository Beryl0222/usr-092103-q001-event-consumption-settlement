"""HTTP JSON 接口测试：鉴权头、幂等头、错误码映射。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from src.settlement.httpapi import build_server

from scenarios import (
    build_app, issue_standard_bundle, seed_world, SEPT_START, SEPT_END,
)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.app = seed_world(build_app())
        issue_standard_bundle(self.app)
        self.server = build_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, role="operator", actor=None, idem=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("X-Actor-Role", role)
        if actor:
            r.add_header("X-Actor-Id", actor)
        if idem:
            r.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        st, b = self.req("GET", "/health")
        self.assertEqual(st, 200)
        self.assertEqual(b["status"], "ok")

    def test_write_denied_for_merchant_and_spectator(self):
        for role in ("merchant", "spectator"):
            st, b = self.req("POST", "/batches",
                             {"batch_id": f"X-{role}", "edition_id": "ED1",
                              "period_start": SEPT_START, "period_end": SEPT_END},
                             role=role, actor="M1")
            self.assertEqual(st, 403)
            self.assertEqual(b["error"], "ACCESS_DENIED")

    def test_redemption_idempotency_header_and_duplicate(self):
        body = {"bundle_id": "B1", "scan_code_id": "QR-1",
                "scanned_at": "2026-09-15T10:00:00+08:00",
                "parts": [{"merchant_id": "M1", "store_id": "S1",
                           "entries": [{"item_id": "I1", "amount_cents": 100}]}]}
        st, b1 = self.req("POST", "/redemptions", body, idem="k-1")
        self.assertEqual(st, 200)
        st, b2 = self.req("POST", "/redemptions", body, idem="k-1")
        self.assertEqual(b1, b2)
        st, b3 = self.req("POST", "/redemptions", body, idem="k-2")
        self.assertEqual(st, 409)
        self.assertEqual(b3["error"], "DUPLICATE_REDEMPTION")

    def test_window_closed_maps_422(self):
        st, b = self.req("POST", "/redemptions", {
            "bundle_id": "B1", "scan_code_id": "QR-X",
            "scanned_at": "2026-09-15T10:00:00+08:00",
            "parts": [{"merchant_id": "M1", "store_id": "S1",
                       "entries": [{"item_id": "I3", "amount_cents": 100}]}]})
        self.assertEqual(st, 422)
        self.assertEqual(b["error"], "WINDOW_CLOSED")

    def test_full_settlement_flow_over_http(self):
        body = {"bundle_id": "B1", "scan_code_id": "QR-9",
                "scanned_at": "2026-09-15T10:00:00+08:00",
                "parts": [{"merchant_id": "M1", "store_id": "S1",
                           "entries": [{"item_id": "I1", "amount_cents": 12000}]}]}
        st, _ = self.req("POST", "/redemptions", body)
        self.assertEqual(st, 200)
        st, b = self.req("POST", "/batches",
                         {"batch_id": "BAT1", "edition_id": "ED1",
                          "period_start": SEPT_START, "period_end": SEPT_END})
        self.assertEqual(st, 200)
        st, b = self.req("POST", "/batches/BAT1/freeze", {})
        self.assertEqual(st, 200)
        self.assertIn("M1", b["totals"]["by_merchant"])
        st, b = self.req("POST", "/batches/BAT1/pay", {})
        self.assertEqual(st, 200)
        self.assertEqual(b["status"], "PAID")

    def test_spectator_and_merchant_read_scopes(self):
        st, b = self.req("GET", "/bundles/B1", role="spectator", actor="U1")
        self.assertEqual(st, 200)
        st, b = self.req("GET", "/bundles/B1", role="spectator", actor="U2")
        self.assertEqual(st, 403)
        st, b = self.req("GET", "/merchant/redemptions", role="merchant", actor="M1")
        self.assertEqual(st, 200)
        st, b = self.req("GET", "/merchant/settlements", role="merchant", actor="M9")
        self.assertEqual(st, 200)
        self.assertEqual(b["batches"], [])

    def test_bad_json_returns_400(self):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/editions", data=b"{not json", method="POST")
        r.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(r)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()
