# 赛事跨业消费清算后端

一套贯通 **赛事届次 → 观赛人 → 权益包 → 商户合同 → 核销凭证 → 退款 → 平台补贴 → 税费 → 结算批次** 的跨业消费结算后端。赛前套餐、场内特许商品、商圈「第二现场」、赛后文旅权益共用同一套核销与清算口径。

外部消息身份与版本语义延续 `contracts/domain.schema.json` 的事件信封：
`event_id / event_type / aggregate_type / aggregate_id / occurred_at / version / summary`，业务数据统一放入 `payload`。

## 核心原则

- **只追加（append-only），旧账永不改写。** 所有状态由事件流折叠得到；退款、差异纠错都以新事件表达（`REFUND_CONFIRMED`、`ADJUSTMENT_POSTED`）。已冻结/已支付批次是不可变快照，事后退款只在后续批次生成**冲正（REVERSAL）**，少付通过**补付（TOPUP）**处理，冲正/补付行带 `source_batch_id/source_line_id` 指回旧账。
- **两层幂等，杜绝重复入账。**
  - `client_request_id`（或 HTTP `Idempotency-Key` 头）：同一请求重放返回首次结果，不产生新事件；
  - 业务键：核销二维码 `scan_code_id` 全局唯一，换请求号重复扫码报 `DUPLICATE_REDEMPTION`；聚合 `(aggregate_id, version)` 唯一约束仲裁并发双写。
- **哈希链防篡改。** 每条事件携带前一条事件哈希，`verify_chain()` 可发现任何对旧账的改写。
- **金额一律整数「分」**，阶梯/比例用整数万分比（bps）计算。

## 领域模型

| 聚合 | 说明 |
|---|---|
| `event_edition` | 赛事届次，含赛前/赛中/赛后三段时间窗；支持改期 |
| `merchant` | 商户，含代扣税率 |
| `merchant_contract` | 商户在某届次下的合同，含多个**生效版本**（`effective_from` + `version_no`） |
| `benefit_bundle` | 发给观众的权益包，含若干权益项（面值 = 平台补贴 + 游客自付，各自带有效窗口） |
| `redemption_record` | 一次核销凭证，支持跨店拆单 `parts[]`、离线补传 `offline` |
| `refund` | 退款单（申请 → 确认），游客到账按自付占比等比计算 |
| `settlement_batch` | 结算批次（开启 → 冻结 → 支付），冻结行是不可变快照 |

### 权益有效窗口

权益项分 `PRE_RACE / RACE_DAY / POST_RACE` 三阶段，发放时可显式给窗口，否则按届次时间派生。
核销以**实际扫码时刻 `scanned_at`** 判定是否在窗口内——离线补传即使在赛后上传，只要扫码发生在有效窗口仍有效；窗口外一律 `WINDOW_CLOSED`。

### 核销防重与防双花

- 同一 `scan_code_id` 只能入账一次（重复扫描拒绝）；
- 同一请求号的重试（网络抖动、离线补传重试）原样回放首次结果；
- 同一权益项累计核销不得超过面值，离线设备的「双花」在补传折叠时被 `INSUFFICIENT_BALANCE` 拒绝；
- 跨店拆单是一条核销凭证下的多个 `parts`，每个商户分项独立结算。

### 改期：只迁移仍可履约的部分

`reschedule_edition`：
- 已核销事实不动；未使用/剩余额度的窗口按时间差整体平移，保留已用额度；
- 显式声明无法在新日期履约的商户，其**专属权益项**取消并按剩余面值自动发起并确认退款；
- 退款流程中、已退款、已全额核销的权益项不迁移；通用权益项随届次迁移。

### 合同条款（按业务时点的生效版本）

每笔核销取**扫码时刻生效的合同版本**计价，新版本不溯及旧账。支持：

- `FIXED`：每笔分项固定商户所得（且不超过该分项面值，防止小消费套大固定额）；
- `TIERED_SHARE`：阶梯商户分成（按周期累计 gross 落档）+ 平台抽佣 `platform_fee_bps`；
- 月度保底 `monthly_guarantee_cents`：当月分成不足保底时在当月批次补足；跨月纯冲正批次不凭空产生新保底。

冻结批次时逐行固化 `terms_snapshot` 与 `contract_version_no`，结算依据可追溯。

### 结算与退款冲正

