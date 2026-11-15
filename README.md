# Antigravity CLI (`agy`) Plugins

Zero-dependency productivity plugins for Google Antigravity CLI (`agy`), modeled on local Claude Code mods:

- **[`done-notify`](./done-notify)** — macOS Notification Center banner + system sound when an `agy` turn finishes or fails while unfocused, or when `agy` pauses waiting for a question answer (`ask_question`) or user permission approval.
- **[`token-weather`](./token-weather)** — Live weather-themed `statusLine` band showing execution mode (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`, `[VIM:*]`), context window fill tier, 5-hour & weekly quota usage and pacing, 8-turn sparkline, token delta, prompt cache TTL countdown, active subagents/tasks, cost, and Git branch.

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

- **Turn completion & background task wakeups**: Posts `<project> · done in <duration>` (or `task done in <duration>` for reactive background task wakeups) with a plain-text summary of the final answer and plays `Glass` when the CLI is unfocused.
- **Turn failure or budget/step limits**: Posts `<project> · turn failed after <duration>` with the error summary and plays `Sosumi` when the CLI is unfocused.
- **Interactive question prompts (`ask_question`)**: Immediately posts `<project> · waiting for input` with the question text and plays `Ping` (`prompt_sound`) when `agy` pauses for an answer.
- **Permission confirmation prompts**: Tails the owning `agy` CLI session log and posts `<project> · waiting for permission` with `Ping` (`prompt_sound`) at the exact moment `agy` surfaces a tool confirmation dialog (unsandboxed commands, sensitive command checks, out-of-workspace file access/edits, MCP/URL/browser confirmations, `--mode default` tool approvals, or subagent approval queues). Auto-approved tools and commands covered by an existing `"Always allow"` rule never chime, and the watcher exits automatically when `agy` closes.
- **Independent focus controls (`focus_mode` & `prompt_focus_mode`)**: Turn-end alerts default to `focus_mode=tab`, querying `/usr/bin/lsappinfo` (~3 ms, zero TCC/Accessibility prompts) plus `/bin/ps` ancestry and active tab/pane state (`tmux`, `Terminal.app`, `iTerm2`, `WezTerm`, `Kitty`) so you are not interrupted while already viewing the CLI. Mid-turn blocking prompts (`ask_question` and permission requests) default to `prompt_focus_mode=always` so integrated terminals such as VS Code still chime even when another editor tab or window is frontmost. Automatically skips `USER_CANCELED` turns, child subagent turns, SSH sessions, and headless `agy -p` / `acp` invocations.

### Quick Test & Configuration

Preferences persist in `~/.config/agy-done-notify/config.json` and take effect immediately without restarting `agy`:

```bash
# Test completion, question, permission, and failure alerts (ignores focus)
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --question
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --permission
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --error

# Test focus gating: switch to another app/tab within 3 seconds
sleep 3 && python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --respect-focus

# Inspect detected host terminal, prompt watcher status, and live focus state
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --status

# Common settings
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set lang=zh
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set sound=Glass --set prompt_sound=Ping --set error_sound=Sosumi
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set focus_mode=tab          # tab | app | always
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set prompt_focus_mode=always # always | tab | app
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set min_duration_sec=5
```

> **macOS Notification Settings**: Banners are dispatched via `/usr/bin/osascript` (**Script Editor** in macOS `System Settings -> Notifications -> Script Editor`). Ensure **Allow Notifications** and **Play sound for notifications** are enabled there.

See [`done-notify/skills/done-notify/references/README.md`](./done-notify/skills/done-notify/references/README.md) for Chinese documentation.

---

## 2. `token-weather` (Context & Quota Forecast Status Line)

[`token-weather`](./token-weather) renders `agy`'s live `statusLine` JSON telemetry as a 1- or 2-line adaptive ANSI status bar:

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
# Preview all 5 weather tiers (1-line and 2-line layouts)
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --preview
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --preview --lines 2

# Common settings
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set lang=zh
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set lines=2
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set quota_metric=remaining   # used | remaining
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set show_mode=always         # always | non-default | off
python3 ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py --set ascii=true
```

See [`token-weather/skills/token-weather/references/README.md`](./token-weather/skills/token-weather/references/README.md) for Chinese documentation.

---

## License

[MIT](./LICENSE)
