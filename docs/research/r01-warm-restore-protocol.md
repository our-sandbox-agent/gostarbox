# R01：Docker checkpoint 與 raw runsc 暖恢復限時比較協定

Refs #31（里程碑 R，#6；前置 #8、#12、#13）。狀態：**規劃中／未執行**——本文件只定義協定與決策規則，**尚無任何量測**，測試環境未佈建，不是 go，也不解除任何等待中的票。Timebox 2–3 工程人天（1 人；到期必交證據與 go/no-go，不無限延長）。實測結果將另立結果報告，不回填本文件。

環境交接、label／manifest 清理與證據包流程沿用 [gvisor-environment.md](gvisor-environment.md)；證據紀律依 [CONTRIBUTING](../../CONTRIBUTING.md)。#8 的 E01–E10 矩陣（[gvisor-spike.md](gvisor-spike.md)）不因本票重跑。

## 目標

回答一個問題：**runtime adapter 是否需要改用 raw runsc**，還是官方 Docker checkpoint 路徑即可。依 #31 驗收順序：

1. 先驗官方 Docker `checkpoint`／`start --checkpoint` 路徑；
2. 再以 raw runsc `checkpoint`／`restore` 同負載對照；
3. 記錄 PTY、網路、bind mounts 的實測相容性。

