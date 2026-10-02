# R08：Firecracker 記憶體恢復與安全驗收協定

Refs #38（里程碑 R，#6；前置 #37）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票，**仍未授權切換 runtime 或啟用 memory fork**。Timebox 10–15 工程人天（1 人；#38 全票估時，含本票測試／說明，不含跨票整合、外部等待與後續修復；大於 5 天的進階票屬待細拆工作包，須先拆 sub-issues 才可開工；到期必交證據與 go/no-go，不無限延長；通過研究不代表功能已交付）。實測結果將另立結果報告，不回填本文件。

**條件式開工（R07 go 才開工，未觸發即 blocked）**：

- **R07（#37）GO 為硬前置**：R07 尚未執行或 blocked 時，本協定一律 blocked，不開跑；R07 no-go 時 #38 依其完成證據欄以 not planned 關閉。
- **基礎設施授權沿用 R07**：僅得使用 R07 manifest 登記、創辦人已授權之主機；主機異動須重新授權並記錄，未取得前本協定 blocked，不算 no-go。
- **#38 開工前須完成三組 sub-issues 切分**（snapshot 一致性、resume hooks、bench/upgrade，見執行前清單）；本文件只預先登記各 sub-issue 共同遵守的驗收語意與決策規則，不是實作。
- **gVisor 暖恢復實作不是前置**：沿用 R02 定義的恢復契約與測試案例，但**不要求 gVisor 暖恢復實作完成**；兩 runtime 行為不同時，以能力旗標與 ADR 明確區分（#38 第一個驗收欄）。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。R06／R07 的量測與 go/no-go 不因本票重跑；恢復模式分列語意（cold restart／warm restore／warm cache，不混稱 cold resume）沿用 [r06-firecracker-eval-protocol.md](r06-firecracker-eval-protocol.md)，Runner 語意（contract suite、jailer 清理、digest 釘選）沿用 [r07-firecracker-runner-protocol.md](r07-firecracker-runner-protocol.md)；恢復契約與測試案例沿用 [r02-warm-suspend-protocol.md](r02-warm-suspend-protocol.md) 的定義（M1–M3、S1–S4、R1–R4、V1–V2 語意），本文件只增補 Firecracker 記憶體恢復與 fork 特有欄位。

## 目標

回答一個問題：**Firecracker 的 memory snapshot 能否成套恢復同一程序狀態，且安全逐項過關**——vmstate＋mem＋該時刻 volume 一致、恢復後時間／亂數／憑證／網路／應用狀態逐項如實驗證，fork 未過即禁用。#38 五個驗收欄對應的協定段落如下：

| #38 驗收欄 | 對應協定段落 |
|---|---|
| 沿用 R02 恢復契約與測試案例，不要求 gVisor 暖恢復實作完成；兩 runtime 行為不同以能力旗標與 ADR 區分 | 執行前清單、相容性檢查表 P1、決策規則 |
| 先拆 snapshot 一致性、resume hooks、bench/upgrade 三組 sub-issues | 執行前清單、決策規則 |
| vmstate＋mem＋該時刻 volume 成套；quiesce/flush、同一時點、失敗清理與磁碟檢查有可重跑證據 | 相容性檢查表 S1–S4 |
| 時間、亂數、session/credential、網路重連與應用狀態逐項驗，不以重新連 guest agent 代表使用者程序已恢復 | 相容性檢查表 R1–R5 |
| end-to-end p50/p95 與失敗率附實測環境；不把約一秒先寫成 SLA；memory fork 另驗 side effects/identity，未過則禁用 | 量測矩陣、相容性檢查表 F1–F3、決策規則 |

官方基準文件：[Firecracker snapshot support](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)（官方對 snapshot／restore、memory 檔案與版本語意的規定；clone 相關議題另見同目錄 `random-for-clones.md` 等文件）。查證日期：**執行時記錄**——旗標、API 與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節（含 UFFD 等 restore 優化與差異快照的可用性），也不預設任何特定恢復秒數。

## 執行前清單（全部完成才開跑）

