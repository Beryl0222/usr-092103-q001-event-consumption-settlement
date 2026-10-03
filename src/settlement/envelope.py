"""领域事件信封与事件目录。

信封字段与 ``contracts/domain.schema.json`` 完全一致：
event_id / event_type / aggregate_type / aggregate_id /
occurred_at / version / summary，业务数据统一放在 payload。

事件目录在起点五个事件之上扩展，新增事件仍使用同一信封；
旧样例（BUNDLE_ISSUED 挂在 event_edition 聚合上）继续有效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ---- 聚合类型 ---------------------------------------------------------

EDITION = "event_edition"
MERCHANT = "merchant"
CONTRACT = "merchant_contract"
BUNDLE = "benefit_bundle"
REDEMPTION = "redemption_record"
REFUND = "refund"
BATCH = "settlement_batch"

AGGREGATE_TYPES = (
    EDITION,
    MERCHANT,
    CONTRACT,
    BUNDLE,
    REDEMPTION,
    REFUND,
    BATCH,
)

# ---- 事件类型 ---------------------------------------------------------

EDITION_REGISTERED = "EDITION_REGISTERED"
EDITION_RESCHEDULED = "EDITION_RESCHEDULED"
MERCHANT_REGISTERED = "MERCHANT_REGISTERED"
CONTRACT_VERSION_EFFECTIVE = "CONTRACT_VERSION_EFFECTIVE"
BUNDLE_ISSUED = "BUNDLE_ISSUED"
REDEMPTION_RECORDED = "REDEMPTION_RECORDED"
REFUND_REQUESTED = "REFUND_REQUESTED"
REFUND_CONFIRMED = "REFUND_CONFIRMED"
BUNDLE_MIGRATED = "BUNDLE_MIGRATED"
SETTLEMENT_BATCH_OPENED = "SETTLEMENT_BATCH_OPENED"
SETTLEMENT_FROZEN = "SETTLEMENT_FROZEN"
SETTLEMENT_PAID = "SETTLEMENT_PAID"
ADJUSTMENT_POSTED = "ADJUSTMENT_POSTED"

#: 事件目录：事件类型 -> 所属聚合类型。
EVENT_AGGREGATE: dict[str, str] = {
    EDITION_REGISTERED: EDITION,
    EDITION_RESCHEDULED: EDITION,
    MERCHANT_REGISTERED: MERCHANT,
    CONTRACT_VERSION_EFFECTIVE: CONTRACT,
    BUNDLE_ISSUED: BUNDLE,
    BUNDLE_MIGRATED: BUNDLE,
    REDEMPTION_RECORDED: REDEMPTION,
    REFUND_REQUESTED: REFUND,
    REFUND_CONFIRMED: REFUND,
    SETTLEMENT_BATCH_OPENED: BATCH,
    SETTLEMENT_FROZEN: BATCH,
    SETTLEMENT_PAID: BATCH,
    ADJUSTMENT_POSTED: BATCH,
}

EVENT_TYPES = tuple(EVENT_AGGREGATE)

#: 起点契约中已登记的五个事件（保持兼容）。
LEGACY_EVENT_TYPES = (
    BUNDLE_ISSUED,
    REDEMPTION_RECORDED,
    REFUND_CONFIRMED,
    SETTLEMENT_FROZEN,
    ADJUSTMENT_POSTED,
)
#: 起点契约中已登记的聚合（保持兼容）。
LEGACY_AGGREGATE_TYPES = (EDITION, BUNDLE, REDEMPTION, BATCH)


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    summary: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
        }


def make_event(
    event_type: str,
    aggregate_id: str,
    version: int,
    summary: str,
    payload: dict[str, Any],
    *,
    event_id: str,
    occurred_at: datetime | str,
    aggregate_type: str | None = None,
) -> Event:
    """构造一个符合公共信封的领域事件。

    聚合类型默认按事件目录推断；起点样例允许 BUNDLE_ISSUED 挂在
    event_edition 上，因此 aggregate_type 可显式覆盖。
    """
    if event_type not in EVENT_AGGREGATE:
        raise ValueError(f"未知事件类型：{event_type}")
    agg_type = aggregate_type or EVENT_AGGREGATE[event_type]
    if agg_type not in AGGREGATE_TYPES:
        raise ValueError(f"未知聚合类型：{agg_type}")
    if not isinstance(version, int) or version < 1:
        raise ValueError("version 必须是正整数")
    if isinstance(occurred_at, datetime):
        occurred_at = occurred_at.isoformat()
    return Event(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=agg_type,
        aggregate_id=aggregate_id,
        occurred_at=occurred_at,
        version=version,
        summary=summary,
        payload=payload,
    )
