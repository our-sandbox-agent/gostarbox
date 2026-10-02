# Sandbox 生命週期與資料契約

狀態：Proposed，待 PR 審查；2026-09-22。Refs #7。
沿用已 Accepted 的 [語言決策](runner-language.md) 與創辦人的受限試用／無預設運行時限決定。本 ADR 定義實作契約，不表示 Runner 已存在。

## 1. 使用者可依賴的語意

- 斷線只移除 terminal attachment，既有程序繼續；tmux／PTY 及重連由 #13 驗證。
- cold Suspend：停止程序，保留 workspace 與允許保存的 home 資料；狀態必須等 Runner 確認程序已停止才變成 Suspend。恢復會啟動新程序，不保留 RAM、socket、PID 或原 tmux 畫面。
- 每個沙盒記錄 Claude session ID 與工作目錄；恢復用明確 session。紀錄不存在時回報 `session_missing`，讓使用者選擇新 session；不靜默載入其他對話。
- warm Suspend 是可選研究，另有 checkpoint ID、相容版本及失敗恢復契約；首版 API 不接受 warm 模式，回 422 `unsupported_suspend_mode`。
- Active 與 Idle 都有存活程序。Idle 只是資源政策，不證明任務完成、不代表 CPU 成本為零。
- 沒有預設最長運行時數。不增加固定 4／12／24 小時回收器、不依無鍵盤或 hook 失聯自動刪除資料。

## 2. 狀態表

`desired_state` 表示要求的目標；`observed_state` 由 Runner 證據確認。UI 不能把 HTTP 202 當成已完成。控制平面保存 operation 與資源事件，Runner 重啟後對帳；不聲稱 DB 與 runtime 可跨系統原子提交。

| observed_state | 進入條件 | 可要求的操作 |
|---|---|---|
| Creating | create 已接受、容量已預留 | 查詢；取消由 Destroying 接管，不能提前釋放預留 |
| Active | 程序就緒、Active 資源政策已套用 | Idle、Suspend、destroy、terminal、讀寫檔案 |
| Idle | 程序存活、Idle 政策已套用 | Active、Suspend、destroy、terminal、讀寫檔案 |
| Suspending | 停止程序進行中 | 查詢；重送同一 operation，不接受相反狀態操作 |
| Suspend | 已確認停止程序，資料 volume 仍存在 | Active（經 Resuming）、讀取／下載、destroy |
| Resuming | 憑證、容量與磁碟檢查通過，啟動中 | 查詢；其他變更回 409 |
| Destroying | 刪除已接受，逐一核對程序及資料資源 | 查詢；同 operation 可重試 |
| Destroyed | 程序、首版 volume 與 session secret 已確認移除 | 查詢 tombstone；重複 destroy 回既有 operation |
| Lost | Runner lease 過期，實際程序狀態未知 | 查詢、人工介入；先 fencing／對帳，再接受變更 |
| Error | 已知操作失敗且非 Lost | 查詢錯誤與殘留資源；只有對帳確認可重試才開放動作 |

Active↔Idle 也使用 operation；切換確認前維持原 observed_state 並顯示 pending operation。其餘轉移：Creating→Active/Error；Active/Idle→Suspending→Suspend/Error；Suspend→Resuming→Active/Error；可管理狀態→Destroying→Destroyed/Error。任一未完成狀態都可能因 lease 過期進 Lost。Lost 不直接變 Destroyed，也不直接重建同一沙盒造成雙開。

Error 必須保留 `last_confirmed_state`、錯誤代碼與資源清單；容量只在確認釋放後歸還。Lost 保留資源預留，獨立於 Runner 的 host watchdog 依 lease fence 舊 generation；沒有停止證據不可宣稱釋放容量或停止計量。

記憶體終止補充（#67 提案，未實作）：確認終止映射為 Error，`error.code=memory_limit_terminated`；重試耗盡仍是 Error，代碼 `recovery_retry_exhausted`，不新增 recovery-blocked 狀態。對帳確認舊實體停止、憑證／容量／volume 檢查通過後，使用既有 state endpoint 要求 Active，經 operation 進 Resuming，建立新 generation；健康確認後才進 Active，失敗回 Error、未知回 Lost。恢復責任對應：執行實體重建與健康檢查由 #11 Runner 驗收、operation／generation／fencing 與對帳由 #17 驗收、自動恢復策略屬 #19 的產品決策（預設仍為使用者明確重啟）。詳見 [記憶體恢復契約](memory-session-recovery.md)。

