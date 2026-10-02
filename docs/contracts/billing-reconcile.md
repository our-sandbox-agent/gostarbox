# Billing reconciliation: units/money split, at-most-once corrections (#28)

Proposed，2026-10-03。Implements the in-repo slice of issue #28:
`scripts/billing_reconcile.py`（stdlib only）是**契約層語意庫**，不是
runtime — 零網路呼叫、零金鑰、零 SDK。遠端互動（meter cancel/adjust、
credit note、draft amendment、以 idempotency id 查詢）全部以注入
callable 模型化；測試 `scripts/test_billing_reconcile.py`（34 tests）
以 `CorrectionProviderDouble` 扮演 provider 端去重。真實 Stripe
endpoints、真實發票與 test-mode 驗證是本文件末段的 runtime TODO，
本票在此 repo 內無法執行。

規範來源：issue #28 驗收條件。**議題明文拒絕「Stripe 不能撤回也不能推
負數」的舊假設**，並引用官方文件作為模型依據：

- meter event 撤銷/調整 endpoint（官方存在，可撤已回報事件）：
  https://docs.stripe.com/api/billing/meter-event-adjustment/create
- usage recording 支援負總量處理：
  https://docs.stripe.com/billing/subscriptions/usage-based/recording-usage-api

Pending aggregate 語意**直接複用** #27（`stripe_bridge.ReconciliationBlocked`
與 pending/ready 詞彙）；跨月調整線**直接對接** #26 usage engine
（`adjustments` 參數、sealed 不可變語意）。

## 1. 對帳列 ReconcileRow：UNITS 與 MONEY 分欄

每列一個 (month, workspace, meter)：

| 欄位 | 意義 |
|---|---|
| `local_units` | 本地確認 units（usage engine confirmed/sealed lines，經 `local_lines()` 轉入） |
| `provider_units` | Stripe 已彙總 units（pending 期間可能帶**暫定值**，僅顯示，永不判對） |
| `provider_status` | `pending` / `ready`（stripe_bridge 詞彙） |
| `in_flight_units` | 尚處理中 events（已送出、未彙總） |
| `invoice_minor` | invoice 金額（minor units） |
| `credits_applied_minor` | 已抵扣金額（來自 ledger 之 money corrections 淨額） |

**分欄是硬規則**：`credits_applied_minor` 只能歸零 MONEY gap，**絕不**
改變任何 units 欄或掩蓋數量差（不把 credit 當 units 被修好了）。
分欄同時是結構性的：`Correction` 建構即拒絕 mixed units+money
（meter_adjustment 必 `units != 0, minor == 0`；credit/draft 必
`minor != 0, units == 0`）。

## 2. diff：數量差 vs 金額差；pending 永不判

`diff(local, provider)`：

- **pending aggregate 直接 raise `ReconciliationBlocked`**（複用
  #27 例外類別）——即使 provider 已暴露暫定彙總數字也**永不**拿來判對。
- ready 時：
  - `units_gap = local_units - (provider_units + in_flight_units)`
    （in-flight 殘差先扣除：異步延遲不是 mismatch）；
  - `money_gap = local_minor - (invoice_minor - credits_applied_minor)`
    （已抵扣金額先歸入 MONEY 面）；
  - kind：`units_gap != 0` → **`units_mismatch`**（單位差 → 單位修法）；
    units 相等而 `money_gap != 0` → **`money_mismatch`**（金額差 →
    credit/amendment）；無發票 → `no_invoice`；否则 `match`。
- 數量差與金額差**分流**：單位修法走 provider meter adjustment，
  金額隨重彙總自然修正，**不**即時開 credit；金額差絕不可能被記成
  units 已修。

## 3. 修正行動對映（cancel/adjustment mapping）

| 差異 | invoice 狀態 | 行動 | 對象 |
|---|---|---|---|
| units mismatch | 任何 | `meter_adjustment`（cancel/resend，注入動作；官方 endpoint 見上） | provider meter |
| money mismatch | `draft`（未定版） | `draft_amendment`（可在 draft 上改 line） | `draft_line` |
| money mismatch | `finalized`（已定版） | `credit_note`（調整落在**下一張**發票；原帳單不可變、永不改動） | `next_invoice` |

**限制註記**（模型化為注入動作的原因）：官方 meter adjustment 可撤
單筆 event（by event id / identifier）或 reset 一段區間，但**不能**改寫
任意彙總總量；把 `units_gap` 映射成正確的 cancel/resend 呼叫是 runtime
接線的工作，本票只定語意。負總量（credit/補收）依官方 usage 文件可行。

## 4. 恢復情境（recovery scenarios）

