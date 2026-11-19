---
name: done-notify
description: >-
  macOS Notification Center banner and sound alert for Antigravity CLI (agy)
  when a turn finishes, fails, asks a question (ask_question), or waits on a
  tool/subagent permission prompt. Alerts regardless of focus by default, with
  optional terminal/tab focus suppression. Use when the user wants to test,
  inspect watcher/focus status, configure sounds/language/focus modes, or
  troubleshoot Done Notify notifications.
---

# Done Notify for Antigravity CLI

This skill bundles [done-notify.py](./scripts/done-notify.py), a zero-dependency macOS notifier wired into `agy`'s `SessionStart`, `PreInvocation`, `PreToolUse` (`ask_question`), and `Stop` lifecycle hooks, modeled on Claude Code's local `done-notify` mod with live permission-prompt monitoring and optional terminal/tab focus filtering (disabled by default so alerts always play even while focused).

## Quick Actions

1. Send an immediate test notification with sound:
   - Completion alert (`sound`, default `Glass`): `python3 ./scripts/done-notify.py --test`
   - Question prompt alert (`prompt_sound`, default `Ping`): `python3 ./scripts/done-notify.py --test --question`
   - Permission prompt alert (`prompt_sound`, default `Ping`): `python3 ./scripts/done-notify.py --test --permission`
   - Failure alert (`error_sound`, default `Sosumi`): `python3 ./scripts/done-notify.py --test --error`
2. Inspect detected terminal host, prompt watcher state, frontmost macOS app, and active configuration:
   - `python3 ./scripts/done-notify.py --status`
3. View or update persistent user preferences in `~/.config/agy-done-notify/config.json`:
   - `python3 ./scripts/done-notify.py --show-config`
   - `python3 ./scripts/done-notify.py --set lang=zh`
   - `python3 ./scripts/done-notify.py --set sound=Glass --set prompt_sound=Ping --set error_sound=Sosumi`
   - `python3 ./scripts/done-notify.py --set sound_mode=both`
   - `python3 ./scripts/done-notify.py --set focus_mode=always --set prompt_focus_mode=always`
   - Optional focus suppression when viewing the CLI: `python3 ./scripts/done-notify.py --set focus_mode=tab`
   - `python3 ./scripts/done-notify.py --set notify_on_question=true --set notify_on_permission=true`
   - `python3 ./scripts/done-notify.py --set show_conversation=true`
   - `python3 ./scripts/done-notify.py --set min_duration_sec=5`
   - `python3 ./scripts/done-notify.py --reset-config`
4. See [README.md](./references/README.md) for full behavior and troubleshooting details.
