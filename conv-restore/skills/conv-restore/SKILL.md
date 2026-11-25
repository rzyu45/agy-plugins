---
name: conv-restore
description: >-
  Diagnose and restore disappeared, missing, or hidden Antigravity CLI (agy)
  conversations (恢复/还原消失或不见的agy历史对话、/resume看不到项目对话). Handles all
  four known agy-cli disappearance modes: (A) default-cli-project.json workspace
  pollution hiding projects whose URI sorts after the polluter (#1657), (B)
  0-step SQLite .db stub files shadowing non-empty .pb conversations (#1602),
  (C) stale, missing, or zeroed summary rows in conversation_summaries.db and
  jetbox_summaries_proto.pb, and (D) archived annotations. Use whenever a user
  reports missing conversations, empty /resume lists, or wants to verify store
  integrity or disable this temporary workaround once agy fixes the bug upstream.
---

# Conversation Restore for Antigravity CLI (`agy`)

This skill bundles [conv-restore.py](./scripts/conv-restore.py), a zero-dependency Python 3 standard-library CLI for diagnosing, safely backing up, restoring, and verifying disappeared Antigravity CLI (`agy`) conversations.

## Known Disappearance Modes

1. **Mode A (`default-cli-project.json` workspace pollution, upstream `#1657`)**: `~/.gemini/config/projects/default-cli-project.json` retains a stale `projectResources.resources[].folderUri`. Every `agy` launch mounts it alongside the active working directory, sorts `WorkspaceURIs` lexicographically by percent-encoded `file://` URI, and groups `/resume` by `WorkspaceURIs[0]`, hiding any project whose URI sorts after the polluter. Because `jetbox_summaries_proto.pb` takes precedence over both `conversation_summaries.db` and `conversations/<cid>.db` on startup, repairing `.db` files alone is insufficient unless `jetbox_summaries_proto.pb` is also rebuilt or removed offline.
2. **Mode B (0-step `.db` stub shadowing `.pb`, upstream `#1602`)**: A lock race leaves an empty `<cid>.db` (`steps == 0`, `trajectory_meta == (0, 0)`, empty `trajectory_metadata_blob`) beside a non-empty `<cid>.pb`. `SqliteStore` shadows `ProtoStore` and zeroes the summary. Moving verified 0-step stubs into a backup folder and removing stale summary caches allows native `reconcile_summaries.go` to restore them from `.pb`.
3. **Mode C (stale, missing, or zeroed summary rows)**: A healthy `<cid>.db` with `steps > 0` has no row in `conversation_summaries.db` or is stuck at `step_count = 0` / `0001-01-01`. Removing `jetbox_summaries_proto.pb` and stale zeroed rows lets `agy` natively rebuild clean summaries on next launch.
4. **Mode D (archived annotations)**: `annotations/<cid>.pbtxt` has `archived: true`, hiding the conversation from default `/resume` lists. Recoverable via `ConversationService/UpdateConversation` (`{"annotations": {"archived": false}}`) upon explicit user confirmation.

## Workflow & Safety Guards

Always follow this sequence:

1. **Diagnose first (read-only, WAL-safe)**:
   - `python3 ./scripts/conv-restore.py --diagnose`
   - Optional filters: `--strip-uri /path/to/polluter`, `--project <name-or-uri>`, `--json`, `--verbose`.
   - All SQLite inspections copy `.db`, `-wal`, and `-shm` to a temporary directory before reading so live databases are never mutated or locked.
2. **Preview planned changes (`--dry-run`)**:
   - `python3 ./scripts/conv-restore.py --dry-run`
   - Confirm which conversations are hidden vs secondary-polluted, which genuine single-workspace conversations are preserved untouched, and whether any `.pb`-only legacy conversations are skipped.
3. **Restore with automatic timestamped backup**:
   - **Preferred (offline, after exiting all `agy` processes)**:
     - `python3 ./scripts/conv-restore.py --restore`
     - Automatically backs up every file before modification into `/tmp/agy-conv-restore-backup-<YYYYMMDD_HHMMSS>/`, clears `projectResources.resources` in `default-cli-project.json`, isolates confirmed Mode B 0-step stubs into `<backup>/stub_dbs/`, strips Mode A polluter URIs from `trajectory_metadata_blob` (with pre-write Protobuf round-trip validation and post-write `PRAGMA integrity_check`), updates `conversation_summaries.db` and `conversations_fts`, and removes stale `jetbox_summaries_proto.pb` and `cache/conversation_metadata.json` so the next `agy` launch reconciles cleanly.
   - **While running inside `agy` (partial live repair + user offline finalize)**:
     - `--restore` refuses by default when any `agy` process is running because the live process holds `jetbox_summaries_proto.pb` in RAM and overwrites it on exit, and `~/.gemini/config/projects/default-cli-project.json` is write-protected from agent sessions.
     - Run `python3 ./scripts/conv-restore.py --restore --live-inactive-only` to back up and repair all inactive `.db` files while skipping open/active swarm conversations, then instruct the user to exit all `agy` sessions and run the printed one-line offline finalize command in a regular terminal:
       `python3 ~/.gemini/config/plugins/conv-restore/skills/conv-restore/scripts/conv-restore.py --finalize`
   - **Mode D unarchive (explicit opt-in only)**:
     - `python3 ./scripts/conv-restore.py --restore --unarchive --cid <conversation-id>` (or `--all-archived` only after user confirmation).
4. **Verify after restoration**:
   - `python3 ./scripts/conv-restore.py --verify`
   - Optional isolated native-reconciliation check in `/tmp` (launches a temporary `agy --hub` against copies of repaired `.db` files without touching live data):
     `python3 ./scripts/conv-restore.py --verify --verify-staging`

## Safety Invariants

- Never delete `brain/` transcripts, `.pb` conversation blobs, or non-empty `.db` files.
- Never strip a workspace URI if doing so would leave a conversation with zero workspaces.
- Never mutate legacy `.pb`-only conversations in place.
- Never attempt to bypass `~/.gemini/config/` write protection from inside an agent session; always print the exact terminal command for the user to run outside `agy`.

## Disabling or Removing Once `agy` Fixes the Bug

This plugin is a temporary workaround for upstream `agy-cli` conversation-index bugs. Once `agy` fixes them:

- Disable immediately (unloads this skill completely while keeping it listed; survives updates):
  `agy plugin disable conv-restore`
- Re-enable later if needed:
  `agy plugin enable conv-restore`
- Uninstall permanently:
  `agy plugin uninstall conv-restore`

See [README.md](./references/README.md) for Chinese documentation, technical details, and troubleshooting.
