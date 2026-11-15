---
name: token-weather
description: >-
  Weather-themed context window status line for Antigravity CLI (agy). Use when
  the user wants to preview, configure, or troubleshoot the Token Weather status
  band (execution mode badge, weather forecast tiers, 5h and weekly quota usage,
  turn sparkline, token delta, and prompt cache TTL).
---

# Token Weather for Antigravity CLI

This skill bundles [token-weather.py](./scripts/token-weather.py), a zero-dependency Python renderer for `agy`'s native `statusLine` pipeline modeled on Claude Code's `token-weather` mod.

## Quick Actions

1. Preview all five weather tiers (`Clear`, `Cloudy`, `Showers`, `Storm`, `Compact soon`), execution mode badges (`[default]`, `[accept-edits]`, `[auto]`, `[plan]`), and 5-hour/weekly quota usage:
   - `python3 ./scripts/token-weather.py --preview`
   - Chinese labels: `python3 ./scripts/token-weather.py --preview --lang zh`
   - Two-line layout with quota bars: `python3 ./scripts/token-weather.py --preview --lines 2`
2. Persist user options in `~/.config/agy-token-weather/config.json` without changing the `/statusline` command:
   - `python3 ./scripts/token-weather.py --set lang=zh`
   - `python3 ./scripts/token-weather.py --set lines=2`
   - `python3 ./scripts/token-weather.py --set show_mode=non-default`
   - `python3 ./scripts/token-weather.py --set quota_metric=remaining`
   - `python3 ./scripts/token-weather.py --set show_quota=false`
   - `python3 ./scripts/token-weather.py --show-config`
3. Bind in an interactive `agy` session using `/statusline <path-to-token-weather.py>`, or refer to [README.md](./references/README.md) for `stack_with_default` and JSON configuration examples.
