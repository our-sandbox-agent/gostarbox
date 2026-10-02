# R03：磁碟 Snapshot／Fork 與憑證處理協定

Refs #33（里程碑 R，#6；前置 #12、#17、#20、#21、#25）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票。Timebox 6–10 工程人天（1 人；#33 全票估時，含本票測試／說明，不含跨票整合與外部等待；大於 5 天的工作包須先拆 sub-issues 才可開工；到期必交證據與 go/no-go，不無限延長）。實測結果將另立結果報告，不回填本文件。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。R01／R02 的量測與 go/no-go 不因本票重跑；terminal-ready 等計時語意沿用 [r01-warm-restore-protocol.md](r01-warm-restore-protocol.md) 的定義，本文件只增補 snapshot／fork 特有欄位。

## 目標

回答一個問題：**能否複製真實檔案的一致時間點（snapshot／fork），又不默默複製原 sandbox 的存取身分**——且失敗時不留半套狀態、不影響仍存在的 sandbox。#33 五個驗收欄對應的協定段落如下：

| #33 驗收欄 | 對應協定段落 |
|---|---|
| 先拆 snapshot storage 與 fork lifecycle 兩張 3–5 天子票；對資料格式是否要 loop/XFS 做 ADR，不預設必遷移 | 執行前清單、決策規則 |
| 一致快照有 flush/freeze/unfreeze 保證，所有錯誤路徑 finally 解凍；snapshot immutable | 相容性檢查表 F1–F3 |
| Fork 新 sandbox id/generation/session；credentials 不繼承策略（gh、Claude history 機密、arbitrary user secrets） | 相容性檢查表 K1–K5 |
| 原／fork 改檔互不干擾；快照 expiry、備份與恢復不影響仍存在的 sandbox | 相容性檢查表 I1–I4 |
| 資源大小、保留期及計量事件接 T30；disk 與 memory 快照名稱清楚 | 量測矩陣、相容性檢查表 T1–T2 |

官方基準文件沿用 R01 所引 gVisor checkpoint/restore 文件；磁碟快照引擎（volume snapshot／CoW 等）無單一官方基準時，以執行時採用引擎的官方文件為準。查證日期：**執行時記錄**——旗標與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節。

## 執行前清單（全部完成才開跑）

- [ ] 已拆兩張 sub-issues：**snapshot storage** 與 **fork lifecycle**，各 **3–5 工程人天**，連結記入 manifest；未拆完不開工（#33 第一個驗收欄）。
- [ ] 資料格式 ADR（是否 loop/XFS，或既有 volume 格式）已依 ADR 流程提出（Proposed，[docs/adr](../adr/)），**不預設必遷移**；ADR 連結記入 manifest，未提出即不開跑 snapshot storage 子票。
- [ ] 前置票 #12、#17、#20、#21、#25 的狀態與完成證據逐一核對並記錄；未完成者如實標 blocked，不假設已完成。
- [ ] R02（#32）關係如實記錄：本票**不含 memory fork**，disk snapshot／fork 不以 R02 GO 為前置；memory 快照僅命名與計量對照（T1–T2），其實測列以 R02 GO 為前置，R02 未執行或 no-go 時該等列標 blocked／not_run 並附原因。
- [ ] 契約面：snapshot／fork 的資源語意（獨立 resource ID、generation、expiry、Lost 區間）目前以 [workspace-persistence](../contracts/workspace-persistence.md)（Proposed）與 [usage-ledger ADR](../adr/usage-ledger.md) 為準；實測若需新語意（如 fork 的 credentials 不繼承欄位、快照 TTL 行為），須先依契約變更流程定義並同 PR 更新受影響文件，不得以 runtime 私改繞過契約。憑證措施對齊 [byok-policy](../contracts/byok-policy.md)。
- [ ] 版本釘選——執行時記錄**實際值**，不沿用 R01／R02 舊值：Docker Engine（`docker version`）、runsc release（runsc `--version` 與 binary checksum）、kernel／arch／cgroup（`uname -a`）、診斷 image digest／ID、磁碟快照引擎與版本。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r03=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源，禁用 `docker system prune` 與通用名字清理。
- [ ] 拋棄式測試 token／marker secret 的建立與撤銷計畫先寫入 manifest；真憑證不進任何命令列、log 或報告。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 工作負載：沿用 R01 診斷 image；workspace／home volume 內以固定 fixture 產生器建立檔案集（1 GB／10 GB 或依 host 與 timebox 調整的代表值），fixture 與 hash 執行時固定並記錄；fork 前另植入固定「變更集」作為 CoW 增量負載。
- **snapshot 耗時（秒）**：flush 指令送出 → snapshot finalize 完成，`date +%s.%N` 差值。
- **凍結期間（秒）**：freeze → unfreeze，代表原 sandbox 寫入停頓，與 snapshot 耗時分開記錄。
- **fork 建立耗時（秒）**：fork 指令送出 → 新 sandbox terminal-ready（沿用 [terminal-protocol](../contracts/terminal-protocol.md) 契約語意）。
- **快照大小（MiB）**：每次成功 snapshot 後 `du -sh` 快照目錄與 volume 增量，並記錄主機可用磁碟。
- **資源大小（bytes）與保留期（秒）**：對照計量事件（T30）——事件語意依 [usage-ledger ADR](../adr/usage-ledger.md)（snapshot.created／expired／deleted、byte·ms、TTL 到期只發刪除請求、確認刪除才結束儲存）。