冻结批次时：收集周期内未结算的消费分项 → 按生效版本计价 → 计提平台补贴分摊与代扣税费 → 保底补足 → 把**已确认退款按 FIFO 分摊到消费明细**：落在本批新分项的直接净额，落在历史已结算分项的生成带 `source_batch_id/source_line_id` 的冲正行。

## 三类访问者与数据边界

| 角色 | 能看到 |
|---|---|
| `operator` 运营 | 全量；可从一笔结算行追到实际消费、权益项、退款单与合同版本依据；可读原始事件流与哈希链 |
| `spectator` 游客 | 仅本人权益包的可用权益/窗口/余额，以及本人退款进度与到账金额 |
| `merchant` 商户 | 仅与自身履约有关的数据：本门店核销分项、本商户结算行与合同版本；视图对其他游客标识脱敏 |

边界在读侧强制（`src/settlement/views.py`），越权抛 `ACCESS_DENIED`。写接口仅 `operator` 可调。

## 代码结构

```
contracts/domain.schema.json   # 事件信封 + 稳定枚举 + 事件目录（向后兼容起点样例）
data/sample.json               # 起点最小样例（未改动，仍有效）
data/sample_lifecycle.json     # 完整生命周期事件流（确定性、可复现）
examples/generate_sample.py    # 生成生命周期样例
src/validator.py               # 公共信封校验
src/settlement/
  envelope.py                  # 信封、事件目录、Event
  store.py                     # SQLite 只追加存储：幂等表、版本约束、哈希链
  model.py                     # 状态模型与事件折叠 fold()
  times.py / money.py          # 有效窗口 / 整数金额
  contracts_engine.py          # 固定额 / 阶梯分成 / 保底计价
  services.py                  # 写侧领域服务（全部业务不变量）
  views.py                     # 读侧投影 + 三类访问者权限
  httpapi.py / server.py       # 标准库 HTTP JSON 接口 / 启动入口
  app.py                       # 组装门面
tests/                         # 64 个 unittest
```

写侧只追加事件、读侧实时折叠，无 ORM、无第三方运行时依赖（Python ≥ 3.11 标准库）。

## HTTP 接口

身份用请求头表达：`X-Actor-Role: operator|spectator|merchant`，`X-Actor-Id: <观众ID或商户ID>`；幂等可用 `Idempotency-Key` 头。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/editions` `/merchants` | 登记届次 / 商户 |
| POST | `/contracts/{id}/versions` | 登记合同生效版本 |
| POST | `/bundles/{id}` | 发放权益包 |
| POST | `/redemptions` | 核销（跨店拆单、离线补传） |
| POST | `/refunds`、`/refunds/{id}/confirm` | 退款申请 / 确认 |
| POST | `/editions/{id}/reschedule` | 改期迁移 |
| POST | `/batches`、`/batches/{id}/freeze`、`/batches/{id}/pay` | 批次开启 / 冻结 / 支付 |
| POST | `/batches/{id}/adjustments` | 在开放批次登记补付/冲正 |
| GET | `/bundles/{id}`、`/bundles/{id}/refunds` | 游客权益 / 退款进度 |
| GET | `/merchant/redemptions` `/merchant/settlements` `/merchant/contracts` | 商户视图 |
| GET | `/operator/batches/{id}` `/operator/lines/{lineId}/trace` `/operator/events` | 运营追溯 / 审计 |

错误码：`NOT_FOUND(404) / ACCESS_DENIED(403) / DUPLICATE_REDEMPTION(409) / INSUFFICIENT_BALANCE(409) / CONFLICT(409) / WINDOW_CLOSED(422) / ILLEGAL_STATE(422) / CONTRACT_ERROR(422) / BAD_REQUEST(400)`。

启动：

```bash
python3 -m src.settlement.server --db ./settlement.db --port 8080
```

## 本地检查

```bash
python3 -m unittest discover -s tests          # 全部 64 个测试
python3 examples/generate_sample.py            # 重新生成生命周期样例并校验哈希链
```

测试覆盖：信封/哈希链/幂等、三段窗口、重复扫码与离线双花、跨店拆单、合同生效版本与阶梯/固定/保底、税费补贴分摊、改期只迁移可履约部分、已付款后冲正与补付且旧账不变、三类视图权限隔离、HTTP 鉴权与幂等。
