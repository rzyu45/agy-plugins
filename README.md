# Antigravity CLI (`agy`) Plugins

Zero-dependency productivity plugins for Google Antigravity CLI (`agy`), modeled on local Claude Code mods:

- **[`done-notify`](./done-notify)** — macOS Notification Center banner + system sound when an `agy` turn finishes, fails, completes a background task, or pauses on `ask_question` while the active CLI window/tab is not focused.
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

## 1. `done-notify` (macOS Unfocused Completion & Failure Alerts)

[`done-notify`](./done-notify) registers lightweight `Stop` and `PreToolUse` (`^ask_question$`) lifecycle hooks with zero mid-turn overhead:

- **Turn completion & background task wakeups**: Posts `<project> · done in <duration>` (or `task done in <duration>` for reactive background task wakeups) with a plain-text summary of the final answer and plays `Glass`.
- **Turn failure or budget/step limits**: Posts `<project> · turn failed after <duration>` with the error summary and plays `Sosumi`.
- **Interactive question prompts (`ask_question`)**: Alerts `<project> · waiting for input` when `agy` pauses for a multiple-choice answer while unfocused.
- **Smart terminal & tab focus guard**: Queries `/usr/bin/lsappinfo` (~3 ms, zero TCC/Accessibility prompts) plus `/bin/ps` ancestry to check the frontmost macOS app (VS Code, Cursor, Terminal.app, iTerm2, Ghostty, WezTerm, Kitty, Warp, Alacritty, Zed) and screen-lock state (`loginwindow`). When the terminal app itself is frontmost, it further checks active tab/pane focus for `tmux`, `Terminal.app`, `iTerm2`, `WezTerm`, and `Kitty`, staying silent when you are already looking at the current CLI session. Automatically skips user-aborted turns (`USER_CANCELED`), subagent child runs, and headless `agy -p` / `acp` invocations.

### Quick Test & Configuration

Preferences persist in `~/.config/agy-done-notify/config.json` and take effect immediately on the next hook trigger:

```bash
# Send an immediate test banner + sound (ignores focus)
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test

# Send a test failure alert (Sosumi)
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --error

# Test focus gating: switch to another app/tab within 3 seconds
sleep 3 && python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --respect-focus

# Inspect detected host terminal and live focus status
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --status

# Common settings
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set lang=zh
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set sound=Glass --set error_sound=Sosumi
python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set focus_mode=tab   # tab | app | always
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
