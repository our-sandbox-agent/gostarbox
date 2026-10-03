# Usage engine: versioned rate cards and exact pricing (#26)

Proposed，2026-10-03。Implements the in-repo slice of issue #26 on top of the
#77/#25 event ledger（[resource-events-slice.md](resource-events-slice.md)、
[resource-lifecycles.md](resource-lifecycles.md)）: `scripts/usage_engine.py`
(stdlib only) prices confirmed projection segments into exact per-line minor
units. 費率範例都是虛構測試值（ADR 聲明）；本文件是可驗算契約，不是
runtime。規範來源：[ADR usage-ledger §4](../adr/usage-ledger.md)（NORMATIVE）。

## Rate card schema

`RateEntry`（immutable dataclass，欄位同 ADR §4）：

| 欄位 | 型別 | 語意 |
|---|---|---|
| `rate_id` | str | 價格識別（同 price id 的版本序列） |
| `version` | int ≥ 1 | 版本號；`(rate_id, version)` 唯一，同步不可覆蓋 |
| `currency` | str | 最小貨幣單位的幣別；同一 bill line 不得混幣別 |
| `meter` | str | `cpu_reserved` / `memory_reserved` / `volume_provisioned` / `snapshot_stored` |
| `state?` | str \| None | 選填；`None` 為萬用（涵蓋該 meter 所有狀態） |
| `effective_from_ms` | int | 生效起（UTC epoch ms，含） |
| `effective_to_ms` | int \| None | 生效迄（不含）；`None` = open-ended |
| `numerator_minor` | int ≥ 0 | 每最小貨幣單位分子 |
| `denominator_meter_units` | int ≥ 1 | 每 meter 積分單位分母 |
| `rounding_policy` | str | 只實作 `round_half_up`；其他值拒絕 |

價格 = 積分單位 × `numerator_minor` / `denominator_meter_units`（精確有理數）。
例：每 vCPU 小時 100 cents → 分母 `1000 × 3600000` milliCPU·ms。GiB 一律
`2^30` bytes（`GIB_BYTES`），沒有 GB/GiB 混用。

## 不變式（invariants）

1. **無重疊**：同 price key（`currency, meter, state`）的有效區間不得重疊
   （ADR §4）。同起點或晚起點的覆蓋列 upsert 直接拒絕；早起點 open-ended
   前版可被「未來生效」的新版在起點處收尾（future-only truncation，不改
   任何已計價時段）。不允許在新版結束、前版結束之前打洞製造未定價缺口。
2. **缺費率＝未定價**：查無費率的切片列入 `unpriced`，絕不默認 0、絕不
   進 totals（ADR §4）。
3. **歷史不可變**：`(rate_id, version)` 已生效（`effective_from_ms <= now`）
   的列拒絕任何內容修改；改價＝建新版本＋生效時間。完全未來的列可替換。
4. **state 特異性**：精確 state 匹配優先於萬用（`state: None`）；同特異性
   兩列同時涵蓋同一時刻 = ambiguity 錯誤，不猜。
5. **精確算術**：全程 integer 與 `Fraction`；禁止 true division、float
   常數、float 值（測試以 AST/token 掃描原始碼強制；輸出結構亦不得出現
   float）。

## 計價規則（price / usage_view）

- 分組：**UTC 月 × workspace × currency × meter** 為一條 line（ADR §4）。
- 切段：每個 confirmed 切片先在 **UTC 月界** 與 **rate-version 界** 切開
  （新率不得覆蓋舊時間、不得重複累計；半開區間 `[start, end)`，零長為零）。
  quantity 為非負整數，`units = quantity × duration_ms` 整數乘法。
- 計價：每個子區間用當時有效的 rate entry，`Fraction(units × numerator,
  denominator)` 精確累加到所屬 line。
- 捨入：**每條 line 最後一次 round-half-up** 到整數 minor unit；帳單總額
  ＝已捨入 line 之和。負數（調整項）以絕對值 half-up 後恢復符號，禁止
  bankers rounding（ADR §4 原文規則）。
