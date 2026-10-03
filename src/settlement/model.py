"""事件溯源的状态模型与折叠（fold）逻辑。

写侧只追加事件；本模块把全量事件流重放成当前状态，供领域服务做
不变量校验。跨聚合的影响（核销消耗权益项、退款冲回权益项）也由
统一折叠器处理：例如 REDEMPTION_RECORDED 属于 redemption_record
聚合，但会同时更新对应 benefit_bundle 中权益项的余量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .envelope import (
    ADJUSTMENT_POSTED,
    BATCH,
    BUNDLE_ISSUED,
    BUNDLE_MIGRATED,
    CONTRACT_VERSION_EFFECTIVE,
    EDITION_REGISTERED,
    EDITION_RESCHEDULED,
    Event,
    MERCHANT_REGISTERED,
    REDEMPTION_RECORDED,
    REFUND_CONFIRMED,
    REFUND_REQUESTED,
    SETTLEMENT_BATCH_OPENED,
    SETTLEMENT_FROZEN,
    SETTLEMENT_PAID,
)
from .times import parse_at

# 权益项状态
AVAILABLE = "AVAILABLE"          # 可用（含部分核销后的剩余）
REDEEMED = "REDEEMED"            # 已全部核销
REFUND_PENDING = "REFUND_PENDING"  # 退款处理中
REFUNDED = "REFUNDED"            # 已退款
CANCELLED = "CANCELLED"          # 改期后无法履约而取消，待退款

# 批次状态
BATCH_OPEN = "OPEN"
BATCH_FROZEN = "FROZEN"
BATCH_PAID = "PAID"

# 退款原因 / 状态
REASON_SPECTATOR_CANCEL = "SPECTATOR_CANCELLATION"
REASON_RACE_RESCHEDULE = "RACE_RESCHEDULE"
REASON_MERCHANT_NONPERFORMANCE = "MERCHANT_NONPERFORMANCE"
REFUND_REQUESTED_STATE = "REQUESTED"
REFUND_CONFIRMED_STATE = "CONFIRMED"
REFUND_REJECTED_STATE = "REJECTED"

# 合同计费模式
TERM_FIXED = "FIXED"                # 每笔核销固定额
TERM_TIERED_SHARE = "TIERED_SHARE"  # 阶梯分成
# 调整类型
ADJ_REVERSAL = "REVERSAL"           # 冲正（负向）
ADJ_TOPUP = "TOPUP"                 # 补付（正向）


@dataclass
class EditionState:
    edition_id: str
    name: str
    race_start: Any
    race_end: Any
    pre_start: Any
    post_end: Any
    status: str = "SCHEDULED"
    reschedules: list[dict] = field(default_factory=list)

    def phase_window(self, phase: str) -> tuple[Any, Any]:
        if phase == "PRE_RACE":
            return self.pre_start, self.race_start
        if phase == "RACE_DAY":
            return self.race_start, self.race_end
        return self.race_end, self.post_end  # POST_RACE


@dataclass
class MerchantState:
    merchant_id: str
    name: str
    tax_withholding_bps: int
    contracts: list[str] = field(default_factory=list)


@dataclass
class ContractVersion:
    version_no: int
    effective_from: str
    terms: dict
    note: str = ""


@dataclass
class ContractState:
    contract_id: str
    merchant_id: str
    edition_id: str
    versions: list[ContractVersion] = field(default_factory=list)

    def version_on(self, moment: Any) -> Optional[ContractVersion]:
        """返回某一业务时点生效的合同版本（生效日期 <= 该时点的最新版）。"""
        moment = parse_at(moment)
        chosen = None
        for v in self.versions:
            if parse_at(v.effective_from) <= moment:
                chosen = v
        return chosen

    def latest(self) -> Optional[ContractVersion]:
        return self.versions[-1] if self.versions else None


@dataclass
class ItemConsumption:
    redemption_id: str
    part_index: int
    store_id: str
    amount_cents: int
    scanned_at: str


@dataclass
class BenefitItem:
    item_id: str
    phase: str
    title: str
    face_value_cents: int
    subsidy_cents: int
    spectator_paid_cents: int
    merchant_id: Optional[str]
    window_start: str
    window_end: str
    status: str = AVAILABLE
    consumed_cents: int = 0
    consumptions: list[ItemConsumption] = field(default_factory=list)
    refund_id: Optional[str] = None
    migrated: bool = False


@dataclass
class BundleState:
    bundle_id: str
    spectator_id: str
    edition_id: str
    issued_at: str
    items: dict[str, BenefitItem] = field(default_factory=dict)
    status: str = "ACTIVE"


@dataclass
class RedemptionPart:
    part_index: int
    merchant_id: str
    store_id: str
    entries: list[dict]  # [{item_id, amount_cents}]
    gross_cents: int


@dataclass
class RedemptionState:
    redemption_id: str
    bundle_id: str
    spectator_id: str
    edition_id: str
    scan_code_id: str
    scanned_at: str
    recorded_at: str
    offline: bool
    parts: list[RedemptionPart] = field(default_factory=list)
    reversed_parts: set[tuple[str, int]] = field(default_factory=set)


@dataclass
class RefundItem:
    item_id: str
    amount_cents: int


@dataclass
class RefundState:
    refund_id: str
    bundle_id: str
    spectator_id: str
    edition_id: str
    reason: str
    items: list[RefundItem]
    requested_at: str
    status: str = REFUND_REQUESTED_STATE
    confirmed_at: Optional[str] = None
    spectator_amount_cents: int = 0
    note: str = ""


@dataclass
class SettlementLine:
    line_id: str
    merchant_id: str
    kind: str  # CONSUMPTION / GUARANTEE_TOPUP / REVERSAL / TOPUP / TAX
    amount_cents: int
    tax_cents: int = 0
    contract_version_no: Optional[int] = None
    redemption_id: Optional[str] = None
    part_index: Optional[int] = None
    refund_id: Optional[str] = None
    source_batch_id: Optional[str] = None
    source_line_id: Optional[str] = None
    detail: dict = field(default_factory=dict)


@dataclass
class BatchState:
    batch_id: str
    edition_id: str
    period_start: str
    period_end: str
    opened_at: str
    status: str = BATCH_OPEN
    frozen_at: Optional[str] = None
    paid_at: Optional[str] = None
    lines: list[SettlementLine] = field(default_factory=list)
    included_parts: set[tuple[str, int]] = field(default_factory=set)
    adjusted_parts: set[tuple[str, int]] = field(default_factory=set)
    totals: dict = field(default_factory=dict)


@dataclass
class World:
    editions: dict[str, EditionState] = field(default_factory=dict)
    merchants: dict[str, MerchantState] = field(default_factory=dict)
    contracts: dict[str, ContractState] = field(default_factory=dict)
    bundles: dict[str, BundleState] = field(default_factory=dict)
    redemptions: dict[str, RedemptionState] = field(default_factory=dict)
    refunds: dict[str, RefundState] = field(default_factory=dict)
    batches: dict[str, BatchState] = field(default_factory=dict)
    # 索引
    scan_index: dict[str, str] = field(default_factory=dict)
    contract_index: dict[tuple[str, str], str] = field(default_factory=dict)

    def item(self, bundle_id: str, item_id: str) -> BenefitItem:
        return self.bundles[bundle_id].items[item_id]

    def settled_parts(self) -> dict[tuple[str, int], str]:
        """已冻结批次中的核销分项 -> batch_id。"""
        out: dict[tuple[str, int], str] = {}
        for b in self.batches.values():
            if b.status in (BATCH_FROZEN, BATCH_PAID):
                for key in b.included_parts:
                    out[key] = b.batch_id
        return out

    def reversed_parts(self) -> set[tuple[str, int]]:
        out: set[tuple[str, int]] = set()
        for b in self.batches.values():
            if b.status in (BATCH_FROZEN, BATCH_PAID):
                out |= b.adjusted_parts
        return out

    def contract_for(self, merchant_id: str, edition_id: str) -> Optional[ContractState]:
        cid = self.contract_index.get((merchant_id, edition_id))
        return self.contracts.get(cid) if cid else None


def fold(events: list[Event]) -> World:
    world = World()
    for e in events:
        p = e.payload
        t = e.event_type
        if t == EDITION_REGISTERED:
            world.editions[e.aggregate_id] = EditionState(
                edition_id=e.aggregate_id,
                name=p["name"],
                race_start=parse_at(p["race_start"]),
                race_end=parse_at(p["race_end"]),
                pre_start=parse_at(p["pre_start"]),
                post_end=parse_at(p["post_end"]),
            )
        elif t == EDITION_RESCHEDULED:
            ed = world.editions[e.aggregate_id]
            ed.race_start = parse_at(p["new_race_start"])
            ed.race_end = parse_at(p["new_race_end"])
            ed.pre_start = parse_at(p["new_pre_start"])
            ed.post_end = parse_at(p["new_post_end"])
            ed.status = "RESCHEDULED"
            ed.reschedules.append(
                {"at": e.occurred_at, "delta_days": p["delta_days"], "reason": p.get("reason", "")}
            )
        elif t == MERCHANT_REGISTERED:
            world.merchants[e.aggregate_id] = MerchantState(
                merchant_id=e.aggregate_id,
                name=p["name"],
                tax_withholding_bps=p.get("tax_withholding_bps", 0),
            )
        elif t == CONTRACT_VERSION_EFFECTIVE:
            cid = e.aggregate_id
            contract = world.contracts.get(cid)
            if contract is None:
                contract = ContractState(
                    contract_id=cid,
                    merchant_id=p["merchant_id"],
                    edition_id=p["edition_id"],
                )
                world.contracts[cid] = contract
                world.contract_index[(p["merchant_id"], p["edition_id"])] = cid
                if cid not in world.merchants[p["merchant_id"]].contracts:
                    world.merchants[p["merchant_id"]].contracts.append(cid)
            contract.versions.append(
                ContractVersion(
                    version_no=p["version_no"],
                    effective_from=p["effective_from"],
                    terms=p["terms"],
                    note=p.get("note", ""),
                )
            )
        elif t == BUNDLE_ISSUED:
            b = BundleState(
                bundle_id=e.aggregate_id,
                spectator_id=p["spectator_id"],
                edition_id=p["edition_id"],
                issued_at=e.occurred_at,
            )
            for it in p["items"]:
                b.items[it["item_id"]] = BenefitItem(
                    item_id=it["item_id"],
                    phase=it["phase"],
                    title=it["title"],
                    face_value_cents=it["face_value_cents"],
                    subsidy_cents=it["subsidy_cents"],
                    spectator_paid_cents=it["spectator_paid_cents"],
                    merchant_id=it.get("merchant_id"),
                    window_start=it["window_start"],
                    window_end=it["window_end"],
                )
            world.bundles[b.bundle_id] = b
        elif t == BUNDLE_MIGRATED:
            b = world.bundles[e.aggregate_id]
            b.edition_id = p["new_edition_id"]
            for m in p["migrated"]:
                it = b.items[m["item_id"]]
                it.window_start = m["new_window_start"]
                it.window_end = m["new_window_end"]
                it.migrated = True
            for c in p.get("cancelled", []):
                it = b.items[c["item_id"]]
                it.status = CANCELLED
                it.refund_id = c.get("refund_id")
        elif t == REDEMPTION_RECORDED:
            r = RedemptionState(
                redemption_id=e.aggregate_id,
                bundle_id=p["bundle_id"],
                spectator_id=p["spectator_id"],
                edition_id=p["edition_id"],
                scan_code_id=p["scan_code_id"],
                scanned_at=p["scanned_at"],
                recorded_at=e.occurred_at,
                offline=p.get("offline", False),
            )
            for part in p["parts"]:
                r.parts.append(
                    RedemptionPart(
                        part_index=part["part_index"],
                        merchant_id=part["merchant_id"],
                        store_id=part["store_id"],
                        entries=part["entries"],
                        gross_cents=part["gross_cents"],
                    )
                )
                for en in part["entries"]:
                    item = world.bundles[p["bundle_id"]].items[en["item_id"]]
                    item.consumed_cents += en["amount_cents"]
                    item.consumptions.append(
                        ItemConsumption(
                            redemption_id=e.aggregate_id,
                            part_index=part["part_index"],
                            store_id=part["store_id"],
                            amount_cents=en["amount_cents"],
                            scanned_at=p["scanned_at"],
                        )
                    )
                    if item.consumed_cents >= item.face_value_cents:
                        item.status = REDEEMED
            world.redemptions[e.aggregate_id] = r
            world.scan_index[p["scan_code_id"]] = e.aggregate_id
        elif t == REFUND_REQUESTED:
            r = RefundState(
                refund_id=e.aggregate_id,
                bundle_id=p["bundle_id"],
                spectator_id=p["spectator_id"],
                edition_id=p["edition_id"],
                reason=p["reason"],
                items=[RefundItem(i["item_id"], i["amount_cents"]) for i in p["items"]],
                requested_at=e.occurred_at,
                note=p.get("note", ""),
            )
            world.refunds[e.aggregate_id] = r
            for i in p["items"]:
                world.bundles[p["bundle_id"]].items[i["item_id"]].status = REFUND_PENDING
                world.bundles[p["bundle_id"]].items[i["item_id"]].refund_id = e.aggregate_id
        elif t == REFUND_CONFIRMED:
            r = world.refunds[e.aggregate_id]
            r.status = REFUND_CONFIRMED_STATE
            r.confirmed_at = e.occurred_at
            r.spectator_amount_cents = p["spectator_amount_cents"]
            r.note = p.get("note", r.note)
            for i in p["items"]:
                world.bundles[r.bundle_id].items[i["item_id"]].status = REFUNDED
        elif t == SETTLEMENT_BATCH_OPENED:
            world.batches[e.aggregate_id] = BatchState(
                batch_id=e.aggregate_id,
                edition_id=p["edition_id"],
                period_start=p["period_start"],
                period_end=p["period_end"],
                opened_at=e.occurred_at,
            )
        elif t == SETTLEMENT_FROZEN:
            b = world.batches[e.aggregate_id]
            # 冻结快照是不可变事实：清空开放期间挂入的手工调整行，
            # 以快照中的完整行集重建（手工调整已包含在快照内）。
            b.lines = []
            b.included_parts = set()
            b.adjusted_parts = set()
            b.status = BATCH_FROZEN
            b.frozen_at = e.occurred_at
            b.totals = p["totals"]
            for ln in p["lines"]:
                line = SettlementLine(**ln)
                b.lines.append(line)
                if line.kind == "CONSUMPTION":
                    b.included_parts.add((line.redemption_id, line.part_index))
                if line.kind in (ADJ_REVERSAL, ADJ_TOPUP):
                    if line.redemption_id:
                        b.adjusted_parts.add((line.redemption_id, line.part_index or 0))
        elif t == SETTLEMENT_PAID:
            b = world.batches[e.aggregate_id]
            b.status = BATCH_PAID
            b.paid_at = e.occurred_at
        elif t == ADJUSTMENT_POSTED:
            # 调整先挂在开放批次上，冻结时进入不可变快照
            b = world.batches[e.aggregate_id]
            for ln in p["lines"]:
                b.lines.append(SettlementLine(**ln))
                if ln.get("redemption_id"):
                    b.adjusted_parts.add((ln["redemption_id"], ln.get("part_index") or 0))
        else:
            raise ValueError(f"折叠器未处理事件类型：{t}")
    return world
