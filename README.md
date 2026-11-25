# Antigravity CLI (`agy`) Plugins

Zero-dependency productivity plugins for Google Antigravity CLI (`agy`), modeled on local Claude Code mods:

- **[`done-notify`](./done-notify)** — macOS Notification Center banner + system sound when an `agy` turn finishes, fails, asks a question (`ask_question`), or pauses waiting for user permission approval (alerts even while focused by default, with optional terminal/tab focus suppression).
- **[`token-weather`](./token-weather)** — Live three-line weather-themed `statusLine` band organized into (1) execution mode (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`, `[VIM:*]`), active model, conversation name, and session metadata, (2) 5-hour & weekly model quota usage alongside Google Colab remaining compute units, and (3) context window fill tier, 8-turn sparkline, token delta, and prompt cache TTL countdown.
- **[`safe-allow`](./safe-allow)** — Smart `PreToolUse` policy guard for `/swarm` subagents and the main `agy` session that validates compound commands segment by segment and returns `allow` with `permissionOverrides` for verified safe operations while keeping destructive commands, credential accesses, and unconfigured hosts gated on `ask`.
- **[`conv-restore`](./conv-restore)** — Zero-dependency diagnostic and recovery skill for disappeared `agy` conversations covering `default-cli-project.json` workspace pollution (`#1657`), 0-step SQLite `.db` stub shadowing (`#1602`), stale or zeroed summaries, and archived annotations, with dry-run previews, automatic timestamped backups, offline finalization, and integrity verification.
- **[Maximum Context Window (`900K` / `1M` Tier)](#5-maximum-context-window-900k--1m-tier)**, how `agy` `v1.5.10` gates context compaction, why `"contextWindow": "900K"` in `settings.json` alone still compacts around 500K tokens, and how to unlock a `900,000`-token threshold on `Gemini 4 Argon` via `AGY_CLI_AUTO_COMPACT_OVERRIDE=900000`.

---

## Installation

Clone this repository and install any plugin with `agy plugin install`:

```bash
git clone https://github.com/rzyu45/agy-plugins.git
agy plugin install ./agy-plugins/done-notify
agy plugin install ./agy-plugins/token-weather
agy plugin install ./agy-plugins/safe-allow
agy plugin install ./agy-plugins/conv-restore
```

Verify installed plugins:

```bash
agy plugin list
```

---

## 1. `done-notify` (macOS Completion, Question & Permission Alerts)

[`done-notify`](./done-notify) wires lightweight `SessionStart`, `PreInvocation`, `PreToolUse` (`^ask_question$`), and `Stop` hooks with zero latency on auto-approved tools:

- **Turn completion & background task wakeups**: Posts `<project> · done in <duration>` (or `task done in <duration>` for reactive background task wakeups) with the live conversation name (`[<conversation>]`) and a plain-text summary of the final answer, and plays `Glass`. Resolves the owning `agy` process's launch workspace in multi-workspace sessions and reads live conversation titles from `conversation_summaries.db` and `annotations/<id>.pbtxt`.
- **`/swarm` worker `[done]` completions & `swarm_wait` handling**: Distinguishes normal `swarm_wait` parking (`TERMINAL_CUSTOM_HOOK` with no error) from genuine hook failures so coordinator waits never emit false `stopped by hook` error banners. When the coordinator processes one or more worker `[done]` reports addressed to `@coordinator` (or `@all`) before parking, posts `<project> · @<worker> done` (or `<N> workers done`) with the worker's summary and plays `Glass` (`notify_on_swarm_done=true` by default); pure launch or peer-chatter parks stay silent.
- **Turn failure or budget/step limits**: Posts `<project> · turn failed after <duration>` (or `stopped by hook` for genuine custom-hook stops) with the error summary and plays `Sosumi`.
- **Interactive question prompts (`ask_question`)**: Immediately posts `<project> · waiting for input` with the question text and plays `Ping` (`prompt_sound`) when `agy` pauses for an answer.
- **Permission confirmation prompts**: Tails the owning `agy` CLI session log and posts `<project> · waiting for permission` with `Ping` (`prompt_sound`) at the exact moment `agy` surfaces a tool confirmation dialog (unsandboxed commands, sensitive command checks, out-of-workspace file access/edits, MCP/URL/browser confirmations, `--mode default` tool approvals, or subagent approval queues). Auto-approved tools and commands covered by an existing `"Always allow"` rule never chime, and the watcher exits automatically when `agy` closes.
- **Always-on by default, with optional focus filtering**: Both `focus_mode` and `prompt_focus_mode` default to `always` so you get an audible cue even when the terminal window is focused. If you prefer silence while actively viewing the CLI, set `--set focus_mode=tab` (or `app`) to check the frontmost macOS app via `/usr/bin/lsappinfo` plus active tab/pane state for `tmux`, `Terminal.app`, `iTerm2`, `WezTerm`, and `Kitty`. Automatically skips `USER_CANCELED` turns, child subagent turns, SSH sessions, and headless `agy -p` / `acp` invocations.

### Quick Test & Configuration

Preferences persist in `~/.config/agy-done-notify/config.json` and take effect immediately without restarting `agy`:

```bash
# Test completion, /swarm worker [done], question, permission, and failure alerts
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --swarm-done
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --question
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --permission
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --error

# Inspect detected host terminal, prompt watcher status, and active config
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --status

# Common settings
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set lang=zh
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set sound=Glass --set prompt_sound=Ping --set error_sound=Sosumi
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set focus_mode=always        # always (default) | tab | app
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set prompt_focus_mode=always # always (default) | tab | app
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set notify_on_swarm_done=true # true (default) | false
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set show_conversation=true   # true (default) | false
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set min_duration_sec=5
```

> **macOS Notification Settings**: Banners are dispatched via `/usr/bin/osascript` (**Script Editor** in macOS `System Settings -> Notifications -> Script Editor`). Ensure **Allow Notifications** and **Play sound for notifications** are enabled there.

See [`done-notify/skills/done-notify/references/README.md`](./done-notify/skills/done-notify/references/README.md) for Chinese documentation.

---

## 2. `token-weather` (Three-Line Mode, Quota/Colab & Context Status Band)

[`token-weather`](./token-weather) renders `agy`'s live `statusLine` JSON telemetry as a default 3-line adaptive ANSI status bar (with optional 2-line and 1-line compact modes):

1. **Line 1 — Mode, model, and conversation**: Execution mode badge (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`, `[VIM:*]`), active model name with reasoning effort and live state (`thinking`), active conversation name (`conversation_title`, updated immediately on `/rename`), plus subagents, background tasks, session cost, and Git branch.
2. **Line 2 — Model usage and Colab usage**: 5-hour and weekly model quota bars, percentages, pace warning (`↑`), and reset countdowns alongside Google Colab remaining compute units (`Colab 1000 CU` when idle, or `Colab 942.5 CU · 1.9/h · 1 VM` when a runtime is active, refreshed asynchronously in the background via `google-colab-cli`).
3. **Line 3 — Context usage**: Weather forecast tier, context fill percentage, progress bar, `used/window` token count, 8-turn context magnitude sparkline with net token delta, and prompt cache hit/TTL countdown.

| Context Fill | Tier (EN / ZH) | Meaning |
| :--- | :--- | :--- |
| `< 25%` | `☀ Clear` / `晴朗` | Plenty of context headroom |
| `25% – 49%` | `☁ Cloudy` / `多云` | Context accumulating normally |
| `50% – 74%` | `☂ Showers` / `阵雨` | Past halfway; watch large file reads |
| `75% – 89%` | `☇ Storm` / `雷暴` | Nearing window cap; wrap up current task |
| `>= 90%` | `↯ Compact soon` / `建议压缩` | Run `/compact` or start a fresh session |

### Activation in `agy`

Inside an interactive `agy` session, bind the status line command:

```text
/statusline ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py
```

To stack `token-weather` underneath `agy`'s built-in status bar instead of replacing it, set `"stack_with_default": true` in `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py",
    "enabled": true,
    "stack_with_default": true
  }
}
```

### Preview & Configuration

Preferences persist in `~/.config/agy-token-weather/config.json`:

```bash
# Preview all 5 weather tiers (default 3-line layout, plus 2-line and 1-line options)
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --preview
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --preview --lang zh
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --preview --lines 2

# Refresh cached Google Colab compute units immediately
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --refresh-colab

# Common settings
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set lang=zh
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set lines=3                  # 3 (default) | 2 | 1
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set quota_metric=remaining   # used | remaining
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set show_colab=true          # true | false
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set colab_auth=auto          # auto | oauth2 | adc
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set colab_ttl=180            # idle cache TTL in seconds (60s when active)
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set show_mode=always         # always | non-default | off
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set show_title=true        # true | false
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set ascii=true
```

See [`token-weather/skills/token-weather/references/README.md`](./token-weather/skills/token-weather/references/README.md) for Chinese documentation.

---

## 3. `safe-allow` (PreToolUse Permission Guard for `/swarm` & CLI)

[`safe-allow`](./safe-allow) pairs a wildcard `PreToolUse` hook with a `--sync-settings` generator so both the main coordinator and `/swarm` workers auto-approve verified non-destructive operations while keeping destructive commands gated on `ask`. In `agy` `v1.5.x`, `hooks.json` runs only on the main coordinator session, while `/swarm` workers evaluate `~/.gemini/antigravity-cli/settings.json` (`Deny > Ask > Allow`) directly. Running `--sync-settings` once in a regular terminal outside `agy` compiles your active categories and local SSH allowlist into Go-RE2 `permissions.allow` and overriding `permissions.ask` rules in `settings.json`:

- **Read-only inspection (`readonly_shell`)**: Splits `&&`, `;`, and `|` pipelines and verifies every segment (such as `ls`, `rg`, `find` without `-delete`/`-exec`, `wc`, `jq`, `pdfinfo`, `pdftotext`, and read-only `git` subcommands), blocking reads against `~/.ssh`, `.env`, `*.pem`, and `*.key`.
- **Local document builds & workspace edits (`doc_and_edit`)**: Allows `write_file`/`edit_file` inside the active workspace, brain directory, or `/tmp` (protecting `.git/`, `.agents/hooks.json`, `settings.json`, and credential paths), along with workspace `latexmk` (no `-e` or `-shell-escape`), `xelatex`/`pdflatex`, `biber`, `bibtex`, `make` document targets, `pdftoppm`, `officecli`, and local `git add`/`commit`.
- **Read-only MCP & web reads (`mcp_and_web`)**: Allows `search_web`, `read_url`, `paper-search-mcp/*`, `drawio/*`, and read/search `zotero` tools while keeping mutating `zotero` tools (`delete`, `create`, `update`, `batch_update`, `merge_duplicates`) gated on `ask`.
- **Workspace Python (`workspace_python`)**: Allows `pytest`, `python3 -m py_compile|pytest|unittest|json.tool`, scripts inside the workspace or `~/.gemini/config/{skills,plugins}`, and safe non-destructive sandboxed `python3 -c` snippets.
- **Configured remote SSH (`remote_gpu_ssh`)**: Disabled for unconfigured hosts by default (`targets: []`). Once you register trusted `user@host[:port]` endpoints and POSIX write prefixes locally, safe `ssh`/`scp`/`rsync` (without `--delete` or port forwarding) against those targets are auto-allowed.
- **Red-flag hard gate**: Even inside the sandbox and inside generated `settings.json` rules (`Ask > Allow`), `rm`, `sudo`, `sed -i`, `rsync --delete`, `git push`, `git reset --hard`, `git clean`, package installs (`pip`/`conda`/`brew`), and `curl | sh` pipelines always return `ask`.

### Configuration, `--sync-settings` & Dry-Run Inspection

Preferences persist in `~/.config/agy-safe-allow/config.json` (mode `0600`), and recent hook decisions are logged to `/tmp/agy-safe-allow-<uid>/audit.jsonl`:

```bash
# Inspect status, active config, and decision counts
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --status

# View recent prompts that remained gated on ask
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --recent --ask-only

# Dry-run a command check
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --check-cmd "cd docs && make pdf" --bypass-sandbox

# Register an authorized remote SSH host and remote write prefix locally
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --add-ssh-target alice@gpu-server.example:2222 --add-ssh-prefix /data/alice

# Compile rules into ~/.gemini/antigravity-cli/settings.json for /swarm workers (run outside agy)
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --sync-settings

# Preview merged settings.json without writing
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --print-settings

# Optional scope or category toggles (re-run --sync-settings after changing categories)
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --set scope=subagents_only
python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --set remote_gpu_ssh=false
```

See [`safe-allow/skills/safe-allow/references/README.md`](./safe-allow/skills/safe-allow/references/README.md) for Chinese documentation.

---

## 4. `conv-restore` (Diagnose & Restore Disappeared Conversations)

[`conv-restore`](./conv-restore) provides a zero-dependency Python 3 recovery CLI and skill for diagnosing and restoring disappeared Antigravity CLI (`agy`) conversations across all four known failure modes:

- **Mode A (`default-cli-project.json` workspace pollution, upstream `#1657`)**: Detects stale `projectResources.resources[].folderUri` entries that sort ahead of real project URIs and hide them in `/resume`, strips the polluter URI from `trajectory_metadata_blob` (Protobuf wire-validated before any write) and `conversation_summaries.db` / `conversations_fts`, preserves genuine single-workspace conversations untouched, skips legacy `.pb`-only conversations safely, and removes stale `jetbox_summaries_proto.pb` + `cache/conversation_metadata.json` offline so `reconcile_summaries.go` rebuilds clean summaries natively.
- **Mode B (0-step `.db` stub shadowing `.pb`, upstream `#1602`)**: Detects verified empty `<cid>.db` stubs (`steps == 0`, `trajectory_meta == (0, 0)`, empty `trajectory_metadata_blob`) beside non-empty `<cid>.pb` files in both CLI and Hub stores, isolates them into `<backup>/stub_dbs/` (never deleting transcripts or `.pb` blobs), and clears stale summary caches for native recovery.
- **Mode C (stale, missing, or zeroed summaries)**: Flags healthy `.db` or `.pb` conversations missing from `conversation_summaries.db` or stuck at `step_count = 0` / `0001-01-01` and clears stale caches so the next launch rebuilds accurate summaries.
- **Mode D (archived annotations)**: Lists conversations marked `archived: true` in `annotations/<cid>.pbtxt` and optionally unarchives explicitly selected IDs via `ConversationService/UpdateConversation`.

### Usage, Safety Guards & Disabling

All read-only scans copy SQLite `.db`/`-wal`/`-shm` files into a temporary directory first. Writes create an automatic timestamped backup under `/tmp/agy-conv-restore-backup-<YYYYMMDD_HHMMSS>/`, enforce `PRAGMA integrity_check`, and refuse full offline edits while any `agy` process is running:

```bash
SCRIPT=~/.gemini/config/plugins/conv-restore/skills/conv-restore/scripts/conv-restore.py

# 1. Read-only diagnosis across all 4 modes
python3 "$SCRIPT" --diagnose

# 2. Dry-run preview (optionally targeting an explicit polluter path or URI)
python3 "$SCRIPT" --dry-run --strip-uri /path/to/polluter-project

# 3. Full offline restore (run in a regular terminal after exiting all agy sessions)
python3 "$SCRIPT" --restore

# 4. Partial live restore of inactive .db files while inside agy, then offline finalize after exit
python3 "$SCRIPT" --restore --live-inactive-only
python3 "$SCRIPT" --finalize

# 5. Verify SQLite/FTS5 integrity and zero remaining anomalies (optional /tmp agy --hub staging check)
python3 "$SCRIPT" --verify
python3 "$SCRIPT" --verify --verify-staging

# 6. Disable or uninstall once agy fixes the bug upstream
agy plugin disable conv-restore
agy plugin enable conv-restore
agy plugin uninstall conv-restore
```

See [`conv-restore/skills/conv-restore/references/README.md`](./conv-restore/skills/conv-restore/references/README.md) for Chinese documentation.

---

## 5. Maximum Context Window (`900K` / `1M` Tier)

Although 1M-window models advertise a 1,000,000-token context window, `agy` `v1.5.10` triggers automatic conversation compaction from `CheckpointConfig.MaxTokenLimit`, which the server catalog sets around 500,000 to 512,000 tokens by default. Raising the compaction threshold to `900,000` tokens in public `agy` `v1.5.10` requires both selecting `Gemini 4 Argon` and setting `AGY_CLI_AUTO_COMPACT_OVERRIDE=900000`.

```bash
# Unlock a 900,000-token auto-compaction threshold on Gemini 4 Argon in agy v1.5.10
AGY_CLI_AUTO_COMPACT_OVERRIDE=900000 agy --model "Gemini 4 Argon (High)"

# Percentage syntax is also accepted by store.AutoCompactOverride and computes 90% of model maxTokens
AGY_CLI_AUTO_COMPACT_OVERRIDE=90% agy --model "Gemini 4 Argon (High)"
```

To make the override persistent across interactive shells, export `AGY_CLI_AUTO_COMPACT_OVERRIDE=900000` in `~/.zshrc` or `~/.bashrc`.

### Why `"contextWindow": "900K"` Alone Still Compacts Around 500K in `agy` `v1.5.10`

Disassembling `store.(*Store).ContextWindow` in `third_party/jetski/cli/store/context_window.go` at `0x102ef5bf0`, `ServerBackend.buildCascadeConfigInternal` in `third_party/jetski/cli/backend/server.go` at `0x1022e6f68`, and `utils.GetCheckpointConfigForModelInfo` at `0x1013f2900` reveals two client-side gates ahead of `settings.json`.

- **Hardcoded `Gemini 4 Argon` model whitelist.** `Store.ContextWindow` and `SelectorModel.offersContextWindow` at `0x103144fa0` extract the active model family via `ModelEffortDisplay` at `0x102f43090` and check membership in the global slice at `0x10d3dcbc0`, which has length `1` and contains only `"Gemini 4 Argon"`. Selecting `"Gemini 4 Carbon (High)"` or `"Orchestrator (Carbon)"` causes `Store.ContextWindow` to return `""` immediately at `0x102ef5cd8`, before `AutoCompactOverride` at `0x102ef5cf0` is even called. Consequently, `Gemini 4 Carbon` ignores both `settings.json` and `AGY_CLI_AUTO_COMPACT_OVERRIDE` in `v1.5.10` and always compacts at its server-supplied `CheckpointConfig.MaxTokenLimit` of roughly 500,000 to 512,000 tokens.
- **Unwired `"context-window-options"` feature flag on `settings.json` presets.** When `Gemini 4 Argon` is active, `Store.ContextWindow` first checks `AGY_CLI_AUTO_COMPACT_OVERRIDE` via `AutoCompactOverride` at `0x102ef59c0` and returns the override immediately at `0x102ef5d24` when non-empty, which `buildCascadeConfigInternal` writes into `CascadeConfig.CheckpointConfig.MaxTokenLimit` at `0x1022e6fbc`. When `AGY_CLI_AUTO_COMPACT_OVERRIDE` is unset, `Store.ContextWindow` at `0x102ef5cfc..0x102ef5d68` inlines `store.go` line 521 and asserts `*types.FeatureFlagReader` at `0x10a1ad5b0` on `Store+0x58` to check the `"context-window-options"` flag. Because no production backend injects `types.FeatureFlagReader` into `Store+0x58` and only the unit-test helper `Store.SetFeatureFlagForTest` at `0x102f496d0` populates that slot, the Go compiler eliminates the preset branch at compile time so every exit jumps to `0x102ef5d30` and returns `""`, leaving `Store.ContextWindowChoice` at `0x102ef5b90` with zero callers in the binary.

### Forward-Compatible `settings.json` Preset Tiers in `agy` `v1.5.10`

Keeping `"contextWindow": "900K"` in `~/.gemini/antigravity-cli/settings.json` on macOS/Linux or `%USERPROFILE%\.gemini\antigravity-cli\settings.json` on Windows passes schema validation cleanly and prepares your configuration for when Google enables the `"context-window-options"` feature flag in a public release.

```json
{
  "$schema": "https://antigravity.google/schemas/v1/settings.json",
  "contextWindow": "900K"
}
```

Verified against `backend.ParsePresetContextWindow` at `0x1022d68a0`, `backend.ContextWindow.MaxTokenLimit` at `0x1022d6970`, `backend.ContextWindow.QuotaMultiplier` at `0x1022d6a20`, and `model.renderQuotaNote`, the preset table defines three valid strings.

| `"contextWindow"` | `MaxTokenLimit` | `/model` UI Quota Note |
| :--- | :--- | :--- |
| `"256K"` | `256,000` | Baseline with no extra note |
| `"512K"` | `512,000` | `about 1.3x quota per turn` |
| `"900K"` | `900,000` | `about 1.8x quota per turn` |

- **Valid preset enum.** Write `"900K"` rather than `"1M"`, because `"1M"` is rejected by `ParsePresetContextWindow` and falls back to `"256K"`.
- **System write-protection boundary.** Edit `~/.gemini/antigravity-cli/settings.json` from a regular terminal outside `agy`, because `agy` blocks agent `write_file` and `edit_file` calls on `settings.json`.
- **CLI `--settings` flag.** Omit `"$schema"` when passing inline JSON to `agy --settings '{...}'` because the CLI flag rejects unknown keys, whereas `"$schema"` loads cleanly from `settings.json` on disk.
- **Quota multiplier note.** The strings `about 1.3x quota per turn` and `about 1.8x quota per turn` are client-side estimates rendered in `/model`, while actual quota deduction is settled server-side.

### One-Line Atomic Merge for `settings.json`

Run this snippet in a regular terminal to create or update `~/.gemini/antigravity-cli/settings.json` atomically while preserving all existing fields and validating the resulting JSON.

```bash
python3 -c '
import json, os, tempfile
p = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p, "r", encoding="utf-8")) if os.path.exists(p) else {}
merged = {
    "$schema": "https://antigravity.google/schemas/v1/settings.json",
    **d,
    "contextWindow": "900K",
}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, p)
' && python3 -m json.tool ~/.gemini/antigravity-cli/settings.json
```

### Local Binary Verification on macOS ARM64 `agy` `v1.5.10`

You can verify both the single-element `["Gemini 4 Argon"]` whitelist at `0x10d3dcbc0` and the zero-caller status of `Store.ContextWindowChoice` at `0x102ef5b90` directly against your local `agy` binary.

```bash
/usr/bin/python3 -c '
import shutil, struct
agy_path = shutil.which("agy") or "/Users/Shared/agy"
with open(agy_path, "rb") as f:
    b = f.read()
ncmds = struct.unpack_from("<I", b, 16)[0]
off, segs = 32, []
for _ in range(ncmds):
    cmd, cmdsize = struct.unpack_from("<II", b, off)
    if cmd == 0x19:
        seg = b[off+8:off+24].split(b"\x00")[0].decode()
        vma, vms, fo, fs = struct.unpack_from("<QQQQ", b, off+24)
        segs.append((seg, vma, fo, fs))
        if seg == "__TEXT":
            nsects = struct.unpack_from("<I", b, off+64)[0]
            for i in range(nsects):
                so = off + 72 + i * 80
                if b[so:so+16].split(b"\x00")[0] == b"__text":
                    t_va, t_sz = struct.unpack_from("<QQ", b, so+32)
                    t_fo = struct.unpack_from("<I", b, so+48)[0]
    off += cmdsize
va2fo = lambda va: next(fo + (va - vma) for _, vma, fo, fs in segs if vma <= va < vma + fs)
ptr, slen, scap = struct.unpack_from("<QQQ", b, va2fo(0x10d3dcbc0))
sptr, sslen = struct.unpack_from("<QQ", b, va2fo(ptr))
print("whitelist:", [b[va2fo(sptr):va2fo(sptr)+sslen].decode()], f"(len={slen}, cap={scap})")
callers = [hex(t_va + p - t_fo) for p in range(t_fo, t_fo + t_sz, 4)
           if (struct.unpack_from("<I", b, p)[0] >> 26) == 0b100101 and
           (t_va + p - t_fo) + ((struct.unpack_from("<I", b, p)[0] & 0x3ffffff) - ((1 << 26) if (struct.unpack_from("<I", b, p)[0] & 0x2000000) else 0)) * 4 == 0x102ef5b90]
print("ContextWindowChoice (0x102ef5b90) callers:", callers)
'
```

See [`docs/max-context-window_zh.md`](./docs/max-context-window_zh.md) for Chinese documentation.

---

## License

[MIT](./LICENSE)
