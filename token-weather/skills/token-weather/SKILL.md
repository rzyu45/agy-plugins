---
name: token-weather
description: >-
  Weather-themed status line for Antigravity CLI (agy). Use when the user wants
  to preview, configure, or troubleshoot the three-line Token Weather status
  band (execution mode, active model, and conversation name, 5h/weekly model
  quota and Google Colab remaining compute units, context window weather
  forecast, turn sparkline, token delta, and prompt cache TTL).
---

# Token Weather for Antigravity CLI

This skill bundles [token-weather.py](./scripts/token-weather.py), a zero-dependency Python renderer for `agy`'s native `statusLine` pipeline modeled on Claude Code's `token-weather` mod.

By default (`lines=3`), the status band is organized into three lines:
1. **Mode, model, and conversation** - execution mode badge (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`, `[VIM:*]`), active model and effort, agent state, active conversation name (`conversation_title`), and session metadata (active subagents/tasks, cost, Git branch).
2. **Model usage and Colab usage** - 5-hour and weekly model quota bars/countdowns alongside Google Colab remaining compute units (`Colab 1000 CU`, plus hourly burn rate and active VM count when a session is running).
3. **Context usage** - weather forecast tier (`Clear`, `Cloudy`, `Showers`, `Storm`, `Compact soon`), context fill percentage, progress bar, used/window tokens, 8-turn sparkline with token delta, and prompt cache TTL.

## Quick Actions

1. Preview all five weather tiers, execution modes, model quota bars, and Colab compute units:
   - Default 3-line layout: `python3 ./scripts/token-weather.py --preview`
   - Chinese labels: `python3 ./scripts/token-weather.py --preview --lang zh`
   - Compact 2-line or 1-line layouts: `python3 ./scripts/token-weather.py --preview --lines 2` or `--lines 1`
2. Refresh cached Google Colab compute-unit balance on demand:
   - `python3 ./scripts/token-weather.py --refresh-colab`
3. Persist user options in `~/.config/agy-token-weather/config.json` without changing the `/statusline` command:
   - `python3 ./scripts/token-weather.py --set lang=zh`
   - `python3 ./scripts/token-weather.py --set lines=3` (`1`, `2`, or `3`)
   - `python3 ./scripts/token-weather.py --set show_mode=non-default`
   - `python3 ./scripts/token-weather.py --set show_title=false`
   - `python3 ./scripts/token-weather.py --set quota_metric=remaining`
   - `python3 ./scripts/token-weather.py --set show_quota=false`
   - `python3 ./scripts/token-weather.py --set show_colab=false`
   - `python3 ./scripts/token-weather.py --set colab_auth=oauth2` (`auto`, `oauth2`, or `adc`)
   - `python3 ./scripts/token-weather.py --set colab_ttl=180`
   - `python3 ./scripts/token-weather.py --show-config`
4. Bind in an interactive `agy` session using `/statusline <path-to-token-weather.py>`, or refer to [README.md](./references/README.md) for `stack_with_default` and JSON configuration examples.
