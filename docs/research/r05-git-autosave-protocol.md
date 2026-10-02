# R05：選用的 Suspend 前 Git 自動存檔協定

Refs #35（里程碑 R，#6；前置 #19、#21、#22）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票。Timebox 2–4 工程人天（1 人；#35 全票估時，含本票測試／說明，不含跨票整合與外部等待；低信心遠期範圍，實作若膨脹逾 5 天須先拆細工作包才可開工；到期必交證據與 go/no-go，不無限延長）。實測結果將另立結果報告，不回填本文件。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。R01–R04 的量測與 go/no-go 不因本票重跑；Suspend 觸發語意沿用 [auto-policy](../contracts/auto-policy.md)（#19 `request_suspend` 操作路徑），憑證與機密排除沿用 [byok-policy](../contracts/byok-policy.md)（#21），對外連線沿用 [network-policy](../contracts/network-policy.md)（#22），額度／緊急停機優先序沿用 [watchdog-lease](../contracts/watchdog-lease.md)（#18 `runtime_deadline_at` 語意），本文件只增補 git 自動存檔特有欄位。

## 目標

回答一個問題：**只有使用者明確開啟時，能否在 Suspend 前把選定 repo 的工作可靠存進遠端分支——不改使用者 HEAD/index、不覆蓋他人更新、不卡停機**——且失敗時 local work 一律保留。#35 五個驗收欄對應的協定段落如下：

| #35 驗收欄 | 對應協定段落 |
|---|---|
| 先完成 repo-scoped GitHub 憑證整合；未具備時此票 blocked，不要求手動長期 token 混進 fork | 執行前清單、相容性檢查表 C1–C2 |
| 開關說明目的 repo/branch 和可能包含的檔案；遵守忽略規則與機密排除策略 | 相容性檢查表 C3–C5 |
| 不改使用者 HEAD/index；採有父鏈 commit 或 force-with-lease，禁止無條件 --force 覆蓋他人更新 | 量測矩陣命令模板、相容性檢查表 G1–G4 |
| 限時失敗保留 local work；額度／緊急隔離停機不可被 push 卡住 | 相容性檢查表 S1–S3 |
| 連續兩次存檔、遠端並發更新、無憑證、大變更與 hook 錯誤有驗收 | 相容性檢查表 T1–T5、量測矩陣 |

官方基準文件：Git 官方文件（`git-push` 的 `--force-with-lease` 語意、`read-tree`／`write-tree`／`commit-tree` 等 plumbing 與 hooks 行為）與目標 git host（GitHub）官方的 token 權限／API 文件。查證日期：**執行時記錄**——旗標、語意與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節，也不預設任何 git host 特有行為。

## 執行前清單（全部完成才開跑）

- [ ] 前置票 #19、#21、#22 的狀態與完成證據逐一核對並記錄；未完成者如實標 blocked，不假設已完成。**repo-scoped GitHub 憑證整合（#21 範圍）未具備時本協定一律 blocked**，不得以手動長期 token 混進 fork 變通開跑（#35 第一個驗收欄）。
- [ ] 契約面：自動存檔開關（說明目的 repo／branch 與可能包含檔案的義務）、忽略規則與機密排除目前以 [files-policy](../contracts/files-policy.md) ignore list（#21 founder list）與 [byok-policy](../contracts/byok-policy.md) `backup_plan` secret exclusions 為準；實測若需新語意（開關 schema、autosave 分支命名、存檔事件），須先依契約變更流程定義並同 PR 更新受影響的 [control-plane-api](../contracts/control-plane-api.md) 與 [cli-surface](../contracts/cli-surface.md)（存檔結果不得是 mocked success），不得以 runtime 私改繞過契約。
- [ ] 網路面：push 所需對外連線（目標 git host）依 [network-policy](../contracts/network-policy.md) domain allowlist 語意規劃；測試環境須同時涵蓋允許與拒絕路徑（如 allowlist 外 host push 必須失敗）。
- [ ] 停機優先序：Suspend 觸發沿用 [auto-policy](../contracts/auto-policy.md) `request_suspend` 操作路徑（#17 one operation per sandbox）；額度／緊急隔離停機（[watchdog-lease](../contracts/watchdog-lease.md) `runtime_deadline_at` 語意）優先於任何 push，實測前先記錄優先序設計。
- [ ] 版本釘選——執行時記錄**實際值**，不假設：git（`git --version`）、目標 git host 與採用 token 類型／權限範圍、後端 runtime／kernel（`uname -a`）、診斷 image digest／ID。
- [ ] fixture 於執行時固定並記錄，逐一可重現：拋棄式測試 repo 與遠端 autosave 分支、固定變更集產生器（小／大）、hook 錯誤模擬手法（如 pre-push 失敗）、無憑證與斷網模擬手法。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r05=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源（含測試 repo／分支），禁用 `docker system prune` 與通用名字清理。
- [ ] 拋棄式 repo-scoped 測試 token（最小權限、僅測試 repo）的建立與撤銷計畫先寫入 manifest；真憑證不進任何命令列、log 或報告。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 工作負載：拋棄式測試 repo 內以固定變更集產生器建立**小變更**（少量文字檔修改）與**大變更**（大量／大檔新增，代表值執行時固定）兩式；fixture 與 hash 執行時固定並記錄。
- **commit 耗時（秒）**：存檔程序啟動 → 本地 autosave commit 物件建立完成，`date +%s.%N` 差值。
- **push 耗時（秒）**：push 指令送出 → 指令 exit（含 `--force-with-lease` 檢查）。
- **存檔總耗時（秒）**：Suspend 觸發 → push exit 0 或明確失敗記錄。
- **Suspend 延遲（秒）**：因存檔造成的 Suspend 完成延遲＝有存檔的 Suspend 耗時 − 同負載無存檔的 Suspend 耗時，分開記錄。
- **變更集大小（KiB／檔案數）**：`git diff --stat` 與 tree 物件大小，探針輸出入證據。
- exit code 探針：commit／push 各步的原始 exit code 入證據。

