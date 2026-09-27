# #8 無 API key 診斷

這是研究用映像與有界 probe，不是 #10 的正式 image。只在已授權、專用的 Linux amd64 測試 VM 執行。Windows 先啟動 VM，再連線到 guest；Docker Desktop 不作為本矩陣的替代環境。

## 固定內容

- Node 22 Debian bookworm slim：Dockerfile 固定官方 manifest digest；本輪 Node 為 22.23.3。
- Debian 套件：固定 20260926T000000Z snapshot（包括 security archive），含 git、tmux、python3；完整 dpkg 清單寫入 image `/usr/local/share/diagnostic-dpkg.txt`。
- Claude：`package-lock.json` 固定 2.1.283 及 npm integrity。`npm ci --ignore-scripts` 不執行 lifecycle scripts，明確連到 npm optional dependency 的 Linux x64 native binary。已核對官方 wrapper 的安裝邏輯；不執行遠端 installer。
- `DISABLE_AUTOUPDATER=1` 與 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。只呼叫 `claude --version`，不登入、不跑 prompt、不注入 key。

Build 前確認 Dockerfile 與 lockfile；不要把本機 home、憑證或 socket 放進 build context 或容器。若 snapshot／固定 artifact 不可取得，讓 build 失敗，不能偷偷換成浮動來源。

```sh
sudo docker build --platform=linux/amd64 --progress=plain \
  -t spike-diagnostic:local diagnostics/gvisor
sudo docker image inspect spike-diagnostic:local --format '{{.Id}}'
```

測試必須使用輸出的 immutable ID，不能把 tag 傳給 runner。固定輸入不承諾每次 BuildKit 產生相同 digest；每次都保存新 image ID、build log 和實測版本。

## 執行

先確認專用 host Docker socket、runsc `--platform=systrap`、cgroup v2。VM 至少可用 4 GiB RAM；probe 期间低於 2 GiB 則停止。Windows 長測試期間保持清醒。apt hold 只固定指定套件，**每輪仍重新記錄 kernel／Docker／runsc／image**。

本輪可用的 pins 如下；僅適用於該 image 已存在的 host。若要重建，改用新 image ID，建立新 evidence 目錄，不能混入舊 run。

```sh
sudo python3 scripts/gvisor-no-key-matrix.py \
  --image sha256:1728145d39d1e09111580fcd3a8a4931d0d0ebc0429caeb16ff295c86108e5cf \
  --pty-image sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0 \
  --output /path/to/new-evidence-directory
```

PTY 對照使用固定 Alpine 3.23 digest；可先 `docker pull alpine@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0`。最小命令只增加 runtime、immutable pin、name/label 與禁止自動 pull；不加 hardening 或資源 flags。操作只有開啟 `/dev/ptmx` 並退出，外層有 15 秒 timeout。第二個命令再加 network none、cap-drop、no-new-privileges 和 CPU／memory／PID 上限。實際命令完整記在 E06.jsonl。

E09 同一 image、相同 64 PID cap 跑 runsc 和 runc；runc **僅供診斷，不作通過的替代 runtime**。最多 fork 80 個 child，不做遞迴 fork。CPU 忙迴圈最長 20 秒，memory 最多嘗試 256 MiB，並以 128 MiB 容器 cap 限制。主機每 50 ms 取 cgroup 樣本；容器快速退出時可能無法捕捉最後 counter，需連同 Docker State／exit code 解讀。

只重測特定項目可加 `--cases E06 E09`。E04 需要同輪 E03 clone，請一起指定。輸出目錄須不存在，避免覆寫。runner 會保存 manifest，逐一核對 ID／label 後移除本輪容器；不刪 image、不使用 prune。被強制中斷時先依 manifest 查殘留，不重用目錄。

目前預期 exit **2 / incomplete**：E05／E10 留作第二包，不能因其他列通過改成 go。單列 `exit_code` 是操作／probe 的結果，不等於 verdict；例如 OOM 有非零退出碼而仍可符合 E08 預期。CLI timeout 或觀測器例外會記 fail，須看原始 log 區分 runtime 問題。

## 證據入口

[2026-09-27 結果與 PID 阻塞](../../docs/research/gvisor-no-key-results-20260927.md)。完整命令、stdout/stderr、cgroup、來源檔案 hash、版本前後比對及 cleanup 都在該 evidence bundle。

`source-manifest.json` 記執行時來源檔案原始 bytes 的 SHA-256。舊檔案可能來自 CRLF checkout；比對來源時保留當時換行，或另確認 Git blob 的 LF 內容，不把換行差異誤認成 runtime 版本漂移。證據目錄用 `.gitattributes -text` 保持原始 hash。

參考：[Claude 安裝與版本固定](https://code.claude.com/docs/en/setup)、[gVisor Docker runtime](https://gvisor.dev/docs/user_guide/quick_start/docker/)。