本狀態表的機器可驗版本見 [contracts/runner-lifecycle.json](../contracts/runner-lifecycle.json)，由 `scripts/verify_runner_contract.py` 檢查內部一致性；語意仍以本 ADR 為準，不代表 Runner 已實作。

## 3. 統一 API

公開入口同一 `console.<domain>`：靜態 console、`/v1` API 與 terminal WebSocket。Runner 僅走私網且驗證 service credential；CLI 不直接呼叫 Runner。GitHub Pages 仍為純模擬，沒有正式 key。

| 方法／路徑 | 契約 |
|---|---|
| POST /v1/sandboxes | 只接受 `agent=claude`；驗證資源、repo、租戶及容量；可選 `runtime_deadline_at`（每沙盒，UTC，null 表示無上限）；202 + operation_id + sandbox_id |
| GET /v1/sandboxes/{id} | desired／observed、generation、version、pending operation、last_confirmed_at、volume IDs；依狀態附加：Error 時 `last_confirmed_state`、`error{code, resources[]}`；Active／Idle 時 `hold{reason: busy|unknown|permission_wait, since}` 與 `next_transition_at`（無倒數為 null）；`runtime_deadline_at`；Suspend／Active 時 `session{id, cwd}`（無紀錄為 null） |
| POST /v1/sandboxes/{id}/state | `{state: Active|Idle|Suspend, expected_version, suspend_mode?: cold}`；202 + operation_id |
| DELETE /v1/sandboxes/{id} | 明確確認資料刪除範圍與 expected_version；202，刪除程序與首版 workspace/home volumes |
| GET /v1/operations/{operation_id} | pending/running/succeeded/failed，result、錯誤、可否重試；租戶授權同 sandbox |
| PUT /v1/workspaces/{id}/policy | expected_version + 完整 workspace policy；保留審計及生效時間 |
| PATCH /v1/sandboxes/{id}/deadline | `{runtime_deadline_at: <UTC>|null, expected_version}`；延長、縮短或取消單一沙盒的運行上限；200 回新值並記審計 |

create／state／destroy 皆需 `Idempotency-Key`，以租戶＋操作作用域去重；同 key 同 body 回原 operation，同 key 不同 body 回 409。expected_version 失配或操作衝突回 409，錯誤訊息讓客戶端重新讀取，不自動覆蓋。資源不足回 409 `capacity_exceeded`，schema 錯誤回 422，未授權資源以 404 避免跨租戶探測。 Suspend→Active 未附有效憑證回 409 `credentials_required`，不進 Resuming、不建 operation。首版 key 由 CLI 經 request body 送控制平面，端點與欄位在 #12／#14 定；憑證不得參與 Idempotency-Key 的 body 比對，也不得進 log。同步完成的相同目標請求可回 200 並附既有狀態，不能重複產生實際轉移。

每次執行實體替換／冷恢復分配新 generation，命令及心跳帶 generation；stale generation 不可提交狀態或事件。每沙盒最多一個變更 operation，fencing token 單調增加。操作 timeout 不等於失敗後資源已消失；先對帳，未知則 Lost。

不提供另一套 `/idle`、`/suspend`、`/resume` endpoint。`sandbox connect` 在 Suspend 時先要求 Active，等 operation 成功再 attach；在 Active/Idle 時只 attach，不重啟程序。

## 4. Workspace policy 與長任務

policy 欄位：`auto_idle_enabled`、`idle_after_seconds`、`auto_suspend_enabled`、`suspend_after_completed_seconds`、`default_runtime_seconds: null|positive integer`（只作建立沙盒時 `runtime_deadline_at` 的預設，改 policy 不影響既有沙盒）、`version`。無預設運行期限；自動門檻的數值在 #19 以工作負載測試確定，未確定前不在正式設定偷填常數。展示版倒數不作正式預設。

- Idle 判定需無使用者互動且無忙碌證據；CPU 低不等於結束，等待模型／網路的受管工作仍算 busy。CPU 需求、任務開始或輸入恢復 Active；CPU 判定包含限速造成的 throttling 指標，不能只看被壓低後的用量。
- completion event 需匹配目前 generation、session、task 與序號，晚到的舊完成訊號不能蓋掉新 busy。子任務、背景工作、權限等待或無法辨識的執行活動阻止 Suspend。新活動取消 countdown。
- hook 只是一個訊號，不是信任根；缺漏、過期、偽造或 API error 不視為任務完成。busy lease 過期改 unknown 並提示，不視為 idle。
- 使用者手動 Suspend 須知道會停止執行中程序；執行前保存可保存資料，但不自動 git commit／push，不承諾應用程式尚未落盤的資料。
- 運行上限是每沙盒的 `runtime_deadline_at`：建立時可選填（或由 workspace 預設帶入），之後可用 deadline 端點延長、縮短或取消；以 wall clock 計，冷恢復不重算。到期執行 cold Suspend，reason 記 `runtime_limit`，不記為 idle；到期會中斷執行中的工作，UI 與 CLI 在建立時明示到期時間，提醒提前量由 #19 定。無預設上限不等於無配額；新增容量不足即拒絕，磁碟／主機事故走明示 incident 路徑。

