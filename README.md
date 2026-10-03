# NexSpace 星際工作站 — 互動雛形

AI Agent 沙盒產品的第一版可操作介面。這是本機模擬，沒有真正的 VM、Agent API、shell 執行或收費。

本輪採用暫定品牌「星際工作站 NexSpace」，以軌道與 N 字母組成簡單字標；字標尚未核准，repo 名稱 `gostarbox` 只是儲存庫名稱，不代表品牌定案。產品願景是「給 AI Agent 一個獨立工作空間。關上筆電，任務繼續。」此願景尚未由雛形實作：目前關閉頁面後，模擬計時會暫停。

品牌預覽不變更 `sandbox` 模擬指令、瀏覽器儲存 key 或 GitHub Pages 路徑。網域 nexspace.fyi 已由創辦人購得，僅規劃供開發與測試用途；DNS 尚未設定，本專案未啟用任何正式網域服務。

可用的連結：[桌面與手機品牌預覽](docs/brand/README.md)（預覽圖與說明）、線上展示 https://our-sandbox-agent.github.io/gostarbox/ （GitHub Pages，PR #81 修復 base 路徑後的位址）。

## 啟動

Node.js 20.19+ 或 22.12+。

```sh
npm ci
npm run dev -- --port 4173
```

開啟 http://localhost:4173/gostarbox/ （`npm run preview` 同路徑：http://127.0.0.1:4173/gostarbox/ ）。`npm run build` 產生靜態頁面於 `dist/`。

`npm test` 執行狀態計時與檔案測試：重新整理接續、關頁不計時、手動 Idle 倒數、延遲回呼跨越多個狀態、切換前的時間結算，以及檔案隔離、刪除與上傳的交易回滾與重試、重複提交、Suspend 保護、覆寫前的衝突判定與重新確認。

倒數和累計秒數會保存；重新整理接續剩餘時間，關頁期間暫停。手動 Set idle 後，從當下開始完整的 Idle → Suspend 倒數。頁面仍開著但背景計時器延遲時，恢復後會把時間分配到各狀態。正常離頁會立即保存；瀏覽器異常終止時，最多可能遺失最後一次定期保存後約 5 秒的前景進度（背景計時器遭節流時可能更久）。

## 本地開發