命令模板（實際指令以執行時採用的引擎與官方文件核對後逐字記錄）：

```sh
# 候選：一致快照（flush/freeze/unfreeze）
sync                                    # flush：落盤
fsfreeze --freeze <mount>               # freeze：凍結寫入（或暫停容器等替代作法，如實記錄採用者）
<engine> snapshot create ...            # 建立快照（CoW／checkpoint image）
fsfreeze --unfreeze <mount>             # unfreeze：所有錯誤路徑也必須 finally 解凍
```

### 效能量測表

單位：期間一律**秒**、大小一律**MiB**；重跑次數與分布（min／median／max）執行時記錄，未跑的列填 `not_run` 並附原因。

| 檔案集 | 變更集 | snapshot 秒 | 凍結秒 | snapshot MiB | fork 秒 | exit code | 次數 |
|---|---|---|---|---|---|---|---|
| 1 GB | 小 | | | | | | |
| 1 GB | 大 | | | | | | |
| 10 GB | 小 | | | | | | |
| 10 GB | 大 | | | | | | |

### 計量對照表（T30）

disk 與 memory 快照分列；memory 快照列以 R02 GO 為前置，未執行時填 `not_run`／`blocked` 並附原因。

| 快照種類 | 事件 | 資源大小 bytes | 保留期秒 | ledger 對齊證據 |
|---|---|---|---|---|
| disk | created | | | |
| disk | expired | | | |
| disk | deleted | | | |
| memory（依 R02） | created | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| F1 一致快照 | 快照進行中與完成後讀寫 marker、比 hash | 快照內容為凍結時點的一致版本；無撕裂或半寫檔 | |
| F2 錯誤路徑 finally 解凍 | 故意觸發快照失敗（空間不足、kill 快照程序） | 任一錯誤路徑後 unfreeze 已執行（或凍結機制明確解除），原 sandbox 可繼續寫入；無殘留凍結狀態；證據含失敗輸出 | |
| F3 snapshot immutable | 對既有快照嘗試寫入／修改（含 host 端路徑）；自同快照 fork 兩次比內容 hash | 寫入被拒或不影響快照內容；兩次 fork 取得一致內容 | |
| K1 fork 新身分 | fork 後讀 sandbox id／generation／session（[workspace-persistence](../contracts/workspace-persistence.md) 語意） | id／generation／session 全為新值，不與原 sandbox 共用；ledger 以新資源 ID 記錄 | |
| K2 gh 憑證不繼承 | fork 前以拋棄式測試 token 登入 gh；fork 後在新 sandbox 驗 `gh auth status` 與設定檔 | token 不出現在 fork（未登入或依策略明確重設）；原 sandbox 不受影響；證據只含遮蔽後 token | |
| K3 Claude history 機密 | fork 前在 Claude history／session 檔植入唯一 marker；fork 後掃整個 fork home | marker 不出現，或依不繼承策略明確排除並記錄排除機制；原 sandbox 的 history 保留完整 | |
| K4 arbitrary user secrets | fork 前在 `~/.netrc`、環境 dump、任意使用者秘密檔等多處植入 marker；fork 後掃描 | 同 K3；掃描範圍與結果如實記錄；對齊 [byok-policy](../contracts/byok-policy.md)（備份不得整包含 secret） | |
| K5 憑證策略文件 | 依 K2–K4 實測修訂 credentials 不繼承策略（含 gh、Claude history、arbitrary secrets 限制），契約／ADR 同 PR 更新 | 策略存在、可執行、與實測一致；不得只靠「沒看到」宣稱安全 | |
| I1 原／fork 改檔互不干擾 | 原與 fork 各寫不同 marker，並對同路徑覆寫，雙向比 hash | 任一方變更不出現在另一方；同路徑覆寫各自獨立 | |
| I2 快照 expiry | 對快照設短 TTL 到期（[usage-ledger](../adr/usage-ledger.md) 語意） | 到期／確認刪除事件如實記錄；原 sandbox 與既有 fork 持續運作不受影響 | |
| I3 備份與恢復不影響既有 sandbox | 從快照恢復／備份匯出期間，原 sandbox 持續寫入 | 原 sandbox 資料不損毀、服務不中斷；恢復體為新 id／generation | |
| I4 快照刪除後 fork 存活 | 刪除來源快照後繼續使用 fork | fork 不依賴來源快照；若相依，生命週期語意已明確定義並如實記錄 | |
| T1 disk／memory 快照命名 | 盤點快照資源命名與計量事件分類 | disk 與 memory 快照名稱可明確區分、不共用混淆 ID；memory 命名沿用 R02 checkpoint 語意 | |
| T2 計量事件接 T30 | 每次 snapshot／fork／expiry 對照 ledger 事件（snapshot.created／expired／deleted 與 runtime／volume 事件） | 資源大小、保留期與事件一一對應；缺事件或大小不符即 fail | |

