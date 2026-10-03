"""商户合同计费引擎。

合同条款按「生效版本」计算：每个合同有若干版本，各带 effective_from，
取业务时点（核销发生 scanned_at）生效的版本。旧账从不改写——版本切换
后，新核销适用新版本，历史核销留在历史批次；版本切换对已冻结批次造成
的差异由结算服务以冲正（REVERSAL）/补付（TOPUP）表达。

支持条款（terms）：
- {"mode": "FIXED", "fixed_cents": N}
    每笔核销商户固定所得 N 分；
- {"mode": "TIERED_SHARE", "platform_fee_bps": B,
    "tiers": [{"up_to_cents": Q, "share_bps": S}, ...],
    "period": "BATCH"|"MONTHLY"|"EDITION",
    "monthly_guarantee_cents": G?}
    阶梯分成：按周期累计 gross 落入的阶梯确定商户分成比例；
    平台抽佣 platform_fee_bps 与商户分成 share_bps 都基于 gross；
    保底 monthly_guarantee_cents：周期内商户分成不足保底时补足。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import ContractError
from .model import TERM_FIXED, TERM_TIERED_SHARE


@dataclass(frozen=True)
class PartPricing:
    gross_cents: int          # 核销面值（游客消费额）
    merchant_cents: int       # 商户应得（抽佣后）
    platform_fee_cents: int   # 平台抽佣
    share_bps: int            # 实际适用分成（万分比）
    tier_index: int
    version_no: int
    mode: str


def _tier_for(gross_cumulative: int, tiers: list[dict]) -> tuple[int, int]:
    """返回 (阶梯序号, 商户分成 bps)。阶梯按 up_to_cents 升序，末档兜底。"""
    ordered = sorted(tiers, key=lambda t: t["up_to_cents"])
    chosen_idx = len(ordered) - 1
    for idx, tier in enumerate(ordered):
        if gross_cumulative <= tier["up_to_cents"]:
            chosen_idx = idx
            break
    return chosen_idx, ordered[chosen_idx]["share_bps"]


def price_part(
    terms: dict,
    *,
    gross_cents: int,
    cumulative_gross_cents: int,
    version_no: int,
) -> PartPricing:
    """对一笔跨店拆单的商户分项计价。

    cumulative_gross_cents 为本周期内该商户此前已入账的累计 gross，
    用于确定阶梯档位（累计制，而非单笔制）。
    """
    mode = terms.get("mode")
    if gross_cents <= 0:
        raise ContractError("核销金额必须为正")
    if mode == TERM_FIXED:
        fixed = int(terms["fixed_cents"])
        if fixed < 0:
            raise ContractError("固定额不能为负")
        # 商户所得不得超过该分项消费面值（防止小消费按大固定额凭空造钱）
        merchant = min(fixed, gross_cents)
        return PartPricing(
            gross_cents=gross_cents,
            merchant_cents=merchant,
            platform_fee_cents=gross_cents - merchant,
            share_bps=0,
            tier_index=0,
            version_no=version_no,
            mode=mode,
        )
    if mode == TERM_TIERED_SHARE:
        tiers = terms.get("tiers")
        if not tiers:
            raise ContractError("阶梯分成条款缺少 tiers")
        fee_bps = int(terms.get("platform_fee_bps", 0))
        after = cumulative_gross_cents + gross_cents
        _, share_bps = _tier_for(after, tiers)
        merchant = gross_cents * share_bps // 10_000
        fee = gross_cents * fee_bps // 10_000
        if fee + merchant > gross_cents:
            raise ContractError("分成与抽佣之和超过消费金额")
        idx = 0
        for i, t in enumerate(sorted(tiers, key=lambda t: t["up_to_cents"])):
            if after <= t["up_to_cents"]:
                idx = i
                break
        else:
            idx = len(tiers) - 1
        return PartPricing(
            gross_cents=gross_cents,
            merchant_cents=merchant,
            platform_fee_cents=fee,
            share_bps=share_bps,
            tier_index=idx,
            version_no=version_no,
            mode=mode,
        )
    raise ContractError(f"未知计费模式：{mode}")


def guarantee_topup(terms: dict, earned_cents: int) -> int:
    """周期保底：返回应补足金额（不足保底时为正，否则为 0）。"""
    if terms.get("mode") != TERM_TIERED_SHARE:
        return 0
    guarantee = int(terms.get("monthly_guarantee_cents", 0) or 0)
    return max(0, guarantee - earned_cents)


def validate_terms(terms: dict) -> None:
    mode = terms.get("mode")
    if mode not in (TERM_FIXED, TERM_TIERED_SHARE):
        raise ContractError(f"未知计费模式：{mode}")
    if mode == TERM_FIXED:
        if int(terms.get("fixed_cents", -1)) < 0:
            raise ContractError("固定额不能为负")
        return
    tiers = terms.get("tiers")
    if not tiers or not isinstance(tiers, list):
        raise ContractError("阶梯分成条款缺少 tiers")
    last = None
    for t in tiers:
        if t["up_to_cents"] <= 0:
            raise ContractError("阶梯上限必须为正")
        if not 0 <= int(t["share_bps"]) <= 10_000:
            raise ContractError("分成比例必须在 0..10000 bps")
        if last is not None and t["up_to_cents"] <= last:
            raise ContractError("阶梯上限必须按声明顺序递增")
        last = t["up_to_cents"]
    if not 0 <= int(terms.get("platform_fee_bps", 0)) <= 10_000:
        raise ContractError("平台抽佣比例非法")
    if int(terms.get("monthly_guarantee_cents", 0) or 0) < 0:
        raise ContractError("保底金额不能为负")