Demo（模擬模式）同[啟動](#啟動)：`npm run dev -- --port 4173`，開 http://localhost:4173/gostarbox/ 。

控制平面 server（真實 API 模式的後端，`server/`）：

```sh
cd server
npm ci
SANDBOX_TOKEN=<自選隨機通行碼> npm start   # http://127.0.0.1:8787
```

`SANDBOX_TOKEN` 是所有端點共用的唯一 Bearer token，僅供本機開發，不會寫進 server 日誌，也不要提交進 repo。server 只繫結 loopback（127.0.0.1），對外需透過 SSH tunnel，契約見 [docs/contracts/control-plane-api.md](docs/contracts/control-plane-api.md)。

測試（三層都在本機可跑）：

```sh
npm test && npm run build                          # demo 狀態計時與檔案測試、建置
python3 -m unittest discover -s scripts -p 'test_*.py'   # Python 契約套件（可執行規格）
cd server && npm run typecheck && npm test        # server 型別檢查與 node:test 契約測試
bash scripts/ci-server-smoke.sh                   # 起 server 跑 create→list→destroy HTTP 契約煙霧測試
```

真實模式邊界：瀏覽器 UI 尚未接上真實 API（#15 未完成）；前端 client 已依 [docs/contracts/console-datasource.md](docs/contracts/console-datasource.md) 實作。`scripts/ci-server-smoke.sh` 只驗證 HTTP 契約層；demo 頁面的實際渲染由 `scripts/ci-smoke.sh` 以模擬模式驗證。

## 可體驗流程

- Claude / Codex / Harness 快速啟動、新增自訂名稱與規格的沙盒。
- 搜尋及依 Active / Idle / Suspend 篩選。
- Active → Idle 或 Suspend；Idle / Suspend → Active。
- 模擬 terminal：help、pwd、ls、status、clear、sandbox claude / codex / harness。其他輸入不執行。
- 個別沙盒的檔案及資料夾上傳、相對路徑保留、原始內容下載。每檔上限 10 MB。
- 上傳遇到相同完整路徑時先列出衝突，可略過衝突、覆寫或取消整批；預設不覆寫，確認期間目標改變會要求重新確認，失敗整批回滾。
- Files 與詳情 Files 分頁可單檔刪除：確認完整路徑後永久刪除，取消不更動檔案或活動時間；沒有垃圾桶。Suspend 期間不能刪除，交易失敗可重試。
- 三段示意費率與按狀態累計的 session 費用，4 vCPU 為 2 vCPU 示意費率的兩倍。
- 自動降級：無活動 N 分鐘 Active → Idle，再 M 分鐘 → Suspend，門檻可在 Usage 頁調整，詳情頁顯示倒數。
- Snapshot / Fork：對沙盒建立快照，從快照分支出新沙盒（含檔案）。
- 從 git repo URL 建立沙盒（模擬 clone）。
- 終端新增 sandbox ls / connect <id> / suspend / snapshot、git status。
- Usage 頁與 E2B、Daytona、Modal、Fly Sprites、Vercel 的同小時工作成本比較。
- Blog：以 MDX 撰寫的文章列表與內文，支援 `#/blog/<slug>` 深度連結，重新整理後接續。
- 手機與桌面布局。

## 部落格

文章放在 `content/blog/*.mdx`，frontmatter 需含 `title`、`date`、`description`、`tags`、`lang`。新增文章即加檔案，列表與路由自動產生，不需改 HTML。MDX 於建置時編譯（`@mdx-js/rollup` + Preact），編譯錯誤會使 `npm run build` 與 CI 失敗；正文中 `<` 與 `{` 是 JSX 語法，需以字元實體或反引號包裹。

## 研究與建議

競品比較、痛點與優化建議見 [docs/research.md](docs/research.md)。

受限試用實作計劃：短版 [docs/plan.md](docs/plan.md)，詳細版 [docs/plan-detail.md](docs/plan-detail.md)。

語言決策：[Runner 語言 ADR（Accepted：Runner 與 CLI 用 Go）](docs/adr/runner-language.md)。

契約提案：[生命週期、API 與資料保存](docs/adr/sandbox-lifecycle.md)。

## 資料與限制

沙盒與紀錄儲存在 localStorage，檔案儲存在 IndexedDB。同一 origin／瀏覽器重整後保留；清除網站資料會移除。沒有跨裝置同步。避免上傳敏感檔案。計時只累計頁面開啟期間；多分頁同步、離線計費與後端權威時鐘尚未實作。初始三個沙盒與既有時數是展示資料，費率不是商業報價。資料夾請用 Upload folder 按鈕選取；拖放區支援一般檔案。不同路徑同名檔案可共存，相同完整路徑再次上傳會先確認，可覆寫或略過衝突。

此版本不驗證 gVisor / Firecracker 的隔離安全性或 suspend 能力。介面中的 Resume 只切換模擬狀態。

## 後續實作邊界

1. 控制平面 API：登入、工作區、沙盒生命週期、租戶隔離、操作授權。
2. Runner 介面：create / exec / suspend / resume / destroy，將 gVisor 與 Firecracker 差異封裝於 adapter。須先驗證主機虛擬化支援與安全模型。
3. Agent 整合：CLI、串流終端、憑證注入、工作退出與重連。
4. 檔案傳輸：物件儲存、目錄 manifest、續傳、權限與路徑驗證。
5. 計量：伺服器端 lifecycle events、單一時鐘、失敗恢復及可核對帳務。

產品原型刻意先驗證「啟動 → 工作 → 暫停 → 接續」流程，尚未承諾底層基礎設施的可行性或效能。

## 開發規範

見 [CONTRIBUTING.md](CONTRIBUTING.md)：分支與 PR、測試、證據與秘密、內容即程式碼、重構與死碼、決策紀錄。每條規則都註明是哪次經驗讓它存在。

送 PR 前最低限度：以 `main` 為基底、一個 PR 一件事、純邏輯有測試、把關條件有會失敗的測試、證據不含憑證、說明寫清楚驗證了什麼與沒驗到什麼。
