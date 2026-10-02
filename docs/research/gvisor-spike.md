# T01：Docker + runsc + Claude 最小驗證

Refs #8，依賴 [#7 契約 PR](https://github.com/our-sandbox-agent/sandbox-console/pull/47)。狀態：**無 key 實測已交付，E09 PID 阻塞；E05／E10 待憑證與預算，尚不可 go**。詳見 [2026-09-27 結果](gvisor-no-key-results-20260927.md)。這不是 #10 正式映像檔或 #11 Runner。研究現況總覽見 [gVisor 研究索引](README.md)。

下一輪操作入口：[測試環境交接、重跑流程與證據包](gvisor-environment.md)。使用 `scripts/gvisor-report.py` 產生十列 not_run 報告並檢查證據完整性；不能以工具或 CI 通過關閉 #8。

## 環境與限時

已在使用者授權的 Windows Hyper-V 專用 Linux VM 安裝 Docker／runsc，連線設定保留於本機私密交接。Claude 實測仍需專用 API key 的配置位置、模型與預算，不要在 issue/PR 貼 key。未確認的 Docker context、SSH 主機或雲端帳戶不能當成授權；本輪未呼叫付費模型。

實驗限 2–3 工程人天：第一天版本與基本工作負載，第二天 PTY／持久化／限制，第三天重現失敗及整理判斷。等待主機不算測試成功，也不把等待時間假裝成實作估時。

操作者先確認隔離的測試機及 headroom，指定測試 CPU/memory/PID/磁碟限額，再進行矩陣。版本記錄包含 kernel/architecture/cgroup、Docker/runsc、診斷 image digest、Claude/Node/git/tmux 版本與安裝來源。診斷 image 需包含 shell、git、Node/npm、Claude、tmux、python3；固定 digest 並人工核對內容，不使用浮動 latest；本機 build 未 push 的映像沒有 repo digest，改用 `docker image inspect --format {{.Id}}` 取得 image ID 來釘。Claude Code 的 native／npm 安裝會在背景自動更新，環境必須設 `DISABLE_AUTOUPDATER=1`，每列測試前後都記 `claude --version`，前後不一致該列重測。不自動下載或執行來路不明安裝腳本。

## 可先跑的唯讀檢查

```sh
# macOS 會回 blocked_environment / exit 2，不會聯絡 Docker。
python3 scripts/gvisor-preflight.py

# 在已授權 Linux 主機上；將 digest 換成已存在且核對的診斷映像。
python3 scripts/gvisor-preflight.py \
  --docker-socket unix:///var/run/docker.sock \
  --image 'YOUR_DIAGNOSTIC_IMAGE@sha256:YOUR_VERIFIED_DIGEST'
```

腳本只讀所指定的本機 Unix socket，不沿用 Docker context 或 DOCKER_HOST；不 pull/start/install/delete、不讀 API key。exit 0 也只是 `ready_for_manual_matrix_not_go`，沒有證明 Claude 或資源限制可用。需 Docker/runsc 註冊可見且 image 已在主機；探測失敗只輸出代碼，避免把原始設定或秘密帶入報告。