命令模板——核心不變式為**不動使用者 HEAD/index**：以拋棄式暫存索引（`GIT_INDEX_FILE`）＋ plumbing 建立有父鏈 commit，push 一律帶 `--force-with-lease`，禁止無條件 `--force`（實際旗標以執行時 Git 官方文件核對後逐字記錄）：

```sh
# 暫存索引：不動使用者 index（機密已先依 files-policy ignore list 與 byok backup_plan 過濾）
TMPINDEX=$(mktemp)
GIT_INDEX_FILE=$TMPINDEX git read-tree HEAD
GIT_INDEX_FILE=$TMPINDEX git add -A -- .
TREE=$(GIT_INDEX_FILE=$TMPINDEX git write-tree)
# 有父鏈：優先接在前次 autosave 分支之後，首次則接在目前 HEAD
PARENT=$(git rev-parse --verify refs/remotes/origin/<autosave-branch>^{commit} \
         || git rev-parse HEAD)
COMMIT=$(git commit-tree $TREE -p $PARENT -m "autosave <run-id>")
git push --force-with-lease=refs/heads/<autosave-branch>:<expected-old> \
         origin $COMMIT:refs/heads/<autosave-branch>
```

### 存檔情境量測表

單位：期間一律**秒**、大小一律 **KiB**；重跑次數與分布（min／median／max）執行時記錄，未跑的列填 `not_run` 並附原因。

