"""合同生效版本、固定额、阶梯分成、保底条款测试。"""

import unittest

from src.settlement.contracts_engine import price_part, validate_terms
from src.settlement.errors import ContractError


TIERED = {"mode": "TIERED_SHARE", "platform_fee_bps": 1000,
          "tiers": [{"up_to_cents": 100000, "share_bps": 8000},
                    {"up_to_cents": 10_000_000, "share_bps": 9000}],
          "monthly_guarantee_cents": 50000}


class PricingTest(unittest.TestCase):
    def test_fixed_term(self):
        p = price_part({"mode": "FIXED", "fixed_cents": 4000},
                       gross_cents=8600, cumulative_gross_cents=0, version_no=2)
        self.assertEqual(p.merchant_cents, 4000)
        self.assertEqual(p.platform_fee_cents, 4600)
        self.assertEqual(p.version_no, 2)

    def test_tier_chosen_by_cumulative_gross(self):
        # 累计未超 100000，落第一档 80%
        p1 = price_part(TIERED, gross_cents=60000, cumulative_gross_cents=0, version_no=1)
        self.assertEqual(p1.share_bps, 8000)
        self.assertEqual(p1.merchant_cents, 48000)
        # 本笔跨越后累计 140000，落第二档 90%
        p2 = price_part(TIERED, gross_cents=80000, cumulative_gross_cents=60000, version_no=1)
        self.assertEqual(p2.share_bps, 9000)
        self.assertEqual(p2.merchant_cents, 72000)

    def test_fee_and_share_cannot_exceed_gross(self):
        bad = {"mode": "TIERED_SHARE", "platform_fee_bps": 5000,
               "tiers": [{"up_to_cents": 100000, "share_bps": 8000}]}
        with self.assertRaises(ContractError):
            price_part(bad, gross_cents=10000, cumulative_gross_cents=0, version_no=1)

    def test_validate_terms_rejects_bad_tiers(self):
        with self.assertRaises(ContractError):
            validate_terms({"mode": "TIERED_SHARE",
                            "tiers": [{"up_to_cents": 100, "share_bps": 99999}]})
        with self.assertRaises(ContractError):
            validate_terms({"mode": "TIERED_SHARE",
                            "tiers": [{"up_to_cents": 100, "share_bps": 8000},
                                      {"up_to_cents": 50, "share_bps": 9000}]})
        with self.assertRaises(ContractError):
            validate_terms({"mode": "UNKNOWN"})

    def test_nonpositive_gross_rejected(self):
        with self.assertRaises(ContractError):
            price_part({"mode": "FIXED", "fixed_cents": 1},
                       gross_cents=0, cumulative_gross_cents=0, version_no=1)


class EffectiveVersionTest(unittest.TestCase):
    def test_version_selected_by_scan_time(self):
        from scenarios import build_app, seed_world
        app = seed_world(build_app())
        # 9.25 起新版本：固定额改为 9000
        app.service.effective_contract_version(
            "C-M2", merchant_id="M2", edition_id="ED1",
            terms={"mode": "FIXED", "fixed_cents": 9000},
            effective_from="2026-09-25T00:00:00+08:00")
        from scenarios import issue_standard_bundle
        issue_standard_bundle(app)
        # 9.15 扫码适用旧版
        app.service.record_redemption(
            bundle_id="B1", scan_code_id="QR-OLD", scanned_at="2026-09-15T10:00:00+08:00",
            parts=[{"merchant_id": "M2", "store_id": "S9",
                    "entries": [{"item_id": "I1", "amount_cents": 8000}]}])
        # 9.26 扫码适用新版；给 B2 的 I1 显式窗口覆盖 9.26
        app.service.issue_bundle("B2", spectator_id="U2", edition_id="ED1", items=[
            {"item_id": "I1", "phase": "PRE_RACE", "title": "套餐",
             "face_value_cents": 20000, "subsidy_cents": 5000,
             "window_start": "2026-09-01T00:00:00+08:00",
             "window_end": "2026-09-30T23:59:59+08:00"}])
        app.service.record_redemption(
            bundle_id="B2", scan_code_id="QR-NEW", scanned_at="2026-09-26T10:00:00+08:00",
            parts=[{"merchant_id": "M2", "store_id": "S9",
                    "entries": [{"item_id": "I1", "amount_cents": 10000}]}])
        app.service.open_batch("BAT1", edition_id="ED1",
                               period_start="2026-09-01T00:00:00+08:00",
                               period_end="2026-09-30T23:59:59+08:00")
        f = app.service.freeze_batch("BAT1")
        m2 = [ln for ln in f["lines"] if ln["merchant_id"] == "M2" and ln["kind"] == "CONSUMPTION"]
        amounts = {(ln["contract_version_no"], ln["amount_cents"], ln["detail"]["scanned_at"][:10])
                   for ln in m2}
        self.assertIn((1, 4000, "2026-09-15"), amounts)
        self.assertIn((2, 9000, "2026-09-26"), amounts)

    def test_versions_must_be_chronological(self):
        from scenarios import build_app, seed_world
        app = seed_world(build_app())
        with self.assertRaises(ContractError):
            app.service.effective_contract_version(
                "C-M2", merchant_id="M2", edition_id="ED1",
                terms={"mode": "FIXED", "fixed_cents": 1},
                effective_from="2026-08-01T00:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
