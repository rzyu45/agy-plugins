# Antigravity CLI (`agy`) Plugins

Zero-dependency productivity plugins for Google Antigravity CLI (`agy`), modeled on local Claude Code mods:

- **[`done-notify`](./done-notify)** — macOS Notification Center banner + system sound when an `agy` turn finishes, fails, asks a question (`ask_question`), or pauses waiting for user permission approval (alerts even while focused by default, with optional terminal/tab focus suppression).
- **[`token-weather`](./token-weather)** — Live three-line weather-themed `statusLine` band organized into (1) execution mode (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`, `[VIM:*]`), active model, and session metadata, (2) 5-hour & weekly model quota usage alongside Google Colab remaining compute units, and (3) context window fill tier, 8-turn sparkline, token delta, and prompt cache TTL countdown.
- **[Maximum Context Window (`900K` / `1M` Tier)](#3-maximum-context-window-900k--1m-tier)** — Persistent `~/.gemini/antigravity-cli/settings.json` configuration for `agy`'s maximum `900K` context preset, valid preset enums, atomic merge snippet, and per-turn quota notes.

---

## Installation

Clone this repository and install either or both plugins with `agy plugin install`:

```bash
git clone https://github.com/rzyu45/agy-plugins.git
agy plugin install ./agy-plugins/done-notify
agy plugin install ./agy-plugins/token-weather
```

Verify installed plugins:

```bash
agy plugin list
```

---

## 1. `done-notify` (macOS Completion, Question & Permission Alerts)

[`done-notify`](./done-notify) wires lightweight `SessionStart`, `PreInvocation`, `PreToolUse` (`^ask_question$`), and `Stop` hooks with zero latency on auto-approved tools:

- **Turn completion & background task wakeups**: Posts `<project> · done in <duration>` (or `task done in <duration>` for reactive background task wakeups) with a plain-text summary of the final answer and plays `Glass`.
- **Turn failure or budget/step limits**: Posts `<project> · turn failed after <duration>` with the error summary and plays `Sosumi`.
- **Interactive question prompts (`ask_question`)**: Immediately posts `<project> · waiting for input` with the question text and plays `Ping` (`prompt_sound`) when `agy` pauses for an answer.
- **Permission confirmation prompts**: Tails the owning `agy` CLI session log and posts `<project> · waiting for permission` with `Ping` (`prompt_sound`) at the exact moment `agy` surfaces a tool confirmation dialog (unsandboxed commands, sensitive command checks, out-of-workspace file access/edits, MCP/URL/browser confirmations, `--mode default` tool approvals, or subagent approval queues). Auto-approved tools and commands covered by an existing `"Always allow"` rule never chime, and the watcher exits automatically when `agy` closes.
- **Always-on by default, with optional focus filtering**: Both `focus_mode` and `prompt_focus_mode` default to `always` so you get an audible cue even when the terminal window is focused. If you prefer silence while actively viewing the CLI, set `--set focus_mode=tab` (or `app`) to check the frontmost macOS app via `/usr/bin/lsappinfo` plus active tab/pane state for `tmux`, `Terminal.app`, `iTerm2`, `WezTerm`, and `Kitty`. Automatically skips `USER_CANCELED` turns, child subagent turns, SSH sessions, and headless `agy -p` / `acp` invocations.

### Quick Test & Configuration

Preferences persist in `~/.config/agy-done-notify/config.json` and take effect immediately without restarting `agy`:

```bash
# Test completion, question, permission, and failure alerts
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test
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
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set min_duration_sec=5
```

> **macOS Notification Settings**: Banners are dispatched via `/usr/bin/osascript` (**Script Editor** in macOS `System Settings -> Notifications -> Script Editor`). Ensure **Allow Notifications** and **Play sound for notifications** are enabled there.

See [`done-notify/skills/done-notify/references/README.md`](./done-notify/skills/done-notify/references/README.md) for Chinese documentation.

---

## 2. `token-weather` (Three-Line Mode, Quota/Colab & Context Status Band)

[`token-weather`](./token-weather) renders `agy`'s live `statusLine` JSON telemetry as a default 3-line adaptive ANSI status bar (with optional 2-line and 1-line compact modes):

1. **Line 1 — Mode and model**: Execution mode badge (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`, `[VIM:*]`), active model name with reasoning effort and live state (`thinking`), plus subagents, background tasks, session cost, and Git branch.
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
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set ascii=true
```

See [`token-weather/skills/token-weather/references/README.md`](./token-weather/skills/token-weather/references/README.md) for Chinese documentation.

---

## 3. Maximum Context Window (`900K` / `1M` Tier)

Set `"contextWindow": "900K"` in `~/.gemini/antigravity-cli/settings.json` (Windows: `%USERPROFILE%\.gemini\antigravity-cli\settings.json`) to switch `agy`'s global default context capacity to its maximum preset permanently:

```json
{
  "$schema": "https://antigravity.google/schemas/v1/settings.json",
  "contextWindow": "900K"
}
```

### Preset Tiers & Notes (`agy` `v1.5.9`)

Verified against `agy`'s `backend.ParsePresetContextWindow`, `backend.ContextWindow.MaxTokenLimit`, `backend.ContextWindow.QuotaMultiplier` (`context_window.go`), and `model.renderQuotaNote` (`model_selector_context_window.go`), `agy` recognizes exactly three preset strings:

| `"contextWindow"` | `MaxTokenLimit` | `/model` UI Quota Note (`QuotaMultiplier`) |
| :--- | :--- | :--- |
| `"256K"` (default) | `256,000` | Baseline (none) |
| `"512K"` | `512,000` | `about 1.3x quota per turn` |
| `"900K"` (`1M` tier) | `900,000` | `about 1.8x quota per turn` |

- **Use `"900K"`, never `"1M"`**: `"1M"` is not a valid enum in `ParsePresetContextWindow` and silently falls back to `"256K"`.
- **Run from a regular terminal outside `agy`**: `agy` guards `~/.gemini/antigravity-cli/settings.json` with a hardcoded system protection boundary (blocking agent `write_file`/`edit_file` calls because the file controls permissions and shell hooks), so apply the change in your own shell or editor.
- **CLI `--settings` flag**: Do not pass `"$schema"` inline to `agy --settings '{...}'` (the CLI flag rejects unknown keys); `"$schema"` loads cleanly from `settings.json` on disk.
- **Quota note**: The `1.3x` / `1.8x` values are the client's per-turn estimate shown in `/model` on models with selectable context presets; actual quota consumption is settled server-side.

### One-Line Atomic Merge (macOS / Linux)

Run this in a normal terminal to create or update `~/.gemini/antigravity-cli/settings.json` atomically while preserving all existing fields (`model`, `permissions`, `statusLine`, `trustedWorkspaces`, etc.) and validating the output JSON:

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

### Verification

Start a new `agy` session (or re-select your model in `/model`) and run `/context` inside `agy` to confirm the active context ceiling. If [`token-weather`](#2-token-weather-three-line-mode-quotacolab--context-status-band) is enabled, the status bar context denominator updates automatically on the next turn.

See [`docs/max-context-window_zh.md`](./docs/max-context-window_zh.md) for Chinese documentation.

---

## License

[MIT](./LICENSE)
