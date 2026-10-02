# #8 M0 證據矩陣索引：來源、版本、hash、清理與成本

> **本索引只是對既有證據的簿記（bookkeeping）。** 2026-10-02 盤點當天沒有重跑任何實驗、沒有建立容器、沒有呼叫模型；本頁所有判定、數值與 hash 都引用下列既有報告與證據包，索引本身不產生新證據。
>
> - **E09 FAIL 維持不變**：原 host PID cap 測試中 runsc sandbox exit 2，原始失敗不覆寫成 pass（[gvisor-no-key-results-20260927.md](gvisor-no-key-results-20260927.md)、[gvisor-pid-results-20260927.md](gvisor-pid-results-20260927.md)）。
> - **#71 只是 GO-candidate，未經核准**：[gvisor-m0-pid-results-20260927.md](gvisor-m0-pid-results-20260927.md) 明言 trial release 仍被 session、containment 與 product gates 阻擋；[ADR](../adr/pid-trial-candidate.md) 仍為 Proposed。
> - **host clone panic 未修復**：保留更多 host task headroom 只避開、不修復該邊界（[gvisor-guest-pid-results-20260927.md](gvisor-guest-pid-results-20260927.md)、[gvisor-m0-pid-results-20260927.md](gvisor-m0-pid-results-20260927.md)）。
> - **#72（#67 memory）不能保證 session 存活**：只實測 same-container `docker start` 與 fsynced marker；recreation、fencing、Claude session 接回皆未驗（[gvisor-m0-memory-results-20260927.md](gvisor-m0-memory-results-20260927.md)、[ADR](../adr/memory-session-recovery.md) 仍為 Proposed）。
> - **report／checker 完整性通過 ≠ 人工 GO**：`gvisor-report.py --check` 的 exit 0／`ready_for_review` 只代表格式完整、可人工審查，`runtime_go` 永遠為 false（[gvisor-environment.md](gvisor-environment.md)）。
> - **#8 的 GO/HOLD/NO-GO 決策表仍為開放**：本索引不填寫、不代替任何決策項，決策以 [#8](https://github.com/our-sandbox-agent/sandbox-console/issues/8) 票身為準。

## 一、矩陣索引（E01–E10，另列兩列補充證據）

判定一律引用證據報告原文；「證據包目錄」是 repo 內已發布的遮蔽證據，hash 見各包的 `bundle-sha256.json`（以發布後檔案計算，見 [gvisor-environment.md](gvisor-environment.md)）。環境 pin 只重述各報告寫下的值，未寫的不補。

