# R04：500 MB 續傳與資料夾可靠匯入協定

Refs #34（里程碑 R，#6；前置 #20、#23）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票。Timebox 5–8 工程人天（1 人；#34 全票估時，含本票測試／說明，不含跨票整合與外部等待；大於 5 天的工作包須先拆 sub-issues 才可開工；到期必交證據與 go/no-go，不無限延長）。實測結果將另立結果報告，不回填本文件。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。#20 的規則測試（files-policy）與既有固定上限一次性上傳行為不因本票重跑；上傳驗證、空間預留與 tar 規則語意沿用 [files-policy](../contracts/files-policy.md) 的定義（`verify_upload`、`UploadQuotaGate`、`TarPolicy`），本文件只增補續傳與匯入可靠性特有欄位。

## 目標

回答一個問題：**500 MB 大檔能否在斷網、瀏覽器重整、CLI Ctrl-C 後可靠續接（hash 一致才算完成），且資料夾匯入在 post-finish 重送、中斷與 TTL 清理下仍可重跑**——且授權與空間不留後門。#34 五個驗收欄對應的協定段落如下：

| #34 驗收欄 | 對應協定段落 |
|---|---|
| 拆 server tus/import 與 Web/CLI resume 子票；授權涵蓋 tus POST/HEAD/PATCH/DELETE，每次綁 workspace/upload id | 執行前清單、相容性檢查表 A1–A3 |
| 500 MB 斷網、重整、CLI Ctrl-C 後重傳可續接；hash 一致才完成 | 相容性檢查表 U1–U4、量測矩陣 |
| 先預留暫存＋最終檔案空間；tar 展開有總大小／數量上限，並发不超額 | 相容性檢查表 S1–S3 |
| post-finish 重送、匯入中斷和 TTL 清理可重跑；100% 傳完不等於匯入完成 | 相容性檢查表 P1–P4 |
| 代理實際是 Caddy，參數按 Caddy 配置，不直接抄 nginx 的 proxy_request_buffering | 相容性檢查表 C1、執行前清單版本釘選 |

官方基準文件：tus 協定（tus.io 官方 protocol 文件，與執行時採用 server／client 的版本及 extensions）與 Caddy 官方文件（反向代理對 request body 串流、buffer 與 timeout 的實際行為）。查證日期：**執行時記錄**——協定版本、extensions、參數與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節，也不以 nginx 參數（如 `proxy_request_buffering`）類比代替 Caddy 實測。

## 執行前清單（全部完成才開跑）

- [ ] 已拆 sub-issues：**server tus/import** 與 **Web/CLI resume**，各 **≤5 工程人天**，授權設計涵蓋 tus POST/HEAD/PATCH/DELETE 且每次請求綁 workspace／upload id；連結記入 manifest；未拆完不開工（#34 第一個驗收欄；全票 5–8 天大於 5 天，不可直接當單一 PR 開工）。
- [ ] 前置票 #20、#23 的狀態與完成證據逐一核對並記錄；未完成者如實標 blocked，不假設已完成。
- [ ] 契約面：[files-policy](../contracts/files-policy.md) 現行明文「不做續傳」（fixed-cap one-shot）且 tar 上限 512 MiB／10,000 entries；500 MB 續傳須先依契約變更流程修訂上限與續傳語意，同 PR 更新受影響的 [control-plane-api](../contracts/control-plane-api.md)（#76 schema）與 [cli-surface](../contracts/cli-surface.md)（resume 不得是 mocked success），不得以 runtime 私改繞過契約。
- [ ] 授權語意沿用 [tenant-authz](../contracts/tenant-authz.md) `file` endpoint class 與 files-policy 的 workspace 綁定：跨 workspace／未知 id 一律 404，不給跨租戶探測訊號。
- [ ] 版本釘選——執行時記錄**實際值**，不假設：tus server 套件與協定版本（含採用 extensions）、Web／CLI client 版本、Caddy（`caddy version`）與實際配置檔、後端 runtime／kernel（`uname -a`）。
- [ ] 500 MB fixture 產生器與 sha256、中斷點（如 25%／50%／75%）、三式中斷手法（斷網、瀏覽器重整、CLI Ctrl-C）於執行時固定並記錄，逐一可重現。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r04=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源，禁用 `docker system prune` 與通用名字清理。
- [ ] 拋棄式測試 token（上傳授權用）的建立與撤銷計畫先寫入 manifest；真憑證不進任何命令列、log 或報告。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 工作負載：固定 fixture 產生器建立 **500 MB** 單檔，與代表性地料夾 tar（小檔多量／大檔少量變體）；fixture 與 sha256 執行時固定並記錄。
- **端到端耗時（秒）**：上傳建立（POST）→ 伺服器確認 hash 一致的完成點，`date +%s.%N` 差值；含中斷情境者另記**淨傳時間**（扣除中斷等待）。
- **續接耗時（秒）**：重傳指令送出 → 自 HEAD 回報的 Upload-Offset 起繼續送出 bytes。
- **重傳 bytes**：中斷後重送的 payload 字節數（重送總量 − 續接 offset），探針為 client 計量與 server 端 `stat`；理想值 0（不从头重傳）。
- **暫存空間（MiB）**：`du -sh` 暫存與最終檔案位置，並記錄主機可用磁碟。
- HTTP 探針：`curl -sS -o /dev/null -w '%{http_code}'`（POST／HEAD／PATCH／DELETE 各步的狀態碼入證據）。

