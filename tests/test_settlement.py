"""结算批次：分成、补贴、税费、保底、冲正/补付与旧账不可改写。"""

import unittest

from src.settlement.errors import IllegalState
from src.settlement.model import ADJ_REVERSAL, ADJ_TOPUP
from src.settlement.views import Principal

from scenarios import (
    build_app, issue_standard_bundle, seed_world,
    SEPT_START, SEPT_END, OCT_START, OCT_END,
)


def _freeze_sept(app, batch_id="BAT1"):
    app.service.open_batch(batch_id, edition_id="ED1",
                           period_start=SEPT_START, period_end=SEPT_END)
    return app.service.freeze_batch(batch_id)


class SettlementBasicTest(unittest.TestCase):
    def setUp(self):
        self.app = seed_world(build_app())
        issue_standard_bundle(self.app)
        # M1 12000（阶梯 80%），M2 8000（固定 4000/笔）
        self.app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]},
                   {"merchant_id": "M2", "store_id": "S9",
                    "entries": [{"item_id": "I1", "amount_cents": 8000}]}])

    def test_freeze_computes_share_fee_tax(self):
        f = _freeze_sept(self.app)
        m1 = f["totals"]["by_merchant"]["M1"]
        m2 = f["totals"]["by_merchant"]["M2"]
        self.assertEqual(m1["consumption_cents"], 9600)          # 12000*80%
        self.assertEqual(m1["tax_cents"], 576)                  # 9600*6%
        self.assertEqual(m1["payable_cents"], 9024)
        self.assertEqual(m2["consumption_cents"], 4000)          # 固定额
        self.assertEqual(m2["tax_cents"], 240)
        # 平台抽佣
        self.assertEqual(f["totals"]["platform_fee_cents"], 600 + 4000)  # M1 抽佣 600 + M2 固定额抽佣 4000

    def test_subsidy_allocated_pro_rata(self):
        f = _freeze_sept(self.app)
        consumption = [ln for ln in f["lines"] if ln["kind"] == "CONSUMPTION"]
        subsidy = sum(ln["detail"]["subsidy_cents"] for ln in consumption)
        # I1 补贴 5000，全部核销 => 5000 分补贴随消费分摊
        self.assertEqual(subsidy, 5000)

    def test_frozen_batch_is_immutable(self):
        f = _freeze_sept(self.app)
        self.app.service.mark_batch_paid("BAT1")
        before = [ln["line_id"] for ln in f["lines"]]
        # 支付后不能再次冻结
        with self.assertRaises(IllegalState):
            self.app.service.freeze_batch("BAT1")
        # 旧批次快照不随后续事件变化
        detail = self.app.views.batch_detail(Principal.operator(), "BAT1")
        self.assertEqual([ln["line_id"] for ln in detail["lines"]], before)
        self.assertEqual(detail["status"], "PAID")


class GuaranteeTest(unittest.TestCase):
    def test_monthly_guarantee_topped_up(self):
        app = seed_world(build_app(), merchants={
            "M1": {"name": "甲", "tax": 0, "terms": {
                "mode": "TIERED_SHARE", "platform_fee_bps": 0,
                "tiers": [{"up_to_cents": 10_000_000, "share_bps": 8000}],
                "monthly_guarantee_cents": 50000}}})
        issue_standard_bundle(app)
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]}])
        f = _freeze_sept(app)
        m1 = f["totals"]["by_merchant"]["M1"]
        # 分成 9600，保底补到 50000
        self.assertEqual(m1["guarantee_topup_cents"], 40400)
        self.assertEqual(m1["payable_cents"], 50000)

    def test_no_guarantee_in_pure_reversal_month(self):
        # 9 月已满足保底并支付；10 月只有跨月退款冲正，不应再产生保底
        app = seed_world(build_app(), merchants={
            "M1": {"name": "甲", "tax": 0, "terms": {
                "mode": "TIERED_SHARE", "platform_fee_bps": 0,
                "tiers": [{"up_to_cents": 10_000_000, "share_bps": 8000}],
                "monthly_guarantee_cents": 50000}}})
        issue_standard_bundle(app)
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]}])
        f1 = _freeze_sept(app)
        app.service.mark_batch_paid("BAT1")
        rf = app.service.request_refund(
            bundle_id="B1", reason="MERCHANT_NONPERFORMANCE",
            items=[{"item_id": "I1", "amount_cents": 5000}])
        app.service.confirm_refund(rf["refund_id"])
        app.service.open_batch("BAT2", edition_id="ED1",
                               period_start=OCT_START, period_end=OCT_END)
        f2 = app.service.freeze_batch("BAT2")
        kinds = [ln["kind"] for ln in f2["lines"]]
        self.assertIn("REVERSAL", kinds)
        self.assertNotIn("GUARANTEE_TOPUP", kinds)


