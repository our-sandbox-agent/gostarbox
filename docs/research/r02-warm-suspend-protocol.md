# R02：暖 Suspend 一致記憶體與磁碟版本協定

Refs #32（里程碑 R，#6；前置 #31、#17、#18、#19、#21、#25）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票。**開工以 R01（#31）GO 為前置**：R01 未執行即本協定 blocked；R01 no-go 時 #32 依其完成證據欄以 not planned 關閉。Timebox 8–15 工程人天（1 人；#32 全票估時，含本票測試／說明，不含跨票整合與外部等待；大於 5 天的工作包須先拆 sub-issues 才可開工；到期必交證據與 go/no-go，不無限延長）。實測結果將另立結果報告，不回填本文件。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。R01 的量測與 go/no-go 不因本票重跑；工作負載與計時語意沿用 [r01-warm-restore-protocol.md](r01-warm-restore-protocol.md) 的定義，本文件只增補 Suspend 特有欄位。

## 目標

回答一個問題：**能否以一致的記憶體與磁碟版本做 warm Suspend，並恢復同一程序狀態**——且失敗時可明確轉冷、不留半套狀態。本票僅當 R01 go 時執行；#32 五個驗收欄對應的協定段落如下：

| #32 驗收欄 | 對應協定段落 |
|---|---|
| 開工前拆 adapter、snapshot lifecycle、resume hooks、failure tests sub-issues（各 ≤5 天） | 執行前清單、決策規則 |
| manifest 同時標 runtime／image／CPU、memory、volume generation；期間 file API 唯讀或作廢轉冷 | 相容性檢查表 M1–M3 |
| 空間預留、atomic finalize、restore 失敗保留證據、cold fallback 明確告知 | 相容性檢查表 S1–S4 |
| API key／session lease、時間、網路、程序重連；guest ping 不算完整恢復 | 相容性檢查表 R1–R4 |
| 取樣含失敗率、樣本數、p50/p95、disk size、checkpoint 耗時、端到端；升版相容性與回退 runbook | 量測矩陣、相容性檢查表 V1–V2 |

官方基準文件沿用 R01 所引 gVisor checkpoint/restore 文件。查證日期：**執行時記錄**——旗標與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節。

## 執行前清單（全部完成才開跑）

- [ ] R01 已交付結果報告且判定 **GO**（依 [r01-warm-restore-protocol.md](r01-warm-restore-protocol.md) 決策規則）；結果報告連結記入 manifest。R01 尚未執行或 blocked 時本協定不開跑，如實標 blocked。
- [ ] 已拆 sub-issues：adapter、snapshot lifecycle、resume hooks、failure tests，**各 ≤5 工程人天**，連結記入 manifest；未拆完不開工（#32 第一個驗收欄）。
- [ ] 前置票 #17、#18、#19、#21、#25 的狀態與完成證據逐一核對並記錄；未完成者如實標 blocked，不假設已完成。
- [ ] 生命週期契約目前不接受 warm 模式（422 `unsupported_suspend_mode`，見 [workspace-persistence.md](../contracts/workspace-persistence.md) 與 [sandbox-lifecycle ADR](../adr/sandbox-lifecycle.md)）；執行前須先依契約變更流程定義 warm Suspend 的 checkpoint ID、相容版本與失敗恢復語意，同 PR 更新受影響文件，不得以 runtime 私改繞過契約。
- [ ] manifest 先驗欄位：每次 run 須能同時記錄 runtime（版本與 binary checksum）、image digest、CPU、memory、**volume generation**；任一欄位無法取得即不開跑，不得留空假設。
- [ ] 版本釘選——執行時記錄**實際值**，不沿用 R01 舊值：Docker Engine（`docker version`）、runsc release（runsc `--version` 與 binary checksum）、kernel／arch／cgroup（`uname -a`）、診斷 image digest／ID。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r02=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源，禁用 `docker system prune` 與通用名字清理。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 工作負載：沿用 R01 診斷 image 固定 fixture（1／2／4 GB 匿名記憶體、heartbeat、fixture hash），執行時固定並記錄；不重跑 R01 的對照矩陣。
- **checkpoint 耗時（秒）**：warm Suspend 指令送出 → checkpoint finalize 完成（含 volume generation 一併確認），`date +%s.%N` 差值。
- **restore 耗時（秒）**／**terminal-ready（秒）**／**首個任務回應（秒）**：沿用 R01 定義（[terminal-protocol](../contracts/terminal-protocol.md) 契約語意）。
- **end-to-end latency（秒）**：觸發 warm Suspend（Active 起算）→ 恢復後 session 內首個任務回應，含 checkpoint、finalize 與 restore 全程。
- **disk size（MiB）**：每次成功 checkpoint 後 `du -sh` checkpoint 目錄與 volume 增量，並記錄主機可用磁碟。

取樣規則（#32 第五個驗收欄）：每列記錄**樣本數、失敗率、p50／p95**；單次成功或 guest ping 不作數。重跑次數與分布（min／median／max）執行時記錄；未跑的列填 `not_run` 並附原因。

### 效能量測表