命令模板（實際端點、header 與參數以執行時採用的 tus 實作官方文件核對後逐字記錄）：

```sh
# tus 流程模板（curl 示意；實際 client 為 Web／CLI 實作）
curl -X POST   <server>/<files>              -H 'Tus-Resumable: <ver>' ...  # 建立（綁 workspace/upload id）
curl -I        <server>/<files>/<upload-id>                                   # HEAD：Upload-Offset／Metadata
curl -X PATCH  <server>/<files>/<upload-id>  -H 'Upload-Offset: <n>' ...     # 自 offset 續接
curl -X DELETE <server>/<files>/<upload-id>                                   # termination（授權必驗）
```

### 續接情境量測表

單位：期間一律**秒**、大小一律 **MiB**、bytes 為 payload 實計；重跑次數與分布（min／median／max）執行時記錄，未跑的列填 `not_run` 並附原因。

| 情境 | 中斷點 | 樣本數 | 續接結果 | 重傳 bytes | 續接秒 | 端到端秒 | 暫存 MiB | exit code |
|---|---|---|---|---|---|---|---|---|
| 無中斷（基線） | — | | | | | | | |
| 斷網 | 25% | | | | | | | |
| 斷網 | 50% | | | | | | | |
| 斷網 | 75% | | | | | | | |
| 瀏覽器重整 | 50% | | | | | | | |
| CLI Ctrl-C | 50% | | | | | | | |

