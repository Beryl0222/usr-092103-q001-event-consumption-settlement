"""核心领域服务（写侧）。

所有写操作：
1. 从只追加事件存储重放当前状态（fold）；
2. 校验业务不变量；
3. 在一个事务里追加新事件——旧账永不改写，纠错只产生新事件
  （REFUND_CONFIRMED / ADJUSTMENT_POSTED 等）。

幂等分两层：
- client_request_id：同一外部请求重放（二维码重复扫描后的重试、
  离线补传重试）返回首次结果，不产生新事件；
- 业务键：scan_code_id 全局唯一，防止换请求号的重复扫码；
  (aggregate_id, version) 唯一防止并发双写。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Optional

from .contracts_engine import price_part, validate_terms
from .envelope import (
    ADJUSTMENT_POSTED,
    BUNDLE_ISSUED,
    BUNDLE_MIGRATED,
    CONTRACT_VERSION_EFFECTIVE,
    EDITION_REGISTERED,
    EDITION_RESCHEDULED,
    MERCHANT_REGISTERED,
    REDEMPTION_RECORDED,
    REFUND_CONFIRMED,
    REFUND_REQUESTED,
    SETTLEMENT_BATCH_OPENED,
    SETTLEMENT_FROZEN,
    SETTLEMENT_PAID,
    make_event,
)
from .errors import (
    Conflict,
    ContractError,
    DuplicateRedemption,
    IllegalState,
    InsufficientBalance,
    NotFound,
    WindowClosed,
)
from .model import (
    ADJ_TOPUP,
    ADJ_REVERSAL,
    AVAILABLE,
    BATCH_FROZEN,
    BATCH_OPEN,
    BATCH_PAID,
    REASON_RACE_RESCHEDULE,
    REDEEMED,
    REFUND_PENDING,
    REFUNDED,
    CANCELLED,
    SettlementLine,
    World,
    fold,
)
from .store import EventStore
from .times import Window, iso, now, parse_at


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _month_key(moment: Any) -> str:
    return parse_at(moment).strftime("%Y-%m")


class Service:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def _world(self) -> World:
        return fold(self.store.load_all())

    def _event(self, event_type: str, aggregate_id: str, summary: str, payload: dict,
               *, at=None, aggregate_type: Optional[str] = None):
        return make_event(
            event_type,
            aggregate_id,
            self.store.next_version(aggregate_id),
            summary,
            payload,
            event_id=_new_id("evt"),
            occurred_at=at or now(),
            aggregate_type=aggregate_type,
        )

    def _append(self, events, *, client_request_id, result):
        return self.store.append(events, client_request_id=client_request_id, result=result)

    def _replay(self, client_request_id: Optional[str]) -> Optional[dict]:
        """命中幂等键时返回首次结果；必须在任何业务校验之前短路，
        否则重放请求会因状态已变而报错（例如改期后再次改期）。"""
        if client_request_id is not None:
            return self.store.replay_result(client_request_id)
        return None

    # ---- 基础登记 -------------------------------------------------------

    def register_edition(self, edition_id: str, name: str, *, race_start: str, race_end: str,
                         pre_start: str, post_end: str, client_request_id: Optional[str] = None) -> dict:
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if edition_id in world.editions:
            raise Conflict(f"赛事届次已存在：{edition_id}")
        rs, re_ = parse_at(race_start), parse_at(race_end)
        ps, pe = parse_at(pre_start), parse_at(post_end)
        if not (ps < rs < re_ < pe):
            raise ValueError("时间顺序应为 pre_start < race_start < race_end < post_end")
        payload = {
            "name": name,
            "race_start": iso(rs), "race_end": iso(re_),
            "pre_start": iso(ps), "post_end": iso(pe),
        }
        event = self._event(EDITION_REGISTERED, edition_id, f"登记赛事届次：{name}", payload)
        self._append([event], client_request_id=client_request_id,
                     result={"edition_id": edition_id})
        return {"edition_id": edition_id}

    def register_merchant(self, merchant_id: str, name: str, *,
                          tax_withholding_bps: int = 0,
                          client_request_id: Optional[str] = None) -> dict:
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if merchant_id in world.merchants:
            raise Conflict(f"商户已存在：{merchant_id}")
        if not 0 <= tax_withholding_bps <= 10_000:
            raise ValueError("代扣税率必须在 0..10000 bps")
        payload = {"name": name, "tax_withholding_bps": tax_withholding_bps}
        event = self._event(MERCHANT_REGISTERED, merchant_id, f"登记商户：{name}", payload)
        self._append([event], client_request_id=client_request_id,
                     result={"merchant_id": merchant_id})
        return {"merchant_id": merchant_id}

    def effective_contract_version(self, contract_id: str, *, merchant_id: str, edition_id: str,
                                   terms: dict, effective_from: str,
                                   version_no: Optional[int] = None, note: str = "",
                                   client_request_id: Optional[str] = None) -> dict:
        """登记一份合同新版本。版本只对生效日之后发生的业务有效，不溯及旧账。"""
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if merchant_id not in world.merchants:
            raise NotFound(f"商户不存在：{merchant_id}")
        if edition_id not in world.editions:
            raise NotFound(f"赛事届次不存在：{edition_id}")
        validate_terms(terms)
        contract = world.contracts.get(contract_id)
        if contract is not None:
            if contract.merchant_id != merchant_id or contract.edition_id != edition_id:
                raise ContractError("合同版本的商户或届次与首版不一致")
            next_no = contract.latest().version_no + 1
            if parse_at(effective_from) <= parse_at(contract.latest().effective_from):
                raise ContractError("新版本生效时间必须晚于上一版")
        else:
            next_no = 1
        version_no = version_no or next_no
        if version_no != next_no:
            raise ContractError(f"合同版本号必须顺序递增，应为 {next_no}")
        payload = {
            "merchant_id": merchant_id,
            "edition_id": edition_id,
            "version_no": version_no,
            "effective_from": iso(parse_at(effective_from)),
            "terms": terms,
            "note": note,
        }
        event = self._event(
            CONTRACT_VERSION_EFFECTIVE, contract_id,
            f"合同 {contract_id} 第 {version_no} 版生效", payload)
        self._append([event], client_request_id=client_request_id,
                     result={"contract_id": contract_id, "version_no": version_no})
        return {"contract_id": contract_id, "version_no": version_no}

    def issue_bundle(self, bundle_id: str, *, spectator_id: str, edition_id: str,
                     items: list[dict], client_request_id: Optional[str] = None) -> dict:
        """发放权益包。

        items: [{item_id, phase, title, face_value_cents, subsidy_cents,
                 spectator_paid_cents, merchant_id?, window_start?, window_end?}]
        未显式给窗口时按届次的赛前/赛中/赛后窗口派生。
        face_value = subsidy + spectator_paid。
        """
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if bundle_id in world.bundles:
            raise Conflict(f"权益包已存在：{bundle_id}")
        if edition_id not in world.editions:
            raise NotFound(f"赛事届次不存在：{edition_id}")
        edition = world.editions[edition_id]
        item_payloads = []
        seen = set()
        for it in items:
            iid = it["item_id"]
            if iid in seen:
                raise ValueError(f"权益项重复：{iid}")
            seen.add(iid)
            face = int(it["face_value_cents"])
            subsidy = int(it.get("subsidy_cents", 0))
            spectator_paid = int(it.get("spectator_paid_cents", face - subsidy))
            if face <= 0 or subsidy < 0 or spectator_paid < 0 or subsidy + spectator_paid != face:
                raise ValueError(f"权益项 {iid} 面值构成不合法")
            phase = it["phase"]
            if phase not in ("PRE_RACE", "RACE_DAY", "POST_RACE"):
                raise ValueError(f"权益项 {iid} 阶段非法：{phase}")
            if "window_start" in it and "window_end" in it:
                win = Window.of(it["window_start"], it["window_end"])
            else:
                ws, we = edition.phase_window(phase)
                win = Window(ws, we)
            merchant_id = it.get("merchant_id")
            if merchant_id is not None and merchant_id not in world.merchants:
                raise NotFound(f"商户不存在：{merchant_id}")
            item_payloads.append({
                "item_id": iid,
                "phase": phase,
                "title": it["title"],
                "face_value_cents": face,
                "subsidy_cents": subsidy,
                "spectator_paid_cents": spectator_paid,
                "merchant_id": merchant_id,
                "window_start": iso(win.start),
                "window_end": iso(win.end),
            })
        payload = {"spectator_id": spectator_id, "edition_id": edition_id, "items": item_payloads}
        # 延续起点样例语义：BUNDLE_ISSUED 允许挂在 event_edition 聚合上，
        # 但服务内部以 benefit_bundle 为聚合根做版本演进。
        event = self._event(BUNDLE_ISSUED, bundle_id,
                            f"向观众 {spectator_id} 发放权益包 {bundle_id}", payload)
        self._append([event], client_request_id=client_request_id,
                     result={"bundle_id": bundle_id, "item_ids": [i["item_id"] for i in item_payloads]})
        return {"bundle_id": bundle_id, "items": item_payloads}

    # ---- 核销 -----------------------------------------------------------

    def record_redemption(self, *, bundle_id: str, scan_code_id: str, scanned_at: str,
                          parts: list[dict], offline: bool = False,
                          client_request_id: Optional[str] = None,
                          recorded_at: Optional[str] = None) -> dict:
        """记录一次核销（支持跨店拆单与离线补传）。

        parts: [{merchant_id, store_id, entries: [{item_id, amount_cents}]}]
        - 同一 scan_code_id 重复扫码：没有同一请求号时报 DuplicateRedemption；
          携带同一 client_request_id 的重试：原样返回首次结果；
        - 业务时点 scanned_at 必须落在每个权益项的有效窗口内
          （离线补传以实际扫码时刻为准，而非上传时刻）；
        - 同一权益项累计核销不得超过面值（离线设备双花在补传时被拒）。
        """
        world = self._world()
        if client_request_id is not None:
            cached = self.store.replay_result(client_request_id)
            if cached is not None:
                return cached
        if bundle_id not in world.bundles:
            raise NotFound(f"权益包不存在：{bundle_id}")
        if scan_code_id in world.scan_index:
            raise DuplicateRedemption(f"二维码已核销：{scan_code_id}")
        bundle = world.bundles[bundle_id]
        edition = world.editions.get(bundle.edition_id)
        if edition is None:
            raise NotFound(f"赛事届次不存在：{bundle.edition_id}")
        scan_time = parse_at(scanned_at)
        if not parts:
            raise ValueError("核销至少包含一个商户分项")

        # 汇总本次各权益项的占用
        demanded: dict[str, int] = {}
        norm_parts = []
        for idx, part in enumerate(parts):
            merchant_id = part["merchant_id"]
            store_id = part["store_id"]
            if merchant_id not in world.merchants:
                raise NotFound(f"商户不存在：{merchant_id}")
            if world.contract_for(merchant_id, bundle.edition_id) is None:
                raise ContractError(f"商户 {merchant_id} 在届次 {bundle.edition_id} 无生效合同，无法核销")
            entries = []
            gross = 0
            for en in part["entries"]:
                iid, amount = en["item_id"], int(en["amount_cents"])
                if amount <= 0:
                    raise ValueError("核销金额必须为正")
                if iid not in bundle.items:
                    raise NotFound(f"权益项不属于该权益包：{iid}")
                item = bundle.items[iid]
                if item.merchant_id is not None and item.merchant_id != merchant_id:
                    raise IllegalState(f"权益项 {iid} 限定商户 {item.merchant_id}，不得在 {merchant_id} 核销")
                if item.status != AVAILABLE:
                    raise IllegalState(f"权益项 {iid} 当前状态 {item.status}，不可核销")
                if not Window.of(item.window_start, item.window_end).contains(scan_time):
                    raise WindowClosed(
                        f"权益项 {iid} 不在有效窗口内：{item.window_start}..{item.window_end}")
                demanded[iid] = demanded.get(iid, 0) + amount
                entries.append({"item_id": iid, "amount_cents": amount})
                gross += amount
            norm_parts.append({
                "part_index": idx,
                "merchant_id": merchant_id,
                "store_id": store_id,
                "entries": entries,
                "gross_cents": gross,
            })

        for iid, amount in demanded.items():
            item = bundle.items[iid]
            if item.consumed_cents + amount > item.face_value_cents:
                raise InsufficientBalance(
                    f"权益项 {iid} 余额不足：面值 {item.face_value_cents}，"
                    f"已用 {item.consumed_cents}，本次申请 {amount}")

        at = parse_at(recorded_at) if recorded_at else now()
        redemption_id = _new_id("rdm")
        payload = {
            "bundle_id": bundle_id,
            "spectator_id": bundle.spectator_id,
            "edition_id": bundle.edition_id,
            "scan_code_id": scan_code_id,
            "scanned_at": iso(scan_time),
            "offline": bool(offline),
            "parts": norm_parts,
        }
        event = make_event(
            REDEMPTION_RECORDED, redemption_id,
            self.store.next_version(redemption_id),
            f"核销 {redemption_id}（{len(norm_parts)} 个商户分项，离线={offline}）",
            payload, event_id=_new_id("evt"), occurred_at=at)
        result = {
            "redemption_id": redemption_id,
            "offline": bool(offline),
            "parts": [{"part_index": p["part_index"], "merchant_id": p["merchant_id"],
                       "store_id": p["store_id"], "gross_cents": p["gross_cents"]}
                      for p in norm_parts],
        }
        self._append([event], client_request_id=client_request_id, result=result)
        return result

    # ---- 退款 -----------------------------------------------------------

    def request_refund(self, *, bundle_id: str, items: list[dict], reason: str,
                       note: str = "", client_request_id: Optional[str] = None,
                       at: Optional[str] = None) -> dict:
        """游客/运营发起退款。items: [{item_id, amount_cents}]

        - 未使用权益项：amount 不超过剩余面值（面值-已核销）；
        - 商户未履约：可对已核销部分申请（amount 不超过已核销额），
          结算时对相应商户分项生成冲正。
        """
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if bundle_id not in world.bundles:
            raise NotFound(f"权益包不存在：{bundle_id}")
        bundle = world.bundles[bundle_id]
        if reason not in ("SPECTATOR_CANCELLATION", "RACE_RESCHEDULE", "MERCHANT_NONPERFORMANCE"):
            raise ValueError("退款原因非法")
        norm = []
        for req in items:
            iid, amount = req["item_id"], int(req["amount_cents"])
            if iid not in bundle.items:
                raise NotFound(f"权益项不属于该权益包：{iid}")
            if amount <= 0:
                raise ValueError("退款金额必须为正")
            item = bundle.items[iid]
            if item.refund_id is not None:
                raise IllegalState(f"权益项 {iid} 已存在退款单 {item.refund_id}")
            if reason == "MERCHANT_NONPERFORMANCE":
                # 可对「已核销」部分主张未履约退款：无论权益项是否已全额
                # 核销（部分核销也适用），金额不超过累计已核销额。
                if item.consumed_cents <= 0:
                    raise IllegalState(f"权益项 {iid} 尚无核销，不适用未履约退款")
                if amount > item.consumed_cents:
                    raise IllegalState(f"权益项 {iid} 未履约退款不能超过已核销额")
            else:
                if item.status != AVAILABLE:
                    raise IllegalState(f"权益项 {iid} 当前状态 {item.status}，不可退")
                if amount > item.face_value_cents - item.consumed_cents:
                    raise IllegalState(f"权益项 {iid} 退款超过剩余可用面值")
            norm.append({"item_id": iid, "amount_cents": amount})
        refund_id = _new_id("rfd")
        payload = {
            "bundle_id": bundle_id,
            "spectator_id": bundle.spectator_id,
            "edition_id": bundle.edition_id,
            "reason": reason,
            "items": norm,
            "note": note,
        }
        event = self._event(REFUND_REQUESTED, refund_id,
                            f"退款申请 {refund_id}（{reason}）", payload, at=at)
        result = {"refund_id": refund_id, "status": "REQUESTED",
                  "items": norm}
        self._append([event], client_request_id=client_request_id, result=result)
        return result

    def confirm_refund(self, refund_id: str, *, note: str = "",
                       client_request_id: Optional[str] = None,
                       at: Optional[str] = None) -> dict:
        """确认退款。游客到账金额按其自付占比等比计算（整数分）。"""
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if refund_id not in world.refunds:
            raise NotFound(f"退款单不存在：{refund_id}")
        refund = world.refunds[refund_id]
        if refund.status != "REQUESTED":
            raise IllegalState(f"退款单 {refund_id} 当前状态 {refund.status}，不可确认")
        bundle = world.bundles[refund.bundle_id]
        out_items = []
        total_spectator = 0
        for ri in refund.items:
            item = bundle.items[ri.item_id]
            spectator_share = item.spectator_paid_cents * ri.amount_cents // item.face_value_cents
            subsidy_share = ri.amount_cents - spectator_share
            total_spectator += spectator_share
            out_items.append({
                "item_id": ri.item_id,
                "amount_cents": ri.amount_cents,
                "spectator_refund_cents": spectator_share,
                "subsidy_clawback_cents": subsidy_share,
            })
        payload = {"items": out_items, "spectator_amount_cents": total_spectator, "note": note}
        event = self._event(REFUND_CONFIRMED, refund_id,
                            f"退款确认 {refund_id}，游客到账 {total_spectator} 分", payload, at=at)
        result = {"refund_id": refund_id, "status": "CONFIRMED",
                  "spectator_amount_cents": total_spectator, "items": out_items}
        self._append([event], client_request_id=client_request_id, result=result)
        return result

    # ---- 改期 -----------------------------------------------------------

    def reschedule_edition(self, edition_id: str, *, new_race_start: str, new_race_end: str,
                           new_pre_start: str, new_post_end: str,
                           unavailable_merchants: Optional[set[str]] = None,
                           reason: str = "", client_request_id: Optional[str] = None,
                           at: Optional[str] = None) -> dict:
        """比赛改期。

        只迁移仍可履约的部分：
        - 已核销部分不动（事实不变）；未使用/剩余额度按时间差平移窗口；
        - 指定商户声明无法在新日期履约的，其专属权益项取消并自动退款；
        - 退款中的权益项不迁移；通用权益项（不绑定商户）一律迁移。
        """
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if edition_id not in world.editions:
            raise NotFound(f"赛事届次不存在：{edition_id}")
        edition = world.editions[edition_id]
        nrs, nre = parse_at(new_race_start), parse_at(new_race_end)
        nps, npe = parse_at(new_pre_start), parse_at(new_post_end)
        if not (nps < nrs < nre < npe):
            raise ValueError("新时间顺序应为 pre_start < race_start < race_end < post_end")
        delta = nrs - edition.race_start
        unavailable = unavailable_merchants or set()
        at_dt = parse_at(at) if at else now()

        events = []
        # 同一事务内可能为同一新聚合（自动退款）追加多条事件，
        # 版本号须在数据库基线之上按本批次计数递增。
        local_versions: dict[str, int] = {}

        def version_of(agg: str) -> int:
            if agg not in local_versions:
                local_versions[agg] = self.store.next_version(agg)
            else:
                local_versions[agg] += 1
            return local_versions[agg]
        migrated_bundles = []
        for bundle in world.bundles.values():
            if bundle.edition_id != edition_id:
                continue
            migrated, cancelled = [], []
            for item in bundle.items.values():
                if item.status in (REFUND_PENDING, REFUNDED, REDEEMED):
                    continue  # 退款流程中/已退/已全量核销：不迁移
                if item.merchant_id is not None and item.merchant_id in unavailable:
                    remaining = item.face_value_cents - item.consumed_cents
                    if remaining <= 0:
                        continue
                    refund_id = _new_id("rfd")
                    spectator_share = item.spectator_paid_cents * remaining // item.face_value_cents
                    req_payload = {
                        "bundle_id": bundle.bundle_id,
                        "spectator_id": bundle.spectator_id,
                        "edition_id": edition_id,
                        "reason": REASON_RACE_RESCHEDULE,
                        "items": [{"item_id": item.item_id, "amount_cents": remaining}],
                        "note": "改期后商户无法履约，系统自动退款",
                    }
                    events.append(make_event(
                        REFUND_REQUESTED, refund_id, version_of(refund_id),
                        f"改期自动退款申请 {refund_id}", req_payload,
                        event_id=_new_id("evt"), occurred_at=at_dt))
                    events.append(make_event(
                        REFUND_CONFIRMED, refund_id, version_of(refund_id),
                        f"改期自动退款确认 {refund_id}",
                        {"items": [{"item_id": item.item_id, "amount_cents": remaining,
                                    "spectator_refund_cents": spectator_share,
                                    "subsidy_clawback_cents": remaining - spectator_share}],
                         "spectator_amount_cents": spectator_share,
                         "note": "改期后商户无法履约"},
                        event_id=_new_id("evt"), occurred_at=at_dt))
                    cancelled.append({"item_id": item.item_id, "refund_id": refund_id,
                                      "remaining_cents": remaining})
                else:
                    new_win = Window.of(item.window_start, item.window_end).shift(delta)
                    migrated.append({
                        "item_id": item.item_id,
                        "new_window_start": iso(new_win.start),
                        "new_window_end": iso(new_win.end),
                        "remaining_cents": item.face_value_cents - item.consumed_cents,
                    })
            if migrated or cancelled:
                events.append(self._event(
                    BUNDLE_MIGRATED, bundle.bundle_id,
                    f"权益包 {bundle.bundle_id} 随届次改期迁移",
                    {"old_edition_id": edition_id, "new_edition_id": edition_id,
                     "delta_days": delta.total_seconds() / 86400,
                     "migrated": migrated, "cancelled": cancelled}, at=at_dt))
                migrated_bundles.append({"bundle_id": bundle.bundle_id,
                                         "migrated": migrated, "cancelled": cancelled})

        edition_payload = {
            "old_race_start": iso(edition.race_start),
            "new_race_start": iso(nrs),
            "new_race_end": iso(nre),
            "new_pre_start": iso(nps),
            "new_post_end": iso(npe),
            "delta_days": delta.total_seconds() / 86400,
            "reason": reason,
        }
        events.append(self._event(EDITION_RESCHEDULED, edition_id,
                                  f"赛事届次 {edition_id} 改期", edition_payload, at=at_dt))
        result = {"edition_id": edition_id, "delta_days": delta.total_seconds() / 86400,
                  "bundles": migrated_bundles}
        self._append(events, client_request_id=client_request_id, result=result)
        return result

    # ---- 结算批次 --------------------------------------------------------

    def open_batch(self, batch_id: str, *, edition_id: str, period_start: str,
                   period_end: str, client_request_id: Optional[str] = None,
                   at: Optional[str] = None) -> dict:
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if edition_id not in world.editions:
            raise NotFound(f"赛事届次不存在：{edition_id}")
        if batch_id in world.batches:
            raise Conflict(f"结算批次已存在：{batch_id}")
        ps, pe = parse_at(period_start), parse_at(period_end)
        if pe <= ps:
            raise ValueError("结算周期结束必须晚于开始")
        payload = {"edition_id": edition_id,
                   "period_start": iso(ps), "period_end": iso(pe)}
        event = self._event(SETTLEMENT_BATCH_OPENED, batch_id,
                            f"开启结算批次 {batch_id}", payload, at=at)
        self._append([event], client_request_id=client_request_id,
                     result={"batch_id": batch_id, "status": BATCH_OPEN})
        return {"batch_id": batch_id, "status": BATCH_OPEN}

    @staticmethod
    def _confirmed_refund_items(world: World) -> dict[str, dict[str, str]]:
        """item_id -> {refund_id, amount_cents}（已确认退款，最近一笔）。"""
        out: dict[str, dict] = {}
        for refund in world.refunds.values():
            if refund.status != "CONFIRMED":
                continue
            for ri in refund.items:
                out[ri.item_id] = {"refund_id": refund.refund_id, "amount_cents": ri.amount_cents}
        return out

    @staticmethod
    def _already_reversed(world: World) -> dict[tuple[str, int, str], int]:
        """已冻结/已支付批次中累计冲正过的 (redemption, part, item) -> 分数。"""
        done: dict[tuple[str, int, str], int] = {}
        for batch in world.batches.values():
            if batch.status not in (BATCH_FROZEN, BATCH_PAID):
                # 开放批次里手工登记、待冻结的调整也占用额度，避免冻结时重复冲正
                pending = batch.lines
            else:
                pending = batch.lines
            for ln in pending:
                if ln.kind == ADJ_REVERSAL:
                    for en in ln.detail.get("entries", []):
                        key = (ln.redemption_id, ln.part_index or 0, en["item_id"])
                        done[key] = done.get(key, 0) + en["amount_cents"]
        return done

    def _allocate_refunds(self, world: World, target_parts: set[tuple[str, int]],
                          already: dict[tuple[str, int, str], int]
                          ) -> list[dict]:
        """把已确认退款按 FIFO 分摊到消费明细，返回冲正描述。

        仅生成落在 target_parts（本批新入账分项）或已结算分项上的冲正；
        后者带 source_batch_id/source_line_id（旧账不改写，只做冲正）。
        """
        refunds = self._confirmed_refund_items(world)
        reversals = []
        settled = world.settled_parts()
        # 找本批新分项对应（尚未冻结）的 CONSUMPTION 行需要由调用方补充来源
        for item_id, info in refunds.items():
            # 该 item 属于哪个 bundle
            owner = None
            for b in world.bundles.values():
                if item_id in b.items:
                    owner = b
                    break
            if owner is None:
                continue
            item = owner.items[item_id]
            remaining = info["amount_cents"]
            for cons in item.consumptions:  # FIFO
                if remaining <= 0:
                    break
                key = (cons.redemption_id, cons.part_index, item_id)
                used = already.get(key, 0)
                available = cons.amount_cents - used
                if available <= 0:
                    continue
                cut = min(remaining, available)
                part_key = (cons.redemption_id, cons.part_index)
                if part_key in target_parts or part_key in settled:
                    reversals.append({
                        "redemption_id": cons.redemption_id,
                        "part_index": cons.part_index,
                        "item_id": item_id,
                        "gross_reverse_cents": cut,
                        "consumption_cents": cons.amount_cents,
                        "refund_id": info["refund_id"],
                        "source_batch_id": settled.get(part_key),
                    })
                    already[key] = used + cut
                    remaining -= cut
        return reversals

    def freeze_batch(self, batch_id: str, *, client_request_id: Optional[str] = None,
                     at: Optional[str] = None) -> dict:
        """冻结批次：计算分成、税费、保底，并自动生成退款冲正。

        冻结后的快照不可变；之后再确认的退款只能在后续批次冲正。
        """
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if batch_id not in world.batches:
            raise NotFound(f"结算批次不存在：{batch_id}")
        batch = world.batches[batch_id]
        if batch.status != BATCH_OPEN:
            raise IllegalState(f"批次 {batch_id} 状态 {batch.status}，仅开放批次可冻结")
        at_dt = parse_at(at) if at else now()

        settled_parts = world.settled_parts()
        already = self._already_reversed(world)

        # 1) 收集本批应入账的消费分项（按扫码时刻排序）
        candidates = []
        for r in world.redemptions.values():
            if r.edition_id != batch.edition_id:
                continue
            for part in r.parts:
                key = (r.redemption_id, part.part_index)
                if key in settled_parts:
                    continue
                if parse_at(batch.period_start) <= parse_at(r.scanned_at) <= parse_at(batch.period_end):
                    candidates.append((parse_at(r.scanned_at), r.redemption_id, part))
        candidates.sort(key=lambda x: (x[0], x[1], x[2].part_index))

        # 历史累计 gross（同商户同合同版本，决定阶梯档位）
        cum_gross: dict[tuple[str, int], int] = {}
        for b in world.batches.values():
            if b.status not in (BATCH_FROZEN, BATCH_PAID):
                continue
            for ln in b.lines:
                if ln.kind == "CONSUMPTION":
                    cum_gross[(ln.merchant_id, ln.contract_version_no)] = (
                        cum_gross.get((ln.merchant_id, ln.contract_version_no), 0) + ln.detail["gross_cents"])

        lines: list[SettlementLine] = []
        line_seq = 0
        new_part_lines: dict[tuple[str, int], SettlementLine] = {}

        def next_line_id() -> str:
            nonlocal line_seq
            line_seq += 1
            return f"{batch_id}-L{line_seq:03d}"

        # 2) 消费分项逐条按生效版本计价
        for scanned, redemption_id, part in candidates:
            redemption = world.redemptions[redemption_id]
            contract = world.contract_for(part.merchant_id, batch.edition_id)
            if contract is None:
                raise IllegalState(f"商户 {part.merchant_id} 缺少届次合同，无法结算")
            cv = contract.version_on(redemption.scanned_at)
            if cv is None:
                raise IllegalState(
                    f"商户 {part.merchant_id} 在扫码时刻 {redemption.scanned_at} 无生效合同版本")
            ck = (part.merchant_id, cv.version_no)
            pricing = price_part(cv.terms, gross_cents=part.gross_cents,
                                 cumulative_gross_cents=cum_gross.get(ck, 0),
                                 version_no=cv.version_no)
            cum_gross[ck] = cum_gross.get(ck, 0) + part.gross_cents
            subsidy = sum(
                world.bundles[redemption.bundle_id].items[e["item_id"]].subsidy_cents
                * e["amount_cents"]
                // world.bundles[redemption.bundle_id].items[e["item_id"]].face_value_cents
                for e in part.entries)
            line = SettlementLine(
                line_id=next_line_id(),
                merchant_id=part.merchant_id,
                kind="CONSUMPTION",
                amount_cents=pricing.merchant_cents,
                contract_version_no=cv.version_no,
                redemption_id=redemption_id,
                part_index=part.part_index,
                detail={
                    "gross_cents": part.gross_cents,
                    "platform_fee_cents": pricing.platform_fee_cents,
                    "mode": pricing.mode,
                    "share_bps": pricing.share_bps,
                    "tier_index": pricing.tier_index,
                    "scanned_at": redemption.scanned_at,
                    "store_id": part.store_id,
                    "bundle_id": redemption.bundle_id,
                    "spectator_id": redemption.spectator_id,
                    "subsidy_cents": subsidy,
                    "entries": part.entries,
                    "terms_snapshot": cv.terms,
                },
            )
            lines.append(line)
            new_part_lines[(redemption_id, part.part_index)] = line

        # 3) 退款冲正：本批新分项 + 已结算分项（后者引用来源批次/行）
        target = set(new_part_lines)
        reversals = self._allocate_refunds(world, target, already)
        for rev in reversals:
            key = (rev["redemption_id"], rev["part_index"])
            if key in new_part_lines:
                src = new_part_lines[key]
                gross_src = src.detail["gross_cents"]
                merchant_src = src.amount_cents
                source_batch_id, source_line_id = batch_id, src.line_id
            else:
                src_batch = world.batches[rev["source_batch_id"]]
                src = next(ln for ln in src_batch.lines
                           if ln.kind == "CONSUMPTION" and ln.redemption_id == rev["redemption_id"]
                           and ln.part_index == rev["part_index"])
                gross_src, merchant_src = src.detail["gross_cents"], src.amount_cents
                source_batch_id, source_line_id = src_batch.batch_id, src.line_id
            cut = rev["gross_reverse_cents"]
            merchant_rev = merchant_src * cut // gross_src
            fee_rev = src.detail["platform_fee_cents"] * cut // gross_src
            lines.append(SettlementLine(
                line_id=next_line_id(),
                merchant_id=src.merchant_id,
                kind=ADJ_REVERSAL,
                amount_cents=-merchant_rev,
                contract_version_no=src.contract_version_no,
                redemption_id=rev["redemption_id"],
                part_index=rev["part_index"],
                refund_id=rev["refund_id"],
                source_batch_id=source_batch_id,
                source_line_id=source_line_id,
                detail={"gross_cents": -cut, "platform_fee_cents": -fee_rev,
                        "entries": [{"item_id": rev["item_id"], "amount_cents": cut}],
                        "reason": "REFUND_AFTER_OR_IN_PERIOD"},
            ))

        # 4) 开放批次上已登记的手工调整（补付/冲正）随之冻结
        manual = [ln for ln in batch.lines if ln.kind in (ADJ_TOPUP, ADJ_REVERSAL)]
        for adj in manual:
            adj.line_id = next_line_id()
            lines.append(adj)

        # 5) 保底补足与税费（按商户汇总）
        # 只处理本批有结算行的商户；保底只对本批存在正向消费或补付的
        # 商户计算——跨月纯冲正批次不会凭空产生新的保底。
        merchants = sorted({ln.merchant_id for ln in lines})
        month = _month_key(batch.period_end)
        totals_by_merchant: dict[str, dict] = {}
        for merchant_id in merchants:
            m_lines = [ln for ln in lines if ln.merchant_id == merchant_id]
            earned = sum(ln.amount_cents for ln in m_lines if ln.kind == "CONSUMPTION")
            reversed_ = sum(-ln.amount_cents for ln in m_lines if ln.kind == ADJ_REVERSAL)
            topups = sum(ln.amount_cents for ln in m_lines if ln.kind == ADJ_TOPUP)
            net_earned = earned - reversed_ + topups
            has_positive_activity = earned > 0 or topups > 0

            # 该商户在本批次所属月份、此前已冻结批次中的累计
            prior_consumption = prior_reversal = prior_topup = prior_guarantee = 0
            for b in world.batches.values():
                if b.status not in (BATCH_FROZEN, BATCH_PAID):
                    continue
                for ln in b.lines:
                    if ln.merchant_id != merchant_id or ln.detail.get("period_month") != month:
                        continue
                    if ln.kind == "CONSUMPTION":
                        prior_consumption += ln.amount_cents
                    elif ln.kind == ADJ_REVERSAL:
                        prior_reversal += -ln.amount_cents
                    elif ln.kind == ADJ_TOPUP:
                        prior_topup += ln.amount_cents
                    elif ln.kind == "GUARANTEE_TOPUP":
                        prior_guarantee += ln.amount_cents

            guarantee = 0
            cv_no = None
            if has_positive_activity:
                contract = world.contract_for(merchant_id, batch.edition_id)
                if contract is not None:
                    cv = contract.version_on(batch.period_end)
                    if cv is not None:
                        month_earnings = (prior_consumption - prior_reversal + prior_topup
                                          + net_earned)
                        # 当月应累计达保底 G：新补 = max(0, G - 当月已得 - 已付保底)
                        g_amount = int(cv.terms.get("monthly_guarantee_cents", 0) or 0)
                        guarantee = max(0, g_amount - month_earnings - prior_guarantee)
                        cv_no = cv.version_no
            if guarantee > 0:
                gl = SettlementLine(
                    line_id=next_line_id(), merchant_id=merchant_id,
                    kind="GUARANTEE_TOPUP", amount_cents=guarantee,
                    contract_version_no=cv_no,
                    detail={"period_month": month,
                            "earned_cents_in_month": prior_consumption - prior_reversal
                            + prior_topup + net_earned,
                            "reason": "MONTHLY_GUARANTEE_SHORTFALL"})
                lines.append(gl)
                net_earned += guarantee

            # 代扣税费只对净应付为正的部分计提；净为负（冲正追回）不计税
            tax_bps = world.merchants[merchant_id].tax_withholding_bps
            tax = net_earned * tax_bps // 10_000 if net_earned > 0 else 0
            if tax > 0:
                tl = SettlementLine(
                    line_id=next_line_id(), merchant_id=merchant_id,
                    kind="TAX", amount_cents=-tax,
                    detail={"period_month": month, "withholding_bps": tax_bps,
                            "tax_base_cents": net_earned})
                lines.append(tl)

            payable = net_earned - tax
            totals_by_merchant[merchant_id] = {
                "gross_cents": sum(ln.detail.get("gross_cents", 0)
                                   for ln in m_lines if ln.kind == "CONSUMPTION"),
                "consumption_cents": earned,
                "reversal_cents": reversed_,
                "topup_cents": topups,
                "guarantee_topup_cents": guarantee,
                "tax_cents": tax,
                "payable_cents": payable,
            }

        totals = {
            "by_merchant": totals_by_merchant,
            "gross_cents": sum(v["gross_cents"] for v in totals_by_merchant.values()),
            "payable_cents": sum(v["payable_cents"] for v in totals_by_merchant.values()),
            "tax_cents": sum(v["tax_cents"] for v in totals_by_merchant.values()),
            "platform_fee_cents": sum(
                ln.detail.get("platform_fee_cents", 0)
                for ln in lines if ln.kind == "CONSUMPTION")
            - sum(-ln.detail.get("platform_fee_cents", 0)
                  for ln in lines if ln.kind == ADJ_REVERSAL),
            "subsidy_cents": sum(
                ln.detail.get("subsidy_cents", 0)
                for ln in lines if ln.kind == "CONSUMPTION"),
            "line_count": len(lines),
        }
        payload = {"period_start": batch.period_start, "period_end": batch.period_end,
                   "frozen_at": iso(at_dt), "totals": totals,
                   "lines": [asdict(ln) for ln in lines]}
        event = self._event(SETTLEMENT_FROZEN, batch_id,
                            f"冻结结算批次 {batch_id}，共 {len(lines)} 行", payload, at=at_dt)
        result = {"batch_id": batch_id, "status": BATCH_FROZEN, "totals": totals,
                  "lines": [asdict(ln) for ln in lines]}
        self._append([event], client_request_id=client_request_id, result=result)
        return result

    def mark_batch_paid(self, batch_id: str, *, client_request_id: Optional[str] = None,
                        at: Optional[str] = None) -> dict:
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if batch_id not in world.batches:
            raise NotFound(f"结算批次不存在：{batch_id}")
        batch = world.batches[batch_id]
        if batch.status != BATCH_FROZEN:
            raise IllegalState(f"批次 {batch_id} 状态 {batch.status}，仅已冻结批次可标记支付")
        event = self._event(SETTLEMENT_PAID, batch_id,
                            f"批次 {batch_id} 已支付", {"paid_at": iso(parse_at(at) if at else now())},
                            at=at)
        result = {"batch_id": batch_id, "status": BATCH_PAID}
        self._append([event], client_request_id=client_request_id, result=result)
        return result

    def post_correction(self, open_batch_id: str, *, merchant_id: str,
                        amount_cents: int, kind: str, reason: str,
                        redemption_id: Optional[str] = None, part_index: Optional[int] = None,
                        source_batch_id: Optional[str] = None, source_line_id: Optional[str] = None,
                        entries: Optional[list[dict]] = None,
                        client_request_id: Optional[str] = None,
                        at: Optional[str] = None) -> dict:
        """在开放批次登记补付（TOPUP，正）或冲正（REVERSAL，负）。

        用于已付款后发现的差异——旧批次不动，差异在新批次表达。
        amount_cents 对 TOPUP 为正、对 REVERSAL 传正数（行金额记负）。
        """
        cached = self._replay(client_request_id)
        if cached is not None:
            return cached
        world = self._world()
        if open_batch_id not in world.batches:
            raise NotFound(f"结算批次不存在：{open_batch_id}")
        batch = world.batches[open_batch_id]
        if batch.status != BATCH_OPEN:
            raise IllegalState("调整只能登记在开放批次；已冻结/支付批次不可改写")
        if merchant_id not in world.merchants:
            raise NotFound(f"商户不存在：{merchant_id}")
        if kind not in (ADJ_TOPUP, ADJ_REVERSAL):
            raise ValueError("kind 必须是 TOPUP 或 REVERSAL")
        if amount_cents <= 0:
            raise ValueError("调整金额必须为正（冲正自动记负）")
        if (redemption_id is not None) != (part_index is not None):
            raise ValueError("redemption_id 与 part_index 必须同时给出")
        signed = amount_cents if kind == ADJ_TOPUP else -amount_cents
        line = SettlementLine(
            line_id="",  # 冻结时统一编号
            merchant_id=merchant_id,
            kind=kind,
            amount_cents=signed,
            redemption_id=redemption_id,
            part_index=part_index,
            source_batch_id=source_batch_id,
            source_line_id=source_line_id,
            detail={"reason": reason,
                    "entries": entries or []})
        payload = {"lines": [asdict(line)]}
        event = self._event(ADJUSTMENT_POSTED, open_batch_id,
                            f"批次 {open_batch_id} 登记{kind}调整：{reason}", payload, at=at)
        result = {"batch_id": open_batch_id, "kind": kind, "amount_cents": signed, "reason": reason}
        self._append([event], client_request_id=client_request_id, result=result)
        return result
