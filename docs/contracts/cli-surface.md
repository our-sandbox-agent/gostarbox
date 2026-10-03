# CLI surface contract

Status: **Contract delivered (Proposed); Go implementation pending.** No Go toolchain
is set up in this repo slice and the #12/#13 runtime pieces are not integrated, so
**no runtime verification is claimed or performed.** The machine-checked table is
[cli-surface.json](cli-surface.json), validated by `scripts/verify_cli_contract.py`
(stdlib-only, offline), in the same idiom as the
[terminal protocol contract](terminal-protocol.md). Sources: the
[runner/language ADR](../adr/runner-language.md) (Accepted; the CLI is Go and always
goes through the control plane, never the Runner directly), issue #14 (acceptance
criteria plus the 2026-10-02 note: `cp` lands with #20, API gaps answer explicit
unsupported, never a mocked success) and docs/plan.md sections 3/4 (M1 minimal
commands).

## What this contract fixes

Only the argv surface and its discipline — nothing about a running binary:

- **8 commands as a closed set**: `login` (minimal token bootstrap, **no OAuth** —
  本票不含), `claude [--repo <https-url>]` (create + clone + attach in one line),
  `ls`, `connect <id>`, `suspend <id>`, `destroy <id>`,
  `exec <id> -- <argv...>`, and `cp <id> <remote> <local>` (download-only).
- **`--repo` validation reuses** the `scripts/clone_args.py` `validate_repo_url`
  rule: https only, host present, no userinfo, no whitespace/control characters;
  a rejected URL is a usage error (64).
- **`exec` literal-argv rule**: everything after `--` is literal argv to the
  sandbox — never shell-parsed, never string-concatenated into a command line
  (the same argv discipline `clone_args.py` applies to `git clone`).
- **3 unsupported commands**: `snapshot`, `fork`, `resume` — plus any other
  unknown command — exit **64 (EX_USAGE)** with the message template
  `unsupported command: {command} (…)`. Missing control-plane capabilities are
  never answered with a mocked success.
- **8 exit codes, unique and documented**: 0 success (including the Ctrl-\ detach
  exit, where the sandbox keeps running); 64 usage/unsupported; 65 data error;
  68 no token (run `sandbox login`); 69 cannot reach the control plane;
  70 internal; 75 temporary failure, retry later; 76 terminal connection lost —
  auto-reconnect attempts exhausted after a network drop, message names the
  sandbox id.
- **Token discipline**: one file, `$XDG_CONFIG_HOME/sandbox/token`
  (default `~/.config/sandbox/token`), mode **0600**, **never in argv or logs**;
  `SANDBOX_TOKEN` env override for CI.
- **Terminal restoration contract**: raw mode is restored on every exit path —
  normal exit, Ctrl-\ detach, SIGINT/SIGTERM, socket close, and panic (closed
  set, machine-checked). On attach after a cold suspend/resume the CLI prints an
  explicit **NEW session** notice driven by `wrong_generation`/replay from the
  terminal protocol (#13); it never presents the new session as the old one.
- **Telemetry, local only**: `terminal_ready_ms` (create → terminal ready) and
  `clone_done_ms` (clone completion, only with `--repo`) as one line on stdout;
  `--quiet` suppresses it. 10 seconds is a **target, not a promise**; clone time
  is outside our control and never a hard commitment.
- **Install artifacts**: a release **must** carry a version file and a checksum
  file (sha256 per binary). Platforms claim support only after real testing:
  darwin/arm64 is planned first; everything else stays untested and unclaimed.

## Control plane only (#76)

Every command routes through the control plane; the CLI never talks to a Runner
directly. The verifier rejects any `direct_runner`-style flag and requires
`control_plane_only` on every command.

## `sandbox cp` boundary

The interface and exit codes are fixed here, but the implementation is owned by
the **#20 integration PR** (`depends_on: [20]`, status `defined-interface`) —
this contract alone is not a standalone-complete `cp`. Until #20 lands, the
command exits 64 pointing at #20; it never fakes a download.

## Non-goals

- No Go implementation in this slice (toolchain + #12/#13 runtime pending).
- No OAuth login, no Windows, no auto-update, no Homebrew, no resumable upload
  (issue #14 本票不含).
- No runtime claims: nothing here is implemented, executed or measured.

## Runtime acceptance still owed (#14 checkboxes)

1. The real Go binary with the surface above — Mac arm64 build tested first;
   other architectures need real testing before any support claim.
2. Network-drop auto-reconnect then exit 76, Ctrl-\ detach with the sandbox
   kept running — behavior unverified until the binary exists.
3. `terminal_ready_ms` / `clone_done_ms` actually recorded; the 10s target
   measured, not promised here.
4. Raw terminal restored on all five exit paths under real signals and panics.
5. `sandbox cp` working end to end via #20.

## Verify

```
python3 scripts/verify_cli_contract.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Both are pure-stdlib and offline. The unittest file also contains guard tests
that mutate the contract in-memory and assert the verifier rejects each broken
rule (in the spirit of CONTRIBUTING's test rule for pure-logic modules).