- [ ] R07 已交付結果報告且判定 **GO**（依 [r07-firecracker-runner-protocol.md](r07-firecracker-runner-protocol.md) 決策規則），結果報告連結記入 manifest；R07 尚未執行或 blocked 時本協定不開跑，如實標 blocked（blocked ≠ no-go）。
- [ ] 基礎設施核對：R07 manifest 登記之授權主機仍可用（型號／區域／取得日期複核記錄）；主機異動須創辦人重新授權，未取得即 blocked，不開跑。
- [ ] 已拆三組 sub-issues：**snapshot 一致性**、**resume hooks**、**bench/upgrade**，**各以 3–5 工程人天為起點**，連結記入 manifest；未拆完不開工（#38 第二個驗收欄）。
- [ ] R02 契約沿用登記：以 [r02-warm-suspend-protocol.md](r02-warm-suspend-protocol.md) 的恢復契約與測試案例為本票驗收基底（M／S／R／V 各項語意），**不要求 gVisor 暖恢復實作完成**——gVisor 側未實作或未跑的欄位如實標 not_run 附原因，以能力旗標與 ADR 區分（#38 第一個驗收欄）。
- [ ] 契約面：生命週期契約目前不接受 warm 模式（422 `unsupported_suspend_mode`，見 [workspace-persistence.md](../contracts/workspace-persistence.md) 與 [sandbox-lifecycle ADR](../adr/sandbox-lifecycle.md)）；執行前須先依契約變更流程定義 Firecracker warm restore 的 checkpoint ID、相容版本與失敗恢復語意，同 PR 更新受影響文件，不得以 runtime 私改繞過契約。
- [ ] 版本釘選——執行時記錄**實際值**，不假設、不沿用 R07 舊值：Firecracker release 與 binary checksum（含 jailer）、kernel image 來源與 hash、guest image digest 與 build 記錄、host 型號與 kernel／arch（`uname -a`）。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r08=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源（microVM、jail 目錄、TAP／netns、磁碟與 snapshot 檔），禁用通用名字清理。
- [ ] 機密紀律：測試用 microVM 不注入任何真憑證；fork 身分檢查用拋棄式 marker secret 的建立與撤銷計畫先寫入 manifest；任何憑證不進命令列、log 或報告。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 工作負載：沿用 R01 診斷 image 固定 fixture 語意（1／2／4 GB 匿名記憶體、heartbeat、fixture hash 執行時固定並記錄），確保與 R01／R06 可對話；不重跑 R06／R07 矩陣。
- 本票只測 **warm restore（memory snapshot）** 與 **memory fork**；cold restart／warm cache 語意沿用 R06 分列定義，不在本票重測。
- **snapshot 耗時（秒）**：含記憶體狀態之 snapshot 指令送出 → finalize 完成（quiesce／flush 含在內），`date +%s.%N` 差值。
- **restore 耗時（秒）**／**terminal-ready（秒）**／**首個任務回應（秒）**：沿用 R01／R06 定義（terminal-ready 依 [#13 終端協定契約](../contracts/terminal-protocol.md) 語意：attach 後取得 shell 回應且 `stty size` 有輸出）。
- **end-to-end（秒）**：觸發 warm Suspend（Active 起算）→ 恢復後 session 內首個任務回應，全程含 snapshot、finalize 與 restore。
- **失敗率**：每列重複 N 次（N 執行時釘選並記錄）；restore 未達終態或契約偏差即計失敗，原始 exit code 與錯誤輸出入證據，不掩蓋。
- **實測環境**：每列附主機型號、Firecracker／kernel 版本；沒附環境的數值不作數。**不把「約一秒」先寫成 SLA**——延遲只記錄供決策，本票不預設秒數門檻（#38 第五個驗收欄）。

### 恢復量測表

單位：期間一律**秒**、大小一律 **MiB**；分布（p50／p95）執行時釘選並記錄，未跑的列填 `not_run` 並附原因。

| 負載 | 樣本數 | 失敗率 | snapshot 秒 p50／p95 | restore 秒 p50／p95 | 端到端秒 p50／p95 | snapshot MiB | 實測環境 | 附註 |
|---|---|---|---|---|---|---|---|---|
| 1 GB | | | | | | | | |
| 2 GB | | | | | | | | |
| 4 GB | | | | | | | | |

### Fork 量測表

memory fork（自含記憶體狀態之 snapshot 建立新實例）另驗 side effects／identity；未過則禁用（見 F1–F3 與決策規則）。

| 負載 | 樣本數 | 失敗率 | fork 秒 p50／p95 | side effects／identity 結果 | 實測環境 | 附註 |
|---|---|---|---|---|---|---|
| 1 GB | | | | | | |
| 4 GB | | | | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| S1 vmstate＋mem＋volume 成套 | 建立含記憶體狀態之 snapshot 後讀 manifest 與檔案清單 | vmstate、mem 與**該時刻 volume 版本**三件成套、同一 generation／時點記錄；缺件或時點不一致即 fail | |
| S2 quiesce／flush 與同一時點 | snapshot 前 quiesce guest（flush 寫入、暫停寫入程序）前後寫 marker；比 snapshot 內記憶體與 volume marker | 記憶體狀態與磁碟內容為同一時點一致版本；無撕裂或半寫；採用的 quiesce 手法如實記錄 | |
| S3 失敗清理 | snapshot 進行中強制中斷（kill、磁碟空間不足）再檢查 | 失敗後無殘留可恢復壞狀態；原 VM 可繼續或有明確毀滅路徑；失敗輸出入證據 | |
| S4 磁碟檢查可重跑 | 每次成功與失敗 snapshot 後跑固定磁碟檢查（fsck 或同義探針，採用者如實記錄），重複 N 次 | 檢查命令與輸出可重跑且一致；異常如實記錄並標註 | |
| R1 時間 | resume 前後讀 session 時間、時區與跳動 | 時間行為如實記錄；時鐘回撥造成的錯誤明確標註 | |
| R2 亂數 | checkpoint 前連續取亂數樣本（如 `/dev/urandom` 固定長度 dump），resume 後再取並比對 | resume 後亂數不重播舊序列；重複或可疑熵不足即 fail 並記錄樣本 | |
| R3 session／credential | warm resume 後驗 API key 與 session lease（[watchdog-lease](../contracts/watchdog-lease.md) 證據規則，同 R02 R1 語意） | key／lease 正確更新或重發、epoch 一致；不以舊 epoch 證據充數 | |
| R4 網路重連 | checkpoint 前建立長連 TCP（如 `nc`）；resume 後觀察連線與重連 | 連線斷／留行為如實記錄；斷線後可重新建立連線 | |
| R5 應用狀態 | checkpoint 前 `tmux` 跑 heartbeat；resume 後 attach 並送固定命令（同 R01 C1 語意） | attach 可見原 session 與畫面、signal 正確、首任務回應；**重新連上 guest agent 不代表使用者程序已恢復**，須見原 session 與首任務回應才算 | |
| P1 能力旗標與 ADR | gVisor 與 Firecracker 對 R02 恢復契約各欄位行為逐項對照（gVisor 未實作欄標 not_run 附原因） | 差異以**能力旗標與 ADR** 明確區分並同 PR 更新；不得無實測即宣稱兩 runtime 行為一致；不以本票私改契約 | |
| F1 fork side effects | fork 後原 VM 與 fork 各自寫入與觸發 side effect（檔案 marker、網路請求 marker），雙向比對 | 任一方變更與請求不出現在另一方；重複、洩漏或互相干擾即 fail | |
| F2 fork identity | fork 後驗 sandbox id／generation／session 全為新值，並植入拋棄式 marker 掃憑證不繼承（沿用 R03 K1–K4 語意） | id／generation／session 不與原實例共用；credential marker 不出現或依策略明確排除並記錄機制 | |
| F3 fork 未過即禁用 | F1／F2 任一 fail 時檢查產品行為 | memory fork 能力旗標維持關閉、明確拒絕，不得部分啟用；如實記錄 | |
| B1 升版相容 | 舊版 snapshot 以新版 Firecracker restore（版本對執行時釘選記錄） | 如實記錄 pass／fail；官方無跨版本承諾即記「無承諾」，不推測 | |
| B2 bench 可重跑 | 效能量測命令以腳本化可重跑形式入證據，同環境重跑一次對照 | 重跑紀錄與原量測同數量級；實測環境（主機、版本）逐列附上；不可重跑的量測不作數 | |

## 證據與發布

- 原始輸出（stdout／stderr、Firecracker API log、jailer／guest agent log、計時、`du`、磁碟檢查輸出、manifest）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r08 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；拋棄式 marker secret 的建立與撤銷記錄入 manifest；任何憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源（microVM、jail 目錄、TAP／netns、磁碟與 snapshot 檔），先停 VM 再刪檔；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：R07 未執行或 blocked → 本協定 blocked；R07 no-go → #38 依其完成證據欄以 **not planned** 關閉，本協定永不開跑。基礎設施未授權同為 blocked ≠ no-go，等待時間不算 timebox。
- **開工授權**：R07 GO ＋ 授權主機可用 ＋ 三組 sub-issues（snapshot 一致性、resume hooks、bench/upgrade，各 3–5 天起點）拆分完成 ＋ 執行前清單全數完成，才授權各 sub-issue 依自身驗收開工；GO 只代表開工授權，本文件與結果報告都不是實作。
- **驗收 GO（關 #38 用）**：S1–S4、R1–R5、P1、F1–F3、B1–B2 無未解決 fail，且恢復與 fork 量測表已依取樣規則填實（樣本數、失敗率、p50／p95、實測環境逐列附上）。
- **runtime 差異走能力旗標與 ADR**：gVisor 暖恢復未實作不阻擋本票判定；兩 runtime 行為不同時以能力旗標與 ADR 區分（P1 實測為準），不得無實測宣稱一致。
- **fork 未過則禁用**：F1／F2 任一未解決 fail，memory fork 能力維持禁用（F3），不因效能數值好看而啟用。
- **不把約一秒寫成 SLA**：不預設恢復秒數門檻，不以任何量測結果承諾恢復秒數或 SLA；延遲只記錄供決策。
- **不以量測結果私改契約**：恢復語意以契約與 R02 定義為準；發現契約缺口走契約變更流程，不得為通過驗收放寬契約。
- **timebox 10–15 人天到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤 M1–M3**：現行 gVisor runtime 保留、不因此切換；研究 no-go 時，對應功能票依 #38 完成證據欄以 not planned 關閉。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go 宣稱、R07 結果預設（R07 尚未執行，本協定隨之 blocked）。
- 跨 host／CPU migration、無限 fork、記憶體去重（#38 本票不含；需另通過 go gate）。
- 「約一秒恢復」的 SLA 承諾、既有 sandbox 自動遷移的宣稱。
- 對 M1–M3 時程的影響宣稱。
