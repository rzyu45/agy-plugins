#!/usr/bin/env python3
"""Smart PreToolUse policy guard for Antigravity CLI (agy) and /swarm subagents.

Evaluates tool calls against five configurable safety categories while keeping
destructive commands, credential reads/writes, protected config edits, and
unrecognized unsandboxed operations gated with ``"ask"``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import posixpath
import re
import shlex
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

BENIGN_UI_AND_SWARM_TOOLS = {
    "ask_question",
    "swarm",
    "post_message",
    "read_board",
    "swarm_wait",
    "invoke_subagent",
    "define_subagent",
    "manage_subagents",
    "send_message",
    "schedule",
    "manage_task",
    "run_workflow",
    "exit_plan_mode",
    "list_resources",
    "read_resource",
    "task_boundary",
    "command_status",
    "notify_user",
    "enter_plan_mode",
}

WRITE_EDIT_TOOLS = {
    "write_file",
    "write_to_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
}

READ_INSPECT_TOOLS = {
    "read_file",
    "view_file",
    "view_file_outline",
    "view_code_item",
    "list_dir",
    "find_by_name",
    "grep_search",
}

ZOTERO_READ_TOOLS = {
    "search",
    "fetch",
    "zotero_get_annotations",
    "zotero_get_notes",
    "zotero_search_notes",
    "zotero_get_page_layout",
    "zotero_get_item_metadata",
    "zotero_get_item_fulltext",
    "zotero_get_attachment_path",
    "zotero_get_collections",
    "zotero_get_collection_items",
    "zotero_get_item_children",
    "zotero_get_items_children",
    "zotero_get_tags",
    "zotero_list_libraries",
    "zotero_list_feeds",
    "zotero_get_feed_items",
    "zotero_get_recent",
    "zotero_get_item_related",
    "zotero_find_related_papers",
    "zotero_library_coverage",
    "zotero_read_pdf_pages",
    "zotero_search_items",
    "zotero_search_by_tag",
    "zotero_search_by_citation_key",
    "zotero_advanced_search",
    "zotero_semantic_search",
    "zotero_synthesize_annotations",
    "zotero_export_bibliography",
    "zotero_find_duplicates",
    "zotero_get_pdf_outline",
    "zotero_get_search_database_status",
    "zotero_search_collections",
    "scite_enrich_item",
    "scite_enrich_search",
    "scite_check_retractions",
}

KNOWN_MCP_SERVERS = ("paper-search-mcp", "drawio", "zotero")

DANGEROUS_COMMANDS = {
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "srm",
    "trash",
    "truncate",
    "dd",
    "mkfs",
    "fdisk",
    "diskutil",
    "sudo",
    "su",
    "doas",
    "osascript",
    "launchctl",
    "csrutil",
    "spctl",
    "dscl",
    "mount",
    "umount",
    "networksetup",
    "scutil",
    "pfctl",
    "iptables",
    "kill",
    "killall",
    "pkill",
    "crontab",
    "chown",
    "chgrp",
    "eval",
}

DANGEROUS_ENV_KEYS = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
    "GIT_SSH_COMMAND",
    "GIT_EXEC_PATH",
    "PROMPT_COMMAND",
    "IFS",
    "PATH",
    "CDPATH",
}

READONLY_COMMANDS = {
    "true",
    "false",
    "echo",
    "printf",
    "ls",
    "ll",
    "la",
    "dir",
    "tree",
    "pwd",
    "wc",
    "stat",
    "file",
    "du",
    "df",
    "which",
    "whereis",
    "type",
    "uname",
    "sw_vers",
    "hostname",
    "id",
    "whoami",
    "date",
    "uptime",
    "printenv",
    "ps",
    "pgrep",
    "lsof",
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "nl",
    "tac",
    "rev",
    "cut",
    "paste",
    "uniq",
    "tr",
    "fold",
    "column",
    "fmt",
    "comm",
    "diff",
    "cmp",
    "strings",
    "xxd",
    "hexdump",
    "od",
    "rg",
    "grep",
    "egrep",
    "fgrep",
    "ag",
    "ack",
    "fd",
    "locate",
    "realpath",
    "readlink",
    "dirname",
    "basename",
    "jq",
    "shasum",
    "sha256sum",
    "sha1sum",
    "md5",
    "md5sum",
    "cksum",
    "base64",
    "pdfinfo",
    "pdftotext",
    "pdffonts",
    "ffprobe",
    "mediainfo",
    "zipinfo",
    "nvidia-smi",
    "free",
}

GIT_READ_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "blame",
    "shortlog",
    "describe",
    "check-ignore",
}

ALLOWED_PYTHON_MODULES = {
    "py_compile",
    "pytest",
    "unittest",
    "json.tool",
    "compileall",
    "tokenize",
    "ast",
    "dis",
    "site",
}

ALLOWED_MAKE_DOC_TARGETS = {
    "pdf",
    "all",
    "clean",
    "view",
    "check",
    "docx",
    "html",
    "tex",
    "fig",
    "figs",
    "figures",
    "build",
    "test",
    "lint",
    "help",
}

REMOTE_READONLY_COMMANDS = {
    "ls",
    "ll",
    "la",
    "pwd",
    "cat",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "wc",
    "stat",
    "file",
    "du",
    "df",
    "ps",
    "pgrep",
    "nvidia-smi",
    "free",
    "uptime",
    "which",
    "uname",
    "whoami",
    "date",
    "echo",
    "printf",
    "true",
    "false",
    "sort",
    "uniq",
    "cut",
    "jq",
}

CONDA_ACTIVATE_PATH_RE = re.compile(r"(?:^|/)(?:conda\.sh|activate)$")
PYTHON_BIN_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
FD_DUP_RE = re.compile(r"^(?:[012])?>&(?:[012]|-)$")
REDIR_OP_RE = re.compile(r"^(?P<fd>&|[012])?(?P<op>>>?|<<?|<>)(?P<rest>.*)$")
PYTHON_C_UNSAFE_RE = re.compile(
    r"\b(?:rmtree|unlink|remove|rmdir|system|popen|execv|spawn|ctypes|socket)\b"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "scope": "all",  # "all" | "subagents_only"
    "categories": {
        "readonly_shell": True,
        "doc_and_edit": True,
        "mcp_and_web": True,
        "workspace_python": True,
        "remote_gpu_ssh": True,
    },
    "extra_allow_cmd_prefixes": [],
    "extra_trusted_write_roots": [],
    "remote_ssh": {
        "targets": [],
        "write_prefixes": ["/tmp"],
    },
    "audit_enabled": True,
    "audit_max_entries": 200,
}


class ShellParseError(ValueError):
    """Raised when a shell command uses unsupported or risky syntax."""


class ShellToken:
    """Shell word token preserving whether any quote or escape was used."""

    def __init__(self, text: str, quoted: bool = False) -> None:
        self.text = text
        self.quoted = quoted


class DecisionOutcome:
    """Structured result of evaluating a tool call or command."""

    def __init__(
        self,
        decision: str,
        reason: str = "",
        category: str = "",
        permission_overrides: Optional[List[str]] = None,
    ) -> None:
        self.decision = decision
        self.reason = reason
        self.category = category
        self.permission_overrides = permission_overrides or []

    def to_hook_response(self) -> Dict[str, Any]:
        """Convert outcome into the PreToolUse stdout JSON payload."""
        resp: Dict[str, Any] = {"decision": self.decision}
        if self.decision != "allow" and self.reason:
            resp["reason"] = self.reason
        if self.decision == "allow" and self.permission_overrides:
            resp["permissionOverrides"] = self.permission_overrides
        return resp


def _config_path() -> str:
    return os.environ.get(
        "SAFE_ALLOW_CONFIG",
        os.path.expanduser("~/.config/agy-safe-allow/config.json"),
    )


def _runtime_dir() -> str:
    override = os.environ.get("SAFE_ALLOW_RUNTIME_DIR", "").strip()
    if override:
        return override
    return f"/tmp/agy-safe-allow-{os.getuid()}"


def _audit_log_path() -> str:
    return os.path.join(_runtime_dir(), "audit.jsonl")


def _parse_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm in ("1", "true", "yes", "on"):
            return True
        if norm in ("0", "false", "no", "off"):
            return False
    return default


def _normalize_config(raw: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "enabled": DEFAULT_CONFIG["enabled"],
        "scope": DEFAULT_CONFIG["scope"],
        "categories": dict(DEFAULT_CONFIG["categories"]),
        "extra_allow_cmd_prefixes": [],
        "extra_trusted_write_roots": [],
        "remote_ssh": {
            "targets": [],
            "write_prefixes": ["/tmp"],
        },
        "audit_enabled": DEFAULT_CONFIG["audit_enabled"],
        "audit_max_entries": DEFAULT_CONFIG["audit_max_entries"],
    }
    if not isinstance(raw, dict):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = _parse_bool(raw["enabled"], True)

    scope = str(raw.get("scope", cfg["scope"])).strip().lower()
    if scope in ("all", "subagents_only"):
        cfg["scope"] = scope

    raw_cats = raw.get("categories")
    if isinstance(raw_cats, dict):
        for key in cfg["categories"]:
            if key in raw_cats:
                cfg["categories"][key] = _parse_bool(
                    raw_cats[key], DEFAULT_CONFIG["categories"][key]
                )

    for list_key in ("extra_allow_cmd_prefixes", "extra_trusted_write_roots"):
        val = raw.get(list_key)
        if isinstance(val, list):
            cfg[list_key] = [str(item).strip() for item in val if str(item).strip()]

    raw_remote = raw.get("remote_ssh")
    if isinstance(raw_remote, dict):
        raw_targets = raw_remote.get("targets")
        if isinstance(raw_targets, list):
            targets: List[str] = []
            for item in raw_targets:
                s = str(item).strip()
                if s and "@" in s and s not in targets:
                    targets.append(s)
            cfg["remote_ssh"]["targets"] = targets

        raw_prefixes = raw_remote.get("write_prefixes")
        if isinstance(raw_prefixes, list):
            prefixes: List[str] = []
            for item in raw_prefixes:
                s = str(item).strip()
                if s.startswith("/"):
                    norm = posixpath.normpath(s)
                    if norm not in prefixes:
                        prefixes.append(norm)
            if "/tmp" not in prefixes:
                prefixes.append("/tmp")
            cfg["remote_ssh"]["write_prefixes"] = prefixes

    if "audit_enabled" in raw:
        cfg["audit_enabled"] = _parse_bool(raw["audit_enabled"], True)

    if "audit_max_entries" in raw:
        try:
            max_entries = int(raw["audit_max_entries"])
            cfg["audit_max_entries"] = max(10, min(2000, max_entries))
        except (TypeError, ValueError):
            pass

    return cfg


def load_config() -> Dict[str, Any]:
    """Load persisted user configuration or return defaults."""
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return _normalize_config(data)
    except (OSError, ValueError):
        return _normalize_config(None)


def save_config(cfg: Dict[str, Any]) -> str:
    """Atomically persist configuration JSON with private permissions."""
    path = _config_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    normalized = _normalize_config(cfg)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return path


def append_audit(
    cfg: Dict[str, Any],
    is_subagent: bool,
    tool_name: str,
    target_summary: str,
    outcome: DecisionOutcome,
) -> None:
    """Append a bounded JSONL audit record for recent PreToolUse decisions."""
    if not cfg.get("audit_enabled", True):
        return
    path = _audit_log_path()
    parent = os.path.dirname(path)
    max_entries = int(cfg.get("audit_max_entries") or 200)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "isSubagent": is_subagent,
        "toolName": tool_name,
        "target": target_summary[:200],
        "decision": outcome.decision,
        "category": outcome.category,
        "reason": outcome.reason[:200],
    }
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        existing: List[str] = []
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                existing = [line for line in fh.read().splitlines() if line.strip()]
        existing.append(json.dumps(record, ensure_ascii=False))
        if len(existing) > max_entries:
            existing = existing[-max_entries:]
        fd, tmp_path = tempfile.mkstemp(prefix=".audit-", suffix=".jsonl", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(existing) + "\n")
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except OSError:
        pass


def _real_path(path: str) -> str:
    expanded = os.path.expanduser(path)
    try:
        return os.path.realpath(expanded)
    except OSError:
        return os.path.abspath(expanded)


def resolve_path(raw_path: str, base_cwd: Optional[str]) -> Optional[str]:
    """Resolve raw_path against base_cwd with symlink normalization."""
    s = (raw_path or "").strip()
    if not s:
        return None
    expanded = os.path.expanduser(s)
    if os.path.isabs(expanded):
        return _real_path(expanded)
    if not base_cwd:
        return None
    return _real_path(os.path.join(base_cwd, expanded))


def is_within_roots(resolved_path: Optional[str], roots: Sequence[str]) -> bool:
    """Return True if resolved_path sits equal to or beneath any normalized root."""
    if not resolved_path:
        return False
    for root in roots:
        if not root:
            continue
        try:
            if os.path.commonpath([resolved_path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def is_secret_or_key_path(resolved_path: Optional[str]) -> bool:
    """Return True if path points to credentials, SSH keys, or .env secrets."""
    if not resolved_path:
        return False
    norm = resolved_path.replace("\\", "/")
    parts = norm.split("/")
    base = os.path.basename(norm)
    ssh_dir = _real_path("~/.ssh")
    if is_within_roots(resolved_path, [ssh_dir]):
        return True
    if base == ".env" or base.startswith(".env."):
        return True
    if base.endswith((".pem", ".key", ".p12", ".pfx", ".keystore")):
        return True
    if base.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
        return True
    if ".ssh" in parts:
        return True
    return False


def is_protected_subpath(resolved_path: Optional[str]) -> bool:
    """Return True if path must never be auto-written or auto-edited."""
    if not resolved_path:
        return True
    if is_secret_or_key_path(resolved_path):
        return True

    norm = resolved_path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    base = os.path.basename(norm)

    if ".git" in parts and base not in (".gitignore", ".gitattributes"):
        return True

    for i in range(len(parts) - 1):
        if parts[i] in (".agents", ".agent", "_agents", "_agent"):
            if parts[i + 1] == "hooks.json" or parts[i + 1] == "plugins":
                return True

    protected_exact = [
        _real_path("~/.gemini/antigravity-cli/settings.json"),
        _real_path("~/.gemini/config/import_manifest.json"),
        _real_path("~/.gemini/config/config.json"),
    ]
    if resolved_path in protected_exact:
        return True

    protected_dirs = [
        _real_path("~/.gemini/config/plugins"),
        _real_path("~/.ssh"),
    ]
    if is_within_roots(resolved_path, protected_dirs):
        return True

    return False


def _build_allowed_roots(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[List[str], List[str], str]:
    """Return (write_roots, python_roots, initial_cwd)."""
    write_candidates: List[str] = []
    for ws in payload.get("workspacePaths") or []:
        if isinstance(ws, str) and ws.strip():
            write_candidates.append(ws.strip())

    artifact_dir = str(payload.get("artifactDirectoryPath") or "").strip()
    if artifact_dir:
        write_candidates.append(artifact_dir)

    raw_cwd = str(payload.get("cwd") or "").strip()
    initial_cwd = _real_path(raw_cwd) if raw_cwd else ""
    if not initial_cwd and write_candidates:
        initial_cwd = _real_path(write_candidates[0])
    if not initial_cwd:
        initial_cwd = _real_path(os.getcwd())

    write_candidates.extend(
        [
            "~/.gemini/antigravity-cli/brain",
            "/tmp",
            "/private/tmp",
            tempfile.gettempdir(),
        ]
    )
    write_candidates.extend(cfg.get("extra_trusted_write_roots") or [])

    write_roots = _dedup_real_paths(write_candidates)
    python_candidates = list(write_roots) + [
        "~/.gemini/config/skills",
        "~/.gemini/config/plugins",
        "~/.gemini/antigravity-cli/builtin/skills",
    ]
    python_roots = _dedup_real_paths(python_candidates)
    return write_roots, python_roots, initial_cwd


def _dedup_real_paths(paths: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for p in paths:
        rp = _real_path(p)
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def _scan_balanced_cmd_subst(script: str, start_idx: int) -> Tuple[str, int]:
    """Scan ``$(...)`` starting at ``$`` and return (inner_cmd, next_index)."""
    n = len(script)
    idx = start_idx + 2
    depth = 1
    in_sq = False
    in_dq = False
    buf: List[str] = []

    while idx < n:
        ch = script[idx]
        if in_sq:
            if ch == "'":
                in_sq = False
            buf.append(ch)
            idx += 1
            continue

        if ch == "\\":
            if idx + 1 < n:
                buf.append(ch)
                buf.append(script[idx + 1])
                idx += 2
                continue
            raise ShellParseError("trailing backslash in command substitution")

        if in_dq:
            if ch == '"':
                in_dq = False
                buf.append(ch)
                idx += 1
                continue
            if ch == "`":
                raise ShellParseError("backtick inside command substitution")
            if ch == "$" and idx + 1 < n and script[idx + 1] == "(":
                raise ShellParseError("nested command substitution is not auto-allowed")
            buf.append(ch)
            idx += 1
            continue

        if ch == "'":
            in_sq = True
            buf.append(ch)
            idx += 1
            continue
        if ch == '"':
            in_dq = True
            buf.append(ch)
            idx += 1
            continue
        if ch == "`":
            raise ShellParseError("backtick inside command substitution")
        if ch == "$" and idx + 1 < n and script[idx + 1] == "(":
            raise ShellParseError("nested command substitution is not auto-allowed")
        if ch == "(":
            depth += 1
            buf.append(ch)
            idx += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                return "".join(buf), idx + 1
            buf.append(ch)
            idx += 1
            continue

        buf.append(ch)
        idx += 1

    raise ShellParseError("unbalanced command substitution")


def split_shell_segments(script: str) -> Tuple[List[Tuple[str, str]], List[str], bool]:
    """Split a shell script into (op_before, segment_text) pairs.

    Also extracts ``$(...)`` substitutions and reports standalone ``&`` usage.
    """
    n = len(script)
    idx = 0
    in_sq = False
    in_dq = False
    buf: List[str] = []
    segments: List[Tuple[str, str]] = []
    substitutions: List[str] = []
    pending_op = ""
    has_bg_amp = False

    def _flush(next_op: str) -> None:
        nonlocal pending_op
        text = "".join(buf).strip()
        buf.clear()
        if text:
            segments.append((pending_op, text))
        pending_op = next_op

    while idx < n:
        ch = script[idx]

        if in_sq:
            if ch == "'":
                in_sq = False
            buf.append(ch)
            idx += 1
            continue

        if ch == "\\":
            if idx + 1 < n:
                buf.append(ch)
                buf.append(script[idx + 1])
                idx += 2
                continue
            raise ShellParseError("trailing backslash in shell command")

        if in_dq:
            if ch == '"':
                in_dq = False
                buf.append(ch)
                idx += 1
                continue
            if ch == "`":
                raise ShellParseError("backtick command substitution is not auto-allowed")
            if ch == "$" and idx + 1 < n and script[idx + 1] == "(":
                inner, next_idx = _scan_balanced_cmd_subst(script, idx)
                substitutions.append(inner)
                buf.append(script[idx:next_idx])
                idx = next_idx
                continue
            buf.append(ch)
            idx += 1
            continue

        if ch == "'":
            in_sq = True
            buf.append(ch)
            idx += 1
            continue

        if ch == '"':
            in_dq = True
            buf.append(ch)
            idx += 1
            continue

        if ch == "`":
            raise ShellParseError("backtick command substitution is not auto-allowed")

        if ch == "$" and idx + 1 < n and script[idx + 1] == "(":
            inner, next_idx = _scan_balanced_cmd_subst(script, idx)
            substitutions.append(inner)
            buf.append(script[idx:next_idx])
            idx = next_idx
            continue

        if ch in ("<", ">") and idx + 1 < n and script[idx + 1] == "(":
            raise ShellParseError("process substitution is not auto-allowed")

        if ch == "#" and (not buf or buf[-1].isspace()):
            while idx < n and script[idx] != "\n":
                idx += 1
            continue

        if ch == "\n":
            _flush("\n")
            idx += 1
            continue

        if ch == ";":
            if idx + 1 < n and script[idx + 1] == ";":
                raise ShellParseError("case separator ';;' is not supported")
            _flush(";")
            idx += 1
            continue

        if ch == "&":
            if idx + 1 < n and script[idx + 1] == "&":
                _flush("&&")
                idx += 2
                continue
            prev_char = buf[-1] if buf else ""
            next_char = script[idx + 1] if idx + 1 < n else ""
            if prev_char in (">", "<") or next_char == ">":
                buf.append(ch)
                idx += 1
                continue
            has_bg_amp = True
            _flush("&")
            idx += 1
            continue

        if ch == "|":
            if idx + 1 < n and script[idx + 1] == "|":
                _flush("||")
                idx += 2
                continue
            if idx + 1 < n and script[idx + 1] == "&":
                _flush("|")
                idx += 2
                continue
            _flush("|")
            idx += 1
            continue

        buf.append(ch)
        idx += 1

    if in_sq or in_dq:
        raise ShellParseError("unclosed quote in command")

    tail = "".join(buf).strip()
    if tail:
        segments.append((pending_op, tail))

    return segments, substitutions, has_bg_amp


def tokenize_segment(segment: str) -> List[ShellToken]:
    """Tokenize a single shell segment, splitting unquoted redirection operators."""
    tokens: List[ShellToken] = []
    n = len(segment)
    idx = 0

    while idx < n:
        while idx < n and segment[idx].isspace():
            idx += 1
        if idx >= n:
            break

        buf: List[str] = []
        was_quoted = False
        in_sq = False
        in_dq = False

        while idx < n:
            ch = segment[idx]
            if in_sq:
                if ch == "'":
                    in_sq = False
                else:
                    buf.append(ch)
                idx += 1
                continue

            if ch == "\\":
                was_quoted = True
                if idx + 1 < n:
                    nxt = segment[idx + 1]
                    if nxt != "\n":
                        buf.append(nxt)
                    idx += 2
                    continue
                raise ShellParseError("trailing backslash in segment")

            if in_dq:
                if ch == '"':
                    in_dq = False
                else:
                    buf.append(ch)
                idx += 1
                continue

            if ch == "'":
                was_quoted = True
                in_sq = True
                idx += 1
                continue

            if ch == '"':
                was_quoted = True
                in_dq = True
                idx += 1
                continue

            if ch.isspace():
                break

            if ch in (">", "<"):
                prefix = "".join(buf)
                if not was_quoted and prefix in ("", "0", "1", "2", "&"):
                    op_buf = [prefix, ch]
                    idx += 1
                    if idx < n and segment[idx] in (">", "<"):
                        op_buf.append(segment[idx])
                        idx += 1
                        if op_buf[-1] == "<" and idx < n and segment[idx] == "<":
                            op_buf.append("<")
                            idx += 1
                    elif idx < n and segment[idx] == "&":
                        op_buf.append("&")
                        idx += 1
                        if idx < n and (segment[idx] in ("0", "1", "2", "-")):
                            op_buf.append(segment[idx])
                            idx += 1
                    tokens.append(ShellToken("".join(op_buf), quoted=False))
                    buf.clear()
                    was_quoted = False
                    break
                if buf:
                    tokens.append(ShellToken(prefix, quoted=was_quoted))
                    buf.clear()
                    was_quoted = False
                op_buf = [ch]
                idx += 1
                if idx < n and segment[idx] in (">", "<"):
                    op_buf.append(segment[idx])
                    idx += 1
                    if op_buf[-1] == "<" and idx < n and segment[idx] == "<":
                        op_buf.append("<")
                        idx += 1
                elif idx < n and segment[idx] == "&":
                    op_buf.append("&")
                    idx += 1
                    if idx < n and (segment[idx] in ("0", "1", "2", "-")):
                        op_buf.append(segment[idx])
                        idx += 1
                tokens.append(ShellToken("".join(op_buf), quoted=False))
                break

            buf.append(ch)
            idx += 1

        if in_sq or in_dq:
            raise ShellParseError("unclosed quote in segment")

        if buf or was_quoted:
            tokens.append(ShellToken("".join(buf), quoted=was_quoted))

    return tokens


def inspect_and_strip_redirections(
    tokens: List[ShellToken],
    effective_cwd: Optional[str],
    write_roots: Sequence[str],
) -> List[str]:
    """Validate I/O redirections in a segment and return remaining command words."""
    words: List[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok.quoted:
            words.append(tok.text)
            idx += 1
            continue

        if FD_DUP_RE.match(tok.text):
            idx += 1
            continue

        m = REDIR_OP_RE.match(tok.text)
        if not m:
            words.append(tok.text)
            idx += 1
            continue

        op = m.group("op")
        target = m.group("rest")
        if op in ("<<", "<>"):
            raise ShellParseError(f"unsupported redirection operator '{op}'")

        if not target:
            if idx + 1 >= len(tokens):
                raise ShellParseError(f"missing target after redirection '{op}'")
            target = tokens[idx + 1].text
            idx += 2
        else:
            idx += 1

        if op == "<<<":
            continue

        if target == "/dev/null":
            continue

        resolved = resolve_path(target, effective_cwd)
        if op in (">", ">>"):
            if not is_within_roots(resolved, write_roots) or is_protected_subpath(resolved):
                raise ShellParseError(f"redirection target outside allowed write roots: {target}")
        elif op == "<":
            if is_secret_or_key_path(resolved):
                raise ShellParseError(f"input redirection reads protected secret file: {target}")

    return words


def _strip_env_and_wrappers(words: List[str]) -> Tuple[List[str], Optional[str]]:
    """Strip safe leading env assignments and wrappers (time, env, caffeinate).

    Returns (remaining_words, failure_reason).
    """
    cur = list(words)
    while cur:
        head = cur[0]
        if ENV_ASSIGN_RE.match(head):
            key = head.split("=", 1)[0]
            if key in DANGEROUS_ENV_KEYS or key.startswith("DYLD_"):
                return cur, f"sensitive environment variable override: {key}"
            cur.pop(0)
            continue

        base = os.path.basename(head)
        if base == "time":
            cur.pop(0)
            if cur and cur[0] == "-p":
                cur.pop(0)
            continue

        if head in ("env", "/usr/bin/env"):
            if len(cur) == 1:
                return cur, None
            cur.pop(0)
            while cur and cur[0].startswith("-"):
                flag = cur[0]
                if flag in ("-i", "-0", "--ignore-environment", "--null"):
                    cur.pop(0)
                    continue
                return cur, f"unsupported env flag: {flag}"
            continue

        if base == "caffeinate":
            cur.pop(0)
            while cur and cur[0].startswith("-"):
                flag = cur.pop(0)
                if flag in ("-w", "-t") and cur:
                    cur.pop(0)
            continue

        break

    return cur, None


def _git_subcommand_and_args(words: List[str]) -> Tuple[Optional[str], List[str], Optional[str], bool]:
    """Parse git global options; return (subcommand, rest_args, -C_dir, has_unsafe_config)."""
    idx = 1
    repo_dir: Optional[str] = None
    has_unsafe_cfg = False
    flags_with_val = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}

    while idx < len(words):
        w = words[idx]
        if w == "--":
            idx += 1
            break
        if w in flags_with_val:
            if idx + 1 >= len(words):
                return None, [], repo_dir, True
            val = words[idx + 1]
            if w == "-C":
                repo_dir = val
            elif w == "-c" and any(
                k in val.lower() for k in ("core.hookspath", "core.sshcommand", "core.fsmonitor")
            ):
                has_unsafe_cfg = True
            idx += 2
            continue
        if w.startswith(("--git-dir=", "--work-tree=", "--namespace=")):
            idx += 1
            continue
        if w.startswith("-c"):
            val = w[2:]
            if any(k in val.lower() for k in ("core.hookspath", "core.sshcommand", "core.fsmonitor")):
                has_unsafe_cfg = True
            idx += 1
            continue
        if w.startswith("-"):
            idx += 1
            continue
        return w, words[idx + 1 :], repo_dir, has_unsafe_cfg

    return None, [], repo_dir, has_unsafe_cfg


def check_red_flag_segment(words: List[str], allow_conda_source: bool = False) -> Optional[str]:
    """Return a non-empty reason string if the segment hits a red-flag hard gate."""
    if not words:
        return None

    base = os.path.basename(words[0])
    args = words[1:]

    if base in DANGEROUS_COMMANDS or base.startswith("mkfs."):
        return f"destructive or privileged command '{base}' requires confirmation"

    if base in ("source", "."):
        if allow_conda_source and len(args) == 1 and CONDA_ACTIVATE_PATH_RE.search(args[0]):
            return None
        return f"shell sourcing '{base}' requires confirmation"

    if base == "defaults":
        sub = args[0] if args else ""
        if sub in ("write", "delete", "import", "rename"):
            return f"defaults {sub} mutates macOS preferences"

    if base == "sysctl":
        if "-w" in args or any("=" in a for a in args):
            return "sysctl write operation requires confirmation"

    if base == "chmod":
        if any(
            a in ("-R", "--recursive", "777", "+s", "a+rwx") or "+s" in a or a.endswith("777")
            for a in args
        ):
            return "recursive or privileged chmod requires confirmation"
        return "chmod requires confirmation"

    if base == "find":
        bad_find = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for a in args:
            if a in bad_find:
                return f"find with {a} requires confirmation"

    if base == "rsync":
        for a in args:
            if a == "--delete" or a.startswith("--delete-") or a == "--remove-source-files":
                return f"rsync with {a} requires confirmation"

    if base in ("sed", "gsed"):
        for a in args:
            if a == "--in-place" or a.startswith("--in-place="):
                return "sed in-place editing requires confirmation"
            if a.startswith("-") and not a.startswith("--") and "i" in a[1:]:
                return "sed -i in-place editing requires confirmation"

    if base == "git":
        sub, gargs, _, unsafe_cfg = _git_subcommand_and_args(words)
        if unsafe_cfg:
            return "git config override for hooks or ssh requires confirmation"
        if sub in ("push", "clean", "rebase", "filter-branch", "filter-repo"):
            return f"git {sub} requires confirmation"
        if sub == "reset" and any(a in ("--hard", "--merge") for a in gargs):
            return "git reset --hard/--merge requires confirmation"
        if sub == "checkout":
            if "--" in gargs or "-f" in gargs or "--force" in gargs:
                return "git checkout discarding changes requires confirmation"
        if sub == "restore":
            if "--staged" not in gargs or "--worktree" in gargs or "-W" in gargs:
                return "git restore modifying worktree requires confirmation"
        if sub == "branch":
            if any(a in ("-D", "-d", "--delete") for a in gargs):
                return "git branch deletion requires confirmation"
        if sub == "tag":
            if any(a in ("-d", "--delete") for a in gargs):
                return "git tag deletion requires confirmation"
        if sub == "stash":
            if gargs and gargs[0] in ("drop", "clear"):
                return f"git stash {gargs[0]} requires confirmation"
        if sub == "config":
            if any(a in ("--global", "--system") for a in gargs):
                return "git global/system config modification requires confirmation"

    if base in ("pip", "pip3", "pipx"):
        if args and args[0] in ("install", "uninstall"):
            return f"{base} {args[0]} requires confirmation"

    if base == "uv" and len(args) >= 2 and args[0] == "pip" and args[1] in ("install", "uninstall"):
        return f"uv pip {args[1]} requires confirmation"

    if base in ("conda", "mamba", "micromamba"):
        if args and args[0] in ("install", "remove", "uninstall", "update", "upgrade", "create"):
            return f"{base} {args[0]} requires confirmation"

    if base == "brew":
        if args and args[0] in ("install", "uninstall", "remove", "upgrade", "reinstall", "tap", "untap"):
            return f"brew {args[0]} requires confirmation"

    if base in ("npm", "pnpm", "yarn", "bun"):
        if any(a in ("-g", "--global") for a in args):
            return f"global {base} package mutation requires confirmation"

    if PYTHON_BIN_RE.match(base):
        if len(args) >= 3 and args[0] == "-m" and args[1] == "pip" and args[2] in ("install", "uninstall"):
            return f"python -m pip {args[2]} requires confirmation"

    return None


def _args_touch_secret(args: Sequence[str], effective_cwd: Optional[str]) -> bool:
    for a in args:
        if a.startswith("-"):
            if "=" in a:
                val = a.split("=", 1)[1]
                if is_secret_or_key_path(resolve_path(val, effective_cwd)):
                    return True
            continue
        if is_secret_or_key_path(resolve_path(a, effective_cwd)):
            return True
    return False


def _evaluate_readonly_segment(
    words: List[str], effective_cwd: Optional[str], write_roots: Sequence[str]
) -> Optional[DecisionOutcome]:
    base = os.path.basename(words[0])
    args = words[1:]

    if _args_touch_secret(args, effective_cwd):
        return DecisionOutcome("ask", reason="command references a credential or secret path")

    if base == "cd":
        return DecisionOutcome("allow", category="readonly_shell")

    if base == "command":
        if any(a in ("-v", "-V") for a in args):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "pmset":
        if args and args[0] == "-g":
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "defaults":
        if args and args[0] in ("read", "read-type", "find", "domains"):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "sysctl":
        return DecisionOutcome("allow", category="readonly_shell")

    if base == "find":
        for a in args:
            if a.startswith(("-fprint", "-fls")):
                return None
        return DecisionOutcome("allow", category="readonly_shell")

    if base == "sort":
        for i, a in enumerate(args):
            if a == "-o" and i + 1 < len(args):
                out_p = resolve_path(args[i + 1], effective_cwd)
                if not is_within_roots(out_p, write_roots) or is_protected_subpath(out_p):
                    return None
            elif a.startswith("-o") and len(a) > 2:
                out_p = resolve_path(a[2:], effective_cwd)
                if not is_within_roots(out_p, write_roots) or is_protected_subpath(out_p):
                    return None
        return DecisionOutcome("allow", category="readonly_shell")

    if base == "yq":
        if any(a in ("-i", "--inplace") for a in args):
            return None
        return DecisionOutcome("allow", category="readonly_shell")

    if base in ("awk", "gawk", "mawk"):
        joined = " ".join(args)
        if "system(" in joined or "| getline" in joined or ">" in joined:
            return None
        return DecisionOutcome("allow", category="readonly_shell")

    if base in ("sed", "gsed"):
        return DecisionOutcome("allow", category="readonly_shell")

    if base == "pdfimages":
        if "-list" in args:
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "exiftool":
        if any(
            a in ("-w", "-o", "-overwrite_original", "-overwrite_original_in_place")
            or ("=" in a and a.startswith("-"))
            for a in args
        ):
            return None
        return DecisionOutcome("allow", category="readonly_shell")

    if base == "sips":
        if any(a in ("-g", "--getProperty", "--getPropertyAll") for a in args):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "qpdf":
        if any(a in ("--check", "--show-npages", "--show-encryption", "--json") for a in args):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "tar":
        if any(a in ("-t", "--list") or (a.startswith("-") and not a.startswith("--") and "t" in a and "x" not in a and "c" not in a) for a in args):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "unzip":
        if any(a in ("-l", "-Z", "-v") for a in args):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "xargs":
        idx = 0
        while idx < len(args) and args[idx].startswith("-"):
            flag = args[idx]
            idx += 1
            if flag in ("-I", "-n", "-P", "-L", "-s", "-d", "-E") and idx < len(args):
                idx += 1
        target_words = args[idx:] if idx < len(args) else ["echo"]
        if check_red_flag_segment(target_words):
            return None
        return _evaluate_readonly_segment(target_words, effective_cwd, write_roots)

    if base == "git":
        sub, gargs, _, unsafe_cfg = _git_subcommand_and_args(words)
        if unsafe_cfg:
            return None
        if sub in GIT_READ_SUBCOMMANDS:
            return DecisionOutcome("allow", category="readonly_shell")
        if sub == "branch":
            allowed_branch_flags = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l", "--show-current", "--contains", "--no-contains", "--merged", "--no-merged"}
            positional = [a for a in gargs if not a.startswith("-")]
            if not positional and all(a in allowed_branch_flags for a in gargs):
                return DecisionOutcome("allow", category="readonly_shell")
        if sub == "tag":
            positional = [a for a in gargs if not a.startswith("-")]
            if not positional or "-l" in gargs or "--list" in gargs:
                return DecisionOutcome("allow", category="readonly_shell")
        if sub == "remote":
            if not gargs or gargs[0] in ("-v", "--verbose", "show", "get-url"):
                return DecisionOutcome("allow", category="readonly_shell")
        if sub == "stash":
            if gargs and gargs[0] in ("list", "show"):
                return DecisionOutcome("allow", category="readonly_shell")
        if sub == "reflog":
            if not gargs or gargs[0] == "show":
                return DecisionOutcome("allow", category="readonly_shell")
        if sub == "config":
            if any(a in ("--get", "--get-all", "--list", "-l", "--show-origin") for a in gargs):
                return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "agy":
        if args and (
            args[0] in ("--help", "-h", "--version", "models", "agent", "agents", "changelog")
            or (args[0] in ("plugin", "plugins") and len(args) >= 2 and args[1] in ("list", "validate", "help"))
            or (args[0] == "mcp" and len(args) >= 2 and args[1] == "list")
        ):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base == "gh":
        if args and (
            args[0] == "status"
            or (len(args) >= 2 and args[0] in ("repo", "pr", "issue", "run") and args[1] in ("list", "view", "status", "checks", "diff"))
            or (
                args[0] == "api"
                and not any(
                    a in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")
                    and (a not in ("-X", "--method") or (i + 1 < len(args) and args[i + 1].upper() != "GET"))
                    for i, a in enumerate(args)
                )
            )
        ):
            return DecisionOutcome("allow", category="readonly_shell")
        return None

    if base in READONLY_COMMANDS:
        return DecisionOutcome("allow", category="readonly_shell")

    return None


def _all_paths_writable(paths: Sequence[str], effective_cwd: Optional[str], write_roots: Sequence[str]) -> bool:
    if not paths:
        return False
    for p in paths:
        resolved = resolve_path(p, effective_cwd)
        if not is_within_roots(resolved, write_roots) or is_protected_subpath(resolved):
            return False
    return True


def _evaluate_doc_and_edit_segment(
    words: List[str], effective_cwd: Optional[str], write_roots: Sequence[str]
) -> Optional[DecisionOutcome]:
    base = os.path.basename(words[0])
    args = words[1:]

    if _args_touch_secret(args, effective_cwd):
        return DecisionOutcome("ask", reason="command references a credential or secret path")

    if base == "latexmk":
        for a in args:
            if a in ("-e", "-usepretex", "-shell-escape", "--shell-escape", "-enable-write18", "--enable-write18") or a.startswith(("-e=", "-usepretex=")):
                return DecisionOutcome("ask", reason=f"latexmk flag '{a}' is not auto-allowed")
            if a.startswith(("-outdir=", "-output-directory=")):
                out_dir = a.split("=", 1)[1]
                if not _all_paths_writable([out_dir], effective_cwd, write_roots):
                    return None
        if not is_within_roots(effective_cwd, write_roots):
            return None
        return DecisionOutcome("allow", category="doc_and_edit")

    if base in ("xelatex", "pdflatex", "lualatex", "latex", "tex"):
        for a in args:
            if "shell-escape" in a or "write18" in a:
                return DecisionOutcome("ask", reason=f"{base} flag '{a}' is not auto-allowed")
            if a.startswith("-output-directory="):
                if not _all_paths_writable([a.split("=", 1)[1]], effective_cwd, write_roots):
                    return None
        if not is_within_roots(effective_cwd, write_roots):
            return None
        return DecisionOutcome("allow", category="doc_and_edit")

    if base in ("bibtex", "bibtex8", "biber", "makeindex", "xindy", "texindy"):
        if not is_within_roots(effective_cwd, write_roots):
            return None
        return DecisionOutcome("allow", category="doc_and_edit")

    if base in ("pdftoppm", "pdftocairo", "pdfunite", "pdfseparate", "pdfcrop", "qpdf", "sips", "gs", "officecli"):
        if base == "gs" and any(a in ("-dNOSAFER", "-dALLOWPSTRANSFER") for a in args):
            return DecisionOutcome("ask", reason="unsafe Ghostscript flag")
        if not is_within_roots(effective_cwd, write_roots):
            return None
        explicit_abs = [a for a in args if not a.startswith("-") and ("/" in a or a.startswith("~"))]
        for p in explicit_abs:
            rp = resolve_path(p, effective_cwd)
            if is_protected_subpath(rp):
                return DecisionOutcome("ask", reason=f"protected path target: {p}")
        return DecisionOutcome("allow", category="doc_and_edit")

    if base == "make":
        if not is_within_roots(effective_cwd, write_roots):
            return None
        for a in args:
            if a in ("-e", "--environment-overrides") or a.startswith("SHELL="):
                return None
            if not a.startswith("-") and "=" not in a and a not in ALLOWED_MAKE_DOC_TARGETS:
                return None
        return DecisionOutcome("allow", category="doc_and_edit")

    if base in ("mkdir", "touch"):
        targets = [a for a in args if not a.startswith("-")]
        if _all_paths_writable(targets, effective_cwd, write_roots):
            return DecisionOutcome("allow", category="doc_and_edit")
        return None

    if base == "cp":
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) >= 2 and _all_paths_writable([positional[-1]], effective_cwd, write_roots):
            return DecisionOutcome("allow", category="doc_and_edit")
        return None

    if base == "mv":
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) >= 2 and _all_paths_writable(positional, effective_cwd, write_roots):
            return DecisionOutcome("allow", category="doc_and_edit")
        return None

    if base == "git":
        sub, gargs, repo_dir, unsafe_cfg = _git_subcommand_and_args(words)
        if unsafe_cfg:
            return None
        repo_cwd = resolve_path(repo_dir, effective_cwd) if repo_dir else effective_cwd
        if not is_within_roots(repo_cwd, write_roots):
            return None
        if sub in ("add", "commit"):
            return DecisionOutcome("allow", category="doc_and_edit")
        return None

    return None


def _evaluate_workspace_python_segment(
    words: List[str],
    effective_cwd: Optional[str],
    python_roots: Sequence[str],
    bypass_sandbox: bool,
) -> Optional[DecisionOutcome]:
    base = os.path.basename(words[0])
    args = words[1:]

    if _args_touch_secret(args, effective_cwd):
        return DecisionOutcome("ask", reason="python invocation references a secret path")

    if base in ("pytest", "py.test"):
        if not is_within_roots(effective_cwd, python_roots):
            return None
        return DecisionOutcome("allow", category="workspace_python")

    if base in ("ruff", "mypy", "flake8"):
        if base == "ruff" and args and args[0] != "check":
            return None
        if is_within_roots(effective_cwd, python_roots):
            return DecisionOutcome("allow", category="workspace_python")
        return None

    if not PYTHON_BIN_RE.match(base):
        return None

    if not args:
        return None

    idx = 0
    while idx < len(args):
        flag = args[idx]
        if flag in ("-V", "--version", "-h", "--help"):
            return DecisionOutcome("allow", category="workspace_python")
        if flag in ("-W", "-X"):
            idx += 2
            continue
        if flag in ("-u", "-B", "-O", "-OO", "-E", "-s", "-S", "-v", "-q", "-I"):
            idx += 1
            continue
        if flag == "-m":
            if idx + 1 >= len(args):
                return None
            mod = args[idx + 1]
            if mod in ALLOWED_PYTHON_MODULES and is_within_roots(effective_cwd, python_roots):
                return DecisionOutcome("allow", category="workspace_python")
            return None
        if flag == "-c":
            if idx + 1 >= len(args):
                return None
            code = args[idx + 1]
            if bypass_sandbox:
                return DecisionOutcome("ask", reason="python -c with BypassSandbox=True requires confirmation")
            if PYTHON_C_UNSAFE_RE.search(code):
                return DecisionOutcome("ask", reason="python -c uses sensitive system/deletion call")
            return DecisionOutcome("allow", category="workspace_python")
        if flag.startswith("-"):
            return None
        script_path = resolve_path(flag, effective_cwd)
        if is_within_roots(script_path, python_roots) and not is_protected_subpath(script_path):
            return DecisionOutcome("allow", category="workspace_python")
        return None

    return None


def _parse_ssh_targets(cfg: Dict[str, Any]) -> Set[Tuple[str, str, str]]:
    """Parse configured ``user@host[:port]`` entries into ``(user, host, port)`` triples."""
    remote_cfg = cfg.get("remote_ssh") or {}
    raw_targets = remote_cfg.get("targets") or []
    parsed: Set[Tuple[str, str, str]] = set()
    for entry in raw_targets:
        s = str(entry).strip()
        if "@" not in s:
            continue
        user, host_port = s.split("@", 1)
        if not user or not host_port:
            continue
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
        else:
            host, port = host_port, "22"
        if host and port:
            parsed.add((user, host, port))
    return parsed


def _remote_write_prefixes(cfg: Dict[str, Any]) -> List[str]:
    remote_cfg = cfg.get("remote_ssh") or {}
    prefixes = [
        posixpath.normpath(str(p))
        for p in (remote_cfg.get("write_prefixes") or [])
        if str(p).startswith("/")
    ]
    if "/tmp" not in prefixes:
        prefixes.append("/tmp")
    return prefixes


def _is_remote_linux_path_allowed(
    raw_path: str, base_cwd: str, write_prefixes: Sequence[str]
) -> bool:
    if not raw_path or raw_path.startswith("~"):
        return False
    joined = raw_path if raw_path.startswith("/") else posixpath.join(base_cwd, raw_path)
    norm = posixpath.normpath(joined)
    return any(
        norm == pfx or norm.startswith(pfx + "/")
        for pfx in write_prefixes
    )


def _strip_remote_redirections(
    tokens: List[ShellToken], remote_cwd: str, write_prefixes: Sequence[str]
) -> List[str]:
    """Validate redirections on remote Linux host using POSIX paths."""
    words: List[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok.quoted:
            words.append(tok.text)
            idx += 1
            continue
        if FD_DUP_RE.match(tok.text):
            idx += 1
            continue
        m = REDIR_OP_RE.match(tok.text)
        if not m:
            words.append(tok.text)
            idx += 1
            continue
        op = m.group("op")
        target = m.group("rest")
        if op in ("<<", "<>"):
            raise ShellParseError(f"unsupported remote redirection '{op}'")
        if not target:
            if idx + 1 >= len(tokens):
                raise ShellParseError(f"missing target after remote redirection '{op}'")
            target = tokens[idx + 1].text
            idx += 2
        else:
            idx += 1
        if op == "<<<" or target == "/dev/null":
            continue
        if op in (">", ">>"):
            if not _is_remote_linux_path_allowed(target, remote_cwd, write_prefixes):
                raise ShellParseError(f"remote redirection outside allowed directories: {target}")
        elif op == "<":
            if any(bad in target for bad in (".ssh", ".env", ".pem", ".key", "id_ed25519", "id_rsa")):
                raise ShellParseError(f"remote input redirection reads protected path: {target}")
    return words


def _is_remote_path_allowed(
    remote_spec: str,
    effective_port: str,
    allowed_targets: Set[Tuple[str, str, str]],
    write_prefixes: Sequence[str],
) -> bool:
    if ":" not in remote_spec:
        return False
    host_part, path_part = remote_spec.split(":", 1)
    if "@" not in host_part:
        return False
    user, host = host_part.split("@", 1)
    if (user, host, effective_port) not in allowed_targets:
        return False
    default_cwd = next((p for p in write_prefixes if p != "/tmp"), "/tmp")
    return _is_remote_linux_path_allowed(path_part, default_cwd, write_prefixes)


def _validate_remote_command(remote_cmd: str, write_prefixes: Sequence[str]) -> Optional[str]:
    """Return error reason if remote GPU command is unsafe, else None."""
    if not remote_cmd or not remote_cmd.strip():
        return "empty remote ssh command"

    try:
        segs, subs, _ = split_shell_segments(remote_cmd)
    except ShellParseError as exc:
        return f"remote command syntax error: {exc}"

    if subs:
        return "command substitution in remote command requires confirmation"

    remote_cwd = next((p for p in write_prefixes if p != "/tmp"), "/tmp")
    for op, seg_text in segs:
        try:
            tokens = tokenize_segment(seg_text)
            words = _strip_remote_redirections(tokens, remote_cwd, write_prefixes)
        except ShellParseError as exc:
            return str(exc)

        words, err = _strip_env_and_wrappers(words)
        if err:
            return err
        if not words:
            continue

        base = os.path.basename(words[0])
        args = words[1:]

        if base in ("bash", "sh", "zsh") and len(args) >= 2 and args[0] in ("-c", "-ic", "-lc"):
            inner_err = _validate_remote_command(args[1], write_prefixes)
            if inner_err:
                return inner_err
            continue

        red_flag = check_red_flag_segment(words, allow_conda_source=True)
        if red_flag:
            return red_flag

        for a in args:
            if any(
                bad in a
                for bad in (
                    ".ssh",
                    ".bashrc",
                    ".bash_profile",
                    ".profile",
                    ".zshrc",
                    "/etc/shadow",
                    "/etc/passwd",
                    "/root/",
                )
            ):
                return f"remote argument touches protected path: {a}"

        if base in ("shutdown", "reboot", "halt", "poweroff", "init", "systemctl", "service", "passwd", "useradd", "userdel", "usermod"):
            return f"remote system command '{base}' requires confirmation"

        if base in ("source", ".") and len(args) == 1 and CONDA_ACTIVATE_PATH_RE.search(args[0]):
            continue
        if base in ("conda", "mamba") and len(args) >= 2 and args[0] == "activate":
            continue
        if base in ("conda", "mamba") and args and args[0] in ("info", "env"):
            continue
        if base == "cd":
            target = args[0] if args else ""
            if not _is_remote_linux_path_allowed(target, remote_cwd, write_prefixes):
                return f"remote cd outside allowed remote directories: {target}"
            if op in ("", "&&", ";", "\n"):
                joined = target if target.startswith("/") else posixpath.join(remote_cwd, target)
                remote_cwd = posixpath.normpath(joined)
            continue
        if base == "mkdir":
            targets = [a for a in args if not a.startswith("-")]
            if not targets or not all(_is_remote_linux_path_allowed(t, remote_cwd, write_prefixes) for t in targets):
                return "remote mkdir outside configured write_prefixes"
            continue
        if base in REMOTE_READONLY_COMMANDS or base in ("find", "pytest") or PYTHON_BIN_RE.match(base):
            continue
        if base in ("pip", "pip3") and args and args[0] == "list":
            continue
        if base == "nvidia-smi":
            continue

        return f"unrecognized remote command '{base}' requires confirmation"

    return None


def _evaluate_remote_gpu_segment(
    words: List[str],
    effective_cwd: Optional[str],
    write_roots: Sequence[str],
    cfg: Dict[str, Any],
) -> Optional[DecisionOutcome]:
    base = os.path.basename(words[0])
    args = words[1:]
    if base not in ("ssh", "scp", "rsync"):
        return None

    allowed_targets = _parse_ssh_targets(cfg)
    write_prefixes = _remote_write_prefixes(cfg)

    if base == "ssh":
        port = ""
        user_opt = ""
        allowed_no_val = {"-T", "-t", "-q", "-n", "-v", "-C", "-4", "-6"}
        unsafe_ssh_opts = {"proxycommand", "localcommand", "permitlocalcommand", "remotecommand", "forwardagent"}

        def _consume_ssh_flags(start: int) -> Tuple[int, Optional[DecisionOutcome]]:
            nonlocal port, user_opt
            cur = start
            while cur < len(args):
                a = args[cur]
                if not a.startswith("-"):
                    break
                if a == "--":
                    cur += 1
                    break
                if a in allowed_no_val:
                    cur += 1
                    continue
                if a == "-p":
                    if cur + 1 >= len(args):
                        return cur, DecisionOutcome("ask", reason="incomplete ssh -p option")
                    port = args[cur + 1]
                    cur += 2
                    continue
                if a.startswith("-p") and len(a) > 2:
                    port = a[2:]
                    cur += 1
                    continue
                if a == "-l":
                    if cur + 1 >= len(args):
                        return cur, DecisionOutcome("ask", reason="incomplete ssh -l option")
                    user_opt = args[cur + 1]
                    cur += 2
                    continue
                if a == "-i":
                    if cur + 1 >= len(args):
                        return cur, DecisionOutcome("ask", reason="incomplete ssh -i option")
                    cur += 2
                    continue
                if a == "-o":
                    if cur + 1 >= len(args):
                        return cur, DecisionOutcome("ask", reason="incomplete ssh -o option")
                    opt_val = args[cur + 1].lower().split("=", 1)[0].strip()
                    if opt_val in unsafe_ssh_opts:
                        return cur, DecisionOutcome("ask", reason=f"unsafe ssh option -o {opt_val}")
                    cur += 2
                    continue
                return cur, DecisionOutcome("ask", reason=f"unsupported ssh flag '{a}'")
            return cur, None

        idx, flag_err = _consume_ssh_flags(0)
        if flag_err:
            return flag_err
        if idx >= len(args):
            return None
        dest = args[idx]
        idx, post_err = _consume_ssh_flags(idx + 1)
        if post_err:
            return post_err

        if not allowed_targets:
            return DecisionOutcome(
                "ask",
                reason="no authorized remote_ssh targets configured in ~/.config/agy-safe-allow/config.json",
            )

        remote_parts = args[idx:]
        if "@" in dest:
            user, host = dest.split("@", 1)
        else:
            user, host = user_opt, dest
        effective_port = port or "22"

        if (user, host, effective_port) not in allowed_targets:
            return DecisionOutcome("ask", reason="ssh target is not in configured remote_ssh.targets")

        remote_cmd = " ".join(remote_parts).strip()
        err = _validate_remote_command(remote_cmd, write_prefixes)
        if err:
            return DecisionOutcome("ask", reason=err)
        return DecisionOutcome("allow", category="remote_gpu_ssh")

    if base == "scp":
        port = ""
        idx = 0
        operands: List[str] = []
        while idx < len(args):
            a = args[idx]
            if a in ("-r", "-p", "-q", "-C", "-T", "-v", "-4", "-6"):
                idx += 1
                continue
            if a == "-P":
                if idx + 1 >= len(args):
                    return None
                port = args[idx + 1]
                idx += 2
                continue
            if a.startswith("-P") and len(a) > 2:
                port = a[2:]
                idx += 1
                continue
            if a in ("-i", "-o"):
                if idx + 1 >= len(args):
                    return None
                if a == "-o" and "proxycommand" in args[idx + 1].lower():
                    return DecisionOutcome("ask", reason="unsafe scp -o ProxyCommand")
                idx += 2
                continue
            if a.startswith("-"):
                return DecisionOutcome("ask", reason=f"unsupported scp flag '{a}'")
            operands.append(a)
            idx += 1

        if len(operands) < 2:
            return None
        srcs = operands[:-1]
        dst = operands[-1]
        remote_srcs = [s for s in srcs if ":" in s]
        remote_dst = ":" in dst
        if remote_dst and remote_srcs:
            return DecisionOutcome("ask", reason="remote-to-remote scp requires confirmation")
        if not remote_dst and not remote_srcs:
            return None

        if not allowed_targets:
            return DecisionOutcome(
                "ask",
                reason="no authorized remote_ssh targets configured in ~/.config/agy-safe-allow/config.json",
            )

        effective_port = port or "22"
        if remote_dst:
            if not _is_remote_path_allowed(dst, effective_port, allowed_targets, write_prefixes):
                return DecisionOutcome("ask", reason="scp remote destination not authorized")
            if _args_touch_secret(srcs, effective_cwd):
                return DecisionOutcome("ask", reason="scp source touches secret file")
        else:
            if not all(_is_remote_path_allowed(s, effective_port, allowed_targets, write_prefixes) for s in remote_srcs):
                return DecisionOutcome("ask", reason="scp remote source not authorized")
            if not _all_paths_writable([dst], effective_cwd, write_roots):
                return DecisionOutcome("ask", reason="scp local destination outside allowed write roots")
        return DecisionOutcome("allow", category="remote_gpu_ssh")

    if base == "rsync":
        rsh_cmd = ""
        idx = 0
        operands = []
        flags_with_val = {"--exclude", "--include", "--filter", "--exclude-from", "--include-from", "--chmod", "--timeout"}
        while idx < len(args):
            a = args[idx]
            if a in ("-e", "--rsh"):
                if idx + 1 >= len(args):
                    return None
                rsh_cmd = args[idx + 1]
                idx += 2
                continue
            if a.startswith("--rsh="):
                rsh_cmd = a.split("=", 1)[1]
                idx += 1
                continue
            if a in flags_with_val:
                if idx + 1 >= len(args):
                    return None
                idx += 2
                continue
            if a.startswith("--"):
                if a == "--delete" or a.startswith("--delete-") or a == "--remove-source-files":
                    return DecisionOutcome("ask", reason=f"rsync with {a} requires confirmation")
                idx += 1
                continue
            if a.startswith("-"):
                idx += 1
                continue
            operands.append(a)
            idx += 1

        if len(operands) < 2:
            return None
        srcs = operands[:-1]
        dst = operands[-1]
        remote_srcs = [s for s in srcs if ":" in s]
        remote_dst = ":" in dst
        if not remote_dst and not remote_srcs:
            return None
        if remote_dst and remote_srcs:
            return DecisionOutcome("ask", reason="remote-to-remote rsync requires confirmation")

        if not allowed_targets:
            return DecisionOutcome(
                "ask",
                reason="no authorized remote_ssh targets configured in ~/.config/agy-safe-allow/config.json",
            )

        rsh_port = "22"
        if rsh_cmd:
            try:
                rsh_parts = shlex.split(rsh_cmd)
            except ValueError:
                rsh_parts = []
            if not rsh_parts or os.path.basename(rsh_parts[0]) != "ssh":
                return DecisionOutcome("ask", reason="rsync -e transport must invoke ssh")
            if any(
                tok in ("-L", "-R", "-D", "-W", "-J")
                or "proxycommand" in tok.lower()
                or "localcommand" in tok.lower()
                for tok in rsh_parts[1:]
            ):
                return DecisionOutcome("ask", reason="unsafe option in rsync ssh transport")
            for ri, tok in enumerate(rsh_parts[1:], start=1):
                if tok == "-p":
                    if ri + 1 >= len(rsh_parts):
                        return DecisionOutcome("ask", reason="incomplete -p in rsync ssh transport")
                    rsh_port = rsh_parts[ri + 1]
                elif tok.startswith("-p") and len(tok) > 2:
                    rsh_port = tok[2:]

        if remote_dst:
            if not _is_remote_path_allowed(dst, rsh_port, allowed_targets, write_prefixes):
                return DecisionOutcome("ask", reason="rsync remote destination not authorized")
            if _args_touch_secret(srcs, effective_cwd):
                return DecisionOutcome("ask", reason="rsync source touches secret file")
        else:
            if not all(_is_remote_path_allowed(s, rsh_port, allowed_targets, write_prefixes) for s in remote_srcs):
                return DecisionOutcome("ask", reason="rsync remote source not authorized")
            if not _all_paths_writable([dst], effective_cwd, write_roots):
                return DecisionOutcome("ask", reason="rsync local destination outside allowed write roots")
        return DecisionOutcome("allow", category="remote_gpu_ssh")

    return None


def _evaluate_command_text(
    cmd_text: str,
    initial_cwd: str,
    write_roots: Sequence[str],
    python_roots: Sequence[str],
    cfg: Dict[str, Any],
    bypass_sandbox: bool,
    allow_conda_source: bool = False,
) -> DecisionOutcome:
    """Evaluate a complete shell command string across all pipeline segments."""
    if not cmd_text.strip():
        return DecisionOutcome("ask", reason="empty command")

    try:
        segments, substitutions, has_bg_amp = split_shell_segments(cmd_text)
    except ShellParseError as exc:
        if bypass_sandbox:
            return DecisionOutcome("ask", reason=str(exc))
        return DecisionOutcome("allow", category="sandbox_default")

    if has_bg_amp and bypass_sandbox:
        return DecisionOutcome("ask", reason="background '&' with BypassSandbox=True requires confirmation")

    cats = cfg.get("categories") or {}

    for inner in substitutions:
        if bypass_sandbox and not cats.get("readonly_shell", True):
            return DecisionOutcome("ask", reason="command substitution requires readonly_shell enabled")
        sub_res = _evaluate_command_text(
            inner,
            initial_cwd,
            write_roots,
            python_roots,
            cfg,
            bypass_sandbox=True,
            allow_conda_source=False,
        )
        if sub_res.decision != "allow":
            return DecisionOutcome("ask", reason=f"command substitution blocked: {sub_res.reason}")
        if sub_res.category not in ("readonly_shell",):
            return DecisionOutcome("ask", reason="command substitution is limited to read-only commands")

    pipelines: List[List[Tuple[str, str]]] = []
    cur_pipe: List[Tuple[str, str]] = []
    for op, seg_text in segments:
        if op == "|" and cur_pipe:
            cur_pipe.append((op, seg_text))
        else:
            if cur_pipe:
                pipelines.append(cur_pipe)
            cur_pipe = [(op, seg_text)]
    if cur_pipe:
        pipelines.append(cur_pipe)

    effective_cwd: Optional[str] = initial_cwd
    matched_categories: List[str] = []
    extra_prefixes = cfg.get("extra_allow_cmd_prefixes") or []

    for pipe in pipelines:
        pipe_bases: List[str] = []
        pipe_cwd = effective_cwd
        for idx_in_pipe, (op, seg_text) in enumerate(pipe):
            try:
                tokens = tokenize_segment(seg_text)
                words = inspect_and_strip_redirections(tokens, pipe_cwd, write_roots)
            except ShellParseError as exc:
                if bypass_sandbox:
                    return DecisionOutcome("ask", reason=str(exc))
                return DecisionOutcome("ask", reason=str(exc)) if "secret" in str(exc) else DecisionOutcome("allow", category="sandbox_default")

            words, env_err = _strip_env_and_wrappers(words)
            if env_err:
                return DecisionOutcome("ask", reason=env_err)
            if not words:
                continue

            base = os.path.basename(words[0])
            pipe_bases.append(base)

            if base in ("bash", "sh", "zsh") and len(words) >= 3 and words[1] in ("-c", "-ic", "-lc"):
                inner_script = words[2]
                inner_res = _evaluate_command_text(
                    inner_script,
                    pipe_cwd or initial_cwd,
                    write_roots,
                    python_roots,
                    cfg,
                    bypass_sandbox=bypass_sandbox,
                    allow_conda_source=True,
                )
                if inner_res.decision != "allow":
                    return inner_res
                matched_categories.append(inner_res.category)
                continue

            if base == "conda" and allow_conda_source and len(words) >= 3 and words[1] == "activate":
                matched_categories.append("workspace_python")
                continue

            red_flag = check_red_flag_segment(words, allow_conda_source=allow_conda_source)
            if red_flag:
                return DecisionOutcome("ask", reason=red_flag)

            if idx_in_pipe > 0 and any(b in ("curl", "wget") for b in pipe_bases[:-1]):
                if base in ("sh", "bash", "zsh", "dash", "ksh", "fish", "node", "perl", "ruby", "osascript") or PYTHON_BIN_RE.match(base):
                    return DecisionOutcome("ask", reason="piping downloaded content to shell/interpreter requires confirmation")

            seg_outcome: Optional[DecisionOutcome] = None

            if cats.get("readonly_shell", True):
                seg_outcome = _evaluate_readonly_segment(words, pipe_cwd, write_roots)

            if seg_outcome is None and cats.get("doc_and_edit", True):
                seg_outcome = _evaluate_doc_and_edit_segment(words, pipe_cwd, write_roots)

            if seg_outcome is None and cats.get("workspace_python", True):
                seg_outcome = _evaluate_workspace_python_segment(
                    words, pipe_cwd, python_roots, bypass_sandbox
                )

            if seg_outcome is None and cats.get("remote_gpu_ssh", True):
                seg_outcome = _evaluate_remote_gpu_segment(words, pipe_cwd, write_roots, cfg)

            if seg_outcome is None and extra_prefixes:
                joined_words = " ".join(words)
                for pfx in extra_prefixes:
                    if joined_words == pfx or joined_words.startswith(pfx + " "):
                        seg_outcome = DecisionOutcome("allow", category="extra_allow")
                        break

            if seg_outcome is not None and seg_outcome.decision != "allow":
                return seg_outcome

            if seg_outcome is None:
                if bypass_sandbox:
                    return DecisionOutcome(
                        "ask",
                        reason=f"unsandboxed command '{base}' is not in safe-allow whitelist",
                    )
                matched_categories.append("sandbox_default")
            else:
                matched_categories.append(seg_outcome.category)

            if len(pipe) == 1 and base == "cd" and op in ("", "&&", ";", "\n"):
                if len(words) == 1:
                    effective_cwd = _real_path("~")
                elif words[1] == "-":
                    effective_cwd = None
                else:
                    effective_cwd = resolve_path(words[1], effective_cwd)

    if not matched_categories:
        return DecisionOutcome("allow", category="readonly_shell")

    if all(c != "sandbox_default" for c in matched_categories):
        primary_cat = matched_categories[-1]
        override = (
            f"command({cmd_text})"
            if "\n" not in cmd_text
            else f"command(regex:^{re.escape(cmd_text)}$)"
        )
        return DecisionOutcome(
            "allow",
            category=primary_cat,
            permission_overrides=[override],
        )

    return DecisionOutcome("allow", category="sandbox_default")


def _extract_mcp_server_tool(tool_name: str, args: Dict[str, Any]) -> Tuple[str, str]:
    if tool_name == "call_mcp_tool":
        server = str(args.get("ServerName") or args.get("serverName") or "").strip()
        tool = str(args.get("ToolName") or args.get("toolName") or "").strip()
        return server, tool
    if tool_name.startswith("mcp_"):
        rest = tool_name[4:]
        for srv in KNOWN_MCP_SERVERS:
            prefix = srv + "_"
            if rest.startswith(prefix):
                return srv, rest[len(prefix) :]
        if "_" in rest:
            srv, tool = rest.split("_", 1)
            return srv, tool
    return "", ""


def _is_subagent_payload(payload: Dict[str, Any]) -> bool:
    if str(payload.get("parentConversationId") or "").strip():
        return True
    conv_id = str(payload.get("conversationId") or "").strip()
    root_id = str(payload.get("rootConversationId") or "").strip()
    return bool(root_id and conv_id and root_id != conv_id)


def _extract_target_summary(tool_name: str, args: Dict[str, Any]) -> str:
    if tool_name == "run_command":
        return str(args.get("CommandLine") or args.get("command") or "")
    if tool_name in WRITE_EDIT_TOOLS or tool_name in READ_INSPECT_TOOLS:
        return str(
            args.get("Path")
            or args.get("TargetFile")
            or args.get("AbsolutePath")
            or args.get("DirectoryPath")
            or args.get("SearchPath")
            or args.get("path")
            or args.get("file_path")
            or ""
        )
    if tool_name == "call_mcp_tool" or tool_name.startswith("mcp_"):
        srv, tl = _extract_mcp_server_tool(tool_name, args)
        return f"{srv}/{tl}"
    if tool_name in ("read_url", "read_url_content"):
        return str(args.get("Url") or args.get("url") or "")
    if tool_name == "search_web":
        return str(args.get("query") or "")
    return tool_name


def evaluate_tool_call(
    payload: Dict[str, Any], cfg: Dict[str, Any]
) -> Tuple[DecisionOutcome, str]:
    """Evaluate a PreToolUse hook payload against policy and return (outcome, target_summary)."""
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return DecisionOutcome("allow", category="non_tool"), ""

    tool_name = str(tool_call.get("name") or "").strip()
    raw_args = tool_call.get("args")
    args: Dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    target_summary = _extract_target_summary(tool_name, args)
    is_subagent = _is_subagent_payload(payload)
    write_roots, python_roots, initial_cwd = _build_allowed_roots(payload, cfg)
    cats = cfg.get("categories") or {}

    if not cfg.get("enabled", True) or (cfg.get("scope") == "subagents_only" and not is_subagent):
        if tool_name in BENIGN_UI_AND_SWARM_TOOLS or tool_name in READ_INSPECT_TOOLS:
            return DecisionOutcome("allow", category="bypass_disabled"), target_summary
        if tool_name in WRITE_EDIT_TOOLS:
            raw_p = str(
                args.get("Path")
                or args.get("TargetFile")
                or args.get("file_path")
                or args.get("path")
                or ""
            )
            resolved = resolve_path(raw_p, initial_cwd)
            if is_within_roots(resolved, write_roots) and not is_protected_subpath(resolved):
                return DecisionOutcome("allow", category="bypass_disabled"), target_summary
        if tool_name == "run_command" and not bool(args.get("BypassSandbox", False)):
            cmd = str(args.get("CommandLine") or args.get("command") or "")
            try:
                segs, _, _ = split_shell_segments(cmd)
                for _, st in segs:
                    w = inspect_and_strip_redirections(tokenize_segment(st), initial_cwd, write_roots)
                    w, _ = _strip_env_and_wrappers(w)
                    rf = check_red_flag_segment(w)
                    if rf:
                        return DecisionOutcome("ask", reason=rf), target_summary
                return DecisionOutcome("allow", category="sandbox_default"), target_summary
            except ShellParseError:
                pass
        return DecisionOutcome("ask", reason="safe-allow inactive for this invocation"), target_summary

    if tool_name in BENIGN_UI_AND_SWARM_TOOLS:
        return DecisionOutcome("allow", category="orchestration"), target_summary

    if tool_name in READ_INSPECT_TOOLS:
        raw_p = str(
            args.get("Path")
            or args.get("AbsolutePath")
            or args.get("DirectoryPath")
            or args.get("SearchPath")
            or args.get("SearchDirectory")
            or args.get("File")
            or args.get("path")
            or ""
        )
        resolved = resolve_path(raw_p, initial_cwd) if raw_p else None
        if is_secret_or_key_path(resolved):
            return (
                DecisionOutcome("ask", reason="reading credential or secret file requires confirmation"),
                target_summary,
            )
        return (
            DecisionOutcome("allow", category="readonly_shell", permission_overrides=["read_file(*)"]),
            target_summary,
        )

    if tool_name in WRITE_EDIT_TOOLS:
        if not cats.get("doc_and_edit", True):
            return DecisionOutcome("ask", reason="doc_and_edit category is disabled"), target_summary
        raw_p = str(
            args.get("Path")
            or args.get("TargetFile")
            or args.get("file_path")
            or args.get("path")
            or ""
        )
        resolved = resolve_path(raw_p, initial_cwd)
        if not is_within_roots(resolved, write_roots):
            return (
                DecisionOutcome("ask", reason="file edit target is outside workspace or trusted temp roots"),
                target_summary,
            )
        if is_protected_subpath(resolved):
            return (
                DecisionOutcome("ask", reason="file edit target is a protected config, git, or secret path"),
                target_summary,
            )
        return (
            DecisionOutcome(
                "allow",
                category="doc_and_edit",
                permission_overrides=[f"write_file({resolved})", f"read_file({resolved})"],
            ),
            target_summary,
        )

    if tool_name == "search_web":
        if cats.get("mcp_and_web", True):
            return DecisionOutcome("allow", category="mcp_and_web"), target_summary
        return DecisionOutcome("ask", reason="mcp_and_web category is disabled"), target_summary

    if tool_name in ("read_url", "read_url_content"):
        if cats.get("mcp_and_web", True):
            return (
                DecisionOutcome("allow", category="mcp_and_web", permission_overrides=["read_url(*)"]),
                target_summary,
            )
        return DecisionOutcome("ask", reason="mcp_and_web category is disabled"), target_summary

    if tool_name == "call_mcp_tool" or tool_name.startswith("mcp_"):
        server, mcp_tool = _extract_mcp_server_tool(tool_name, args)
        if not cats.get("mcp_and_web", True):
            return DecisionOutcome("ask", reason="mcp_and_web category is disabled"), target_summary
        if server in ("paper-search-mcp", "drawio"):
            return (
                DecisionOutcome(
                    "allow",
                    category="mcp_and_web",
                    permission_overrides=[f"mcp({server}/{mcp_tool})"],
                ),
                target_summary,
            )
        if server == "zotero":
            if mcp_tool in ZOTERO_READ_TOOLS:
                return (
                    DecisionOutcome(
                        "allow",
                        category="mcp_and_web",
                        permission_overrides=[f"mcp(zotero/{mcp_tool})"],
                    ),
                    target_summary,
                )
            return (
                DecisionOutcome("ask", reason=f"mutating Zotero MCP tool '{mcp_tool}' requires confirmation"),
                target_summary,
            )
        return (
            DecisionOutcome("ask", reason=f"MCP tool '{server}/{mcp_tool}' is not in read-only whitelist"),
            target_summary,
        )

    if tool_name == "run_command":
        cmd = str(args.get("CommandLine") or args.get("command") or args.get("Command") or "")
        bypass_sandbox = bool(args.get("BypassSandbox", False))
        outcome = _evaluate_command_text(
            cmd,
            initial_cwd=initial_cwd,
            write_roots=write_roots,
            python_roots=python_roots,
            cfg=cfg,
            bypass_sandbox=bypass_sandbox,
        )
        return outcome, target_summary

    return DecisionOutcome("ask", reason=f"tool '{tool_name}' requires confirmation"), target_summary


def _emit(obj: Dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _run_hook() -> int:
    try:
        raw = sys.stdin.read()
        payload: Dict[str, Any] = {}
        if raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        cfg = load_config()
        outcome, summary = evaluate_tool_call(payload, cfg)
        tool_name = str(((payload.get("toolCall") or {}).get("name") or ""))
        append_audit(
            cfg,
            is_subagent=_is_subagent_payload(payload),
            tool_name=tool_name,
            target_summary=summary,
            outcome=outcome,
        )
        return _emit(outcome.to_hook_response())
    except Exception as exc:
        return _emit({"decision": "ask", "reason": f"safe-allow fallback: {exc}"})


def _read_recent_audit(limit: int, ask_only: bool) -> List[Dict[str, Any]]:
    path = _audit_log_path()
    if not os.path.isfile(path):
        return []
    items: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    if ask_only and obj.get("decision") == "allow":
                        continue
                    items.append(obj)
    except OSError:
        return []
    return items[-limit:]


def _apply_setting_update(cfg: Dict[str, Any], key: str, val: str) -> Optional[str]:
    if key in ("enabled", "audit_enabled"):
        cfg[key] = _parse_bool(val, bool(cfg[key]))
        return None
    if key == "scope":
        norm = val.strip().lower()
        if norm not in ("all", "subagents_only"):
            return f"Invalid scope '{val}'. Expected 'all' or 'subagents_only'."
        cfg["scope"] = norm
        return None
    if key in cfg["categories"]:
        cfg["categories"][key] = _parse_bool(val, bool(cfg["categories"][key]))
        return None
    if key.startswith("categories."):
        sub = key.split(".", 1)[1]
        if sub in cfg["categories"]:
            cfg["categories"][sub] = _parse_bool(val, bool(cfg["categories"][sub]))
            return None
    valid = sorted(
        ["enabled", "scope", "audit_enabled"]
        + list(cfg["categories"].keys())
        + [f"categories.{k}" for k in cfg["categories"]]
    )
    return f"Unknown config key '{key}'. Valid keys: {', '.join(valid)}"


def _run_cli(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect, test, or configure Safe Allow PreToolUse guard."
    )
    parser.add_argument("--status", action="store_true", help="Show config and audit summary.")
    parser.add_argument("--recent", action="store_true", help="Show recent audit log decisions.")
    parser.add_argument("--limit", type=int, default=20, help="Max recent entries to print.")
    parser.add_argument("--ask-only", action="store_true", help="Filter --recent to non-allow decisions.")
    parser.add_argument("--check-cmd", type=str, default=None, help="Dry-run command check.")
    parser.add_argument("--bypass-sandbox", action="store_true", help="Simulate BypassSandbox=True.")
    parser.add_argument("--cwd", type=str, default=os.getcwd(), help="Working directory for --check-cmd.")
    parser.add_argument("--show-config", action="store_true", help="Print active config JSON.")
    parser.add_argument("--set", dest="updates", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--allow-cmd", type=str, default=None, help="Add an extra allowed command prefix.")
    parser.add_argument("--add-ssh-target", action="append", default=[], metavar="USER@HOST[:PORT]", help="Authorize a remote SSH target.")
    parser.add_argument("--add-ssh-prefix", action="append", default=[], metavar="PATH", help="Authorize a remote POSIX write prefix.")
    parser.add_argument("--clear-ssh-targets", action="store_true", help="Clear configured remote_ssh targets and prefixes.")
    parser.add_argument("--reset-config", action="store_true", help="Reset config to defaults.")
    args = parser.parse_args(argv)

    cfg = _normalize_config(None) if args.reset_config else load_config()
    changed = False
    if args.reset_config and not args.updates and not args.allow_cmd and not args.add_ssh_target and not args.add_ssh_prefix and not args.clear_ssh_targets:
        saved = save_config(cfg)
        print(f"Reset config at {saved}")

    if args.clear_ssh_targets:
        cfg["remote_ssh"] = {"targets": [], "write_prefixes": ["/tmp"]}
        changed = True

    if args.updates:
        for item in args.updates:
            if "=" not in item:
                print(f"Invalid --set argument '{item}' (expected KEY=VALUE)", file=sys.stderr)
                return 2
            key, val = item.split("=", 1)
            err = _apply_setting_update(cfg, key.strip(), val.strip())
            if err:
                print(err, file=sys.stderr)
                return 2
        changed = True

    if args.allow_cmd is not None:
        prefix = args.allow_cmd.strip()
        if prefix and prefix not in cfg["extra_allow_cmd_prefixes"]:
            cfg["extra_allow_cmd_prefixes"].append(prefix)
        changed = True

    for raw_target in args.add_ssh_target:
        target = raw_target.strip()
        if "@" not in target:
            print(f"Invalid --add-ssh-target '{raw_target}' (expected USER@HOST[:PORT])", file=sys.stderr)
            return 2
        if target not in cfg["remote_ssh"]["targets"]:
            cfg["remote_ssh"]["targets"].append(target)
        changed = True

    for raw_prefix in args.add_ssh_prefix:
        prefix = raw_prefix.strip()
        if not prefix.startswith("/"):
            print(f"Invalid --add-ssh-prefix '{raw_prefix}' (expected absolute POSIX path)", file=sys.stderr)
            return 2
        norm = posixpath.normpath(prefix)
        if norm not in cfg["remote_ssh"]["write_prefixes"]:
            cfg["remote_ssh"]["write_prefixes"].append(norm)
        changed = True

    if changed:
        saved = save_config(cfg)
        print(f"Saved config to {saved}")

    if args.show_config or changed or args.reset_config:
        print(json.dumps({"config_path": _config_path(), "config": cfg}, indent=2, ensure_ascii=False))

    if args.status:
        recent = _read_recent_audit(200, ask_only=False)
        allows = sum(1 for r in recent if r.get("decision") == "allow")
        asks = len(recent) - allows
        print(
            json.dumps(
                {
                    "config_path": _config_path(),
                    "audit_path": _audit_log_path(),
                    "config": cfg,
                    "audit_summary": {"total": len(recent), "allow": allows, "ask": asks},
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if args.recent:
        items = _read_recent_audit(max(1, args.limit), ask_only=args.ask_only)
        print(json.dumps(items, indent=2, ensure_ascii=False))

    if args.check_cmd is not None:
        sim_payload = {
            "hookEventName": "PreToolUse",
            "cwd": args.cwd,
            "workspacePaths": [args.cwd],
            "toolCall": {
                "id": "dryrun",
                "name": "run_command",
                "args": {
                    "CommandLine": args.check_cmd,
                    "BypassSandbox": args.bypass_sandbox,
                },
            },
        }
        outcome, _ = evaluate_tool_call(sim_payload, cfg)
        print(
            json.dumps(
                {
                    "command": args.check_cmd,
                    "bypassSandbox": args.bypass_sandbox,
                    "cwd": args.cwd,
                    "category": outcome.category,
                    "response": outcome.to_hook_response(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return _run_cli(sys.argv[1:])
    return _run_hook()


if __name__ == "__main__":
    sys.exit(main())