| 實驗 | 現況判定 | 證據報告 | 證據包目錄 | harness 腳本 | 環境 pin（依報告所載） | 建立資源與清理／成本（依報告所載） | 後續票 |
|---|---|---|---|---|---|---|---|
| E01 runtime | pass | [gvisor-no-key-results-20260927.md](gvisor-no-key-results-20260927.md)（final run 表） | [evidence/2026-09-27-no-key/](evidence/2026-09-27-no-key/)（E01.jsonl） | [gvisor-no-key-matrix.py](../../scripts/gvisor-no-key-matrix.py) | Ubuntu 24.04.5、kernel 6.8.0-142-generic、Docker 29.8.1、runsc release-20260921.0、systrap、cgroup v2 systemd；image `sha256:172814…85cf`（見報告環境表） | 見「no-key 整輪清理」段：容器依 manifest／label 清除、label 查詢為空、無 volume；image 與 evidence 保留 | #8 決策表 |
| E02 shell | pass | 同上 | 同上（E02.jsonl） | 同上 | 同上 | 同上 | #8 決策表 |
| E03 repo | pass | 同上 | 同上（E03.jsonl） | 同上 | 同上 | 同上 | #8 決策表 |
| E04 package | pass | 同上 | 同上（E04.jsonl） | 同上 | 同上 | 同上 | #8 決策表 |
| E05 Claude marker | not_run | 同上（未跑，等專用 key／模型／預算） | 無（本列無證據檔） | 同上（本列未執行） | 同上（環境已就緒但本列未執行） | 本輪未執行；報告明言不涉及任何 Claude 模型請求或費用 | #75 |
| E06 PTY | pass | 同上 | 同上（E06.jsonl） | 同上 | 同上 | 同上（no-key 整輪清理） | #8 決策表 |
| E07 CPU | pass | 同上 | 同上（E07.jsonl、E07-cgroup.json） | 同上 | 同上 | 同上 | #8 決策表 |
| E08 memory | pass（僅表上限生效） | 同上（pass 不含 session 存活，session 存活由 #67 另驗） | 同上（E08.jsonl、E08-cgroup.json） | 同上 | 同上 | 同上 | #8 決策表；session 存活見下列 #72 補充 |
| E09 PID | **fail（原始判定保留）** | [gvisor-no-key-results-20260927.md](gvisor-no-key-results-20260927.md)、[gvisor-pid-results-20260927.md](gvisor-pid-results-20260927.md)（#62/#63 調查）、[gvisor-guest-pid-results-20260927.md](gvisor-guest-pid-results-20260927.md)（部分緩解仍阻塞）、[gvisor-m0-pid-results-20260927.md](gvisor-m0-pid-results-20260927.md)（#70 修正是 candidate，不改 E09 FAIL） | [evidence/2026-09-27-no-key/](evidence/2026-09-27-no-key/)（E09.jsonl、E09-runsc-cgroup.json、E09-runc-cgroup.json）、[evidence/2026-09-27-pid/](evidence/2026-09-27-pid/)、[evidence/2026-09-27-guest-pid/](evidence/2026-09-27-guest-pid/)、[evidence/2026-09-27-m0-pid/](evidence/2026-09-27-m0-pid/)、[evidence/2026-09-27-pid-review/](evidence/2026-09-27-pid-review/) | [gvisor-no-key-matrix.py](../../scripts/gvisor-no-key-matrix.py)、[gvisor-pid-investigation.py](../../scripts/gvisor-pid-investigation.py)、[gvisor-m0-pid.py](../../scripts/gvisor-m0-pid.py) | no-key 包同上；#63 調查包：Ubuntu 24.04.5、kernel 6.8.0-142-generic、Docker 29.8.1、runsc release-20260921.0、systrap、systemd cgroup v2；guest-pid／m0-pid 包：kernel 6.8.0-142-generic、Docker 29.8.1、runsc release-20260921.0/systrap（同 image） | 各包清理見「證據包清單」：owned 容器均依 label／manifest 核對後移除、daemon 還原 systrap-only；無付費模型呼叫 | #8 決策表（host clone panic 未修復，headroom 不是修復） |
| E10 cold/session | not_run | [gvisor-no-key-results-20260927.md](gvisor-no-key-results-20260927.md)（缺 key，留待完整驗證） | 無（本列無證據檔） | 同 E01（本列未執行） | 同 no-key 環境表（本列未執行） | 本輪未執行；不涉及模型請求或費用 | #75（新實體與明確 session ID 接回；same-container start 不算重建） |
| 補充：guest PID 候選（非矩陣列） | GO-candidate（candidate-only，未核准） | [gvisor-guest-pid-results-20260927.md](gvisor-guest-pid-results-20260927.md)、[gvisor-m0-pid-results-20260927.md](gvisor-m0-pid-results-20260927.md)、[ADR pid-trial-candidate.md](../adr/pid-trial-candidate.md)（Proposed） | [evidence/2026-09-27-guest-pid/](evidence/2026-09-27-guest-pid/)、[evidence/2026-09-27-m0-pid/](evidence/2026-09-27-m0-pid/)、[evidence/2026-09-27-pid-review/](evidence/2026-09-27-pid-review/) | [gvisor-pid-investigation.py](../../scripts/gvisor-pid-investigation.py)、[gvisor-m0-pid.py](../../scripts/gvisor-m0-pid.py) | guest-pid 包：Ubuntu kernel 6.8.0-142-generic、Docker 29.8.1、runsc release-20260921.0、systrap；m0-pid 包：kernel 6.8.0-142-generic、Docker 29.8.1、runsc release-20260921.0/systrap、H=2N+128 為候選非容量保證 | 見各報告：guest-pid 四個 owned 容器 label 核對後移除、daemon 還原；m0-pid owned 容器 label 核對後移除、host-after 無測試容器；無付費 Claude 任務 | #70 剩實際 Agent 啟動／退出／恢復 fixture；多沙盒與權限邊界 #11/#18；#8 決策表 |
| 補充：memory session 研究（非矩陣列） | 研究完成、恢復契約 Proposed | [gvisor-m0-memory-results-20260927.md](gvisor-m0-memory-results-20260927.md)、[ADR memory-session-recovery.md](../adr/memory-session-recovery.md)（Proposed） | [evidence/2026-09-27-m0-memory/](evidence/2026-09-27-m0-memory/)（published-67-base、published-67-multi、review72 三包） | [gvisor-m0-memory.py](../../scripts/gvisor-m0-memory.py) | kernel 6.8.0-142-generic、Docker 29.8.1、runsc release-20260921.0/systrap；2 CPU／256 MiB／host PID cap 512（見報告） | 八個 owned 容器與十六個具名 volume 在 ownership 檢查後移除；測試 marker 先讀回再刪；daemon 還原 systrap-only；無 Claude 模型任務 | #75（新實體／Claude session 接回）；產品實作 #11/#12/#17；#8 決策表 |

