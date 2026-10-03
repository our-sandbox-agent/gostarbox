# Stripe bridge: outbox, webhook idempotence, metering timestamps (#27)

Proposed，2026-10-03。Implements the in-repo slice of issue #27:
`scripts/stripe_bridge.py`（stdlib only）是**契約層語意庫**，不是 runtime —
零網路呼叫、零金鑰、零 SDK。遠端互動（transport、status query、簽章驗證、
aggregation poll）全部以注入 callable 模型化；測試
`scripts/test_stripe_bridge.py`（41 tests）以 `ProviderDouble` 扮演
provider 端去重。真實 Stripe test-mode 驗證是本文件末段的 checklist，
需要真帳號，本票在此 repo 內無法執行。

規範來源：issue #27 驗收條件；issue 自身連結指出 meter usage 為
**非同步彙總**（送出後不可立即判對帳）且 meter event 自帶 timestamp。

## 1. Outbox：先記待送，再發送／確認（OutboxRecorder）

狀態機 `pending -> sent -> confirmed`；`record(payload)` 先落本地意圖，
`send()` 經注入 transport 送出，`confirm()` 將 receipt 轉為持久。
Durability 模型：**send 的 receipt 在 confirm 之前是揮發性的**（crash 會
失去 response — 正是 remote-success/local-timeout 視窗）。

| 語意 | 規則 |
|---|---|
| Provider 去重鍵 | **outbox id**。遠端成功本機 timeout 後重送同一 entry：provider 去重回傳**原始 receipt**，帳只扣一次（`billings == 1`） |
| 已錄 receipt 的重送 | 直接回傳已錄 receipt，**不再碰 provider**（`_already_delivered`） |
| send 與 confirm 之間 crash | 重啟後 `recover()` 對 `sent` entry 走**冪等 status query**（READ）重新 confirm — 恢復絕不發新 write（`_remote_receipt`） |
| 未送出的 entry | `recover()` 補送（provider 去重保證只計一次）再 confirm |
| crash-point matrix | after-record / before-send / after-send / before-confirm × replay → **恰一張 provider receipt**、狀態收斂 = 乾淨執行；`recover()` 冪等 |

## 2. Webhook：驗簽、重送、亂序 idempotent（WebhookProcessor）

處理順序：**驗簽 → 解析 → event id 去重 → 封閉事件集檢查 → 套用**。

- **驗簽**：注入 verifier callable（`verify(raw_body_bytes, signature) ->
  bool`）。**恆定時間比較是硬性要求** — 內建 `hmac_verifier(secret)` 使用
  `hmac.compare_digest`；以 `==` 比對簽章會經 timing 逐位洩漏，測試掃描
  原始碼強制 `compare_digest` 存在。驗簽失敗 **不記錄** event id：同一事件
  補上正確簽章仍可處理。
- **重送**：apply-once per event id — 重送回傳**同一張儲存 receipt**，不重
  處理（meter error 只記一筆）；去重庫隨 `to_dict()` 持久，重啟後仍成立。
- **亂序**：單調 rank 狀態機，後到事件**絕不降級** — `invoice.paid` 早於
  `invoice.finalized` 到達收斂為 paid；`subscription.deleted` 早於
  `subscription.canceled` 收斂為 deleted；遲到的 `payment_failed` 不會
  un-pay 一張已付發票；retry 成功（failed 後 paid）回到 paid。
- **封閉事件集**（closed set，issue #27「不可把三種事件當完整生命週期」）：

  | 事件 | 效果 |
  |---|---|
  | `invoice.finalized` | 發票 state -> finalized（排序用生命週期事件） |
  | `invoice.paid` | 發票 state -> paid（rank 3） |
  | `invoice.payment_failed` | 發票 state -> payment_failed（rank 2），記 `failed_at_ms` |
  | `subscription.canceled` | 訂閱 state -> canceled（rank 1） |
  | `subscription.deleted` | 訂閱 state -> deleted（rank 2，terminal） |
  | `meter.error_reported` | 記錄 meter 錯誤（flag），apply-once |

  集合外的事件類型 → **logged（`ignored()`）+ 忽略，不 crash**； malformed
  body（非 JSON、缺 id/type）→ `rejected_malformed`，不 crash。
- `customer_state(customer_id)`：derived 付款狀態 — subscription
  deleted/canceled terminal 優先；任一發票 payment_failed → warn；否則
  paid；無證據 → open。

## 3. Meter：自帶發生時間 + 非同步彙總（MeterEvent / MeteringClient）

- **`occurred_at_ms` 是事件自己的時間**：report payload 的 `timestamp` 一律
  取 occurred time，**絕不用送出時間** — 月底最後一小時（例：10/31 23:30
  發生、11/1 00:05 送出）歸帳 2026-10，不歸 2026-11（`_payload_timestamp`
  mutation point + guard）。
