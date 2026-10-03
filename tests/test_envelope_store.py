"""事件信封、只追加存储、幂等与哈希链测试。"""

import json
import sqlite3
import unittest

from src.settlement.envelope import (
    BATCH,
    BUNDLE_ISSUED,
    EDITION,
    REDEMPTION_RECORDED,
    make_event,
)
from src.settlement.store import EventStore, hash_event, GENESIS_HASH


def _evt(event_id="evt-1", event_type=BUNDLE_ISSUED, agg="a1", version=1,
         aggregate_type=None):
    return make_event(event_type, agg, version, "测试", {"k": 1},
                      event_id=event_id, occurred_at="2026-09-20T10:00:00+08:00",
                      aggregate_type=aggregate_type)


class EnvelopeTest(unittest.TestCase):
    def test_envelope_fields_match_contract(self):
        e = _evt()
        d = e.as_dict()
        for field in ("event_id", "event_type", "aggregate_type", "aggregate_id",
                      "occurred_at", "version", "summary"):
            self.assertIn(field, d)
        self.assertEqual(d["payload"], {"k": 1})

    def test_aggregate_type_inferred_from_catalog(self):
        self.assertEqual(_evt(event_type=REDEMPTION_RECORDED).aggregate_type, "redemption_record")

    def test_legacy_override_allowed(self):
        # 起点样例允许 BUNDLE_ISSUED 挂在 event_edition 上
        e = _evt(aggregate_type=EDITION)
        self.assertEqual(e.aggregate_type, EDITION)

    def test_version_must_be_positive_int(self):
        with self.assertRaises(ValueError):
            make_event(BUNDLE_ISSUED, "a", 0, "x", {}, event_id="e",
                       occurred_at="2026-09-20T10:00:00+08:00")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_append_and_replay(self):
        self.store.append([_evt()])
        stream = self.store.load_stream("a1")
        self.assertEqual(len(stream), 1)
        self.assertEqual(stream[0].event_id, "evt-1")

    def test_version_must_be_sequential(self):
        self.store.append([_evt(version=1)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append([_evt(event_id="evt-2", version=3)])

    def test_event_id_unique(self):
        self.store.append([_evt(event_id="dup")])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append([_evt(event_id="dup", agg="a2")])

    def test_idempotency_returns_first_result(self):
        r1 = self.store.append([_evt(event_id="e1")],
                               client_request_id="req-1", result={"x": 1})
        self.assertEqual(len(r1), 1)
        cached = self.store.append([_evt(event_id="e2")],
                                   client_request_id="req-1", result={"x": 2})
        self.assertEqual(cached, {"x": 1})
        # 没有产生第二条事件
        self.assertEqual(len(self.store.load_all()), 1)

    def test_hash_chain_detects_tampering(self):
        self.store.append([_evt(event_id="e1", version=1)])
        self.store.append([_evt(event_id="e2", agg="a2", version=1)])
        self.assertTrue(self.store.verify_chain())
        # 直接篡改旧账
        self.store._conn.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (json.dumps({"k": 999}), "e1"))
        self.store._conn.commit()
        self.assertFalse(self.store.verify_chain())


if __name__ == "__main__":
    unittest.main()