- Uncertain（Lost）：絕不進 confirmed totals；單獨成「試用估價」區
  （每 line 同規則捨入），並產生 `uncertain_note`：
  「至少 X，另有待核對區間 N ms（試用估價 Y，未計入確認總額）」（ADR §3）。
- **已封帳（sealed）不重算**：`sealed_periods`（`period, start_ms, end_ms,
  lines, totals?`）內的原始 lines 原樣輸出（`kind: "sealed"`），該時窗的
  segment 重算被排除；晚到 correction 以 `adjustments`（`period,
  workspace_id, currency, meter, minor, reason`）成補充／折讓 line，
  關聯原帳單，**不改已封帳金額**（ADR §3 no-restatement）。
- `usage_view(card, segments, period=None, sealed_periods=(), adjustments())`
  是 API/Web/CLI 單源，回傳
  `{lines, totals, currency, unpriced, uncertain_note, sealed,
  adjustment_totals, estimate}`；`render_text(view)` 供 CLI/終端顯示，
  估價一律標「試用估價（未收取款項）」，未定價顯示「未定價（不可默認 0）」。

## Provider 同步（append-only）

`sync_from_provider(rows, now_ms)`：Stripe 等供應商同步**只能附加未來生效
的新版本**。判定：

| 情形 | 結果 |
|---|---|
| 與已存列完全相同 | `unchanged`（冪等） |
| 同 `(rate_id, version)` 內容不同 | `conflict`：歷史不可重寫 |
| 新列但 `effective_from_ms <= now_ms` | `conflict`：會改現行/過去價 |
| 新列與現存列重疊／打洞 | `conflict`（upsert 規則） |
| 未來生效、無衝突 | `appended`（自動收尾 open-ended 前版） |

回傳 report `{appended, unchanged, conflicts}`，row 層級問題只 flag 不丟
例外；任何 conflict 都不改變已存列（測試驗證 `entries()` 前後相等）。

## Fixtures 與 guards

`scripts/test_usage_engine.py`（38 tests）：手算 fixtures 對齊
[ledger-examples.json](ledger-examples.json) 與 ADR §5 — UTC 月界各 1 秒
（兩條 line）、mid-cycle 1¢→2¢＝3 cents exact、兩段 0.5¢ 同 line＝1¢、
Lost（confirmed 1M＋uncertain 2M，estimate 另計）、round-half-up 邊界
（+0.5→up、-0.5→away from zero）、fractional GiB（0.25 GiB·h × 10¢＝2.5→3）、
destroy（compute 停在確認停止、volume 計到獨立 purge）、snapshot TTL 到期
非釋出（計到確認刪除 6,144,000）、同狀態 resize、重送冪等、多沙盒累加。
Mutation guards（7 個，破壞任一防護測試即失敗）：bankers rounding 取代
half-up、月界/費率界不切、未定價默認 0、重疊接受（歧義）、歷史可改
（permissive upsert）、已封帳重算。另有靜態原始碼掃描（禁 true division、
float 常數與 float 名稱）。

## Runtime TODO（未建、不聲稱）

- **沒有真實 Stripe 整合**：`sync_from_provider` 是語意契約，未接任何
  API；費率仍是虛構測試值。
- **沒有真實 invoice**：不出帳、不收款；試用只展示估價（ADR §3）。
- **沒有 DB／Runner**：segments 來自 #77/#25 in-memory projection；
  #11 runner、#76 control plane 落地後由其饋入。
- 封帳水位（月結 watermark）由呼叫方以 `sealed_periods` 傳入；產生與
  保存封帳快照屬後續帳務票。
- 四個 meter 價格是否逐項出現在每張 invoice、零用量與捨入的實際 API
  行為，依 issue #26「本票不含」留待實測。

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/verify-ledger-examples.py
```