## 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| A1 tus 方法授權 | 以拋棄式 token 對 POST／HEAD／PATCH／DELETE（含未列方法）逐一探測 | 每個方法都驗 workspace 綁定與 token 範圍；跨 workspace／未知 upload id 一律 404（[tenant-authz](../contracts/tenant-authz.md) 語意），無 401／403 差異洩漏探測訊號；未授權 PATCH 寫不進任何 byte | |
| A2 upload id 跨租戶 | workspace A 建立上傳後，以 workspace B 的有效 token 存取同一 upload id（HEAD 與 PATCH） | 一律 404／拒絕；offset、metadata 與錯誤訊息不洩漏 B 不該知道的存在性 | |
| A3 Suspend 唯讀 | 於 cold Suspend 狀態嘗試 POST／PATCH 續傳，並嘗試讀取／下載（[files-policy](../contracts/files-policy.md) `assert_mutable` 語意） | 寫入類被拒（`suspend_read_only`），讀取與下載仍可用；寫入未被拒即 fail | |
| U1 斷網續接 | 500 MB 傳至中斷點切斷網路，恢復後重傳 | HEAD 回報 offset 等於伺服器實收 bytes；只補剩餘、不从头；完成前後 sha256 一致才算成功 | |
| U2 瀏覽器重整 | 傳至中斷點重新整理頁面，再進入同 workspace 上傳 | 同 U1；重整不產生孤兒暫存（或依明確 TTL 語意記錄去處） | |
| U3 CLI Ctrl-C 續接 | 傳至中斷點送 SIGINT，重跑相同 CLI 指令 | 同 U1；Ctrl-C 不留半寫檔被當成功，暫存可被後續重跑接管或依 TTL 清理 | |
| U4 hash 一致才完成 | 正常完成與故意送損毀 bytes 兩式對照 | hash 一致才標完成；不一致明確 fail 且不覆寫既有成品（`verify_upload` 語意） | |
| S1 空間預留 | 傳輸前驗暫存＋最終檔案**雙份**空間預留；先填滿磁碟再觸發上傳 | 開始前即拒絕（空間不足），不產生部分寫入；預留走 `UploadQuotaGate` 原子語意，失敗／到期即釋放；exit code 與輸出入證據 | |
| S2 tar 展開上限 | 構造超過總大小與條數上限的 tar（對照契約修訂後上限；未修訂前為 512 MiB／10,000） | 超限即 abort、不留部分檔；計數只在 accept 前進（`TarPolicy` 語意） | |
| S3 並发不超額 | 多個上傳同時預留至 volume 上限 | 聯合預留 ≤ volume cap（`UploadQuotaGate` 語意）；超額者明確拒絕，不默默放行 | |
| P1 post-finish 重送 | 上傳完成事件重送 N 次 | 冪等：不重複匯入、不重複計量；重送本身可重跑 | |
| P2 匯入中斷重跑 | 匯入（tar 展開）中途 kill，再觸發同一次匯入 | 可重跑至完成；無半套狀態被當成功；重跑證據入 manifest | |
| P3 TTL 清理 | 對暫存設短 TTL 到期，再對同 upload id 操作；重跑清理 | 清理冪等可重跑；清理後重傳行為明確（從頭或明確拒絕）並如實記錄 | |
| P4 100% ≠ 匯入完成 | 傳完 100% 但匯入未完成／失敗時查狀態 | 上傳完成與匯入完成為可區分狀態；不得把 100% 顯示為匯入成功 | |
| C1 Caddy 代理實測 | 500 MB 經實際 Caddy 配置傳輸，核對 request body 串流、buffer 與 timeout 行為（含中斷續接經代理） | 參數按 Caddy 官方文件以實際配置核對記錄，不以 nginx `proxy_request_buffering` 類比充數；全程無代理造成的 OOM／逾時／buffering 中斷；行為如實記錄 | |

## 證據與發布

- 原始輸出（stdout／stderr、HTTP 交談記錄、計時、`du`、manifest）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r04 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；拋棄式測試 token 的建立與撤銷記錄入 manifest，任何真憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源（含暫存檔與過期 upload）；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **前置鐵門**：#20、#23 任一未完成 → 本協定對應範圍如實標 blocked；blocked ≠ no-go，等待時間不算 timebox。
- **開工授權**：sub-issues（server tus/import、Web/CLI resume，各 ≤5 天）拆分完成＋執行前清單全數完成，才授權各子票依自身驗收開工；授權只代表開工，本文件與結果報告都不是實作。
- **驗收 GO（關 #34 用）**：A1–A3、U1–U4、S1–S3、P1–P4、C1 無未解決 fail，且量測矩陣各列已填實（含重傳 bytes 與暫存空間）。
- **失敗處置**：任一 fail 未解決即不得宣稱續傳可用；維持既有固定上限一次性上傳（files-policy 現行語意），不掩蓋。
- **不預設協定細節與門檻**：tus 版本、extensions 與 Caddy 參數以執行時官方文件核對記錄；本協定不預設吞吐或秒數門檻，也不以任何量測結果承諾特定傳輸速率。
- **timebox 到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- **失敗不延誤 M1–M3**：既有功能不受本票影響；研究 no-go 時，#34 依完成證據欄以 not planned 關閉。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go 宣稱、#20／#23 完成狀態預設（核對結果執行時記錄）。
- 檔案版本歷史／線上編輯器（#34 本票不含）。
- 對 M1–M3 時程的影響宣稱。
