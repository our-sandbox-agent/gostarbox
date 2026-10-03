# R06：Firecracker 需求、主機與成本門檻評估協定

Refs #36（里程碑 R，#6）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票，**仍未授權切換 runtime**。Timebox 2–3 工程人天（1 人；含本票測試／說明，不含跨票整合、外部等待與後續修復；到期必交證據與 go/no-go，不無限延長；通過研究不代表功能已交付）。實測結果將另立結果報告，不回填本文件。

**條件式開工（兩條觸發路徑，未觸發即暫緩）**：

- 常規升級研究：#31／#24 完成後依需求評估，兩票提供資料，不設為所有情境的硬 blocker。
- M0 退路：若 #8 對 host clone panic 的 containment 無法接受，由創辦人明確選擇比較範圍後可提前研究，不需先完成 gVisor Alpha 才研究替代。
- 沒有明確觸發／決策仍暫緩；本票不決定改用 Firecracker、不購主機。原生移除 #31／#24 只消除循環，並非宣告 ready。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。R01–R05 的量測與 go/no-go 不因本票重跑；#8 的 E01–E10 矩陣不重跑，#8 的 host clone panic 判定只引用、不覆核。供應商與單價背景可參考 [docs/research.md](../research.md) 的盤點，但其中價格與延遲皆為歷史查證，**不得**作為本票的量測值或採購依據。

## 目標

回答一個問題：**是否真的需要 VM 隔離／更好的恢復——先判斷需求成立，通過才決定採購**；本票是研究協定，不是切換 runtime 的授權，也不是實作。#36 五個驗收欄對應的協定段落如下：

| #36 驗收欄 | 對應協定段落 |
|---|---|
| 分別比較 cold restart、warm restore cold-cache 和 warm-cache，不混叫 cold resume；R01 no-go 時仍可比較 cold restart | 量測矩陣（恢復模式定義與分列表） |
| 硬條件檢查 Linux/KVM/dev-kvm、CPU/kernel 支援、nested virtualization 與供應商政策；不能把『必須裸機』當普遍定理 | 執行前清單、相容性檢查表 H1–H4 |
| 比較實測端到端恢復、失敗率、每 sandbox 成本、維運／升版成本與客戶隔離需求 | 量測矩陣（恢復表＋成本表）、相容性檢查表 O1–O3、I1 |
| 產出 go/no-go；機型價格當日查證，不把舊 €200–250 當採購承諾 | 決策規則 |
| 若 sandbox 契約保證 VM 隔離，裸機滿載必須排隊／拒絕，不自動降成 gVisor | 決策規則、相容性檢查表 I1 |

官方基準文件：[Firecracker getting-started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)（官方前提是 KVM 與 /dev/kvm access），以及執行時官方文件對 snapshot／restore 語意的規定。查證日期：**執行時記錄**——旗標、API 與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節（含 UFFD 等 restore 優化的可用性），也不預設「必須裸機」或任何特定恢復秒數。

## 執行前清單（全部完成才開跑）

- [ ] 開工條件核對（條件式，依上述兩條觸發路徑）：常規路徑須 #31／#24 已完成並依需求評估成立；M0 退路須 #8 的 host clone panic containment 判定為無法接受**且**創辦人明確選擇比較範圍。兩條皆未成立即本協定暫緩不開跑，如實記錄狀態；暫緩 ≠ blocked ≠ no-go。
- [ ] 硬條件檢查（候選主機逐項、證據入 manifest）：Linux/KVM 與 `/dev/kvm` 存取（如 `ls -l /dev/kvm`、以實際命令確認可開啟且非僅存在）、CPU／kernel 支援（虛擬化擴充、kernel 版本與 `uname -a`）、nested virtualization 可用性、供應商政策（條款是否允許 KVM／nested、裸機與 VM 執行個體的差別）。**不能把『必須裸機』當普遍定理**——供應商政策與 nested 實測可用即如實記錄，逐供應商逐機型判斷。
- [ ] 比較範圍先登記：對照組（現行 gVisor Docker adapter 同負載）、要量測的恢復模式（cold restart 必測；warm 兩式依創辦人選定的比較範圍），寫入 manifest 後才開跑；R01 no-go 時 cold restart 比較仍可執行，warm 列標 not_run 附原因即可。
- [ ] 版本釘選——執行時記錄**實際值**，不假設、不沿用 #8 與 R01 舊值：Firecracker release 與 binary checksum、kernel／arch（`uname -a`）、候選主機型號／供應商／區域與定價頁查證日期、診斷 image digest／ID、rootfs／kernel image 來源與 hash。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r06=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源（microVM、磁碟／snapshot 檔、TAP 等網路設定），禁用通用名字清理。
- [ ] 機密紀律：測試用 microVM 不注入任何真憑證；供應商帳號資訊不進命令列、log 或報告。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 恢復模式與計時定義

