# T04：Web IDE 模式 spike（code-server 每沙盒可選附加）協定

Refs #52（里程碑 #4；前置 #8、#10、#11、#13；冷恢復資料基礎 #12）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票。創辦人 2026-09-22 決定：受限試用維持終端優先（`sandbox claude`＋tmux＋xterm），瀏覽器版 VS Code 做成每個沙盒可選的「IDE 模式」，**在 #8 有結果、真 Runner 與冷 Suspend 驗證通過後再做**；2026-10-02 註記：**可選／暫緩，不是受限試用必要項**。Timebox 3–5 工程人天（1 人；到期必交證據與 go/no-go，不無限延長）。實測結果將另立結果報告，不回填本文件。

**條件式開工（鐵門未全數通過即 blocked，不開跑）**：

- **#8 人工 GO**：E01–E10 矩陣經第二份實測 PR 人工審查確認 go 才算（目前 E09 fail、#8 保持 open，見[現況結論](README.md)）；本協定不重跑 E01–E10。
- **真 Runner 與冷 Suspend 驗證**：#10／#11 真 Runner 已存在，且 cold Suspend／恢復（程序停止、workspace 與 home volume 保留）已依 [workspace-persistence](../contracts/workspace-persistence.md) 語意驗證——IDE 的 user-data／extensions 跨冷 Suspend 保留依賴同一 home volume 基礎（#12）。
- **#13 終端協定**：整合終端「只是 `tmux attach` 的另一個客戶端」語意以 [#13 終端協定契約](../contracts/terminal-protocol.md)（Proposed）為準；本票**不改該協定**。
- **E06／PTY 已有實測**：#8 2026-09-27 第一包無 key 實測 E06 已 pass（非 root ptmx、tmux resize／Ctrl-C／detach／reattach，見[結果](gvisor-no-key-results-20260927.md)）。**不得再把上游 google/gvisor#14761 歷史疑慮當成未驗證的唯一阻礙引用**；開工時只須在自建 IDE 映像重測同一 PTY 路徑，上游狀態重查僅作記錄。
- **可選／暫緩**：IDE 模式不列入受限試用放行條件（2026-10-02）；本票任何時候不阻塞試用放行。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。#8 的 E01–E10 矩陣與 R01–R08 的量測不因本票重跑；#52 issue 內的競品、授權與上游版本**均是 2026-09-22 前後的舊快照**，可參考 [docs/research.md](../research.md) 的歷史盤點，但**不得**作為本票的量測值或決策依據（2026-10-02 註記），開工前依官方來源重查（見執行前清單）。

## 目標

回答一個問題：**IDE 模式值不值得進 M2**——限時 spike 以量測數字與整合查驗回答，留下 go/no-go；GO 只代表授權另立 M2 實作提案，本文件與結果報告都不是實作。#52 的完成證據是量測表＋go/no-go 結論（含 ADR 補充建議），不憑文件關票。#52 驗收欄對應的協定段落如下：

| #52 驗收欄 | 對應協定段落 |
|---|---|
| 自建映像預裝 code-server 與 Claude Code 擴充（非 root、去掉 sudo／fixuid；user-data／extensions 放跨冷 Suspend 保留的 home） | 執行前清單、相容性檢查表 I1 |
| 量測 idle RSS、PID 數、resume→WebSocket 秒數、inotify watch 用量；與純終端沙盒對照 | 量測矩陣（IDE 附加成本表） |
| 代理路徑 `console.<domain>/s/<id>/ide/`：cookie 驗證、strip prefix、WebSocket upgrade、HTTPS／CSP 讓 webview 可用；Suspend 中回「喚醒中」頁並走 connect→Active 流程，不回 502 | 相容性檢查表 P1–P3 |
| 整合終端只是 `tmux attach` 的另一個客戶端，關 IDE 不影響 Agent 執行；擴充聊天面板的私有 CLI 是否共享 `~/.claude` session 紀錄 | 相容性檢查表 T1–T3 |
| Idle 訊號以代理層「最後一次使用者輸入」為準，排除 code-server 自身程序；IDE 心跳不阻止自動 Idle | 相容性檢查表 D1–D3 |
| 安全：`/proxy/<port>` 轉發面關閉；Open VSX 自由安裝擴充的供應鏈風險→白名單或自建 gallery 決定 | 相容性檢查表 S1–S2、決策規則 |
| 記憶體壓力下 IDE／Agent／Sentry 退出範圍量測；不預先承諾優先殺 IDE；無法保 Agent 須記限制與 go/no-go；deadline 冷 Suspend 前提示未儲存 buffer | 量測矩陣（壓力表）、相容性檢查表 M1–M3、決策規則 |

