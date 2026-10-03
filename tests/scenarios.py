"""测试场景工厂：快速搭好一届赛事、商户、合同与权益包。"""

from __future__ import annotations

from src.settlement import App

EDITION = "ED1"
# 2026-09-20/21 比赛，赛前 9.10 起，赛后窗口至 9.27
RACE_START = "2026-09-20T07:00:00+08:00"
RACE_END = "2026-09-21T12:00:00+08:00"
PRE_START = "2026-09-10T00:00:00+08:00"
POST_END = "2026-09-27T23:59:59+08:00"
SEPT_START = "2026-09-01T00:00:00+08:00"
SEPT_END = "2026-09-30T23:59:59+08:00"
OCT_START = "2026-10-01T00:00:00+08:00"
OCT_END = "2026-10-31T23:59:59+08:00"


def build_app(path: str = ":memory:") -> App:
    return App(path)


def seed_world(app: App, *, merchants: dict[str, dict] | None = None) -> App:
    """登记一届赛事与若干商户（默认 M1 阶梯分成、M2 固定额）。"""
    s = app.service
    s.register_edition(EDITION, "2026 城市路跑",
                       race_start=RACE_START, race_end=RACE_END,
                       pre_start=PRE_START, post_end=POST_END)
    merchants = merchants or {
        "M1": {"name": "特许商甲", "tax": 600,
               "terms": {"mode": "TIERED_SHARE", "platform_fee_bps": 500,
                         "tiers": [{"up_to_cents": 100000, "share_bps": 8000},
                                   {"up_to_cents": 10_000_000, "share_bps": 9000}],
                         "monthly_guarantee_cents": 0}},
        "M2": {"name": "商圈乙", "tax": 600,
               "terms": {"mode": "FIXED", "fixed_cents": 4000}},
    }
    for mid, cfg in merchants.items():
        s.register_merchant(mid, cfg["name"], tax_withholding_bps=cfg.get("tax", 0))
        s.effective_contract_version(f"C-{mid}", merchant_id=mid, edition_id=EDITION,
                                     terms=cfg["terms"],
                                     effective_from="2026-09-01T00:00:00+08:00")
    return app


def issue_standard_bundle(app: App, bundle_id: str = "B1", spectator: str = "U1",
                          items: list[dict] | None = None) -> dict:
    items = items or [
        {"item_id": "I1", "phase": "PRE_RACE", "title": "赛前套餐",
         "face_value_cents": 20000, "subsidy_cents": 5000},
        {"item_id": "I2", "phase": "RACE_DAY", "title": "场内水券",
         "face_value_cents": 1000, "subsidy_cents": 0},
        {"item_id": "I3", "phase": "POST_RACE", "title": "文旅权益",
         "face_value_cents": 30000, "subsidy_cents": 10000},
    ]
    return app.service.issue_bundle(bundle_id, spectator_id=spectator,
                                    edition_id=EDITION, items=items)
