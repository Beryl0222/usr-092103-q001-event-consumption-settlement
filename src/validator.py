"""校验领域事件公共字段。

``validate_event`` 保持起点契约行为：只校验必填字段与 version 正整数。
``validate_envelope`` 在此基础上按事件目录校验 event_type、aggregate_type
及其归属（起点样例 BUNDLE_ISSUED 挂 event_edition 的兼容用法除外）。
"""

from __future__ import annotations

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id",
            "occurred_at", "version", "summary")

#: 事件类型 -> 规范聚合类型
EVENT_AGGREGATE = {
    "EDITION_REGISTERED": "event_edition",
    "EDITION_RESCHEDULED": "event_edition",
    "MERCHANT_REGISTERED": "merchant",
    "CONTRACT_VERSION_EFFECTIVE": "merchant_contract",
    "BUNDLE_ISSUED": "benefit_bundle",
    "BUNDLE_MIGRATED": "benefit_bundle",
    "REDEMPTION_RECORDED": "redemption_record",
    "REFUND_REQUESTED": "refund",
    "REFUND_CONFIRMED": "refund",
    "SETTLEMENT_BATCH_OPENED": "settlement_batch",
    "SETTLEMENT_FROZEN": "settlement_batch",
    "SETTLEMENT_PAID": "settlement_batch",
    "ADJUSTMENT_POSTED": "settlement_batch",
}

#: 起点契约登记、向后兼容的事件/聚合集合
LEGACY_EVENT_TYPES = {
    "BUNDLE_ISSUED", "REDEMPTION_RECORDED", "REFUND_CONFIRMED",
    "SETTLEMENT_FROZEN", "ADJUSTMENT_POSTED",
}
LEGACY_AGGREGATE_TYPES = {
    "event_edition", "benefit_bundle", "redemption_record", "settlement_batch",
}

#: 起点样例允许的归属覆盖：BUNDLE_ISSUED 可挂在 event_edition 上
LEGACY_OVERRIDE = {("BUNDLE_ISSUED", "event_edition")}


def validate_event(record: dict) -> list[str]:
    """公共必填字段与 version 校验（与起点契约一致）。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int)
                                or isinstance(record["version"], bool)
                                or record["version"] < 1):
        errors.append("version 必须是正整数")
    return errors


def validate_envelope(record: dict) -> list[str]:
    """在公共字段校验之上，追加事件目录与归属校验。"""
    errors = validate_event(record)
    event_type = record.get("event_type")
    agg_type = record.get("aggregate_type")
    if event_type is not None and event_type not in EVENT_AGGREGATE:
        errors.append(f"未知事件类型：{event_type}")
    if agg_type is not None and (
        agg_type not in EVENT_AGGREGATE.values()
        and agg_type not in LEGACY_AGGREGATE_TYPES
    ):
        errors.append(f"未知聚合类型：{agg_type}")
    if event_type in EVENT_AGGREGATE and agg_type:
        expected = EVENT_AGGREGATE[event_type]
        if agg_type != expected and (event_type, agg_type) not in LEGACY_OVERRIDE:
            errors.append(
                f"事件 {event_type} 应归属聚合 {expected}，实际为 {agg_type}")
    return errors
