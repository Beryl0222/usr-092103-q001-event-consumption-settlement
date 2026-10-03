"""比赛改期：只迁移仍可履约部分，窗口平移、无法履约自动退款。"""

import unittest

from src.settlement.errors import WindowClosed
from src.settlement.views import Principal

from scenarios import build_app, seed_world


def _reschedule(app, **overrides):
    args = dict(
        new_race_start="2026-10-04T07:00:00+08:00",
        new_race_end="2026-10-05T12:00:00+08:00",
        new_pre_start="2026-09-24T00:00:00+08:00",
        new_post_end="2026-10-11T23:59:59+08:00",
        reason="天气", at="2026-09-16T10:00:00+08:00")
    args.update(overrides)
    return app.service.reschedule_edition("ED1", **args)


class RescheduleTest(unittest.TestCase):
    def _seed(self):
        app = seed_world(build_app(), merchants={
            "M1": {"name": "甲", "tax": 0,
                   "terms": {"mode": "FIXED", "fixed_cents": 1000}},
            "M3": {"name": "文旅丙", "tax": 0,
                   "terms": {"mode": "FIXED", "fixed_cents": 1000}}})
        app.service.issue_bundle("B1", spectator_id="U1", edition_id="ED1", items=[
            {"item_id": "I1", "phase": "PRE_RACE", "title": "套餐",
             "face_value_cents": 20000, "subsidy_cents": 5000, "merchant_id": "M1"},
            {"item_id": "I2", "phase": "POST_RACE", "title": "文旅",
             "face_value_cents": 30000, "subsidy_cents": 0, "merchant_id": "M3"},
            {"item_id": "I3", "phase": "RACE_DAY", "title": "通用券",
             "face_value_cents": 1000, "subsidy_cents": 0}])
        return app

    def test_windows_shift_and_unfulfillable_merchant_refunded(self):
        app = self._seed()
        # I1 已部分核销 8000
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 8000}]}])
        res = _reschedule(app, unavailable_merchants={"M3"})
        bundle_change = res["bundles"][0]
        migrated = {m["item_id"] for m in bundle_change["migrated"]}
        cancelled = {c["item_id"] for c in bundle_change["cancelled"]}
        self.assertEqual(migrated, {"I1", "I3"})
        self.assertEqual(cancelled, {"I2"})

        b = app.views.spectator_bundle(Principal.spectator("U1"), "B1")
        by_id = {i["item_id"]: i for i in b["items"]}
        # 已用额度保留，剩余随窗口平移
        self.assertEqual(by_id["I1"]["available_cents"], 12000)
        self.assertTrue(by_id["I1"]["window_start"].startswith("2026-09-24"))
        self.assertEqual(by_id["I2"]["status"], "CANCELLED")
        # 自动退款已确认且全额（I2 游客自付 30000）
        progress = app.views.spectator_refund_progress(Principal.spectator("U1"), "B1")
        self.assertEqual(len(progress["refunds"]), 1)
        self.assertEqual(progress["refunds"][0]["status"], "CONFIRMED")
        self.assertEqual(progress["refunds"][0]["spectator_refund_cents"], 30000)

    def test_remaining_balance_redeemable_after_shift(self):
        app = self._seed()
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 8000}]}])
        _reschedule(app)
        # 旧窗口失效
        with self.assertRaises(WindowClosed):
            app.service.record_redemption(
                bundle_id="B1", scan_code_id="QR-2",
                scanned_at="2026-09-19T10:00:00+08:00",
                parts=[{"merchant_id": "M1", "store_id": "S1",
                        "entries": [{"item_id": "I1", "amount_cents": 100}]}])
        # 新窗口内可继续核销剩余 12000
        r = app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-3",
            scanned_at="2026-09-29T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 12000}]}])
        self.assertTrue(r["redemption_id"])

    def test_consumed_and_refunding_items_not_migrated(self):
        app = self._seed()
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-1", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M1", "store_id": "S1",
                    "entries": [{"item_id": "I1", "amount_cents": 20000}]}])
        # I3 处于退款流程中
        rf = app.service.request_refund(
            bundle_id="B1", reason="SPECTATOR_CANCELLATION",
            items=[{"item_id": "I3", "amount_cents": 1000}])
        _reschedule(app)
        b = app.views.spectator_bundle(Principal.spectator("U1"), "B1")
        by_id = {i["item_id"]: i for i in b["items"]}
        self.assertEqual(by_id["I1"]["status"], "REDEEMED")
        self.assertEqual(by_id["I3"]["status"], "REFUND_PENDING")
        self.assertEqual(by_id["I3"]["refund_id"], rf["refund_id"])

    def test_reschedule_is_idempotent(self):
        app = self._seed()
        r1 = _reschedule(app, client_request_id="rc-1")
        r2 = _reschedule(app, client_request_id="rc-1")
        self.assertEqual(r1, r2)
        # 届次只产生一条改期事件
        stream = app.store.load_stream("ED1")
        self.assertEqual(len([e for e in stream if e.event_type == "EDITION_RESCHEDULED"]), 1)


if __name__ == "__main__":
    unittest.main()