runsc 安裝／Docker daemon 修改需主機維護者安排；重啟 daemon 可能影響既有工作，不由 preflight 執行。官方依據：[gVisor Docker quick start](https://gvisor.dev/docs/user_guide/quick_start/docker/)；使用 `--runtime=runsc` 選 runtime。容器內 `dmesg` 可作輔助，不能當安全證明。

## 最小矩陣

每列記：固定環境、完整但不含秘密的命令、開始/結束時間、exit code、stdout/stderr、觀測數值與結果。`pass/fail/not-run` 三種值；本輪數值見獨立結果報告。下表定義驗收條件，測試值不是產品配額預設。

| ID | 操作／證據 | 通過條件 |
|---|---|---|
| E01 runtime | Docker inspect 只取 `.HostConfig.Runtime`；宿主核對該 instance 的 runsc 程序、runtime 配置及版本 | 明確為 runsc；不是只看 uname/dmesg 字串 |
| E02 shell | 非 root 容器跑 `id; printf 'SHELL_OK\n'`，禁止 privileged、host mount、Docker socket | exit 0，非 UID 0，輸出 marker |
| E03 repo | 公開小型 fixture repo 固定 commit，`git clone` 後 `git rev-parse HEAD`，工作檔 hash | 正確 commit，TLS/DNS 正常；不帶私人 repo/token |
| E04 package | fixture 含固定 package-lock，跑 `npm ci --ignore-scripts`；再跑已檢視 fixture 的測試 | install 與測試 exit 0；記 network error 和耗時，不把失敗省略 |
| E05 Claude | tmpfs 短期注入專用 key，固定版本跑下方 marker 任務，保存輸出與 marker hash | Claude 真的寫出 marker 且 exit 0；不是人工預建 marker。結束後 secret 清除，log 無 key |
| E06 PTY | 實際互動 `tmux new -s spike`，啟動有進度的短工作；resize、Ctrl-C、detach、客戶端斷線再 attach | stty 尺寸匹配；signal 正確；未停止 runtime 時 PID/進度保留，無黑屏或混流；另驗 google/gvisor#14761：依 [完整 PTY 重現命令](gvisor-environment.md#pty-重現必須選對-runtime) 明確指定 `--runtime=runsc` 與 `--user 1000:1000` 開啟 `/dev/ptmx`；失敗記版本及換版／等修正方案，不以 root 跑 |
| E07 CPU | 專用 disposable instance 設 0.5 CPU，容器內單 worker 忙迴圈 20 秒；宿主讀實際 cgroup cpu.stat 差值並與 wall time 比較 | 實測使用量／節流符合設定及事先記錄容差；不能只看 inspect。過大環境抖動標重測 |
| E08 memory | 先確認限額生效，再在 disposable instance 逐批配置超過限額的記憶體；記宿主 memory.current/events 與容器結束原因 | 分配失敗／OOM 限於該容器，host 可用量保留；不關 OOM killer、不無限配置。**通過不含 session 存活**：本列不檢查既有 shell／工作是否續活，該問題由 #67 另行驗收，不要把本列的 pass 當成「撞到記憶體上限仍可用」 |
| E09 PID | 設小額 PID cap；受 timeout 控制逐一建立 child，最多嘗試 cap+16；完整 wait/cleanup，宿主讀 pids.current/events | 達限制後拒絕新程序，host 無洩漏；gVisor guest PID 與宿主 task 若不同須揭露，不能將宿主 cap 當 guest cap |
| E10 cold | workspace/home 各寫隨機 marker 和 hash，記錄原 session；stop 後刪測試容器、用相同 volumes 建新實體再查 | 檔案 hash 和明確 Claude session 可恢復；新程序沒有 RAM/原 tmux，無 key 時不啟動 Agent，重新注入才繼續 |

Claude marker 命令須在只有測試資料、已注入 key 的 sandbox 內執行；不加 bypass permissions。已核對的 CLI 版本若選項不同，留失敗證據後調整：

```sh
claude --version
claude -p 'Create /workspace/marker.txt with exactly GV_MARKER_OK followed by a newline.' \
  --allowedTools 'Write' --output-format json
test "$(cat /workspace/marker.txt)" = GV_MARKER_OK
```

API key 不出現在命令參數、shell trace、`docker inspect` 整包輸出或報告；模型需費用，先核對專用 workspace 花費額度。不要用 `docker run -e KEY=...` 留在容器 metadata；注入方案由操作者確認，以唯讀 tmpfs 檔案／短期入口提供程序，完成即撤除。官方 [Claude 非互動執行](https://code.claude.com/docs/en/headless) 說明 `-p` 與 allowedTools；此處仍需真實執行驗證。

資源限制使用 Docker 的 `--cpus`、`--memory`、`--memory-swap`、`--pids-limit`，但 runsc 相容性必須實測。參考 [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)；不要沿用無上限預設，記憶體測試先確認宿主 headroom。`memory-swap` 與 memory 相等用來禁止容器 swap，主機 tmpfs 的秘密仍需另外確認 swap/core dump 政策。

每次建立資源使用不可重複 run ID 與 `sandbox.spike=<run-id>` label，將回傳 ID 記在本次 manifest。清理只允許 manifest 的確切 ID 且 label 匹配；先停止容器、移除容器後再刪本次 volumes。禁止 `docker system prune`、通用名字清理、清除未知 volume。中斷時留下 manifest，下一次先核對殘留。

## 判斷與報告

複製以下表格到 issue/PR；原始證據儲存在經遮蔽、權限受控的位置。沒有憑證／主機用 blocked，不等於 runtime 技術 no-go。

| 欄位 | 目前結果 |
|---|---|
| 版本、主機架構、資源/headroom、image digest | 見 2026-09-27 無 key 實測報告 |
| E01–E10 結果與證據 | E01–E04／E06–E08 pass；E09 fail；E05／E10 not_run |
| 模型花費及花費上限配置 | 未使用 |
| 殘留測試資源及清理證據 | 本輪容器依 manifest 清除；保留診斷 image 和證據 |
| 結論 | E09 PID 限制待解；E05／E10 待憑證／預算；不得解除 #10/#11 |
| 審查者、日期、後續範圍 | 待實測 |

**go：** 全部必要列 pass，失敗重測有證據、無未解決隔離／限制問題，可進 #10/#11；仍不是完整受限試用放行。**no-go：** 必要工作負載或限制可重現失敗，列明縮小方案及風險，重新對齊後才換 runtime。**blocked：** 必要條件或證據缺失，保持等待。

runc 可經授權用作同負載診斷對照，但通過 runc 不能取代 runsc。暖 checkpoint/Firecracker 不在本矩陣，也不能用它們的結果替代 cold 語意。