### no-key 整輪清理（E01–E08 引用）

[gvisor-no-key-results-20260927.md](gvisor-no-key-results-20260927.md)：[cleanup.jsonl](evidence/2026-09-27-no-key/cleanup.jsonl) 與 [manifest.json](evidence/2026-09-27-no-key/manifest.json) 記錄每個容器 ID／label 的核對和刪除，最終 label 查詢為空；本輪沒建立 volume、沒掛載 host 資料或 Docker socket；診斷 image、build cache 與 evidence 保留供重跑。成本：無模型呼叫、無費用。

## 二、證據包清單（hash 與檔案數）

每包的完整 hash 清單在該包的 `bundle-sha256.json`（發布後位元組計算）；以下 manifest hash 為自該檔抄錄的實際值。檔案數為 `find -type f` 實點結果，各報告自述的檔案數（如 m0-pid「282 files」）不含此 hash manifest。

| 證據包 | 檔案數 | bundle hash 清單 | manifest.json sha256（抄自 bundle-sha256.json） | 報告自述出處 |
|---|---|---|---|---|
| [evidence/2026-09-27-no-key/](evidence/2026-09-27-no-key/) | 25 | [bundle-sha256.json](evidence/2026-09-27-no-key/bundle-sha256.json) | `9381515cf78d999f9be5886a813601883c81335e1093ca3cc56ffbc660c707c3`；report.json `ab3a574df87677e982b5f05a2b2dda78124d3b5deee95e8096e15d71892c1e94` | no-key 報告 |
| [evidence/2026-09-27-pid/](evidence/2026-09-27-pid/) | 73 | [bundle-sha256.json](evidence/2026-09-27-pid/bundle-sha256.json) | `df05c873f2387f079333fbb927fc7aa6c0b2782e6f2765c32b6bee4266feadc3`；results.json `cc469cdb03913cad958ca792b42e34f79b938b544e68eecd507acb0a3ef402fd` | PID 調查報告（清理：六容器 label 核對移除、無全域 prune、daemon 還原） |
| [evidence/2026-09-27-guest-pid/](evidence/2026-09-27-guest-pid/) | 100 | [bundle-sha256.json](evidence/2026-09-27-guest-pid/bundle-sha256.json) | guest-pid-32 `f28d0d848fdfcd16b32ddd114b6fdd18abcf01c009e1608332d3ce1ccbe4e562`、guest-pid-64 `d48c527d4ec67b1f4ebda7becd0f4e3fbbf09691d5abe81bb4f8e421816bee22`、guest-pid-host `1a2f32eb142fac2045d9eb1d58bb49e05455da209310b7fec8599120b350246e` | guest-PID 報告（清理：四容器 label 核對移除、daemon 還原、containers-after.txt 為空） |
| [evidence/2026-09-27-m0-pid/](evidence/2026-09-27-m0-pid/) | 283 | [bundle-sha256.json](evidence/2026-09-27-m0-pid/bundle-sha256.json) | `c862d0c7939a4740d8197da09067ce7182972cdf5856a028e76c47f35760ca2e`；results.json `d926ca3a1109f693e35c6edf7f9401afaa90e5431a8a18452495e1dc16c09648` | m0-pid 報告（自述 282 檔＋hash manifest；清理：owned 容器 label 核對移除、host-after 無測試容器） |
| [evidence/2026-09-27-pid-review/](evidence/2026-09-27-pid-review/)（pilot／interactive／noninteractive） | 62／306／49 | 各子目錄 bundle-sha256.json | 見 [pilot/](evidence/2026-09-27-pid-review/pilot/)、[interactive/](evidence/2026-09-27-pid-review/interactive/)、[noninteractive/](evidence/2026-09-27-pid-review/noninteractive/) 各自的 bundle-sha256.json | m0-pid 報告（自述 61/305/48 檔＋hash manifest） |
| [evidence/2026-09-27-m0-memory/](evidence/2026-09-27-m0-memory/)（published-67-base／published-67-multi／review72） | 131／68／147 | 各子目錄 bundle-sha256.json | published-67-base `c1b23dd175c4695575deb56077fc66463ebdccbfdbfee4fb0a5998098baeb837`、review72 `e5e3fe4e0177c6839010c8764ba9c5151871b2064c3d95b0465b80157ef0dd0d` | m0-memory 報告（自述 130/67/146 文字檔＋hash manifest；清理：八容器十六 volume ownership 檢查後移除） |

