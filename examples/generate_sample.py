"""生成完整生命周期的中文联调样例事件流，写入 data/sample_lifecycle.json。

用法：
    python3 examples/generate_sample.py

样例串起：届次登记 → 商户/合同 → 发券 → 跨店拆单核销 →
9 月批次冻结/支付 → 赛后退款 → 10 月批次冲正（旧账不改写）。
事件 ID 使用进程内顺序计数器，保证样例可复现、便于人工核对版本链。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.settlement import App  # noqa: E402

_counter = {"n": 0}


def _seq_uuid():
    _counter["n"] += 1
    n = _counter["n"]
    # 计数放在前 16 位，保证 _new_id 截取 hex[:16] 仍唯一
    return type("U", (), {"hex": f"{n:016x}" + "0" * 16})()


def build() -> App:
    uuid.uuid4 = _seq_uuid  # 仅样例：确定性 ID
    app = App(":memory:")
    s = app.service

    s.register_edition(
        "ED-2026-CITYRUN", "2026 城市路跑",
        race_start="2026-09-20T07:00:00+08:00",
        race_end="2026-09-21T12:00:00+08:00",
        pre_start="2026-09-10T00:00:00+08:00",
        post_end="2026-09-27T23:59:59+08:00")

    s.register_merchant("M-GOODS", "场内特许商", tax_withholding_bps=600)
    s.register_merchant("M-PLAZA", "商圈第二现场", tax_withholding_bps=600)

    # 特许商：阶梯分成 80%/90%，平台抽佣 5%，月保底 500 元
    s.effective_contract_version(
        "C-GOODS", merchant_id="M-GOODS", edition_id="ED-2026-CITYRUN",
        terms={"mode": "TIERED_SHARE", "platform_fee_bps": 500,
               "tiers": [{"up_to_cents": 100000, "share_bps": 8000},
                         {"up_to_cents": 10_000_000, "share_bps": 9000}],
               "monthly_guarantee_cents": 50000},
        effective_from="2026-09-01T00:00:00+08:00")
    # 商圈：每笔固定结算 40 元
    s.effective_contract_version(
        "C-PLAZA", merchant_id="M-PLAZA", edition_id="ED-2026-CITYRUN",
        terms={"mode": "FIXED", "fixed_cents": 4000},
        effective_from="2026-09-01T00:00:00+08:00")

    # 游客 U-1001 的权益包：赛前套餐 200 元（自付 150+平台补贴 50）
    s.issue_bundle(
        "B-1001", spectator_id="U-1001", edition_id="ED-2026-CITYRUN",
        items=[{"item_id": "I-PRE-PACKAGE", "phase": "PRE_RACE",
                "title": "赛前套餐券", "face_value_cents": 20000,
                "subsidy_cents": 5000, "spectator_paid_cents": 15000}])

    # 9.15 一笔二维码在两家店跨店拆单核销（120 元特许商 + 80 元商圈）
    s.record_redemption(
        bundle_id="B-1001", scan_code_id="QR-20260915-0007",
        scanned_at="2026-09-15T10:32:00+08:00",
        parts=[
            {"merchant_id": "M-GOODS", "store_id": "STORE-G01",
             "entries": [{"item_id": "I-PRE-PACKAGE", "amount_cents": 12000}]},
            {"merchant_id": "M-PLAZA", "store_id": "STORE-P07",
             "entries": [{"item_id": "I-PRE-PACKAGE", "amount_cents": 8000}]},
        ], client_request_id="scan-20260915-0007")

    # 9 月批次冻结并支付
    s.open_batch("BAT-2026-09", edition_id="ED-2026-CITYRUN",
                 period_start="2026-09-01T00:00:00+08:00",
                 period_end="2026-09-30T23:59:59+08:00")
    s.freeze_batch("BAT-2026-09")
    s.mark_batch_paid("BAT-2026-09")

    # 赛后认定特许商部分未履约，游客退 60 元
    rf = s.request_refund(
        bundle_id="B-1001", reason="MERCHANT_NONPERFORMANCE",
        items=[{"item_id": "I-PRE-PACKAGE", "amount_cents": 6000}],
        note="赛前套餐中两项预约服务未提供")
    s.confirm_refund(rf["refund_id"])

    # 10 月批次只登记对旧账的冲正，不改写 9 月批次
    s.open_batch("BAT-2026-10", edition_id="ED-2026-CITYRUN",
                 period_start="2026-10-01T00:00:00+08:00",
                 period_end="2026-10-31T23:59:59+08:00")
    s.freeze_batch("BAT-2026-10")
    return app


def main() -> None:
    app = build()
    events = [e.as_dict() for e in app.store.load_all()]
    out = Path(__file__).resolve().parents[1] / "data" / "sample_lifecycle.json"
    out.write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写出 {len(events)} 条事件到 {out}")
    print("哈希链校验：", app.store.verify_chain())


if __name__ == "__main__":
    main()
