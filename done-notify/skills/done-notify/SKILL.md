---
name: done-notify
description: >-
  macOS Notification Center banner and sound alert for Antigravity CLI (agy)
  when a turn finishes, fails, completes a background task, or pauses on
  ask_question while the current CLI is not focused. Use when the user wants
  to test, inspect focus detection, configure sounds/language/focus mode, or
  troubleshoot Done Notify notifications.
---

# Done Notify for Antigravity CLI

This skill bundles [done-notify.py](./scripts/done-notify.py), a zero-dependency macOS notifier wired into `agy`'s `Stop` and `PreToolUse` (`ask_question`) lifecycle hooks, modeled on Claude Code's local `done-notify` mod with terminal and tab focus detection.

## Quick Actions

1. Send an immediate test notification with sound (ignores focus):
   - Completion alert (`sound`, default `Glass`): `python3 ./scripts/done-notify.py --test`
   - Failure alert (`error_sound`, default `Sosumi`): `python3 ./scripts/done-notify.py --test --error`
   - Focus-gated test (switch away from the terminal within 3 seconds): `python3 -c 'import time; time.sleep(3)' && python3 ./scripts/done-notify.py --test --respect-focus`
2. Inspect detected terminal host, frontmost macOS app, and focus state:
   - `python3 ./scripts/done-notify.py --status`
3. View or update persistent user preferences in `~/.config/agy-done-notify/config.json`:
   - `python3 ./scripts/done-notify.py --show-config`
   - `python3 ./scripts/done-notify.py --set lang=zh`
   - `python3 ./scripts/done-notify.py --set sound=Glass --set error_sound=Sosumi`
   - `python3 ./scripts/done-notify.py --set sound_mode=both`
   - `python3 ./scripts/done-notify.py --set focus_mode=tab`
   - `python3 ./scripts/done-notify.py --set min_duration_sec=5`
   - `python3 ./scripts/done-notify.py --reset-config`
4. See [README.md](./references/README.md) for full behavior and troubleshooting details.