| 情境 | 語意 |
|---|---|
| API 已接受但本地 ack 遺失 | 重跑時 ledger 以 correction id（= idempotency key）**查詢** provider（READ）：有 receipt → 記為 `recovered`，**不再呼叫動作**（無雙重調整） |
| 部分成功 | 逐 meter 獨立：某 meter 動作失敗 → `failed`（可重試），其餘 `applied`；重跑時已確認者 `already_applied` **不再重送**，失敗者重試 |
| 跨月結算 | 修正**歸原月**：correction.month = 原月份，`to_adjustment_line()` 產出 `period = 原月` 的 adjustment line；sealed 原線**逐欄不變**、totals 不重算（usage engine §已封帳語意）；已定版帳單之差額走**下一張**發票 |
| 異步延遲 | 未彙總 events 進 `in_flight_units` 欄，殘差為 0 時**不是 mismatch**；整體 pending 時為 `awaiting_aggregation`，只有超過 ready deadline 才升級 `stalled`（升級仍不判對、不產生 correction） |

## 5. Correction：唯一 id、審計、最多一次

- **唯一 correction id**：`corr:{month}:{workspace}:{meter}:{kind}`
  ——確定性函數（同一差異每次重跑得到同一 id），這是 `--repair`
  重跑不重複的唯一保證基礎。
- **審計**：`approved_by`（誰）、`approved_at_ms`（何時）、`evidence`
  （觸發修正的完整對帳列 snapshot + gaps）；ledger `to_dict()/from_dict()`
  持久全部證據——**每日備份還原後可重算核對，snapshot 過期不丟證據**。
- **最多一次**（三層）：
  1. `ledger.apply()` 以 id 去重——同 correction apply 兩次，第二次
     `already_applied`、不碰動作；
  2. lost-ack：以 id 查 provider 得 receipt → `recovered`，不重送；
  3. 金額面：已套用 credit 計入 `credits_applied_minor`，重跑時 money
     gap 歸零 → 不再提案（雙保險）。
- 動作拋出例外時 ledger **不記錄**——correction 保持可重試。

## 6. 第一版：人工批准（不自動對客戶重複扣款）

- `reconcile(..., approved_by="")`（預設）→ 一切修正僅 **proposed**，
  零 provider 寫入、零 ledger 記錄；
- `CorrectionsLedger.apply()` 對空 `approved_by` **直接拒絕**
  （ValueError）——即使繞過 reconciler 也無法自動套用；
- 核可後重跑（同輸入 + `approved_by`）套用的**正是**先前提案的同一組
  確定性 id。

## 7. Fixtures 與 guards

`scripts/test_billing_reconcile.py`（34 tests）：分欄（credit 不減
units、涵蓋金額的 credit 不掩蓋 units gap、mixed correction 拒絕）、
units/money kind 分流、輸入驗證、pending 阻擋（#27 例外複用）、
deadline 容忍與 stalled 升級、in-flight 殘差容忍、lost-ack 恢復
（READ 不重送）、部分成功重試（confirmed 不重送）、跨月（usage engine
整合：sealed 原線逐欄不變、adjustment line 歸原月）、draft vs
finalized 分流、apply 兩次 no-op、`--repair` 重跑（units →
already_applied；money → credit 歸零 match）、snapshot 還原（證據保留、
無重複）、人工批准（未批准僅提案；ledger 拒絕未批准；審計欄位）。

Mutation guards（8 個，CONTRIBUTING mutate-and-fail）：pending 可判對
（`_judgeable`）、in-flight 忽略（`_units_gap`）、ledger 去重失效
（`_known`）、恢復改用 write（`_remote_receipt`）、deadline 立即升級
（`_deadline_passed`）、finalized 被 draft 改寫（`_money_target`）、
批准全過（`_approval_ok`）、id 帶 nonce（`_correction_id` → 重跑雙重
credit）。

## Runtime TODO（未建、不聲稱）

- **真實 endpoints 未接**：`meter_adjustment` / `credit_note` /
  `draft_amendment` 皆為注入 callable；`CorrectionProviderDouble` 是語意
  替身，不是行為保證。接線時需對準官方
  meter-event-adjustment cancel 語意（by event id / identifier / range
  reset）與負總量表示法。
- **真實發票未接**：invoice minor 金額、draft/finalized 狀態與「下一張
  發票」的實際落點（next invoice adjustment line / credit note）由跨票
  整合落地；本票只定義狀態分流語意。
- **ready deadline 與聚合輪詢**：deadline 是注入參數，實際值待 test-mode
  實測聚合延遲後訂定。
- 每日備份還原之端到端重算（含 provider 端重查）是 test-mode
  checklist 項目，本 repo 內無法執行。

### Runtime-blocked list（本票不做，永遠不做）

- **live key：永不**。本 repo 任何 slice 不得持有、讀取或請求 live key。
- 不刷真卡、不真收款、不出真帳單。
- 不自動對客戶重複扣款：第一版修正一律人工批准（結構性強制）。
- 不判對 pending aggregate（`ReconciliationBlocked` 已是契約）。

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
npm test
npm run build
```
