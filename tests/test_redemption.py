"""权益有效窗口、二维码重复扫描、离线补传、跨店拆单幂等测试。"""

import unittest

from src.settlement.errors import (
    DuplicateRedemption,
    IllegalState,
    InsufficientBalance,
    WindowClosed,
)
from src.settlement.views import Principal

from scenarios import build_app, issue_standard_bundle, seed_world


def redeem(app, scan_id, scanned_at, parts, *, offline=False, client_request_id=None):
    return app.service.record_redemption(
        bundle_id="B1", scan_code_id=scan_id, scanned_at=scanned_at,
        parts=parts, offline=offline, client_request_id=client_request_id)


P1 = [{"merchant_id": "M1", "store_id": "S1",
       "entries": [{"item_id": "I1", "amount_cents": 12000}]},
      {"merchant_id": "M2", "store_id": "S9",
       "entries": [{"item_id": "I1", "amount_cents": 8000}]}]


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.app = seed_world(build_app())
        issue_standard_bundle(self.app)

    def test_phase_windows_derived_from_edition(self):
        b = self.app.views.spectator_bundle(Principal.spectator("U1"), "B1")
        windows = {i["item_id"]: (i["window_start"], i["window_end"]) for i in b["items"]}
        self.assertTrue(windows["I1"][0].startswith("2026-09-10"))
        self.assertTrue(windows["I2"][0].startswith("2026-09-20"))
        self.assertTrue(windows["I3"][1].startswith("2026-09-27"))

    def test_pre_race_item_redeemable_during_pre_window(self):
        r = redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00",
                   [P1[0] | {"entries": [{"item_id": "I1", "amount_cents": 100}]}])
        self.assertTrue(r["redemption_id"])

    def test_post_race_item_closed_before_race(self):
        with self.assertRaises(WindowClosed):
            redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00",
                   [{"merchant_id": "M1", "store_id": "S1",
                     "entries": [{"item_id": "I3", "amount_cents": 100}]}])

    def test_pre_race_item_closed_after_window(self):
        with self.assertRaises(WindowClosed):
            redeem(self.app, "QR-1", "2026-09-21T08:00:00+08:00",
                   [{"merchant_id": "M1", "store_id": "S1",
                     "entries": [{"item_id": "I1", "amount_cents": 100}]}])

    def test_offline_uses_scan_time_not_upload_time(self):
        # 扫码发生在赛中窗口，补传在赛后窗口——以扫码时刻判定，应成功
        r = redeem(self.app, "QR-1", "2026-09-20T09:00:00+08:00",
                   [{"merchant_id": "M2", "store_id": "S9",
                     "entries": [{"item_id": "I2", "amount_cents": 1000}]}],
                   offline=True)
        self.assertTrue(r["offline"])

    def test_offline_scan_outside_window_rejected_on_upload(self):
        with self.assertRaises(WindowClosed):
            redeem(self.app, "QR-1", "2026-09-10T08:00:00+08:00",
                   [{"merchant_id": "M2", "store_id": "S9",
                     "entries": [{"item_id": "I2", "amount_cents": 10}]}],
                   offline=True)


class RedemptionIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.app = seed_world(build_app())
        issue_standard_bundle(self.app)

    def test_rescan_with_same_request_id_replays(self):
        r1 = redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00", P1,
                    client_request_id="req-1")
        r2 = redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00", P1,
                    client_request_id="req-1")
        self.assertEqual(r1, r2)
        world = self.app.service._world()
        self.assertEqual(len(world.redemptions), 1)

    def test_rescan_with_new_request_id_rejected(self):
        redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00", P1,
               client_request_id="req-1")
        with self.assertRaises(DuplicateRedemption):
            redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00", P1,
                   client_request_id="req-2")

    def test_distinct_scan_codes_do_not_clash(self):
        redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00",
               [{"merchant_id": "M1", "store_id": "S1",
                 "entries": [{"item_id": "I1", "amount_cents": 100}]}])
        redeem(self.app, "QR-2", "2026-09-15T11:00:00+08:00",
               [{"merchant_id": "M1", "store_id": "S1",
                 "entries": [{"item_id": "I1", "amount_cents": 100}]}])
        self.assertEqual(len(self.app.service._world().redemptions), 2)

    def test_cross_store_split_consumes_once_each(self):
        r = redeem(self.app, "QR-1", "2026-09-15T10:00:00+08:00", P1)
        self.assertEqual(len(r["parts"]), 2)
        b = self.app.views.spectator_bundle(Principal.spectator("U1"), "B1")
        i1 = next(i for i in b["items"] if i["item_id"] == "I1")
        self.assertEqual(i1["consumed_cents"], 20000)
        self.assertEqual(i1["available_cents"], 0)
        self.assertEqual(i1["status"], "REDEEMED")

    def test_offline_double_spend_rejected_at_upload(self):
        redeem(self.app, "QR-1", "2026-09-20T09:00:00+08:00",
               [{"merchant_id": "M2", "store_id": "S9",
                 "entries": [{"item_id": "I2", "amount_cents": 600}]}], offline=True)
        with self.assertRaises(InsufficientBalance):
            redeem(self.app, "QR-2", "2026-09-20T09:05:00+08:00",
                   [{"merchant_id": "M2", "store_id": "S9",
                     "entries": [{"item_id": "I2", "amount_cents": 500}]}], offline=True)

    def test_redeem_redeemed_item_rejected(self):
        redeem(self.app, "QR-1", "2026-09-20T09:00:00+08:00",
               [{"merchant_id": "M2", "store_id": "S9",
                 "entries": [{"item_id": "I2", "amount_cents": 1000}]}])
        with self.assertRaises(IllegalState):
            redeem(self.app, "QR-2", "2026-09-20T09:05:00+08:00",
                   [{"merchant_id": "M2", "store_id": "S9",
                     "entries": [{"item_id": "I2", "amount_cents": 10}]}])

    def test_item_bound_to_merchant_cannot_redeem_elsewhere(self):
        issue_standard_bundle(self.app, bundle_id="B2", spectator="U2", items=[
            {"item_id": "J1", "phase": "PRE_RACE", "title": "专属券",
             "face_value_cents": 1000, "subsidy_cents": 0, "merchant_id": "M1"}])
        with self.assertRaises(IllegalState):
            self.app.service.record_redemption(
                bundle_id="B2", scan_code_id="QX", scanned_at="2026-09-15T10:00:00+08:00",
                parts=[{"merchant_id": "M2", "store_id": "S9",
                        "entries": [{"item_id": "J1", "amount_cents": 10}]}])

    def test_merchant_without_contract_cannot_redeem(self):
        self.app.service.register_merchant("M9", "无合同商户")
        with self.assertRaises(Exception):
            redeem(self.app, "QR-9", "2026-09-15T10:00:00+08:00",
                   [{"merchant_id": "M9", "store_id": "S9",
                     "entries": [{"item_id": "I1", "amount_cents": 10}]}])


if __name__ == "__main__":
    unittest.main()
