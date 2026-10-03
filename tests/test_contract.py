import json
import unittest
from pathlib import Path

from src.validator import validate_envelope, validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_legacy_sample_bundle_on_edition_accepted(self) -> None:
        # 起点样例：BUNDLE_ISSUED 挂在 event_edition 上（兼容）
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_envelope(sample), [])

    def test_lifecycle_sample_events_valid(self) -> None:
        events = json.loads(
            (ROOT / "data" / "sample_lifecycle.json").read_text(encoding="utf-8"))
        self.assertGreater(len(events), 10)
        for event in events:
            self.assertEqual(validate_envelope(event), [], msg=event["event_id"])
        # 版本在每个聚合内从 1 连续递增
        versions: dict[str, list[int]] = {}
        for e in events:
            versions.setdefault(e["aggregate_id"], []).append(e["version"])
        for agg_id, vs in versions.items():
            self.assertEqual(vs, list(range(1, len(vs) + 1)), msg=agg_id)

    def test_unknown_event_type_rejected(self) -> None:
        errors = validate_envelope({
            "event_id": "x", "event_type": "NOPE", "aggregate_type": "merchant",
            "aggregate_id": "m", "occurred_at": "2026-09-01T00:00:00+08:00",
            "version": 1, "summary": "x"})
        self.assertTrue(any("未知事件类型" in e for e in errors))

    def test_wrong_aggregate_ownership_rejected(self) -> None:
        errors = validate_envelope({
            "event_id": "x", "event_type": "REFUND_CONFIRMED",
            "aggregate_type": "merchant", "aggregate_id": "m",
            "occurred_at": "2026-09-01T00:00:00+08:00",
            "version": 1, "summary": "x"})
        self.assertTrue(any("应归属聚合" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
