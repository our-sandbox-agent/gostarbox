# 工作映像契約（Work Image）

Proposed，2026-10-03。Refs #10；#8 為 blocker。本文件只交付規格，映像未在此環境建置；建置與 runtime 驗收待 #8 GO 與實際建置主機。固定版本模式沿用 [diagnostics/gvisor/Dockerfile](../../diagnostics/gvisor/Dockerfile) 已驗證的做法。

## 1. 固定版本政策

- 基底映像以 digest 固定：`node:22-bookworm-slim@sha256:43ac6c60…30b772c`，與診斷映像同一 digest，不另立來源。
- Debian 套件經 snapshot.debian.org 快照 pin 全部依賴（不只 top-level 套件）：git、tmux、ca-certificates、procps。
- 建置時以 `dpkg-query -W` 把全部套件版本寫進映像檔（`/usr/local/share/work-image-dpkg.txt`）；Claude 版本由 package-lock.json 固定（`npm ci --ignore-scripts --omit=dev`）。build 記錄所有版本，相同輸入可重建相同依賴。
- 映像升級即換 digest／快照日期，且需重驗受影響矩陣；#61 通過不等於本票關閉。

## 2. 非 root 與首次開機

- 全程 `USER node`，無 sudo／fixuid。
- 首次開機初始化 tmux（固定 session 名稱）與 `/workspace`（node 擁有）。
- 為 IDE 模式（#52）預留非 root home 目錄（user-data／extensions 位置先建目錄），本票不裝 code-server。

### 冪等契約

首次啟動、再次啟動與冷恢復三種路徑行為一致，均不得重複 clone、不得覆寫已有 repo：

| workspace 目標目錄狀態 | 行為 |
|---|---|
| 非空 | 跳過 clone，記錄決策；絕不破壞既有內容 |
| 空 且 REPO_URL 已設 | `git clone -- "$URL" "$DEST"`（argv 傳參） |
| 空 且未設 REPO_URL | 裸啟動，不視為錯誤 |
| clone 失敗 | 非零退出，不留半套狀態讓下次誤判 |

「跳過」必須留下可稽核記錄；任何路徑不得具破壞性。

## 3. argv 安全 clone 契約

公開 HTTPS repo 一律 argv 傳參：`git clone -- <url> <dest>`；不得拼 shell 字串、不得經 tmux send-keys。`--` 之後全部是 positional，選項注入在結構上不可能。

驗證層為 [scripts/clone_args.py](../../scripts/clone_args.py)（純標準函式庫），負責 scheme／host 合理性與 dest 安全：

- 僅允許 `https`；拒絕 `ssh`、`git@`（scp 形式）、`file`、`data`、`http`，以及無 host、含 userinfo、含空白／控制字元的 URL。公開 repo 不需要 host allowlist。
- dest 不得為空、不得以 `-` 開頭。
- `should_skip_clone` 依目錄 listing 做純決策：非空即跳過。

惡意 URL、選項注入與 clone 失敗案例由 [scripts/test_clone_args.py](../../scripts/test_clone_args.py) 覆蓋（每個 guard 都有「移除即失敗」的測試）。未來 Go Runner 移植同一契約，不自創第二套規則；entrypoint（[image/entrypoint.sh](../../image/entrypoint.sh)）是此契約的 shell 骨架，runtime 行為待驗。

## 4. BYOK 邊界

- 內部 BYOK key 不進映像檔、build log 或提交。
- Suspend→Active 的重注入／清除依生命週期契約的 `credentials_required` 語意；完整對外隔離屬 #21 範圍，本票不處理。

## 5. Codex 與不支援項

依 2026-09-22 創辦人決定（#10 補充）：「映像檔可一併預裝 `codex` 二進位（成本低），但憑證契約（Suspend→Active `credentials_required`、注入與不落 log）與驗收只做 Claude；對受邀者揭露 Codex『已安裝、未支援』」。Codex 即使預裝仍為未支援。本票另不含私有 repo 與 OAuth 訂閱憑證。

## 6. 狀態與未驗證項

規格已交付；映像未於本環境建置。`image/` 下為參考實作（header 已標注）。建置、重複 clone 測試、冷恢復等 runtime 驗收待 #8 GO 與實際建置主機；產品映像固定版本後須重驗受影響矩陣。
