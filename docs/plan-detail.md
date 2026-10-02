# 實作計劃：工程版

本版同步創辦人已決定的 Go／先受限試用／無預設運行時限。它取代舊 8 步正文；舊實驗指令與估時保存在 [4033a91 歷史版本](https://github.com/our-sandbox-agent/sandbox-console/blob/4033a91/docs/plan-detail.md)，不得將歷史回收器、暖恢復或收費承諾當成當前契約。

讀者入口：[短版](plan.md)、[語言 ADR](adr/runner-language.md)、[生命週期 ADR](adr/sandbox-lifecycle.md)。生命週期 ADR 是狀態、API、保存與任務保護的唯一詳細契約，以下不維護第二套狀態表。

## 1. 架構與邊界

| 元件 | 技術／責任 | 不負責 |
|---|---|---|
| Console | 現有 JavaScript/Vite；明示 demo 或 real API 模式 | 不保存正式 key、不在前端推算權威狀態 |
| 控制平面 | TypeScript、Hono、Postgres；授權、operation、事件及對帳 | 不把 HTTP 202 當成 runtime 成功 |
| Runner | Go；Docker + runsc adapter、容量、generation／fencing、runtime observation | 不在同一程序內自稱 host watchdog |
| CLI | Go；安全認證、create/connect/state、基本下載 | 不繞過控制平面直接呼叫 Runner |
| 宿主監督 | 獨立 lease watchdog、容量／磁碟與 incident 訊號 | 不以未知狀態默認程序已停止 |

受限試用是多使用者授權、單 Runner 部署；單機不等於可省略租戶隔離。對外固定一個 `console.<domain>` 提供 UI、API、WebSocket；Runner 僅私網、每次驗 service identity。內部實驗可用 loopback＋授權的 SSH tunnel，不能把實驗 token 當正式登入。

## 2. 里程碑與依賴

以 GitHub 原生 blocked-by 為排程來源。M0=#2 規格／可行性、M1=#3 內部啟動、M2=#4 完整 Alpha、M3=#5 收費、R=#6 研究。受限試用是 M1 後的獨立放行點；文件不再用 A/B 命名暗示暖恢復必做。

整體順序固定為：**M0 可行性 GO（#8 人工審查）→ M1 內部可用 → 受限試用 G01–G10 實測與簽核（#79）→ 完整 Alpha（#24）**。#79 與 #46 規格都不是 #10／#11 或 M1 的前置；M1 只等 #8 人工 GO，兩者不構成循環。

- #7、#9、#46 規格與 #67 研究已交付（2026-09-21／09-27 關票）；#8 剩 #75 E05／E10 與人工 GO 審查。
- #8 go 才做 #10；#8 go 且 #9 完成才做 #11。映像檔和 Runner 可並行，但不宣稱 gVisor 之外沒有技術未知。
- #12 在 #10/#11 之後；#13 依映像檔／Runner 接 terminal；之後 #14 CLI、#15 real console。#76 內部控制平面最小 API 併入 M1（契約準備可先行，真實整合等 #11）；#77 最小資源事件 slice 是 #25 的前移子範圍，在首次真 Runner 整合時完成，不放回 #11 的 blocker 造成循環。
- #52 IDE 模式 spike 在 #8、#10、#11、#13 之後，M2 timebox；不阻擋受限試用。
- #16 起的授權、#17 operation、#18 watchdog、#19 policy、#20 files、#21 secrets、#22 network、#23 operations 各自保留功能驗收；受限試用只可對其中必要子範圍填證據，不能把整票提前關閉。#78 是 #22 的 G04 子範圍（先行實作，通過不關閉 #22）。
- 受限試用實際放行由 #79 追蹤：M1 完成真實流程後，逐列填 G01–G10 證據並由創辦人簽核；#46 只交付規格。
- #24 仍是完整 Alpha，保留其全部依賴。#79 放行不解除 #24。
- #9 的事件契約在第一個真 Runner 使用（#77 前移 slice）；#25–#30 的持久帳本、計價、Stripe、對帳和正式放行逐步接上。
- 暖恢復、Snapshot/Fork、續傳及 Firecracker 不阻擋檔案／網路／secrets。沒有要求先 raw runsc 重寫才能做基本功能。

### 總估時拆帳（2026-10-02 重算，非交期）

各票初始區間合計，不重複計算：前移 slice（#76、#77、#78）只在執行階段計一次，母票所屬範圍（#16／#25 的 API 與帳本、#22 的 G04）屆時扣減，不與 slice 相加。

| 範圍 | 票（初始區間，工程人天） | 小計 |
|---|---|---|
| M0 剩餘 | #75（1–3，不含等 key／預算）、#70 剩餘（1–2）、#8 整合／人工 GO（約 0.5–1） | 約 2.5–6 |
| M1 內部可用 | #10（2–3）、#11（3–5）、#12（2–4）、#13（3–5）、#14（3–5）、#15（2–3），加前移 slice #76（2–4）、#77（2–3） | 19–32 |
| 受限試用放行 | #78（2–4，#22 的 G04 拆分）、#79 整合（1–2）；其餘 G 列為既有票必要子範圍，不另估 | 3–6 |
| 完整 Alpha 剩餘 | #16（3–5）、#17（4–6）、#18（2–4）、#19（3–5）、#20（3–5）、#21（4–6）、#23（3–5）；#22 扣除 #78 後的剩餘（未估）；#24 端到端驗收（未估） | 22–36＋ |
| M3 收費試行 | #25 扣除 #77 後的剩餘（原 3–5）；#26–#30 未重估 | 未估 |

合計約 46.5–80 工程人天＋未估項（#24、#26–#30、#22／#25 殘餘）。以上皆為初始區間、尚無實績校準，不是交期。原 13 週與初始 64–107 人天合計為 **HISTORICAL**（見 [4033a91 歷史版本](https://github.com/our-sandbox-agent/sandbox-console/blob/4033a91/docs/plan.md)），僅供對照。

## 3. 本輪交付

### T00／#7：契約（規格完成，PR #47 已合併）

交付 [生命週期 ADR](adr/sandbox-lifecycle.md)、本計劃及短版。驗收對照：

| #7 驗收 | 落點 |
|---|---|
| 斷線／cold／warm 語意 | ADR §1 |
| 統一狀態及 /state | ADR §2–3，涵蓋 Lost/Error、operation、409、fencing |
| 固定 Runner 語言 | Accepted 語言 ADR；第一版 Go，無暫時 Node |
| 保存矩陣、任務保護及首版範圍 | ADR §4–6 |
| 修正文件與命名 | 本文件與短版；一個公開 domain，M0–M3＋R，無 A/B 前置假設 |

合併 #7 的規格不代表其所有 runtime 案例已通過。其後各票按 ADR 產生實測證據。

### T01／#8：限時相容性矩陣（進行中）

2026-09-27 無 key 實測已交付：E01–E04／E06–E08 pass、E09 fail、E05／E10 not_run。原 E09 FAIL 判定保留；#71 的 PID 候選（GO-candidate）未經創辦人核准，host clone panic 未修復。E05／E10 移至 #75 執行。尚不可 go。

2–3 工程人天是試驗上限，不是承諾通過。需要已授權 Linux 主機、Docker 中註冊 runsc、固定 image digest／Claude 版本與憑證配置；沒有環境標 waiting environment。

必做：環境版本與 runtime 設定、shell、公開 repo clone、套件安裝、Claude marker、PTY resize／detach/reconnect、CPU／memory／PID 限制、workspace／home cold restart。實際量測而非只 inspect 設定；`uname -r` 不是唯一隔離證據。runc 只作診斷對照，不能替代 runsc pass。不得將 key／完整環境／憑證放 log。

結果逐列 pass/fail/not-run；必做列缺失則沒有 go。no-go 附可重現失敗及可行縮小範圍，等待另次決策。測試只建立帶唯一 test label 的 disposable 資源；不得清理未知容器或既有資料碟。

### T02／#9：事件與帳本語意（規格完成，PR #49 已合併；最小事件 slice 由 #77 前移 M1）

必含 operation_id、generation、sequence、recorded/effective_at、同狀態 resize、sandbox／volume／snapshot 獨立資源及 Lost 不確定區間。UTC、半開區間、固定最小單位、版本化 rate card；試用不收費。手算案例與機器可驗 fixture 是契約驗證，不是正式 billing engine。

### T03／#46：受限試用放行規格（規格完成，PR #48 已合併；實際放行由 #79 追蹤）

3–5 人範圍與創辦人決定一致。列出最低控制、證據格式、責任人及揭露草稿；這張規格票合併不代表邀人授權。完整 API key proxy／完整出口 allowlist 可列延後，但內網與 metadata 阻擋、隔離、憑證不落一般 log/backup 等最低控制必須驗證。

## 4. 後續實作規格

### 映像檔與 Runner（#10／#11）

Go adapter 隔離 Docker 呼叫，image digest、runsc、Claude 版本固定並記錄相容矩陣；Agent 第一版只有 Claude。非 root、必要 capabilities、PID／CPU／memory／disk 上限及 host headroom 由實測決定，不能 8 GB 主機直接配兩個 4 GB。

create 原子預留租戶與主機容量，Runtime 必須為 runsc；權威 state 依觀測推進；timeout 與 crash 交給對帳。所有變更需 idempotency、generation 及 fencing；destroy 未確認資源釋放前不歸還容量。沒有按存活 4 小時／停止 24 小時自動回收的開發期捷徑。

### 資料與終端（#12／#13）

workspace 與 home 分開；home 只保存已選定非敏感設定／對話，runtime credentials 另放 ephemeral storage。具體備份、destroy 與重送流程按 ADR 矩陣。先驗證 cold persistence；暖 checkpoint 不作前置。

只選一套 terminal transport，以 #8 的 PTY 相容結果決定 ttyd 或薄 PTY adapter，不預排替換。CLI／Web 使用同協定；驗 raw mode、Ctrl-C、resize、detach、重連、斷網與停止中的 409。tmux 斷線保留與冷恢復新程序分開驗收；不驗「stop/start 原 PID 還在」。

### CLI、Console 與檔案（#14／#15／#20）

最小命令：login、claude、ls、connect、suspend、exec 及只下載 cp。CLI 帶 token 呼叫控制平面，token 本機受限權限；安裝產物按 OS／arch 驗證，不承諾未測平台。使用者主動 push 不替代下載；不自動 force push／commit，不把個人 git 憑證寫進映像。

Console real API 模式顯示 pending operation、Lost／Error 及錯誤處理；demo localStorage 與真服務明確分隔。#39／#41 已收束：單檔刪除（#42）與上傳覆寫（#64）已合併，發布回歸由 #74（修復 PR #81）收尾；後續瀏覽器檔案功能由 #20 真 API 與 #15 Web 整合承接，不再從舊票排。

cp 下載驗租戶／沙盒／路徑，防 symlink 與目的地解壓逃逸；Suspend 期間為唯讀：只允許讀取與下載，不接受寫入或刪除，檔案寫入待恢復 Active 後進行（#20，對齊生命週期 ADR §2 的 Suspend 操作邊界）。安全路徑實作按選定 Go 版本官方 API 確認，不猜行數或把未驗 API 行為當保證。資料夾上傳簡單版進受限試用（#20 補充：打包直傳 volume、固定大小上限、忽略清單、UI 顯示被排除檔案）；大檔續傳與檔案 snapshot 留後續。

IDE 模式（#52）若通過 spike，是每沙盒可選附加：code-server 與 Agent 同容器同 home、經 `console.<domain>/s/<id>/ide/` 子路徑代理、整合終端只是 tmux 的另一個客戶端；Idle 訊號以使用者輸入為準並排除 IDE 程序（#19）。

### 登入、狀態與營運（#16–#23）

GitHub OAuth + invite allowlist，所有資源操作重新驗 workspace 授權；workspace ID 不是授權。單一公開 domain；HTTP／WebSocket、下載及 operations 都有相同授權邊界。

operation 狀態與 runtime observation 分離，失聯時保留容量並標 Lost；host watchdog 在 runner crash 下仍有效。自動 Idle 看 busy/unknown 保護與限速指標；自動 Suspend 只在已確認任務完成後倒數；無 hook 不當成結束。使用者選的 deadline 是另一個 reason；預設 null。

受限試用 key 可被沙盒程序讀取，揭露、ephemeral 注入、撤銷與清理必須測試。CLI/API request body、trace、shell history、images、一般備份不能記 key。home 備份不能整包包含 gh token。完整代理進 #21，但不能以延後代理為由省略基本憑證措施。

網路先驗 host/control-plane／其他租戶／RFC1918／link-local／metadata 等路徑阻擋（含 IPv6、DNS rebinding 和既有連線處理）；保留必要對外 clone／套件／模型連線。完整 domain allowlist 另於 #22 完成，不以網路清理失敗 destroy 使用者 volume。

backup 是 allowlist 匯出、加密儲存、受限讀取與還原演練；RPO/RTO、保留天數和刪除流程要在放行表填值。未知錯誤預設停止新增 allocation 並隔離故障執行實體、保留可救資料；沒有固定運行期限不代表無主機容量／磁碟配額。

### 計量與正式收費（#25–#30）

事件從 Runner 第一版記錄。Active／Idle／Suspend 的實際資源量和未來收費政策分開：Idle 仍可能有 CPU，cold Suspend 仍有 workspace/home 儲存。snapshot 是另一個資源，過期不是 sandbox.destroyed。同狀態 resize 也要事件。

DB 帳本為可核對資料源，費率有版本／生效日；Stripe 是後續結算整合，不是覆蓋歷史價格的四列配置。Lost 區間標 uncertain，恢復證據以校正事件補足，不覆寫舊事件。試用 Usage 顯示數量及清楚標示的估價，未確定區間不偽裝精確金額。正式收費另有測試出帳、shadow billing 與明確 go-live。

### 可選研究（#31–#38）

checkpoint、Fork、500 MB 續傳、自動 push 與 Firecracker 各有獨立 go/no-go。Docker checkpoint 與 raw runsc 都可作研究候選，不先假設一定要換底層。Firecracker 主機需實際確認 KVM 權限，不能用「一定裸機」或特定恢復秒數代替驗證。SDK 覆蓋依語言 ADR，必要時直接打 API；此輪不購機、不寫 VM backend。

## 5. 驗收與交付節奏

每張票有獨立分支與 PR，CI 通過且人工審查後才合併；上游規格未合併的下游 PR 標示依賴。不因寫好 spec 就把功能票關閉。文件變更用連結／衝突檢查；程式變更用對應單元及整合測試，runtime 必須附原始 log 和環境版本。

#7／#9／#46／#67 已合併交付；#8 經 #75 補齊 E05／E10 並由人工審查確認 go，才把 M0 視為足以啟動預定 M1 範圍。若 #8 等待環境，其準備工作可以交 PR，但 M1 #10/#11 不解除等待；反之，#79 試用放行 gate 不阻擋 M1。
