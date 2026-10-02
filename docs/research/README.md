# gVisor 研究索引（#8）

本頁是 docs/research/ 的入口，只陳列現況結論；各報告的原始證據、版本與日期以該報告為準，不因本索引改寫。

## 現況結論（2026-10-02 盤點）

- **原 E09（PID cap）FAIL 判定維持不變。** 第一包無 key 實測（2026-09-27）E01–E04／E06–E08 pass、E09 fail、E05／E10 not run；#8 保持 open，尚不可 go。
- **#71（PID 候選）只是 candidate。** #70 修正後提出 GO-candidate 提案（見 [PID ADR](../adr/pid-trial-candidate.md)，Proposed）：guest quota 滿時乾淨拒絕、解除後可恢復。**host clone panic 未修復**：觸及 host PID 邊界仍會使 Sentry 崩潰，headroom 只避開、不修復該邊界。未經創辦人核准，也未授權 #10/#11。
- **#72（#67 記憶體 session）不能保證 session 存活。** 只實測 same-container `docker start` 與兩個 fsynced marker；recreation、fencing、Claude session 接回皆未驗。恢復契約仍為 Proposed（見 [記憶體恢復 ADR](../adr/memory-session-recovery.md)），由 #11（runtime）／#17（operation／對帳）／#19（自動策略）後續驗收。
- **report checker 成功 ≠ 人工 runtime GO。** `gvisor-report.py --check` 的 exit 0／`ready_for_review` 只代表證據格式完整、可人工審查；`runtime_go` 永遠為 false。#8 關閉需第二份實測 PR 經人工審查確認 go。

## 文件清單

| 文件 | 內容 |
|---|---|
| [gvisor-spike.md](gvisor-spike.md) | #8 矩陣定義（E01–E10）、通過條件與 go/no-go 規則 |
| [gvisor-environment.md](gvisor-environment.md) | 測試環境交接、重跑流程與證據包發布 |
| [gvisor-no-key-results-20260927.md](gvisor-no-key-results-20260927.md) | 第一包無 key 實測：E09 fail，E05／E10 not run |
| [gvisor-pid-results-20260927.md](gvisor-pid-results-20260927.md) | E09 PID cap 失敗調查 |
| [gvisor-guest-pid-results-20260927.md](gvisor-guest-pid-results-20260927.md) | guest NPROC 部分緩解；E09 仍阻塞 |
| [gvisor-m0-pid-results-20260927.md](gvisor-m0-pid-results-20260927.md) | #70 修正判定與證據（#71 候選來源） |
| [gvisor-m0-memory-results-20260927.md](gvisor-m0-memory-results-20260927.md) | #67 記憶體壓力與恢復證據（#72 來源） |
| [m0-matrix-index.md](m0-matrix-index.md) | #8 矩陣→來源／版本／hash／清理／成本索引（僅簿記既有證據，not_run 與 FAIL 保留） |
| [r01-warm-restore-protocol.md](r01-warm-restore-protocol.md) | R01（#31）暖恢復比較協定：先驗官方 Docker checkpoint、raw runsc 對照；規劃中，**未執行** |
| [r02-warm-suspend-protocol.md](r02-warm-suspend-protocol.md) | R02（#32）暖 Suspend 一致記憶體與磁碟版本協定：僅當 R01 go 才執行；規劃中，**未執行** |
| [r03-snapshot-fork-protocol.md](r03-snapshot-fork-protocol.md) | R03（#33）磁碟 Snapshot／Fork 與憑證處理協定：disk 快照先行、memory 快照列依 R02；規劃中，**未執行** |
| [r04-resume-upload-protocol.md](r04-resume-upload-protocol.md) | R04（#34）500 MB 續傳與資料夾可靠匯入協定：tus 授權綁 workspace/upload id、斷點續接與 Caddy 代理實測；規劃中，**未執行** |
| [r05-git-autosave-protocol.md](r05-git-autosave-protocol.md) | R05（#35）選用的 Suspend 前 Git 自動存檔協定：repo-scoped 憑證鐵門、HEAD/index 不變與 force-with-lease 語意、停機不被 push 卡住；規劃中，**未執行** |
| [r06-firecracker-eval-protocol.md](r06-firecracker-eval-protocol.md) | R06（#36）Firecracker 需求／主機／成本門檻評估協定：條件式開工（仍未授權切換 runtime）、KVM 硬條件檢查、cold restart／warm restore cold-cache／warm-cache 分列、價格當日查證；規劃中，**未執行** |
| [evidence/](evidence/) | 遮蔽後的原始證據包（歷史紀錄，保留原狀） |

E05／E10 的付費實測由 #75 追蹤；#75 完成只補執行證據，不自動完成 #8 的人工 GO。