版本與來源 pin（均抄自報告）：image `sha256:1728145d39d1e09111580fcd3a8a4931d0d0ebc0429caeb16ff295c86108e5cf`、image recipe commit `1711b8d`（no-key 報告）；m0-pid source commit `7ef80b33834aee16cb0f2b54c1ff71dec499eb37`；m0-memory source commit `d0bf20e93d21c418f8b8c18d5e07162451e6c321`；發布工具 commit `34cc81f0a4b996921edf250a6c65322dce83cd67`。這些是原證據版本，不是推薦自動升級至最新版。

## 三、harness 腳本與診斷材料

| 檔案 | 角色（依檔案／報告所述） |
|---|---|
| [scripts/gvisor-preflight.py](../../scripts/gvisor-preflight.py) | 唯讀本機 preflight；不安裝、不 pull、不啟動、不刪資源 |
| [scripts/gvisor-no-key-matrix.py](../../scripts/gvisor-no-key-matrix.py) | 在授權主機執行 E01–E10 無 key 診斷；E05/E10 保持 not_run，只清理本次建立資源 |
| [scripts/gvisor-pid-investigation.py](../../scripts/gvisor-pid-investigation.py) | #63 六案例 PID cap 調查（runsc/runc 對照），需操作者配好的 debug logs |
| [scripts/gvisor-m0-pid.py](../../scripts/gvisor-m0-pid.py) | #70 有界 CPU／guest-limit／fork／thread 矩陣；raw 輸出留私密 |
| [scripts/gvisor-m0-memory.py](../../scripts/gvisor-m0-memory.py) | #67 allocation limit 對 host OOM；raw 輸出留私密 |
| [scripts/gvisor-publish-evidence.py](../../scripts/gvisor-publish-evidence.py) | 產生遮蔽後發布包；hash 以發布後檔案計算 |
| [scripts/gvisor-report.py](../../scripts/gvisor-report.py) | 建立／檢查證據報告；完整性不是 runtime go 決定 |
| [scripts/gvisor_redact.py](../../scripts/gvisor_redact.py) | harness 層（Matrix.write/log）就地遮蔽 |

診斷材料：[diagnostics/gvisor/Dockerfile](../../diagnostics/gvisor/Dockerfile)、[diagnostics/gvisor/README.md](../../diagnostics/gvisor/README.md)、[diagnostics/gvisor/package.json](../../diagnostics/gvisor/package.json)、[diagnostics/gvisor/package-lock.json](../../diagnostics/gvisor/package-lock.json)、probes（[cpu.py](../../diagnostics/gvisor/probes/cpu.py)、[memory.py](../../diagnostics/gvisor/probes/memory.py)、[memory-session.py](../../diagnostics/gvisor/probes/memory-session.py)、[node-workload.cjs](../../diagnostics/gvisor/probes/node-workload.cjs)、[pid-pressure.py](../../diagnostics/gvisor/probes/pid-pressure.py)、[pids.py](../../diagnostics/gvisor/probes/pids.py)、[progress.py](../../diagnostics/gvisor/probes/progress.py)、[task-pressure.py](../../diagnostics/gvisor/probes/task-pressure.py)）。對應測試為 [scripts/test_gvisor_*.py](../../scripts/)（如 test_gvisor_evidence.py、test_gvisor_m0_pid_evidence.py、test_gvisor_m0_memory_evidence.py）。

## 四、未了事項

- #75：E05／E10 付費實測與 cold recreation／session ID 接回；完成只補執行證據，不自動完成 #8 的人工 GO。
- #8 決策表（GO/HOLD/NO-GO、owner、ADR 接受）仍未填；本索引列出的 pass/fail/not_run 都不是決策本身。