class RefundReversalTest(unittest.TestCase):
    def setUp(self):
        self.app = seed_world(build_app())
        issue_standard_bundle(self.app)
        self.app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]},
                   {"merchant_id": "M2", "store_id": "S9",
                    "entries": [{"item_id": "I1", "amount_cents": 8000}]}])
        _freeze_sept(self.app)
        self.app.service.mark_batch_paid("BAT1")

    def test_post_payment_refund_creates_reversal_referencing_old_line(self):
        old = self.app.views.batch_detail(Principal.operator(), "BAT1")
        old_m1 = next(ln for ln in old["lines"]
                      if ln["merchant_id"] == "M1" and ln["kind"] == "CONSUMPTION")
        rf = self.app.service.request_refund(
            bundle_id="B1", reason="MERCHANT_NONPERFORMANCE",
            items=[{"item_id": "I1", "amount_cents": 6000}])
        cf = self.app.service.confirm_refund(rf["refund_id"])
        # I1 自付 15000/面值 20000；退 6000 中游客到账 4500
        self.assertEqual(cf["spectator_amount_cents"], 4500)

        self.app.service.open_batch("BAT2", edition_id="ED1",
                                    period_start=OCT_START, period_end=OCT_END)
        f2 = self.app.service.freeze_batch("BAT2")
        reversals = [ln for ln in f2["lines"] if ln["kind"] == ADJ_REVERSAL]
        # FIFO：6000 先冲 M1 的 12000 消费（M1 9600 分成按比例 -4800），
        # 再冲 M2 的 8000（本笔 6000 全落在 M1，因为 M1 消费 12000>=6000）
        self.assertEqual(len(reversals), 1)
        rev = reversals[0]
        self.assertEqual(rev["merchant_id"], "M1")
        self.assertEqual(rev["amount_cents"], -4800)
        self.assertEqual(rev["source_batch_id"], "BAT1")
        self.assertEqual(rev["source_line_id"], old_m1["line_id"])
        self.assertEqual(rev["refund_id"], rf["refund_id"])

        # 旧账行金额不变
        old_after = self.app.views.batch_detail(Principal.operator(), "BAT1")
        same = next(ln for ln in old_after["lines"] if ln["line_id"] == old_m1["line_id"])
        self.assertEqual(same["amount_cents"], old_m1["amount_cents"])

    def test_reversal_spans_multiple_parts_fifo(self):
        rf = self.app.service.request_refund(
            bundle_id="B1", reason="MERCHANT_NONPERFORMANCE",
            items=[{"item_id": "I1", "amount_cents": 15000}])
        self.app.service.confirm_refund(rf["refund_id"])
        self.app.service.open_batch("BAT2", edition_id="ED1",
                                    period_start=OCT_START, period_end=OCT_END)
        f2 = self.app.service.freeze_batch("BAT2")
        revs = {(ln["merchant_id"], ln["amount_cents"])
                for ln in f2["lines"] if ln["kind"] == ADJ_REVERSAL}
        # M1 消费 12000 全冲（-9600），余 3000 冲 M2（固定额按比例 -1500）
        self.assertIn(("M1", -9600), revs)
        self.assertIn(("M2", -1500), revs)

    def test_refund_before_freeze_nets_consumption_in_same_batch(self):
        app = seed_world(build_app())
        issue_standard_bundle(app)
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-9", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 20000}]}])
        app.service.open_batch("BAT1", edition_id="ED1",
                               period_start=SEPT_START, period_end=SEPT_END)
        # 冻结前先确认退款（未使用权益项 I3），不影响消费行
        rf = app.service.request_refund(
            bundle_id="B1", reason="SPECTATOR_CANCELLATION",
            items=[{"item_id": "I3", "amount_cents": 30000}])
        cf = app.service.confirm_refund(rf["refund_id"])
        self.assertEqual(cf["spectator_amount_cents"], 20000)  # I3 自付 20000
        f = app.service.freeze_batch("BAT1")
        # I3 未消费，批次中不应出现它的冲正行
        rev_items = [e["item_id"] for ln in f["lines"] if ln["kind"] == ADJ_REVERSAL
                     for e in ln["detail"].get("entries", [])]
        self.assertNotIn("I3", rev_items)


class ManualCorrectionTest(unittest.TestCase):
    def test_topup_on_open_batch_after_payment(self):
        app = seed_world(build_app())
        issue_standard_bundle(app)
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]}])
        _freeze_sept(app)
        app.service.mark_batch_paid("BAT1")
        old_payable = app.views.batch_detail(Principal.operator(), "BAT1")["totals"][
            "by_merchant"]["M1"]["payable_cents"]

        # 已付款后发现少付：在新开放批次补付，差异可追溯到旧行
        app.service.open_batch("BAT2", edition_id="ED1",
                               period_start=OCT_START, period_end=OCT_END)
        app.service.post_correction(
            "BAT2", merchant_id="M1", amount_cents=700, kind=ADJ_TOPUP,
            reason="现场服务费漏计", source_batch_id="BAT1",
            source_line_id="BAT1-L001")
        with self.assertRaises(IllegalState):
            # 冲正/补付不能登记到已支付批次
            app.service.post_correction(
                "BAT1", merchant_id="M1", amount_cents=1, kind=ADJ_TOPUP, reason="x")
        f2 = app.service.freeze_batch("BAT2")
        top = [ln for ln in f2["lines"] if ln["kind"] == ADJ_TOPUP]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["amount_cents"], 700)
        self.assertEqual(top[0]["source_line_id"], "BAT1-L001")
        # 旧账金额不变
        new_old = app.views.batch_detail(Principal.operator(), "BAT1")
        self.assertEqual(new_old["totals"]["by_merchant"]["M1"]["payable_cents"], old_payable)


if __name__ == "__main__":
    unittest.main()
