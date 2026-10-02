# R07：Firecracker 基礎 Runner 與冷恢復協定

Refs #37（里程碑 R，#6；前置 #36、#17、#18、#20、#22、#23）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票，**仍未授權切換 runtime 或購置基礎設施**。Timebox 15–25 工程人天（1 人；#37 全票估時，含本票測試／說明，不含跨票整合、外部等待與後續修復；大於 5 天的進階票屬待細拆工作包，須先拆 sub-issues 才可開工；到期必交證據與 go/no-go，不無限延長；通過研究不代表功能已交付）。實測結果將另立結果報告，不回填本文件。

**條件式開工（R06 go 才開工，未觸發即 blocked）**：

- **R06（#36）GO 為硬前置**：R06 尚未執行或 blocked 時，本協定一律 blocked，不開跑；R06 no-go 時 #37 依其完成證據欄以 not planned 關閉。
- **基礎設施授權**：R06 GO 只授權提出採購／實作提案；主機取得（裸機或 nested 實測可用之 VM）與授權由創辦人另行決定，未取得前本協定 blocked，不算 no-go。
- **#37 為 epic**：開工前須完成五組 sub-issues 切分（見執行前清單）；本文件只預先登記各 sub-issue 共同遵守的驗收語意與決策規則，不是實作。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。R06 的量測與 go/no-go 不因本票重跑；恢復模式分列語意（cold restart／warm restore／warm cache，不混稱 cold resume）與計時定義沿用 [r06-firecracker-eval-protocol.md](r06-firecracker-eval-protocol.md)，本文件只增補 Runner 交付特有欄位。

## 目標

回答一個問題：**VM 隔離的 runtime 能否以同一產品介面交付 create／exec／cold resume／terminal／file 操作**——建立與毀滅可復原、失敗不丟 volume、版本可固定。#37 五個驗收欄對應的協定段落如下：

| #37 驗收欄 | 對應協定段落 |
|---|---|
| 先拆 host/jailer、guest image/agent、network、volume/terminal、contract tests 五組 sub-issues，各以 3–5 天為起點 | 執行前清單、決策規則 |
| 建立與毀滅 jail/tap/netns 可復原；guest files 服務不能讓 host 同時掛載 running VM 的 ext4 | 相容性檢查表 N1–N3 |
| create/exec/cold resume/terminal/file 操作通過同一 contract suite，錯誤時保留 volume | 量測矩陣（contract suite 表）、相容性檢查表 C1–C2 |
| runtime 選擇固定於 sandbox，旗標只影響新實例，不宣稱既有 VM 自動遷回 | 相容性檢查表 R1、決策規則 |
| image、kernel 與 runtime digest 固定；最小可用 guest 不以 CI 範例映像直接當 production | 執行前清單（版本釘選）、相容性檢查表 D1–D2 |