| 情境 | 變更集 | 樣本數 | 存檔結果 | commit 秒 | push 秒 | Suspend 延遲秒 | 變更 KiB | exit code |
|---|---|---|---|---|---|---|---|---|
| 首次存檔（基線） | 小 | | | | | | | |
| 連續第二次存檔 | 小 | | | | | | | |
| 首次存檔 | 大 | | | | | | | |
| 連續第二次存檔 | 大 | | | | | | | |
| 無憑證 | 小 | | | | | | | |
| 斷網逾時 | 小 | | | | | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| C1 repo-scoped 憑證鐵門 | 核對 #21 repo-scoped GitHub 憑證整合的完成證據；檢視存檔路徑的 token 取得方式 | 憑證來自 #21 整合語意；全程無手動長期 token 混進 fork 或共用 host 帳密；未具備即 blocked 不開跑 | |
| C2 token 範圍 | 以拋棄式 token 嘗試存取範圍外 repo／權限（push 範圍外分支、讀私庫外 repo） | 越權操作一律被拒；token 僅涵蓋聲明的 repo／分支範圍；證據只含遮蔽後 token | |
| C3 開關預設 off 與說明 | 預設狀態觸發 Suspend；開啟開關時檢視說明文案 | 預設 off 時零 commit、零網路對外行為；開啟時說明確實標示目的 repo／branch 與可能包含的檔案，不得隱瞞推送範圍 | |
| C4 忽略規則 | 變更集含 `.gitignore` 命中檔案；存檔後比對遠端 tree | 被 ignore 的檔案不出現在 autosave commit；行為與 [files-policy](../contracts/files-policy.md) ignore list 一致 | |
| C5 機密排除 | 依 [byok-policy](../contracts/byok-policy.md) `backup_plan` 於工作區植入可辨識假秘密（`.netrc`、環境 dump、gh 設定等）後存檔；掃描遠端分支整棵 tree | 假秘密不出現在 autosave commit／遠端；掃描範圍與結果如實記錄；不得只靠「沒看到」宣稱安全 | |
| G1 HEAD/index 不變 | 存檔前後比對 `git rev-parse HEAD`、`git rev-parse HEAD^{tree}`、`git status --porcelain` 與 `git ls-files -s` hash | HEAD、index、工作區狀態完全一致；使用者未察覺任何本地 reflog 以外的變動；殘留暫存檔已清理 | |
| G2 有父鏈 commit | `git cat-file -p` 檢視遠端 autosave commit 的 parent 鏈 | 每次 autosave commit 的 parent 為前次 autosave（或首次的目前 HEAD），鏈不中斷、不從頭孤立建立 | |
| G3 force-with-lease 語意 | 模擬遠端已被他人更新（expected-old 過期）後 push；再以新 expected 值重試 | expected 不符時 push 被拒、遠端他人 commit 未被覆蓋；更新 expected 後可成功；全程遠端無 commit 遺失 | |
| G4 禁止無條件 --force | 掃描存檔路徑的命令、腳本與文件（含錯誤重試路徑） | 不存在任何無條件 `--force`／`+<ref>` 強制覆蓋路徑；發現即 fail，不得以「重試方便」理由保留 | |
| S1 限時失敗保留 local work | 存檔各環節注入逾時（push 斷網、逾時上限到期）後檢查工作區 | 本地工作完整保留（含未 commit 變更與已建 autosave commit 物件）；失敗明確記錄，不留半套狀態被當成功 | |
| S2 額度／緊急隔離停機不被 push 卡住 | 觸發 `runtime_deadline_at`（[watchdog-lease](../contracts/watchdog-lease.md) 語意）與隔離停機，同時存檔進行中 | 停機如期完成；push 被放棄或中斷且 local work 依 S1 保留；停機耗時不被 push 拖延（延遲量測入矩陣） | |
| S3 Suspend 不被 push 卡住 | 開啟自動存檔後觸發 auto Suspend（[auto-policy](../contracts/auto-policy.md) 語意），對照無存檔基線 | Suspend 逾期上限（執行時釘選）內完成或明確降級（放棄 push、保留 local work）；不得無限等待 push | |
| T1 連續兩次存檔 | 小變更集連續觸發兩次存檔，比對兩個遠端 commit | 兩次皆成功；第二次 parent 鏈含第一次（G2）；無重複物件暴增、無 conflict 半套狀態 | |
| T2 遠端並發更新 | 存檔同時以另一身分 push 同一 autosave 分支（模擬他人更新） | 依 G3 語意拒絕或明確合併策略處理；他人 commit 不被覆蓋；結果如實記錄，不得靜默丟失任一方變更 | |
| T3 無憑證 | 撤銷／不注入 token 後觸發存檔 | 存檔明確失敗或跳過；local work 保留；Suspend 照常完成；無錯誤的「已存檔」宣稱 | |
| T4 大變更 | 大變更集觸發存檔 | 完成或依限時明確降級（S1–S3 語意）；耗時與大小入矩陣；不因大變更卡死 Suspend 或暴衝空間 | |
| T5 hook 錯誤 | 於測試 repo 裝失敗的 pre-commit／pre-push hook 後觸發存檔 | hook 失敗時存檔明確失敗、local work 保留、遠端半套狀態不成立；錯誤輸出入證據；不得繞過 hook 偷推 | |

## 證據與發布

- 原始輸出（stdout／stderr、git 交談記錄、計時、tree／diff 證據、manifest、機密掃描結果）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r05 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；拋棄式 repo-scoped 測試 token 的建立與撤銷記錄入 manifest，任何真憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源（含測試 repo、autosave 分支與拋棄式暫存索引）；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：#19、#21、#22 任一未完成，或 repo-scoped GitHub 憑證整合未具備 → 本協定如實標 blocked；blocked ≠ no-go，等待時間不算 timebox；**不得以手動長期 token 混進 fork 變通解除**。
- **驗收 GO（關 #35 用）**：C1–C5、G1–G4、S1–S3、T1–T5 無未解決 fail，且量測矩陣各列已填實（含 Suspend 延遲與變更集大小）。
- **失敗處置**：任一 fail 未解決即不得宣稱自動存檔可用；維持不自動存檔（功能預設 off），不掩蓋。
- **不預設協定細節與門檻**：git 旗標語意、git host 行為與 token 類型以執行時官方文件核對記錄；本協定不預設耗時／大小門檻，也不以任何量測結果承諾特定存檔速度。
- **timebox 到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤 M1–M3**：既有功能不受本票影響；研究 no-go 時，#35 依完成證據欄以 not planned 關閉。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go 宣稱、#19／#21／#22 完成狀態預設（核對結果執行時記錄）。
- 默認打開：自動存檔永遠預設 off，僅使用者明確開啟才作用（#35 本票不含預設啟用）。
- 推送使用者未指名的第三方專案（僅開關聲明的 repo／branch）。
- 跨票整合與 runtime 實作（#35 本票不含）。
- 對 M1–M3 時程的影響宣稱。