- **非同步彙總**（issue 引 Stripe 文件）：`report()` 成功後
  `aggregate_status` 仍是 **`pending`**（即使 transport 即刻 ACK）；
  只有注入 poll 回報 ready 才轉 `ready`。**`reconcile()` 對 pending
  aggregate 直接拒絕**（`ReconciliationBlocked`）— 送出後不可立即判對帳
  錯誤。
- report 以 event id 去重：重送回傳已錄 payload，不二次送出。

## 4. 付費資格與未付款處置（BillingEligibility）

policy hook：`evaluate(payment_state, failed_at_ms=None, now_ms=0)` —
`{level, allowed, blocked, grace, disposition}`。可注入同介面物件覆寫。

| state | level | allowed / blocked | 處置（未付款處置明文） |
|---|---|---|---|
| `paid` | eligible | 全部允許 | 全額付款，正常服務 |
| `payment_failed`（寬限內） | warn | 既有續跑＋resume＋destroy；**provision 封鎖** | 寬限窗（預設 7 天，`grace_ms` 可調）內警告 |
| `payment_failed`（寬限滿） | blocked | 僅 destroy | **升級 suspend**：停排程但保留資料與 volumes；補繳或取消才解；**絕不因未付款自動銷毀** |
| `canceled` | blocked | 全封鎖 | 保留期內可匯出資料，期滿依保留政策清除 |
| `deleted` | blocked | 全封鎖 | terminal，不可 provision/resume |
| `open` | warn | continue/destroy；provision/resume 封鎖 | 尚無帳務證據，僅試用估價 |

寬限錨點來自 webhook 的 `failed_at_ms`（`last_failure`）；未知失敗時間
不自行到期（warn 維持到有真錨點）。

## 5. Fixtures 與 guards

`scripts/test_stripe_bridge.py`（41 tests）：crash-point matrix（4 點 ×
replay → 恰一次 billing、與乾淨執行逐欄相等、recover 冪等、恢復只讀不
寫）、remote-success-then-local-timeout 重送（`ProviderDouble(timeout_sends)`
先計帳再丟 TimeoutError）、驗簽拒絕（偽簽章、竄改 body、rejected 不鎖死
event）、`compare_digest` 原始碼掃描、重送同 receipt 單次處理（含重啟）、
亂序收斂（4 種排列同終態）、未知類型 logged-not-crash、
occurred_at 時間戳規則（月底最後一小時）、pending 阻擋 reconcile、
eligibility 矩陣與寬限升級。Mutation guards（9 個，
CONTRIBUTING mutate-and-fail）：時間戳改送出時間、pending 可判對帳、
驗簽全過、去重失效、亂序 last-write-wins、重送再打 provider、封閉集清空、
寬限永不升級、恢復改用 write。

## Runtime TODO（未建、不聲稱）

- **API/SDK 版本鎖定 TODO**：未選定也未 pin Stripe API 版本／SDK。接線時
  需 pin（例：`stripe-python == x.y.z`、`Stripe-Version` header 固定），
  並把 provider 端 idempotency key（= outbox id）與 webhook tolerance
  （時間戳容忍窗）對準本契約。
- **無真實 Stripe 整合**：本庫零網路；`ProviderDouble` 是語意替身，不是
  行為保證。
- **無 Checkout／subscription customer mapping 持久化**：customer 對應
  表由後續票（跨票整合）落地；本票只定義事件→狀態語意。

### Test-mode 驗證 checklist（需真 test-mode 帳號；本 repo 內無法執行）

- [ ] 鎖定 API/SDK 版本；test mode 建小型 subscription＋meter。
- [ ] 單位與 decimal：meter value 的整數單位與 decimal 行為、invoice line
      的 minor-unit 表示。
- [ ] Rounding：provider 端 line 捨入規則 vs 本地 round-half-up-once-per-line
      （usage_engine §4）對同一 fixture 的差異。
- [ ] Timestamps：webhook `created` 與 meter `occurred_at` 的實際格式/時區；
      月底最後一小時事件歸帳月份實測。
- [ ] 去重：同 idempotency key 重送與 webhook 重送/亂序的 provider 實際
      行為（含去重窗時限）。
- [ ] 非同步彙總：meter event 送出到 aggregate ready 的實際延遲與輪詢
      介面；pending 期間的 invoice 計量是否為最終值。
- [ ] `invoice.paid` / `invoice.payment_failed`、`subscription.canceled` /
      `subscription.deleted`、meter error 通知在 test mode 的實際事件
      形狀（封閉集欄位對齊）。

### Runtime-blocked list（本票不做，永遠不做）

- **live key：永不**。本 repo 任何 slice 不得持有、讀取或請求 live key；
  test-mode key 亦不入庫（本票根本無 key）。
- 不刷真卡、不真收款、不出真帳單。
- 不把 webhook 三種事件當完整生命週期（封閉集 + unknown ignored 已是
  契約）。
- 不在送出後立刻判彙總對帳（`ReconciliationBlocked` 已是契約）。

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
npm test
npm run build
```