## 證據與發布

- 原始輸出（stdout／stderr、計時、`du`、manifest、掃描結果）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r03 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；拋棄式測試 token／marker 的建立與撤銷記錄入 manifest，任何真憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源，先停容器再刪 volumes 與快照；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：#12、#17、#20、#21、#25 任一未完成 → 本協定對應範圍如實標 blocked；blocked ≠ no-go，等待時間不算 timebox。
- **R02 關係**：disk snapshot／fork 不以 R02 為前置；memory 快照實測列以 R02 GO 為前置，R02 no-go 或未執行時該等列標 blocked／not_run 附原因，不影響 disk 範圍判定。
- **開工授權**：兩張 sub-issues（snapshot storage、fork lifecycle，各 3–5 天）拆分完成＋資料格式 ADR（loop/XFS，不預設必遷移）以 Proposed 提出＋執行前清單全數完成，才授權各 sub-issue 依自身驗收開工；授權只代表開工，本文件與結果報告都不是實作。
- **驗收 GO（關 #33 用）**：F1–F3、K1–K5、I1–I4、T1–T2 無未解決 fail，且量測矩陣與計量對照表各列已填實（memory 快照實測列可依 R02 狀態標 blocked／not_run 並附原因）。
- **失敗處置**：任一 fail 未解決即不得宣稱 snapshot／fork 可用；可記錄後維持無 snapshot／fork 功能，不掩蓋。
- **不預設移轉**：loop/XFS ADR 在有實測證據支持前，不宣稱必須遷移資料格式，也不預設既有格式不足。
- **timebox 到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤 M1–M3**：既有功能不受本票影響；研究 no-go 時，對應功能票依 #33 完成證據欄以 not planned 關閉。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go 宣稱、R01／R02 結果預設（兩者皆未執行）。
- gh 登入自動跟過去（#33 本票不含）。
- 跨租戶分享／memory fork（#33 本票不含）。
- 對 M1–M3 時程的影響宣稱。
