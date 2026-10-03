"""三类访问者视图与数据边界、运营端到端追溯测试。"""

import unittest

from src.settlement.errors import AccessDenied
from src.settlement.model import ADJ_REVERSAL
from src.settlement.views import Principal

from scenarios import build_app, issue_standard_bundle, seed_world, SEPT_START, SEPT_END, OCT_START, OCT_END


class TraceAndAclTest(unittest.TestCase):
    def setUp(self):
        self.app = seed_world(build_app())
        issue_standard_bundle(self.app)
        self.app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]},
                   {"merchant_id": "M2", "store_id": "S9",
                    "entries": [{"item_id": "I1", "amount_cents": 8000}]}])
        self.app.service.open_batch("BAT1", edition_id="ED1",
                                    period_start=SEPT_START, period_end=SEPT_END)
        self.f1 = self.app.service.freeze_batch("BAT1")
        self.app.service.mark_batch_paid("BAT1")

    def test_operator_trace_line_to_consumption_contract(self):
        line = next(ln for ln in self.f1["lines"] if ln["kind"] == "CONSUMPTION"
                    and ln["merchant_id"] == "M1")
        trace = self.app.views.settlement_trace(Principal.operator(), line["line_id"])
        self.assertEqual(trace["line"]["line_id"], line["line_id"])
        self.assertEqual(trace["contract_basis"]["contract_id"], "C-M1")
        self.assertEqual(trace["contract_basis"]["version_no"], 1)
        self.assertIn("terms_snapshot", trace["line"]["detail"])
        self.assertEqual(trace["consumption"]["redemption_id"],
                         line["redemption_id"])
        self.assertEqual(trace["consumption"]["spectator_id"], "U1")

    def test_trace_reversal_links_source_line_and_refund(self):
        rf = self.app.service.request_refund(
            bundle_id="B1", reason="MERCHANT_NONPERFORMANCE",
            items=[{"item_id": "I1", "amount_cents": 4000}])
        self.app.service.confirm_refund(rf["refund_id"])
        self.app.service.open_batch("BAT2", edition_id="ED1",
                                    period_start=OCT_START, period_end=OCT_END)
        f2 = self.app.service.freeze_batch("BAT2")
        rev = next(ln for ln in f2["lines"] if ln["kind"] == ADJ_REVERSAL)
        trace = self.app.views.settlement_trace(Principal.operator(), rev["line_id"])
        self.assertEqual(trace["source"]["batch_id"], "BAT1")
        self.assertTrue(trace["source"]["line_id"])
        self.assertEqual(trace["line"]["refund_id"], rf["refund_id"])
        # 消费 -> 权益项 -> 退款串起来
        refund = trace["consumption"]["entries"][0]["refund"]
        self.assertEqual(refund["refund_id"], rf["refund_id"])
        self.assertEqual(refund["status"], "CONFIRMED")

    def test_spectator_sees_only_own_bundle_and_refund(self):
        u1 = Principal.spectator("U1")
        b = self.app.views.spectator_bundle(u1, "B1")
        self.assertEqual({i["item_id"] for i in b["items"]}, {"I1", "I2", "I3"})
        with self.assertRaises(AccessDenied):
            self.app.views.spectator_bundle(Principal.spectator("U2"), "B1")
        with self.assertRaises(AccessDenied):
            self.app.views.spectator_refund_progress(Principal.spectator("U2"), "B1")

    def test_spectator_refund_progress_states(self):
        rf = self.app.service.request_refund(
            bundle_id="B1", reason="SPECTATOR_CANCELLATION",
            items=[{"item_id": "I3", "amount_cents": 30000}])
        prog = self.app.views.spectator_refund_progress(Principal.spectator("U1"), "B1")
        self.assertEqual(prog["refunds"][0]["status"], "REQUESTED")
        self.assertEqual(prog["refunds"][0]["spectator_refund_cents"], 0)
        self.app.service.confirm_refund(rf["refund_id"])
        prog = self.app.views.spectator_refund_progress(Principal.spectator("U1"), "B1")
        self.assertEqual(prog["refunds"][0]["status"], "CONFIRMED")
        self.assertEqual(prog["refunds"][0]["spectator_refund_yuan"], "200.00")

    def test_merchant_sees_only_own_data(self):
        m1 = Principal.merchant("M1")
        reds = self.app.views.merchant_redemptions(m1)
        self.assertTrue(all(r["redemption_id"] for r in reds["redemptions"]))
        # QR-1 在 M1 只有一个分项
        self.assertEqual(len(reds["redemptions"]), 1)
        self.assertEqual(reds["redemptions"][0]["gross_cents"], 12000)

        sets = self.app.views.merchant_settlements(m1)
        for batch in sets["batches"]:
            self.assertTrue(all(ln["merchant_id"] == "M1" for ln in batch["lines"]))
            # 商户视图不暴露其他游客标识
            for ln in batch["lines"]:
                self.assertNotIn("spectator_id", ln["detail"])

        # M2 看不到 M1 的核销
        m2_reds = self.app.views.merchant_redemptions(Principal.merchant("M2"),
                                                      edition_id="ED1")
        self.assertEqual({r["store_id"] for r in m2_reds["redemptions"]}, {"S9"})

    def test_merchant_cannot_read_operator_views(self):
        with self.assertRaises(AccessDenied):
            self.app.views.batch_detail(Principal.merchant("M1"), "BAT1")
        with self.assertRaises(AccessDenied):
            self.app.views.settlement_trace(Principal.merchant("M1"), "BAT1-L001")
        with self.assertRaises(AccessDenied):
            self.app.views.audit_events(Principal.merchant("M1"))

    def test_spectator_cannot_read_merchant_or_operator(self):
        with self.assertRaises(AccessDenied):
            self.app.views.merchant_settlements(Principal.spectator("U1"))

    def test_merchant_contract_versions_visible_only_to_self(self):
        c1 = self.app.views.merchant_contract_versions(Principal.merchant("M1"))
        self.assertEqual(c1["contracts"][0]["contract_id"], "C-M1")
        c2 = self.app.views.merchant_contract_versions(Principal.merchant("M2"))
        self.assertNotIn("C-M1", [c["contract_id"] for c in c2["contracts"]])

    def test_audit_chain_reports_valid(self):
        audit = self.app.views.audit_events(Principal.operator())
        self.assertTrue(audit["chain_valid"])
        self.assertGreater(len(audit["events"]), 3)


if __name__ == "__main__":
    unittest.main()
