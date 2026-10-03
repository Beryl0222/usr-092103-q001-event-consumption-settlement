"""读侧投影：运营追溯、游客视图、商户视图。

三类访问者的数据边界：
- operator（清算运营）：全量；可从一笔结算行追到实际消费、退款与合同依据；
- spectator（游客）：仅本人权益包、核销、退款进度；
- merchant（商户）：仅与自身履约有关的数据——自己门店产生的核销分项、
  自己的结算行与合同版本；看不到游客标识以外的他人数据，也看不到
  其他商户的行。

所有视图都从事件流实时折叠得到（事件溯源天然支持按事件重放审计）。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from .errors import AccessDenied, NotFound
from .model import (
    World,
    fold,
)
from .money import cents_to_yuan
from .store import EventStore

OPERATOR = "operator"
SPECTATOR = "spectator"
MERCHANT = "merchant"


class Principal:
    """访问者身份。role 决定数据边界，id 限定本人/本商户。"""

    def __init__(self, role: str, principal_id: Optional[str] = None) -> None:
        if role not in (OPERATOR, SPECTATOR, MERCHANT):
            raise ValueError(f"未知角色：{role}")
        self.role = role
        self.id = principal_id

    @classmethod
    def operator(cls) -> "Principal":
        return cls(OPERATOR)

    @classmethod
    def spectator(cls, spectator_id: str) -> "Principal":
        return cls(SPECTATOR, spectator_id)

    @classmethod
    def merchant(cls, merchant_id: str) -> "Principal":
        return cls(MERCHANT, merchant_id)


def _line_public(ln, *, for_merchant: bool = False) -> dict:
    d = asdict(ln)
    if for_merchant:
        # 商户视图脱敏：不暴露其他游客/权益包的明细金额构成
        detail = dict(d["detail"])
        detail.pop("spectator_id", None)
        entries = detail.get("entries")
        if entries is not None:
            detail["entries"] = [{"item_ref": f"item#{i}", **{k: v for k, v in e.items() if k != "item_id"}}
                                 for i, e in enumerate(entries)]
        d["detail"] = detail
    d["amount_yuan"] = cents_to_yuan(d["amount_cents"])
    return d


class Views:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def _world(self) -> World:
        return fold(self.store.load_all())

    # ---- 游客视图 -------------------------------------------------------

    def spectator_bundle(self, principal: Principal, bundle_id: str) -> dict:
        if principal.role != SPECTATOR or not principal.id:
            raise AccessDenied("仅游客本人可查看权益包")
        world = self._world()
        if bundle_id not in world.bundles:
            raise NotFound(f"权益包不存在：{bundle_id}")
        b = world.bundles[bundle_id]
        if b.spectator_id != principal.id:
            raise AccessDenied("只能读取本人的权益包")
        items = []
        for it in b.items.values():
            items.append({
                "item_id": it.item_id,
                "phase": it.phase,
                "title": it.title,
                "status": it.status,
                "face_value_cents": it.face_value_cents,
                "available_cents": max(0, it.face_value_cents - it.consumed_cents),
                "consumed_cents": it.consumed_cents,
                "window_start": it.window_start,
                "window_end": it.window_end,
                "refund_id": it.refund_id,
            })
        return {"bundle_id": b.bundle_id, "edition_id": b.edition_id,
                "issued_at": b.issued_at, "status": b.status, "items": items}

    def spectator_refund_progress(self, principal: Principal, bundle_id: str) -> dict:
        """游客看到的退款进度：申请、确认、到账金额、补贴回收（只读）。"""
        if principal.role != SPECTATOR or not principal.id:
            raise AccessDenied("仅游客本人可查看退款进度")
        world = self._world()
        if bundle_id not in world.bundles:
            raise NotFound(f"权益包不存在：{bundle_id}")
        if world.bundles[bundle_id].spectator_id != principal.id:
            raise AccessDenied("只能读取本人的退款进度")
        out = []
        for r in world.refunds.values():
            if r.bundle_id != bundle_id:
                continue
            entry = {
                "refund_id": r.refund_id,
                "reason": r.reason,
                "requested_at": r.requested_at,
                "status": r.status,
                "confirmed_at": r.confirmed_at,
                "spectator_refund_cents": r.spectator_amount_cents if r.confirmed_at else 0,
                "spectator_refund_yuan": cents_to_yuan(r.spectator_amount_cents) if r.confirmed_at else "0.00",
                "items": [asdict(i) for i in r.items],
            }
            out.append(entry)
        return {"bundle_id": bundle_id, "refunds": out}

    # ---- 商户视图 -------------------------------------------------------

    def merchant_redemptions(self, principal: Principal, edition_id: Optional[str] = None) -> dict:
        if principal.role != MERCHANT or not principal.id:
            raise AccessDenied("仅商户可读取自身核销数据")
        world = self._world()
        out = []
        for r in world.redemptions.values():
            if edition_id and r.edition_id != edition_id:
                continue
            for part in r.parts:
                if part.merchant_id != principal.id:
                    continue
                out.append({
                    "redemption_id": r.redemption_id,
                    "part_index": part.part_index,
                    "edition_id": r.edition_id,
                    "store_id": part.store_id,
                    "scanned_at": r.scanned_at,
                    "recorded_at": r.recorded_at,
                    "offline": r.offline,
                    "gross_cents": part.gross_cents,
                    "gross_yuan": cents_to_yuan(part.gross_cents),
                })
        return {"merchant_id": principal.id, "redemptions": out}

    def merchant_settlements(self, principal: Principal) -> dict:
        if principal.role != MERCHANT or not principal.id:
            raise AccessDenied("仅商户可读取自身结算数据")
        world = self._world()
        batches = []
        for b in world.batches.values():
            if b.status == "OPEN":
                continue
            mine = [ln for ln in b.lines if ln.merchant_id == principal.id]
            if not mine:
                continue
            batches.append({
                "batch_id": b.batch_id,
                "edition_id": b.edition_id,
                "period_start": b.period_start,
                "period_end": b.period_end,
                "status": b.status,
                "frozen_at": b.frozen_at,
                "paid_at": b.paid_at,
                "lines": [_line_public(ln, for_merchant=True) for ln in mine],
                "payable_cents": (b.totals.get("by_merchant", {}).get(principal.id) or {})
                    .get("payable_cents"),
            })
        return {"merchant_id": principal.id, "batches": batches}

    def merchant_contract_versions(self, principal: Principal, edition_id: Optional[str] = None) -> dict:
        if principal.role != MERCHANT or not principal.id:
            raise AccessDenied("仅商户可读取自身合同")
        world = self._world()
        out = []
        for c in world.contracts.values():
            if c.merchant_id != principal.id:
                continue
            if edition_id and c.edition_id != edition_id:
                continue
            out.append({"contract_id": c.contract_id, "edition_id": c.edition_id,
                        "versions": [asdict(v) for v in c.versions]})
        return {"merchant_id": principal.id, "contracts": out}

    # ---- 运营追溯视图 ---------------------------------------------------

    def settlement_trace(self, principal: Principal, line_id: str) -> dict:
        """从一笔结算行追到：消费明细 -> 权益项/退款 -> 合同版本依据。

        可传 CONSUMPTION / REVERSAL / TOPUP / GUARANTEE_TOPUP / TAX 任意行号。
        """
        if principal.role != OPERATOR:
            raise AccessDenied("仅运营可追溯结算")
        world = self._world()
        owner_batch = None
        target = None
        for b in world.batches.values():
            for ln in b.lines:
                if ln.line_id == line_id:
                    owner_batch, target = b, ln
        if target is None:
            raise NotFound(f"结算行不存在：{line_id}")

        trace: dict = {
            "line": asdict(target),
            "batch": {"batch_id": owner_batch.batch_id, "edition_id": owner_batch.edition_id,
                      "status": owner_batch.status, "period_start": owner_batch.period_start,
                      "period_end": owner_batch.period_end},
        }

        if target.contract_version_no is not None:
            contract = world.contract_for(target.merchant_id, owner_batch.edition_id)
            if contract is not None:
                cv = next((v for v in contract.versions if v.version_no == target.contract_version_no), None)
                trace["contract_basis"] = {
                    "contract_id": contract.contract_id,
                    "version_no": target.contract_version_no,
                    "effective_from": cv.effective_from if cv else None,
                    "terms": cv.terms if cv else None,
                }

        if target.redemption_id is not None:
            r = world.redemptions.get(target.redemption_id)
            if r is not None:
                part = next((p for p in r.parts if p.part_index == (target.part_index or 0)), None)
                entries_view = []
                for en in (part.entries if part else []):
                    item = world.bundles[r.bundle_id].items[en["item_id"]]
                    refund = None
                    if item.refund_id:
                        rf = world.refunds.get(item.refund_id)
                        refund = {"refund_id": rf.refund_id, "status": rf.status,
                                  "requested_at": rf.requested_at, "confirmed_at": rf.confirmed_at}
                    entries_view.append({
                        "item_id": item.item_id, "title": item.title, "phase": item.phase,
                        "amount_cents": en["amount_cents"],
                        "subsidy_cents": item.subsidy_cents * en["amount_cents"] // item.face_value_cents,
                        "item_status": item.status,
                        "refund": refund,
                    })
                trace["consumption"] = {
                    "redemption_id": r.redemption_id,
                    "bundle_id": r.bundle_id,
                    "spectator_id": r.spectator_id,
                    "store_id": part.store_id if part else None,
                    "scanned_at": r.scanned_at,
                    "recorded_at": r.recorded_at,
                    "offline": r.offline,
                    "gross_cents": part.gross_cents if part else None,
                    "entries": entries_view,
                }

        if target.source_batch_id:
            trace["source"] = {"batch_id": target.source_batch_id,
                               "line_id": target.source_line_id,
                               "note": "旧账不可改写，本行是对来源行的冲正/补付"}
        return trace

    def batch_detail(self, principal: Principal, batch_id: str) -> dict:
        if principal.role != OPERATOR:
            raise AccessDenied("仅运营可查看完整批次")
        world = self._world()
        if batch_id not in world.batches:
            raise NotFound(f"结算批次不存在：{batch_id}")
        b = world.batches[batch_id]
        return {"batch_id": b.batch_id, "edition_id": b.edition_id,
                "status": b.status,
                "period_start": b.period_start, "period_end": b.period_end,
                "opened_at": b.opened_at, "frozen_at": b.frozen_at, "paid_at": b.paid_at,
                "totals": b.totals,
                "lines": [asdict(ln) for ln in b.lines]}

    def audit_events(self, principal: Principal, *, aggregate_id: Optional[str] = None,
                     event_type: Optional[str] = None) -> dict:
        """运营审计：原始事件流（含哈希链），支持按聚合/事件类型过滤。"""
        if principal.role != OPERATOR:
            raise AccessDenied("仅运营可读取审计事件流")
        if aggregate_id:
            events = self.store.load_stream(aggregate_id)
        elif event_type:
            events = self.store.load_by_type(event_type)
        else:
            events = self.store.load_all()
        return {"chain_valid": self.store.verify_chain(),
                "events": [e.as_dict() for e in events]}
