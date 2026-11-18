---
name: safe-allow
description: >-
  Smart PreToolUse policy guard for Antigravity CLI (agy) and /swarm subagents
  that auto-approves verified safe read-only commands, local document builds,
  workspace edits/Python runs, read-only MCP/web queries, and authorized GPU
  server SSH/SCP/rsync operations while keeping destructive actions gated for
  confirmation. Use when inspecting safe-allow status, auditing recent
  decisions, dry-running a command check, or updating category switches.
---

# Safe Allow for Antigravity CLI

This skill bundles [safe-allow.py](./scripts/safe-allow.py), a zero-dependency `PreToolUse` hook guard and `settings.json` rule generator for `agy` and `/swarm` workers that eliminates repetitive `Ctrl+K` approval prompts for safe operations without lowering guardrails on destructive commands.

In `agy` `v1.5.x`, `PreToolUse` hooks in `hooks.json` run on the main coordinator session, while `/swarm` workers evaluate `~/.gemini/antigravity-cli/settings.json` (`permissions.allow` / `permissions.ask` / `permissions.deny`) directly. After configuring `safe-allow`, run `--sync-settings` once in a regular terminal outside `agy` so `/swarm` workers inherit the same policy.

## Quick Actions

1. Sync `safe-allow` categories and local SSH rules into `~/.gemini/antigravity-cli/settings.json` for `/swarm` workers (run in a regular terminal outside `agy`):
   - `python3 ./scripts/safe-allow.py --sync-settings`
   - Preview without writing: `python3 ./scripts/safe-allow.py --print-settings`
2. Check active configuration and recent decision counts:
   - `python3 ./scripts/safe-allow.py --status`
3. Inspect recent hook decisions (or only prompts gated with `ask`):
   - `python3 ./scripts/safe-allow.py --recent --limit 20`
   - `python3 ./scripts/safe-allow.py --recent --ask-only`
4. Dry-run a shell command check before running or debugging why a prompt appeared:
   - `python3 ./scripts/safe-allow.py --check-cmd "cd docs && make pdf" --bypass-sandbox`
5. Configure authorized remote SSH hosts and remote write prefixes (empty by default), then re-run `--sync-settings`:
   - `python3 ./scripts/safe-allow.py --add-ssh-target alice@gpu-server.example:2222`
   - `python3 ./scripts/safe-allow.py --add-ssh-prefix /data/alice`
   - `python3 ./scripts/safe-allow.py --clear-ssh-targets`
6. Update persistent preferences in `~/.config/agy-safe-allow/config.json`:
   - `python3 ./scripts/safe-allow.py --show-config`
   - `python3 ./scripts/safe-allow.py --set scope=subagents_only` (`all` or `subagents_only`)
   - `python3 ./scripts/safe-allow.py --set remote_gpu_ssh=false`
   - `python3 ./scripts/safe-allow.py --set enabled=false`
   - `python3 ./scripts/safe-allow.py --allow-cmd "my-safe-tool"`
   - `python3 ./scripts/safe-allow.py --reset-config`
6. Disable or re-enable the plugin completely via `agy`:
   - `agy plugin disable safe-allow`
   - `agy plugin enable safe-allow`
7. See [README.md](./references/README.md) for category details, red-flag gates, and troubleshooting.