官方基準文件：code-server 官方文件（子路徑、驗證委交外層代理、非 root 映像）、Open VSX 與 Claude Code 官方文件（擴充發佈與 `~/.claude/ide/<port>.lock` 互連語意）、gVisor 官方文件。查證日期：**執行時記錄**——旗標、授權條款與上游狀態以執行當下的官方來源為準，本文件不預先抄錄可能過期的細節，也不預設任何 RSS／秒數／PID 門檻。

## 執行前清單（全部完成才開跑）

- [ ] 開工鐵門核對並記錄：#8 人工 GO、真 Runner（#10／#11）與冷 Suspend 驗證通過、#13 終端協定語意可用；任一未成立即如實標 blocked（blocked ≠ no-go），等待時間不算 timebox。
- [ ] **舊快照重查（2026-10-02 註記）**：下列聲明開工前依官方來源逐項重查並記錄查證日期——code-server 授權／子路徑／驗證委交／非 root 映像／更新頻率；微軟 VS Code Server 授權條款；OpenVSCode Server 上游狀態；Theia 瀏覽器版狀態；Coder 定位；Claude Code 擴充在 Open VSX 的發佈與 `~/.claude/ide/<port>.lock` 互連語意。重查結果改變前提時，先更新 #52 issue 再開跑。
- [ ] E06／PTY 書記：#8 已有實測 pass（見[結果](gvisor-no-key-results-20260927.md)）；開工時僅在自建 IDE 映像重測同一 PTY 路徑（併入 I1 證據），上游 issue 狀態重查僅作記錄，不作為未驗證阻礙。
- [ ] 映像紀律：自建映像預裝 code-server 與 Claude Code 擴充，非 root、去掉 sudo／fixuid；user-data／extensions 位於跨冷 Suspend 保留的 home volume（沿用 [work-image](../contracts/work-image.md) 預留目錄與 [workspace-persistence](../contracts/workspace-persistence.md) home volume 語意）；image digest／ID 與各套件版本入 manifest。
- [ ] 版本釘選——執行時記錄**實際值**，不假設、不沿用 #8 與 R01–R08 舊值：code-server 版本與 checksum、擴充版本、Claude CLI 前後版本、Docker／runsc／kernel（`uname -a`）與 runtime platform 配置。
- [ ] 契約面：IDE 模式為每沙盒可選附加，開關語意若需新契約欄位，先依契約變更流程定義並同 PR 更新受影響契約；Idle 排除沿用 [auto-policy](../contracts/auto-policy.md) `excluded_processes` 保留介面（`scripts/auto_policy.py`），不得另造第二套排除機制；不改 [#13 終端協定](../contracts/terminal-protocol.md)。
- [ ] 模型呼叫與憑證：擴充與 CLI session 互通查驗（T3）需要真 key 時，依 [gvisor-environment.md](gvisor-environment.md) 憑證與預算交接紀律；無 key 時對應列標 not_run 附原因，不得以 mock 冒充。任何憑證不進命令列、log 或報告。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.t04=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源，禁用 `docker system prune` 與通用名字清理。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 對照組：**純終端沙盒**（同映像基底、同負載、不安裝 code-server）；兩組同 host、同資源限額，對照差值即 IDE 附加成本。
- **idle RSS（MiB）**：sandbox 開機、IDE 首次連上並閒置固定時間後，code-server 相關程序合計常駐 RSS；採樣手法與次數執行時釘選並記錄。
- **PID 數**：同時點沙盒內可見程序數，與對照組並列。
- **resume→WebSocket（秒）**：Suspend 狀態經 state endpoint 要求 Active 起算 → `console.<domain>/s/<id>/ide/` 的 IDE WebSocket 完成 upgrade，`date +%s.%N` 差值；與 R 系列終端 terminal-ready 定義分列，不混稱。
- **inotify watch 用量**：閒置與開啟 workspace 後的 watch 數（探針如 `/proc/<pid>/fdinfo` 統計，採用者如實記錄），對照主機 `fs.inotify` 上限。
- 重跑次數與分布（min／median／max）執行時釘選並記錄；未跑的列填 `not_run` 並附原因。

### IDE 附加成本量測表

單位：記憶體一律 **MiB**、期間一律**秒**。

| 情境 | idle RSS MiB | PID 數 | resume→WebSocket 秒 | inotify watches | exit code | 附註 |
|---|---|---|---|---|---|---|
| IDE 沙盒（閒置） | | | | | | |
| IDE 沙盒（開啟 workspace） | | | | | | |
| 純終端對照組 | | | not_applicable | | | |

### 記憶體壓力退出範圍量測表

#72 已實測 aggregate OOM 可能殺整個 sandbox——本表只記錄 IDE／Agent／Sentry 在壓力下的實際退出順序與範圍，**不預先承諾優先殺 IDE**；無法保證 Agent 存活時如實記為限制並計入 go/no-go。壓力手法與限額執行時釘選並記錄。

| 情境 | 限額 | 首個退出程序 | IDE 存活 | Agent 存活 | sandbox 存活 | OOM 證據 | 附註 |
|---|---|---|---|---|---|---|---|
| 緩慢壓力（IDE＋Agent 同開） | | | | | | | |
| 突發壓力 | | | | | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| I1 映像與 home 保留 | 自建映像（非 root、無 sudo／fixuid）啟動 code-server，user-data／extensions 寫入 home 後走 cold Suspend／恢復；於 IDE 整合終端重測 E06 同一 PTY 路徑 | 恢復後擴充與設定仍在（home volume 保留語意，不依賴 RAM／PID 保存）；code-server 以非 root 啟動；非 root ptmx／tmux 行為與 #8 E06 實測一致 | |
| P1 子路徑代理 | 經 `console.<domain>/s/<id>/ide/` 連入：cookie 驗證、strip prefix、WebSocket upgrade | 未帶有效 cookie 拒絕；子路徑資源與 webview 正常載入；WebSocket 完成 upgrade；白名單外路徑不可達 | |
| P2 CSP／HTTPS | 以 HTTPS 存取並檢查 CSP 與 webview 內容載入 | webview 可用；CSP 實際語意如實記錄；任何放寬須附理由，不為通過任意放寬 | |
| P3 Suspend 中行為 | Suspend 狀態下連 `/s/<id>/ide/` | 回「喚醒中」頁並走 ADR connect→Active 流程（[sandbox-lifecycle](../adr/sandbox-lifecycle.md) 第 3 節：先經 state endpoint 要求 Active，等 operation 成功再連）；不回 502、不暴露內部錯誤 | |
| T1 關 IDE 不殺 Agent | Agent 在 tmux session 內跑固定工作；關閉 IDE 分頁／中斷 IDE WebSocket | 工作程序與 tmux session 存活、進度保留；IDE 重開後整合終端可再 attach 同一 session | |
| T2 整合終端＝tmux 客戶端 | IDE 整合終端與外部終端同時 attach 同一 tmux session | 兩客戶端見同一 session；語意以 [#13 終端協定](../contracts/terminal-protocol.md) 為準，本票未另造第二協定 | |
| T3 擴充與 CLI session 互通 | 擴充聊天面板操作後檢查 `~/.claude` session 紀錄；再以整合終端 CLI 接續同一 session | 共享與否**如實記錄**；不互通即計入 go/no-go 限制，不得掩蓋；憑證紀律依執行前清單 | |
| D1 Idle 排除介面 | 開 IDE、無使用者輸入，觀察 [auto-policy](../contracts/auto-policy.md) `excluded_processes`（code-server）下的 CPU wakeup 歸因 | code-server 心跳與自身程序 CPU 不構成 wakeup，Idle 照常降級——IDE 心跳不阻止自動 Idle（2026-09-22 founder note）；排除一律走 `scripts/auto_policy.py` 既有介面 | |
| D2 代理層最後輸入 | Idle 訊號以代理層最後一次使用者輸入為準；IDE WebSocket client→server 訊號歸 user_input 類，ping／resize／output 依 [#13 終端協定](../contracts/terminal-protocol.md) activity classes 不計 | 分類與 activity classes（liveness／layout／server_stream 非 activity）一致；非使用者訊號不刷新倒數；語意經 `scripts/auto_policy.py` 介面驗證 | |
| D3 不確定保守 | 送出無法歸因的 CPU 需求訊號 | 依 auto-policy 保守語意處理（不確定不降級）；如實記錄 | |
| S1 轉發面關閉 | 掃描代理路由設定；嘗試直連 `/proxy/<port>` 與沙盒內部 port | 對外轉發面**關閉**：僅 `/s/<id>/ide/` 白名單子路徑可達；發現任何任意 port 轉發路徑即 fail | |
| S2 Open VSX 供應鏈 | 檢查擴充安裝來源與預設 gallery 行為；評估白名單與自建 gallery 選項 | spike 記錄現行預設行為與供應鏈風險，並產出白名單或自建 gallery 的決定建議；決定記入結果報告與 go/no-go，不在本文件預先擇定 | |
| M1 壓力退出範圍 | 依記憶體壓力量測表執行並觀察退出順序 | 實際退出順序與範圍如實記錄；不預先承諾優先殺 IDE；#72 aggregate OOM 事實計入解讀 | |
| M2 Agent 存活限制 | 壓力後檢查 Agent 與 sandbox 狀態 | 無法保證 Agent 存活時明確記為限制並計入 go/no-go；不掩蓋、不改寫為「安全」 | |
| M3 deadline 未存 buffer 提示 | 設 `runtime_deadline_at`（[watchdog-lease](../contracts/watchdog-lease.md) 語意），IDE 內留未儲存 buffer，等待到期 cold Suspend | 到期前 IDE 出現未儲存 buffer 提示（手法與提前量執行時記錄）；到期 cold Suspend 照常執行，不被提示阻塞 | |

## 證據與發布

- 原始輸出（stdout／stderr、代理設定與 CSP header 記錄、程序與記憶體採樣、計時、inotify 統計、manifest）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-t04 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 需要 key 的查驗（T3 等）依交接紀律使用拋棄式憑證與預算；任何憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源，先停容器再刪 volumes；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：#8 未人工 GO，或真 Runner／冷 Suspend 驗證未通過 → 本協定 blocked（blocked ≠ no-go），等待時間不算 timebox；可選／暫緩性質不變，任何時候不阻塞受限試用放行。
- **GO（建議 IDE 模式進 M2）**：量測矩陣已填實（含對照組差值與壓力退出範圍）、P／T／D／S／M 各項無未解決 fail、Agent 存活限制已如實記錄且創辦人接受、Open VSX 供應鏈對策（白名單或自建 gallery）已有決定建議；GO 只代表授權另立 M2 實作提案與 ADR 補充，本文件與結果報告都不是實作。
- **NO-GO（不進 M2）**：整合語意有未解決 fail、附加成本經創辦人判定不值得、或供應鏈對策無法成立；#52 依完成證據欄以 not planned 關閉，終端優先主線不受影響。
- **不預設門檻與順序**：不預先寫死 RSS／秒數／PID 門檻，不預先承諾壓力下的退出順序（含「優先殺 IDE」）；所有數值與順序只記錄供決策。
- **不以量測結果私改契約**：發現契約缺口（IDE 開關、Idle 排除、代理語意）走契約變更流程，不得為通過驗收放寬契約或另造第二套終端協定。
- **timebox 3–5 人天到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤受限試用**：IDE 模式可選／暫緩，不列入試用放行條件；研究 no-go 時試用主線照常。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go／no-go 宣稱、對 M2 時程的影響宣稱。
- 正式 IDE 產品化（#52 本票不含；GO 後另立實作票）。
- #13 終端協定的任何變更（整合終端只是 `tmux attach` 的另一個客戶端）。
- 把 IDE 列為受限試用放行條件（可選／暫緩，2026-10-02 註記）。