三式**分列、不得混稱 cold resume**：

- **cold restart**：全新 microVM 啟動（掛 rootfs、init 完成）→ terminal 可用；不依賴任何 snapshot。
- **warm restore（cold cache）**：由 snapshot restore，且頁快取為冷——於專用拋棄式測試 host 以 `sync; echo 3 > /proc/sys/vm/drop_caches`（需 root；執行時間與 exit code 入證據）或同義可重現手法確立，僅限專用 host，共享主機不得執行。
- **warm cache**：同 host、頁快取未清下連續 restore。

- 工作負載：沿用 R01 診斷 image 固定 fixture 語意（1／2／4 GB 匿名記憶體、heartbeat、fixture hash 執行時固定並記錄），確保與 R01 可對話；不重跑 R01 對照矩陣。
- **端到端恢復（秒）**：觸發恢復（Active 起算或重啟指令送出）→ 恢復後 session 內首個任務回應，全程含啟動／restore 與 terminal 可用。
- **terminal-ready（秒）**：依 [#13 終端協定契約](../contracts/terminal-protocol.md)（Proposed）語意：attach 後取得 shell 回應且 `stty size` 有輸出。契約變更時此定義跟著改。
- **首個任務回應（秒）**：恢復後 session 內送出固定命令 `python3 -c 'print("TASK_OK")'` → 收到輸出。
- **失敗率**：每模式重複 N 次（N 執行時釘選），restore／重啟未達終態即計失敗；原始 exit code 與錯誤輸出入證據，不掩蓋。
- **每 sandbox 成本**：常駐記憶體足跡（MiB）、磁碟佔用（rootfs＋snapshot，`du -sh`）、每主機可並發 sandbox 數（以固定記憶體上限推導）。

### 恢復模式量測表

單位：期間與回應一律**秒**、記憶體與磁碟一律 **MiB**；分布（min／median／max 或 p50／p95）執行時釘選並記錄，未跑的列填 `not_run` 並附原因。

| 模式 | runtime | 負載 | 樣本數 | 失敗率 | 端到端秒 | terminal-ready 秒 | 首任務回應秒 | 記憶體 MiB | 磁碟 MiB | exit code |
|---|---|---|---|---|---|---|---|---|---|---|
| cold restart | gVisor 現行 | 1 GB | | | | | | | | |
| cold restart | gVisor 現行 | 4 GB | | | | | | | | |
| cold restart | Firecracker | 1 GB | | | | | | | | |
| cold restart | Firecracker | 4 GB | | | | | | | | |
| warm restore（cold cache） | Firecracker | 1 GB | | | | | | | | |
| warm restore（cold cache） | Firecracker | 4 GB | | | | | | | | |
| warm cache | Firecracker | 1 GB | | | | | | | | |
| warm cache | Firecracker | 4 GB | | | | | | | | |

2 GB 列與 UFFD／lazy restore 等優化：依 timebox 與執行時官方文件（該版本是否提供）決定是否加列；無則記 `not_applicable` 並附版本證據。

### 成本與維運比較表

比較對象為現行 gVisor Docker adapter；「每 sandbox 成本」與上表數值聯動，維運／升版以實際操作記錄為準，不用印象填表。

| 項目 | gVisor 現行 | Firecracker | 備註（查證日期／證據） |
|---|---|---|---|
| 每 sandbox 常駐成本（記憶體＋磁碟） | | | |
| 每主機並發 sandbox 數 | | | |
| 機型月費（候選清單逐列） | | | |
| 維運成本（監控、失敗處置、容量規劃） | | | |
| 升版成本（runtime 升級步驟與 snapshot 相容性） | | | |
| 客戶（租戶）隔離需求對應 | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| H1 Linux／KVM／`/dev/kvm` | 確認 `/dev/kvm` 存在且可存取（執行身分、權限位元），並以最小 probe 實際開啟 | 存在且可存取，probe 記錄原始輸出；不可存取時該主機標 blocked，不得以「應該可以」代替實測 | |
| H2 CPU／kernel 支援 | 虛擬化擴充（如 `grep -cE 'vmx\|svm' /proc/cpuinfo`）、kernel 版本與 Firecracker 官方需求對照 | 支援與否逐項如實記錄並附官方文件查證日期；不符即該主機 blocked | |
| H3 nested virtualization | 在 VM 執行個體內重複 H1／H2 probe | nested 可用與否如實記錄；可用即續測，不可用即標註該供應商該型號，不推廣為通則 | |
| H4 供應商政策 | 逐供應商查閱條款與定價頁（KVM／nested／裸機是否允許與計費） | 政策與價格**當日查證**並記錄查證日期與 URL；**不把『必須裸機』當普遍定理**，也不把單一供應商政策推廣為通則 | |
| O1 失敗率與失敗處置 | 每模式重複量測，收集 restore／啟動失敗的原始錯誤 | 失敗率與失敗樣態入矩陣與證據；無失敗也須有樣本數支撐，不得以單次成功宣稱穩定 | |
| O2 維運／升版成本 | 實際執行一次 Firecracker 版本升級步驟（文件導讀＋沙盤），並記錄 snapshot 跨版本相容性官方語意 | 步驟、耗時與相容性官方語意如實記錄；官方無承諾即記「無承諾」，不推測 | |
| O3 每 sandbox 成本推導 | 由量測表記憶體／磁碟足跡與候選機型定價推導每 sandbox 成本 | 推導式與單價來源（當日查證）入證據；單價變動時標註查證日期，不作跨期承諾 | |
| I1 租戶隔離需求與滿載規則 | 盤點現行契約（[workspace-persistence](../contracts/workspace-persistence.md)、[tenant-authz](../contracts/tenant-authz.md) 等）是否保證 VM 隔離；若保證，設計裸機滿載時的排隊／拒絕語意 | 若契約保證 VM 隔離：滿載必須**排隊或拒絕**，不得自動降級成 gVisor；契約無此保證亦如實記錄，兩者都以契約現文為準，不得以本票私改契約 | |

## 證據與發布

- 原始輸出（stdout／stderr、Firecracker API log、計時、`du`、probe 輸出、定價頁快照與查證日期）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r06 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；供應商帳號、報價單等商務資訊遮蔽後才入證據。
- 清理只刪自建、label 匹配的資源（microVM、磁碟／snapshot、網路設定），先停再刪；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **先決需求門**：先回答「是否真的需要 VM 隔離／更好的恢復」——需求不成立即 no-go，不進入採購比較；需求成立才以量測矩陣與成本表支撐 go。
- **GO（僅授權提出採購／實作提案）**：硬條件 H1–H4 有可行主機、三式恢復與成本比較已填實、需求門成立。**GO 不是切換 runtime 的授權、不是購買承諾**；採購與 Firecracker backend 實作另立票由創辦人決定。
- **價格當日查證**：機型價格一律以**執行當日**查證為準；過往文件中的 €200–250 等數字只是歷史參考，**不是採購承諾**，不得引用為本票依據。
- **VM 隔離契約鐵則**：若 sandbox 契約保證 VM 隔離，裸機滿載時必須**排隊或拒絕**，**不得自動降級成 gVisor**；此規則先於任何恢復速度或成本考量。
- **不以延遲單一數字決定安全需求**：恢復秒數只記錄供決策，隔離與安全需求以契約與 H1–H4／I1 的隔離證據為準。
- **blocked ≠ no-go**：主機、KVM 權限或供應商政策不可得時如實標 blocked，不算技術結論，等待時間不算 timebox。
- **timebox 2–3 人天到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- 研究結束不改變現行 runtime；研究 no-go 時，對應功能票依 #36 完成證據欄以 not planned 關閉。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go／no-go 宣稱與對 M1–M3 時程的影響。
- 租機、購買主機或任何採購承諾。
- 開 Firecracker coding／backend 實作、切換 runtime 的決定。
- 以延遲單一數字決定安全需求。