| 負載 | 樣本數 | 失敗率 | checkpoint 秒 p50／p95 | restore 秒 p50／p95 | 端到端秒 p50／p95 | snapshot MiB | 附註 |
|---|---|---|---|---|---|---|---|
| 1 GB | | | | | | | |
| 2 GB | | | | | | | |
| 4 GB | | | | | | | |

### 升版相容性表

升版／回退以執行時實際版本對（checkpoint 產生版本 → restore 版本）記錄；回退 runbook（步驟、證據保留點、cold fallback 判定）為執行時交付物，隨結果報告發布，不回填本文件。

| 版本對（checkpoint → restore） | 樣本數 | 失敗率 | 結果 | runbook 演練證據 |
|---|---|---|---|---|
| | | | | |
| | | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| M1 manifest 版本欄位 | 產生 warm checkpoint 後讀 manifest | runtime／image／CPU、memory、volume generation 同時存在且為實測值；任一欄缺漏或為猜測值即 fail | |
| M2 Suspend 期間 file API 唯讀 | warm Suspend 進行中與 checkpoint 未驗證時，經外部 file API 嘗試寫入／刪除／上傳，並嘗試讀取／下載（[files-policy](../contracts/files-policy.md) `assert_mutable` 語意） | 寫入類被拒（`suspend_read_only`／`warm_checkpoint_unverified`），讀取與下載仍可用；寫入未被拒即 fail | |
| M3 版本不一致作廢轉冷 | checkpoint 完成後自 host 端變動 volume 內容，再嘗試 resume | checkpoint 被明確作廢並轉冷啟動，不殘留半新半舊可恢復狀態；行為如實記錄 | |
| S1 空間預留 | 先佔滿 checkpoint 目錄所在磁碟再觸發 warm Suspend | 開始前即拒絕（空間不足），不產生部分寫入；exit code 與輸出入證據 | |
| S2 atomic finalize | checkpoint 進行中強制中斷（如 kill），再檢查目錄 | finalize 前不存在可用的部分 checkpoint；中斷後無殘留可恢復壞狀態 | |
| S3 restore 失敗保留證據 | 故意觸發 restore 失敗（如損毀 checkpoint image 檔） | 失敗輸出、checkpoint 與 manifest 全部保留不刪，可供事後分析 | |
| S4 cold fallback 告知 | 觸發 warm 失敗轉冷啟動 | 使用者可見訊息明確告知**不再保留原程序**（對齊 [memory-session-recovery ADR](../adr/memory-session-recovery.md) 通知規範），不得顯示為正常恢復 | |
| R1 API key／session lease | warm resume 後驗 API key 與 session lease 狀態（[watchdog-lease](../contracts/watchdog-lease.md) 證據規則） | key／lease 正確更新或重發、epoch 一致；不以舊 epoch 證據充數 | |
| R2 時間 | resume 前後讀 session 時間、時區與跳動 | 時間行為如實記錄；時鐘回撥造成的錯誤明確標註 | |
| R3 網路 | checkpoint 前建立長連 TCP（如 `nc`）；resume 後觀察連線與重連 | 連線斷／留行為如實記錄；斷線後可重新建立連線 | |
| R4 使用者程序重連 | checkpoint 前 `tmux` 跑 heartbeat；resume 後 attach 並送固定命令（同 R01 C1 語意） | attach 可見原 session 與畫面、signal 正確、首任務回應；**guest ping 或 container 健康檢查通過不算完整恢復** | |
| V1 升版相容 | 舊版 checkpoint 以新版 restore（依升版相容性表版本對執行） | 如實記錄 pass／fail；不相容即列入回退 runbook 的回退條件 | |
| V2 回退 runbook | 依 runbook 實際演練一次回退 | 步驟可重現、證據齊全；runbook 缺可執行步驟即 fail | |

## 證據與發布

- 原始輸出（stdout／stderr、runsc log、計時、`du`、manifest）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r02 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；任何憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源，先停容器再刪 volumes；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：R01 no-go → #32 依其完成證據欄以 **not planned** 關閉，本協定永不開跑；R01 blocked ≠ go，等待時間不算 timebox。
- **開工授權**：R01 GO ＋ sub-issues 拆分完成（各 ≤5 天）＋ 執行前清單全數完成，才授權各 sub-issue 依自身驗收開工；GO 只代表開工授權，本文件與結果報告都不是實作。
- **驗收 GO（關 #32 用）**：M1–M3、S1–S4、R1–R4、V1–V2 無未解決 fail，且量測矩陣各列已依取樣規則填實（失敗率、樣本數、p50／p95、disk size、checkpoint 耗時、end-to-end latency）。
- **失敗處置**：任一 fail 未解決即不得宣稱暖 Suspend 可用；可記錄後維持 cold-only，不掩蓋。
- **不預設秒數門檻**：未達 latency 目標時記錄實值而不隱藏（#32 本票不含欄），也不以任何量測結果承諾恢復秒數。
- **timebox 到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤 M1–M3**：cold Suspend 與既有功能不受本票影響；研究 no-go 時依 #32 完成證據欄處理。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go 宣稱、R01 結果預設（R01 尚未執行，本協定隨之 blocked）。
- 跨 host 遷移與 memory fork（#32 本票不含）。
- 對 M1–M3 時程的影響宣稱。