## 5. 保存矩陣

| 資料 | 斷線 | cold Suspend／恢復 | 備份 | destroy |
|---|---|---|---|---|
| /workspace 專案與成果 | 保留 | workspace volume 保留 | 僅選定非敏感資料；恢復實測後才承諾 RPO | 明確確認後刪除 volume；既有備份依 retention 刪除流程 |
| home 的 Claude 對話、必要非敏感設定 | 保留 | 獨立 home volume；保存 session ID／工作目錄 | allowlist 匯出，逐檔檢查；不整顆 home 打包 | 刪除 volume 與 session 索引 |
| Claude API key | 執行需要時可讀 | runtime tmpfs；停止後清除，恢復由 CLI 安全重送；未注入回 credentials_required，停留 Suspend | 不進一般 DB、log、image、volume 或備份 | 撤銷 session 注入並清除；供應商 key 撤銷由使用者執行 |
| Git／gh token、SSH material | 僅使用者明確提供時存在 | ephemeral config／credential helper，cold restart 不保證保存 | 排除；不沿用預設持久化 gh config | 清除 ephemeral 資料；提示使用者撤銷 |
| shell history、cache、rootfs 可寫層 | 程序執行中可用 | 不承諾保留；秘密不得靠 history 保存 | 排除 | 隨執行實體清除 |
| RAM、程序、tmux、socket | 保留至程序停止 | 不保留；重新啟動程序 | 不備份 | 終止 |
| metadata／audit／usage events | 保留 | 保留 | 獨立資料庫備份，遮蔽憑證 | 保留最小 tombstone／audit；保留期限在試用揭露中明示 |

workspace 也可能含使用者自行寫入的秘密：排除常見檔名不能保證沒有秘密。受限試用只備份使用者確認的資料範圍，說明限制；未通過備份／還原驗收不得承諾自動備份。備份加密、ACL、retention 數值由試用放行表確認，不由本 ADR 猜定。

cold Suspend 保留檔案不代表 key 也保存，Web 恢復尚無安全重送流程時引導使用 CLI，不偷偷落盤 key。代理注入屬後續完整 Alpha 能力；受限試用必須揭露 key 可被沙盒中的使用者／Agent 程序讀取。

### 記憶體終止後重新建立執行實體（#67 提案）

此路徑套用同一保存矩陣：保留 approved workspace/home volumes、session ID/cwd 及 metadata；不保留 RAM、程序、tmux/socket、rootfs 可寫層或 ephemeral credential。CLI 重新提供 key，不能靠 volume 偷存 key。已落盤不等於所有應用程式緩衝／部分寫入都安全；無法依賴 OOM 當下的 save hook。當前實測只涵蓋同一容器 docker start 與兩個 fsynced marker，重新建立實體仍需 #11（runtime）／#17（operation／對帳）／#19（自動策略）驗收。

## 6. 首版邊界及驗收

只 Claude、API key、invite allowlist。公開 repo clone；使用者主動 push；安全的只下載 cp 必須提供權限不足時的成果取回路徑。檔案路徑驗證、symlink 及解壓逃逸防護仍屬必要驗收，不因稱作「基本下載」而省略。

Snapshot／Fork、warm restore、大檔續傳、自動 push、Codex／Harness、正式收費均不阻擋首版。#39／#41 已收束：實作（#42／#64）與 Pages 發布修復（#74，PR #81）已交付；後續瀏覽器檔案能力由 #20 真 API 與 #15 Web 整合承接，既有 demo 不會因文件修改突然變成真服務。

契約案例：斷線後 PID／任務仍在；cold stop/start 後檔案一致但 PID 不同；缺 key 不啟動 Claude；同 key 重試不重建；相反操作回 409；Lost 不雙開；old-generation hook 不停新任務；沒有 hook 的長任務不因無鍵盤停機；destroy 刪除範圍可核對。

本 PR 交付規格，案例的真環境證據分別由 #8、#12–#19 與試用放行清單補齊。不能用文件合併取代實測。