官方基準文件：[gVisor checkpoint/restore](https://gvisor.dev/docs/user_guide/checkpoint_restore/)。查證日期：**執行時記錄**——旗標與限制以執行當下的官方文件與版本為準，本文件不預先抄錄可能過期的細節，也不先假設「必須 raw runsc」。

## 執行前清單（全部完成才開跑）

- [ ] 已授權 Linux 測試主機，隔離與 headroom 先確認（同[交接表](gvisor-environment.md#先交接什麼)）；**冷主機 cache 測試僅限專用拋棄式測試 host**，共享主機不得執行。
- [ ] 版本釘選——執行時記錄**實際值**，不假設、不沿用 #8 舊值：Docker Engine（`docker version`）、runsc release（runsc `--version` 與 binary checksum）、kernel／arch／cgroup（`uname -a`）、診斷 image digest／ID。
- [ ] Docker checkpoint 功能可用性先驗：`docker checkpoint --help` 的 exit code 與 daemon 實際配置，如實記錄；不可用時該路徑標 blocked，不得以替代路徑冒充官方路徑通過。
- [ ] 每個資源以不可重複 run ID 建立，label `sandbox.r01=<run-id>`，ID 記入 manifest；清理只刪 manifest 內、label 匹配的自建資源，禁用 `docker system prune` 與通用名字清理。
- [ ] timebox 起算時間寫入 manifest；到期即停、交證據。

## 量測矩陣（執行時填；本文件保持空表）

### 工作負載與計時定義

- 工作負載：診斷 image 內以固定 fixture 配置並觸碰 1／2／4 GB 匿名記憶體後閒置，持續 heartbeat 輸出；fixture 與 hash 於執行時固定並記錄。
- **checkpoint 期間（秒）**：checkpoint 指令送出 → 指令 exit，`date +%s.%N` 差值。
- **restore 期間（秒）**：restore 指令送出 → 指令 exit 0。
- **terminal-ready（秒）**：restore exit → PTY 可用，依 [#13 終端協定契約](../contracts/terminal-protocol.md)（Proposed）語意：attach 後取得 shell 回應且 `stty size` 有輸出。契約變更時此定義跟著改。
- **首個任務回應（秒）**：恢復後的 session 內送出固定命令 `python3 -c 'print("TASK_OK")'` → 收到輸出。

命令模板（實際旗標以執行時官方文件核對後逐字記錄）：

```sh
# 路徑 A：官方 Docker checkpoint
docker checkpoint create --checkpoint-dir=<dir> <container> r01-<run-id>
docker start --checkpoint r01-<run-id> --checkpoint-dir=<dir> <container>

# 路徑 B：raw runsc（--root 以 daemon 實際 runtime 配置為準）
runsc --root /var/run/docker/runtime-runc/moby checkpoint --image-path=<dir> <container-id>
runsc --root /var/run/docker/runtime-runc/moby restore --image-path=<dir> <container-id>
```

### 效能量測表

單位：期間與回應一律**秒**；重跑次數與分布（min／median／max）執行時記錄，未跑的列填 `not_run` 並附原因。

| 負載 | 路徑 | compression | checkpoint 秒 | restore 秒 | terminal-ready 秒 | 首任務回應秒 | exit code | 次數 |
|---|---|---|---|---|---|---|---|---|
| 1 GB | docker | none | | | | | | |
| 1 GB | docker | flate-best-speed | | | | | | |
| 1 GB | runsc | none | | | | | | |
| 1 GB | runsc | flate-best-speed | | | | | | |
| 2 GB | docker | none | | | | | | |
| 2 GB | docker | flate-best-speed | | | | | | |
| 2 GB | runsc | none | | | | | | |
| 2 GB | runsc | flate-best-speed | | | | | | |
| 4 GB | docker | none | | | | | | |
| 4 GB | docker | flate-best-speed | | | | | | |
| 4 GB | runsc | none | | | | | | |
| 4 GB | runsc | flate-best-speed | | | | | | |
| 依上表最佳列 | 視版本 | 官方背景還原優化 | | | | | | |

`none` 與 `flate-best-speed` 的實際旗標名於執行時依該版 runsc／Docker 核對記錄。**背景（background／lazy）restore 等官方優化：先查執行版本是否提供，有則加列量測，不先寫死最慢路徑；無則記 `not_applicable` 並附版本證據。**

### 冷 cache 表（僅專用測試 host）

冷 cache 作法（僅專用 host、經維護者同意）：`sync; echo 3 > /proc/sys/vm/drop_caches`（需 root；執行時間與 exit code 入證據）。列由執行者依 timebox 選代表性負載填入。

| 負載 | 路徑 | cache | restore 秒 | terminal-ready 秒 | drop_caches 證據 |
|---|---|---|---|---|---|
| | | 冷 | | | |
| | | 冷 | | | |

### 相容性檢查表

每項填 pass／fail／not_run 與證據檔；「如實記錄」欄位的行為本身不是 fail，掩蓋才是。

| ID | 檢查 | 操作／探針 | 通過條件 | 結果 |
|---|---|---|---|---|
| C1 PTY／tmux reattach | checkpoint 前 `tmux new -s r01` 跑 heartbeat；restore 後 `tmux attach`，驗 `stty size` 與 Ctrl-C | attach 可見原 session 與畫面；signal 正確；無黑屏混流 | |
| C2 網路 | 容器內建立長連 TCP（如 `nc`）；restore 後觀察連線與重連 | tcp 在 restore 後的斷／留行為如實記錄；斷線後可重新建立連線 | |
| C3 bind mounts | 掛 host 測試目錄，checkpoint 前寫 marker 與 hash | restore 後 marker 可讀、hash 一致；容器內寫入反映到 host | |
| C4 opened files | checkpoint 前開檔持有 fd，restore 後讀寫該 fd | fd 行為如實記錄；資料毀損即 fail | |
| C5 磁碟變動 | restore 後容器內寫新 marker | host／volume 可見新 marker 且 hash 一致 | |
| C6 失敗清理 | 故意觸發失敗 checkpoint（如 checkpoint 目錄空間不足） | 失敗後容器可繼續或可明確復原；無殘留壞狀態；證據含失敗輸出 | |
| C7 snapshot 大小／容量 | 每次成功 checkpoint 後 `du -sh` 目錄、記錄可用磁碟 | 記錄實際 MiB 與主機容量占用；異常成長標註並複測 | |

## 證據與發布

- 原始輸出（stdout／stderr、runsc log、計時、`du`）存 repo 外私密目錄；只以 `scripts/gvisor-publish-evidence.py --raw <原始> --out docs/research/evidence/<日期>-r01 --redact-file <私密>` 產生遮蔽發布包後進 repo，hash 以發布後檔案計算。
- 每列記錄：完整但不含秘密的命令、時區起迄時間、原始 exit code、量測值與環境版本；沒觀測到的東西不寫成觀測到。
- 本票無模型呼叫；任何憑證不進命令列、log 或報告。
- 清理只刪自建、label 匹配的資源，先停容器再刪 volumes；cleanup 證據（ID／label 核對與刪除記錄）入 manifest，中斷時保留 manifest 供下次核對殘留。

## 決策規則（執行前寫定，不得事後改）

- **NO-GO（保留 Docker adapter）**：官方 Docker checkpoint 路徑在 1／2／4 GB 全部可完成 checkpoint→restore，且 C1–C7 無未解決 fail。此時即使 runsc 數值較快，仍不足以證明更換底層的維護成本合理。
- **GO（授權提出 adapter 變更提案）**：Docker 路徑功能缺失或可重現失敗，且 raw runsc 同負載實測可完成、對應相容性檢查通過。GO 只代表另立實作票，本文件與結果報告都不是實作。
- **blocked ≠ no-go**：主機、版本或功能不可得時如實標 blocked，不算技術結論，也不把等待時間算進測試。
- **失敗不延誤 M1–M3**：原 adapter 保留；研究 no-go 時，對應功能票依 #31 完成證據欄以 not planned 關閉。
- **timebox 2–3 人天到期**：無論矩陣是否填滿，交已得證據與 go/no-go；未執行列標 not_run 與原因，不延期。
- 本協定不預設秒數門檻，也不以任何量測結果承諾「1 秒恢復」；只記錄實測值供決策。

## 本文件明確不含

- 任何已量測數值（所有表保持空）。
- 任何 go／no-go 宣稱、對 M1–M3 時程的影響。
- Docker orchestration 重寫（#31 本票不含）。