官方基準文件：[Firecracker getting-started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md) 與 [jailer 說明](https://github.com/firecracker-microvm/firecracker/blob/main/docs/jailer.md)（jailer 的 chroot／cgroup／netns 語意、由 operator 自行負責 cleanup）。查證日期：**執行時記錄**——旗標、API 與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節，也不預設任何特定恢復秒數。

## 執行前清單（全部完成才開跑）

- [ ] R06 已交付結果報告且判定 **GO**（依 [r06-firecracker-eval-protocol.md](r06-firecracker-eval-protocol.md) 決策規則），結果報告連結記入 manifest；R06 尚未執行或 blocked 時本協定不開跑，如實標 blocked（blocked ≠ no-go）。
- [ ] 基礎設施授權：創辦人已明確授權並取得 R06 GO 標的之候選主機；主機型號／供應商／區域與取得日期記入 manifest。未取得即 blocked，不開跑。
- [ ] 已拆五組 sub-issues：host/jailer、guest image/agent、network、volume/terminal、contract tests，**各以 3–5 工程人天為起點**，連結記入 manifest；未拆完不開工（#37 第一個驗收欄）。
- [ ] 前置票 #17、#18、#20、#22、#23 的狀態與完成證據逐一核對並記錄；未完成者如實標 blocked，不假設已完成。
- [ ] 版本釘選——執行時記錄**實際值**，不假設、不沿用 R06 舊值：Firecracker release 與 binary checksum（含 jailer）、kernel image 來源與 hash、guest image digest 與 build 記錄、host 型號與 kernel／arch（`uname -a`）。**最小可用 guest 為自建 production 候選，不得以官方 CI 範例映像直接當 production**（#37 第五個驗收欄）。
- [ ] contract suite 先登記：以現行契約（[terminal-protocol](../contracts/terminal-protocol.md)、[files-policy](../contracts/files-policy.md)、[workspace-persistence](../contracts/workspace-persistence.md)、[runner-lifecycle](../contracts/runner-lifecycle.md)）組成**同一份** suite，gVisor 現行 adapter 與 Firecracker runner 均跑此份；suite 內容寫入 manifest 後才開跑，不得為單一 runtime 另編 suite。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r07=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源（microVM、jail 目錄、TAP／netns、磁碟檔），禁用通用名字清理。
- [ ] 機密紀律：測試用 microVM 不注入任何真憑證；任何憑證不進命令列、log 或報告。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 操作與計時定義

- 五個操作面：create、exec、cold resume、terminal、file（上傳／下載），全部經同一 contract suite 驗證；操作語意以契約現文為準（terminal-ready 依 [#13 終端協定契約](../contracts/terminal-protocol.md) 語意：attach 後取得 shell 回應且 `stty size` 有輸出），契約變更時此表跟著改。
- **cold resume**：Suspend 後不帶 memory 狀態的恢復（workspace volume 保留、程序為新起），計時語意沿用 R06 cold restart 定義（端到端恢復、terminal-ready、首任務回應 `python3 -c 'print("TASK_OK")'`）；不與 warm restore 混稱。
- **失敗率**：每操作重複 N 次（N 執行時釘選）；未達終態或契約偏差即計失敗，原始 exit code 與錯誤輸出入證據，不掩蓋。

### Contract suite 量測表

單位：期間與回應一律**秒**；樣本數與分布（min／median／max 或 p50／p95）執行時釘選並記錄，未跑的列填 `not_run` 並附原因。

| 操作 | runtime | 樣本數 | 失敗率 | 端到端秒 | 錯誤時 volume 保留 | exit code |
|---|---|---|---|---|---|---|
| create | gVisor 現行（對照） | | | | | |
| create | Firecracker | | | | | |
| exec | Firecracker | | | | | |
| cold resume | Firecracker | | | | | |
| terminal（attach／resize／detach／重連） | Firecracker | | | | | |
| file（上傳／下載） | Firecracker | | | | | |

對照組（gVisor 現行）跑同一 suite 同表填列；除 create 外哪些列需 gVisor 對照由執行者依 timebox 決定，未跑列標 not_run 附原因。本表不重跑 R06 的恢復模式比較。

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| N1 jail 建立與毀滅可復原 | 建立含 jail 目錄之 microVM 後以正常與異常（kill runner／jailer）路徑毀滅，重複 N 次，前後對照檔案與程序 | jail 目錄、程序、權限無殘留且可重複；異常終止亦有清理或明確殘留清單；證據含每次對照 | |
| N2 TAP／netns 建立與毀滅可復原 | 同 N1 手法驗 TAP 裝置、netns 與路由規則 | 裝置與規則清乾淨、可重複；殘留即 fail 並記錄 | |
| N3 running VM 磁碟唯 guest 通道 | guest files 服務運作中，由 host 嘗試直接 mount 該 ext4 volume；並驗 agent 死亡等錯誤路徑 | running 期間 host **不得**同時掛載；檔案存取只經 guest agent 通道；錯誤路徑亦不得改由 host 掛載 | |
| C1 同一 contract suite | create／exec／cold resume／terminal／file 於 gVisor 現行與 Firecracker 各跑 manifest 登記之同一 suite | 兩 runtime 行為一致通過；差異逐項如實記錄，不得為湊 pass 放寬 suite 或契約 | |
| C2 錯誤注入保留 volume | 對每個操作注入失敗（kill runner、斷網、磁碟空間不足）再毀滅／重啟 | workspace volume 與其資料保留、錯誤明確可見不靜默；不得以程序或網路失敗為由銷毀 volume | |
| R1 runtime 選擇固定於 sandbox | 切換預設 runtime 旗標後，新建立與既有 sandbox 分別查 runtime 記錄 | runtime 於 sandbox 建立時固定；旗標只影響**新實例**；既有 sandbox／VM 不遷移、不重啟，亦不宣稱自動遷回 | |
| D1 image／kernel／runtime digest 固定 | 重建 guest image 與 kernel 配置，比對 digest／hash 與 manifest | 全部固定且可重現；升級即換 digest 並重驗受影響矩陣；來源與 build 記錄入證據 | |
| D2 最小可用 guest | 以自建最小 guest 映像跑 suite；官方 CI 範例映像只用於開發對照 | production 候選為自建最小 guest；不以 CI 範例映像直接當 production，如實記錄兩者差異 | |

## 證據與發布

- 原始輸出（stdout／stderr、Firecracker API log、jailer／guest agent log、計時、`du`、mount／netns 探針輸出）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r07 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；任何憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源（microVM、jail 目錄、TAP／netns、磁碟檔），先停 VM 再刪檔；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：R06 未執行或 blocked → 本協定 blocked；R06 no-go → #37 依其完成證據欄以 **not planned** 關閉，本協定永不開跑。基礎設施未授權同為 blocked ≠ no-go，等待時間不算 timebox。
- **開工授權**：R06 GO ＋ 創辦人基礎設施授權 ＋ 五組 sub-issues 拆分完成（各 3–5 天起點）＋ 執行前清單全數完成，才授權各 sub-issue 依自身驗收開工；GO 只代表開工授權，本文件與結果報告都不是實作。
- **驗收 GO（關 #37 用）**：N1–N3、C1–C2、R1、D1–D2 無未解決 fail，且 contract suite 表已依取樣規則填實（含對照列或 not_run 附原因）。
- **既有 sandbox 不自動遷移**：GO 只代表新實例可選 Firecracker；不宣稱既有 VM 自動遷回，旗標語意以 R1 實測為準。
- **不以量測結果私改契約**：suite 以契約現文為準；發現契約缺口走契約變更流程，不得為通過 suite 放寬契約。
- **不以延遲單一數字決定安全需求**：恢復秒數只記錄供決策，隔離與安全需求以契約與 R06 的隔離證據為準。
- **timebox 15–25 人天到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤 M1–M3**：現行 gVisor runtime 保留、不因此切換；研究 no-go 時，對應功能票依 #37 完成證據欄以 not planned 關閉。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go 宣稱、R06 結果預設（R06 尚未執行，本協定隨之 blocked）。
- memory snapshot、跨主機遷移、GPU（#37 本票不含）。
- 購機或任何基礎設施承諾、既有 sandbox 自動遷回的宣稱。
- 對 M1–M3 時程的影響宣稱。
