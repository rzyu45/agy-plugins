#!/usr/bin/env python3
"""macOS Notification Center + sound notifier for Antigravity CLI (agy).

Modeled on Claude Code's local `done-notify` plugin:
- Plays a macOS system sound + Notification Center banner when a turn finishes,
  fails, completes a background task, or receives a `/swarm` worker `[done]`
  report (`Stop`), regardless of focus by default (`focus_mode="always"`), with
  optional terminal/tab focus suppression (`focus_mode="tab"` or `"app"`).
- Plays a prompt sound + banner when `agy` pauses for user input (`ask_question`)
  or waits for user permission approval (`Surfacing tool confirmation` / queued
  subagent approval).
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata
from urllib.parse import quote as _url_quote, unquote, urlparse

OSASCRIPT_LINES = (
    "on run argv",
    'if (count of argv) >= 4 and (item 4 of argv) is not "" then',
    "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv) sound name (item 4 of argv)",
    "else",
    "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
    "end if",
    "end run",
)

NORMAL_STOP_REASONS = {
    "",
    "NO_TOOL_CALL",
    "TERMINAL_STEP_TYPE",
    "INJECTED_RESPONSE",
}

FAILURE_STOP_REASONS = {
    "ERROR",
    "MAX_INVOCATIONS",
    "MAX_FORCED_INVOCATIONS",
    "MAX_TOKEN_BUDGET_EXCEEDED",
    "TERMINAL_CUSTOM_HOOK",
}

AGY_FLAGS_WITH_VALUE = {
    "--add-dir",
    "--agent",
    "--conversation",
    "--effort",
    "--input-format",
    "--json-schema",
    "--log-file",
    "--mode",
    "--model",
    "--output-format",
    "--print-timeout",
    "--project",
    "--settings",
}

AGY_NON_TUI_SUBCOMMANDS = {
    "acp",
    "agent",
    "agents",
    "changelog",
    "help",
    "install",
    "mcp",
    "mic-serve",
    "models",
    "plugin",
    "plugins",
    "remote-control",
    "update",
}

TERM_PROGRAM_BUNDLES: Dict[str, Set[str]] = {
    "apple_terminal": {"com.apple.Terminal"},
    "iterm.app": {"com.googlecode.iterm2"},
    "ghostty": {"com.mitchellh.ghostty"},
    "wezterm": {"com.github.wez.wezterm", "org.wezfurlong.wezterm"},
    "kitty": {"net.kovidgoyal.kitty"},
    "alacritty": {"org.alacritty", "io.alacritty"},
    "warpterminal": {"dev.warp.Warp-Stable", "dev.warp.Warp", "dev.warp.Warp-Preview"},
    "vscode": {
        "com.microsoft.VSCode",
        "com.microsoft.VSCodeInsiders",
        "com.vscodium",
        "com.todesktop.230313mzl4w4u92",
        "com.codeium.windsurf",
    },
    "zed": {"dev.zed.Zed", "dev.zed.Zed-Preview"},
}

LOCK_BUNDLE_IDS = {
    "com.apple.loginwindow",
    "com.apple.ScreenSaver.Engine",
}

MID_TURN_STEP_TYPES = {
    "DIRECTORY_RULES",
    "CHECKPOINT",
    "EPHEMERAL_MESSAGE",
    "RETRIEVE_MEMORY",
    "KI_INSERTION",
    "BRAIN_UPDATES",
    "TASK_BOUNDARY",
}

PERMISSION_LABELS_ZH: Dict[str, str] = {
    "Command": "终端命令执行需要你确认权限。",
    "Create file": "创建文件需要你确认权限。",
    "Pending edit": "修改文件需要你确认权限。",
    "File access": "访问文件需要你确认权限。",
    "MCP": "MCP工具调用需要你确认权限。",
    "Read URL": "读取网页需要你确认权限。",
    "Browser action": "浏览器操作需要你确认权限。",
    "Browser download": "浏览器下载需要你确认权限。",
    "UAC Escalation": "提权操作需要你确认权限。",
    "Tool": "工具调用需要你确认权限。",
    "": "工具调用需要你确认权限。",
}

PERMISSION_LABELS_EN: Dict[str, str] = {
    "Command": "A command is waiting for your permission.",
    "Create file": "File creation is waiting for your permission.",
    "Pending edit": "File edit is waiting for your permission.",
    "File access": "File access is waiting for your permission.",
    "MCP": "An MCP tool call is waiting for your permission.",
    "Read URL": "URL access is waiting for your permission.",
    "Browser action": "A browser action is waiting for your permission.",
    "Browser download": "A browser download is waiting for your permission.",
    "UAC Escalation": "Privilege escalation is waiting for your permission.",
    "Tool": "A tool call is waiting for your permission.",
    "": "A tool call is waiting for your permission.",
}

APP_BUNDLE_RE = re.compile(r"(/[^:\n]*?\.app)(?=/Contents/)")
FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
MD_BLOCK_PREFIX_RE = re.compile(r"^\s{0,3}(?:#{1,6}|[-*+]|\d+\.|>)\s+", re.MULTILINE)
MD_EMPHASIS_RE = re.compile(r"\*\*|~~|__")
HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
WHITESPACE_RE = re.compile(r"\s+")

GLOG_LINE_RE = re.compile(
    r"^[IWEF]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+[^\]]+\]\s+(?P<msg>.*)$"
)
TOOL_CONFIRM_RE = re.compile(
    r'^Surfacing tool confirmation:\s+"(?P<label>(?:\\.|[^"\\])*)"\s+at step\s+(?P<step>\d+)\b'
)
SUBAGENT_QUEUE_RE = re.compile(
    r"^updatePendingApprovalsQueue: added to queue and marked seen:\s+(?P<subagent>\S+)\s+step\s+(?P<step>\d+)\b"
)
ASK_QUESTION_LOG_RE = re.compile(
    r"^Surfacing ask_question at step\s+(?P<step>\d+)\b"
)
CLI_STORE_WORKSPACE_RE = re.compile(
    r"^Initializing CLI store manager for workspace\s+(?P<path>/.*)$"
)
LSOF_HEX_ESCAPE_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PBTXT_TITLE_RE = re.compile(r'(?:^|[\s{])title\s*:\s*"((?:\\.|[^"\\])*)"')
CONV_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SWARM_WAIT_RESULT_RE = re.compile(r"(?:^|\n)Waiting for a message\b")
TOOL_OUTPUT_FILE_RE = re.compile(r"file://(/[^\s\"'<>]+?/output\.txt)\b")
SWARM_MENTION_TOKEN_RE = re.compile(r"@([A-Za-z0-9_-]+)")
SWARM_DONE_LEAD_RE = re.compile(
    r"^(?P<lead>(?:@[A-Za-z0-9_-]+[:,，]?\s*)*)(?:\[done\][:,：]?\s*)+(?P<after>(?:@[A-Za-z0-9_-]+[:,，]?\s*)*)",
    re.IGNORECASE,
)
SWARM_MSG_FALLBACK_RE = re.compile(
    r'"agent"\s*:\s*"(?P<agent>(?:\\.|[^"\\])+)"\s*,\s*"text"\s*:\s*"(?P<text>(?:\\.|[^"\\])*)'
)

SCRIPT_VERSION = "1.4.0"
CONV_TITLE_MAX = 60
TOOL_OUTPUT_MAX_BYTES = 4 * 1024 * 1024

_PROC_PIDVNODEPATHINFO = 9
_PROC_VNODEPATHINFO_SIZE = 2352
_PVI_CDIR_PATH_OFFSET = 152
_MAXPATHLEN = 1024
_LIBPROC_CACHE: Optional[Any] = None

WATCHER_POLL_SEC = 0.20
WATCHER_LSTART_CHECK_SEC = 30.0
WATCHER_MAX_LIFETIME_SEC = 86400.0
PRETOOL_QUESTION_DEDUP_SEC = 8.0

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "focus_mode": "always",         # "always" | "tab" | "app" (for Stop completion/failure)
    "prompt_focus_mode": "always",  # "always" | "tab" | "app" (for question & permission prompts)
    "sound": "Glass",               # macOS sound on completed turns
    "prompt_sound": "Ping",         # macOS sound on question & permission prompts
    "error_sound": "Sosumi",        # macOS sound on failed turns
    "sound_mode": "notification",   # "notification" | "afplay" | "both" | "off"
    "lang": "en",                   # "en" | "zh"
    "title": "Antigravity CLI",
    "body_max": 180,
    "min_duration_sec": 0.0,
    "cli_only": True,
    "notify_on_question": True,
    "notify_on_permission": True,
    "notify_when_bg_running": True,
    "notify_on_swarm_done": True,
    "show_conversation": True,
}


def _config_path() -> str:
    return os.environ.get(
        "DONE_NOTIFY_CONFIG",
        os.path.expanduser("~/.config/agy-done-notify/config.json"),
    )


def _runtime_dir() -> str:
    override = os.environ.get("DONE_NOTIFY_RUNTIME_DIR", "").strip()
    if override:
        return override
    return f"/tmp/agy-done-notify-{os.getuid()}"


def _watcher_state_path(agy_pid: int) -> str:
    return os.path.join(_runtime_dir(), f"watcher-{agy_pid}.json")


def _watcher_context_path(agy_pid: int) -> str:
    return os.path.join(_runtime_dir(), f"context-{agy_pid}.json")


def _question_marker_path(agy_pid: int) -> str:
    return os.path.join(_runtime_dir(), f"question-{agy_pid}.json")


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


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


def _parse_int(val: Any, default: int, minimum: int = 20, maximum: int = 500) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        parsed = int(float(val))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_float(val: Any, default: float, minimum: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = _parse_bool(raw["enabled"], True)

    for mode_key in ("focus_mode", "prompt_focus_mode"):
        mode_val = str(raw.get(mode_key, cfg[mode_key])).strip().lower()
        if mode_val in ("tab", "app", "always"):
            cfg[mode_key] = mode_val

    for key in ("sound", "prompt_sound", "error_sound", "title"):
        if key in raw and raw[key] is not None:
            cfg[key] = str(raw[key]).strip()

    sound_mode = str(raw.get("sound_mode", cfg["sound_mode"])).strip().lower()
    if sound_mode in ("notification", "afplay", "both", "off", "none"):
        cfg["sound_mode"] = "off" if sound_mode == "none" else sound_mode

    lang = str(raw.get("lang", cfg["lang"])).strip().lower()
    if lang in ("en", "zh", "cn"):
        cfg["lang"] = "zh" if lang in ("zh", "cn") else "en"

    if "body_max" in raw:
        cfg["body_max"] = _parse_int(raw["body_max"], 180, minimum=40, maximum=500)

    if "min_duration_sec" in raw:
        cfg["min_duration_sec"] = _parse_float(raw["min_duration_sec"], 0.0, minimum=0.0)

    for key in (
        "cli_only",
        "notify_on_question",
        "notify_on_permission",
        "notify_when_bg_running",
        "notify_on_swarm_done",
        "show_conversation",
    ):
        if key in raw:
            cfg[key] = _parse_bool(raw[key], DEFAULT_CONFIG[key])

    return cfg


def load_config() -> Dict[str, Any]:
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return _normalize_config(data)
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


def save_config(cfg: Dict[str, Any]) -> str:
    path = _config_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    normalized = _normalize_config(cfg)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=parent)
    try:
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


def _normalize_tty(tty: str) -> str:
    t = (tty or "").strip()
    if not t or t in ("?", "??"):
        return ""
    if t.startswith("/dev/"):
        t = t[5:]
    return t


def _safe_realpath(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _process_lstart(pid: int) -> str:
    if pid <= 1:
        return ""
    try:
        out = subprocess.check_output(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
        return out
    except (OSError, subprocess.SubprocessError):
        return ""


def plain_text(markdown: str) -> str:
    if not markdown:
        return ""
    text = HTML_COMMENT_RE.sub(" ", markdown)
    text = FENCED_CODE_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = MD_BLOCK_PREFIX_RE.sub("", text)
    text = MD_EMPHASIS_RE.sub("", text)
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def clip_text(text: str, max_len: int = 180) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, rem_min = divmod(minutes, 60)
    return f"{hours}h {rem_min}m"


def _parse_iso_epoch(ts: str) -> Optional[float]:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_agy_command_headless(args_str: str) -> Optional[bool]:
    """Inspect an `agy` process command line.

    Returns True if headless (`-p`/`--print`/`--prompt` or non-TUI subcommand),
    False if interactive TUI, or None if `args_str` is not an `agy` invocation.
    """
    try:
        tokens = shlex.split(args_str)
    except ValueError:
        tokens = args_str.split()
    if not tokens:
        return None

    exe_base = os.path.basename(tokens[0])
    if exe_base != "agy" and not exe_base.startswith("antigravity-cli"):
        return None

    idx = 1
    saw_positional = False
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            break
        if tok in ("-p", "--print", "--prompt") or tok.startswith(("--print=", "--prompt=")):
            return True
        if tok in AGY_FLAGS_WITH_VALUE:
            idx += 2
            continue
        if any(tok.startswith(flag + "=") for flag in AGY_FLAGS_WITH_VALUE):
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        if not saw_positional:
            saw_positional = True
            if tok in AGY_NON_TUI_SUBCOMMANDS:
                return True
        idx += 1

    return False


def _read_ps_table() -> Dict[int, Tuple[int, str, str]]:
    """Return {pid: (ppid, normalized_tty, args)} for all processes."""
    table: Dict[int, Tuple[int, str, str]] = {}
    try:
        out = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid=,tty=,args="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return table

    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        tty = _normalize_tty(parts[2])
        args = parts[3] if len(parts) > 3 else ""
        table[pid] = (ppid, tty, args)
    return table


def _walk_ancestors(
    start_pid: int, table: Dict[int, Tuple[int, str, str]]
) -> List[Tuple[int, str, str]]:
    chain: List[Tuple[int, str, str]] = []
    seen: Set[int] = set()
    pid = start_pid
    while pid > 1 and pid not in seen and pid in table:
        seen.add(pid)
        ppid, tty, args = table[pid]
        chain.append((pid, tty, args))
        if ppid == pid or ppid <= 1:
            break
        pid = ppid
    return chain


def _extract_bundle_paths(chain: List[Tuple[int, str, str]]) -> Set[str]:
    bundles: Set[str] = set()
    for _, _, args in chain:
        for match in APP_BUNDLE_RE.finditer(args):
            bundles.add(_safe_realpath(match.group(1)))
    return bundles


def _query_tmux_state(tmux_pane: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "present": True,
        "pane": tmux_pane,
        "queried": False,
        "pane_focused": True,
        "client_pids": [],
        "client_ttys": [],
    }
    try:
        msg = subprocess.check_output(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                tmux_pane,
                "#{session_attached}\t#{window_active}\t#{pane_active}\t#{session_name}",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return info

    fields = msg.split("\t")
    if len(fields) < 3:
        return info

    info["queried"] = True
    try:
        attached = int(fields[0]) > 0
    except ValueError:
        attached = fields[0] == "1"
    win_active = fields[1] == "1"
    pane_active = fields[2] == "1"
    session_name = fields[3] if len(fields) > 3 else ""
    info["session_attached"] = attached
    info["window_active"] = win_active
    info["pane_active"] = pane_active
    info["session_name"] = session_name

    if not (attached and win_active and pane_active):
        info["pane_focused"] = False
        return info

    cmd = ["tmux", "list-clients", "-F", "#{client_pid}\t#{client_tty}\t#{client_session}"]
    try:
        clients_out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=1.0
        )
        for line in clients_out.splitlines():
            cparts = line.strip().split("\t")
            if len(cparts) < 2:
                continue
            if session_name and len(cparts) >= 3 and cparts[2] != session_name:
                continue
            try:
                cpid = int(cparts[0])
                info["client_pids"].append(cpid)
            except ValueError:
                pass
            ctty = _normalize_tty(cparts[1])
            if ctty:
                info["client_ttys"].append(ctty)
    except (OSError, subprocess.SubprocessError):
        pass

    return info


def _query_frontmost_app() -> Dict[str, Any]:
    override = os.environ.get("DONE_NOTIFY_MOCK_FRONT_JSON")
    if override:
        try:
            return json.loads(override)
        except ValueError:
            pass

    result: Dict[str, Any] = {
        "asn": "",
        "name": "",
        "bundle_id": "",
        "bundle_path": "",
        "pid": 0,
    }
    try:
        asn = subprocess.check_output(
            ["/usr/bin/lsappinfo", "front"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return result

    result["asn"] = asn
    if not asn:
        return result

    try:
        info = subprocess.check_output(
            ["/usr/bin/lsappinfo", "info", asn],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return result

    first_line = info.splitlines()[0] if info else ""
    name_match = re.match(r'^"([^"]*)"', first_line.strip())
    if name_match:
        result["name"] = name_match.group(1)

    bid_match = re.search(r'bundleID="([^"]*)"', info)
    if bid_match:
        result["bundle_id"] = bid_match.group(1)

    bpath_match = re.search(r'bundle path="([^"]*)"', info)
    if bpath_match:
        result["bundle_path"] = _safe_realpath(bpath_match.group(1))

    pid_match = re.search(r"\bpid\s*=\s*(\d+)", info)
    if pid_match:
        try:
            result["pid"] = int(pid_match.group(1))
        except ValueError:
            pass

    return result


def _check_apple_terminal_tab_focused(our_tty: str) -> Optional[Tuple[bool, str]]:
    if not our_tty:
        return None
    script = (
        'tell application "Terminal"\n'
        'if (count of windows) is 0 then return ""\n'
        "return tty of selected tab of front window\n"
        "end tell"
    )
    try:
        out = subprocess.check_output(
            ["/usr/bin/osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    active_tty = _normalize_tty(out)
    if not active_tty:
        return (False, "Terminal.app has no active window")
    if active_tty != our_tty:
        return (False, f"Terminal.app active tab tty ({active_tty}) != session tty ({our_tty})")
    return (True, f"Terminal.app active tab matches session tty ({our_tty})")


def _check_iterm2_tab_focused(our_tty: str, iterm_session_id: str) -> Optional[Tuple[bool, str]]:
    expected_guid = ""
    if ":" in iterm_session_id:
        expected_guid = iterm_session_id.split(":", 1)[1].strip()
    if not our_tty and not expected_guid:
        return None

    script = (
        'tell application "iTerm2"\n'
        'if (count of windows) is 0 then return ""\n'
        "tell current session of current window\n"
        'return (unique ID) & "\\t" & (tty)\n'
        "end tell\n"
        "end tell"
    )
    try:
        out = subprocess.check_output(
            ["/usr/bin/osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None

    if not out:
        return (False, "iTerm2 has no active window")
    parts = out.split("\t")
    active_guid = parts[0].strip() if parts else ""
    active_tty = _normalize_tty(parts[1]) if len(parts) > 1 else ""

    if expected_guid and active_guid:
        if active_guid != expected_guid:
            return (False, f"iTerm2 active session ({active_guid}) != session ({expected_guid})")
        return (True, f"iTerm2 active session matches ({expected_guid})")

    if our_tty and active_tty:
        if active_tty != our_tty:
            return (False, f"iTerm2 active tty ({active_tty}) != session tty ({our_tty})")
        return (True, f"iTerm2 active tty matches ({our_tty})")

    return None


def _check_wezterm_pane_focused(wezterm_pane: str) -> Optional[Tuple[bool, str]]:
    if not wezterm_pane:
        return None
    try:
        expected_pane = int(wezterm_pane)
    except ValueError:
        return None
    try:
        out = subprocess.check_output(
            ["wezterm", "cli", "list-clients", "--format", "json"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
        clients = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(clients, list) or not clients:
        return None
    focused_panes = {c.get("focused_pane_id") for c in clients if isinstance(c, dict)}
    if expected_pane not in focused_panes:
        return (False, f"WezTerm focused pane {focused_panes} != session pane {expected_pane}")
    return (True, f"WezTerm focused pane matches ({expected_pane})")


def _check_kitty_window_focused(kitty_win_id: str, listen_on: str) -> Optional[Tuple[bool, str]]:
    if not kitty_win_id or not listen_on:
        return None
    try:
        expected_id = int(kitty_win_id)
    except ValueError:
        return None
    try:
        out = subprocess.check_output(
            ["kitty", "@", "--to", listen_on, "ls"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
        os_windows = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(os_windows, list):
        return None
    for os_win in os_windows:
        if not isinstance(os_win, dict):
            continue
        os_focused = bool(os_win.get("is_focused"))
        for tab in os_win.get("tabs") or []:
            if not isinstance(tab, dict):
                continue
            tab_focused = bool(tab.get("is_focused"))
            for win in tab.get("windows") or []:
                if isinstance(win, dict) and win.get("id") == expected_id:
                    is_focused = os_focused and tab_focused and bool(win.get("is_focused"))
                    if not is_focused:
                        return (False, f"Kitty window {expected_id} is not the active tab/window")
                    return (True, f"Kitty window {expected_id} is focused")
    return None


def inspect_focus_state(
    focus_mode: str = "always",
    target_pid: Optional[int] = None,
    include_frontmost: bool = False,
) -> Dict[str, Any]:
    """Inspect CLI interactivity, terminal ownership, and current macOS focus."""
    ps_table = _read_ps_table()
    start_pid = target_pid if (target_pid is not None and target_pid > 1) else os.getpid()
    ancestors = _walk_ancestors(start_pid, ps_table)

    is_interactive = True
    agy_pid = 0
    mock_agy_pid = os.environ.get("DONE_NOTIFY_MOCK_AGY_PID", "").strip()
    if mock_agy_pid:
        try:
            agy_pid = int(mock_agy_pid)
        except ValueError:
            agy_pid = 0

    for pid, _, args in ancestors:
        headless = _is_agy_command_headless(args)
        if headless is not None:
            if not agy_pid:
                agy_pid = pid
            if headless:
                is_interactive = False
            break

    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        is_interactive = False
    if _parse_bool(os.environ.get("DONE_NOTIFY_FORCE_INTERACTIVE"), False):
        is_interactive = True

    our_tty = ""
    for _, tty, _ in ancestors:
        if tty:
            our_tty = tty
            break

    env_bundle_id = os.environ.get("__CFBundleIdentifier", "").strip()
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    tmux_pane = os.environ.get("TMUX_PANE", "").strip() if os.environ.get("TMUX") else ""

    tmux_info: Dict[str, Any] = {"present": False}
    effective_ancestors = list(ancestors)
    has_reattached_tmux_client = False

    if tmux_pane and focus_mode != "always":
        tmux_info = _query_tmux_state(tmux_pane)
        if tmux_info.get("queried") and not tmux_info.get("pane_focused", True):
            return {
                "is_interactive": is_interactive,
                "focused": False,
                "reason": "tmux session/window/pane is not active",
                "host": {
                    "agy_pid": agy_pid,
                    "tty": our_tty,
                    "env_bundle_id": env_bundle_id,
                    "term_program": term_program,
                    "tmux": tmux_info,
                },
                "frontmost": _query_frontmost_app(),
            }
        client_pids = tmux_info.get("client_pids") or []
        client_ttys = tmux_info.get("client_ttys") or []
        if client_pids and ps_table:
            client_chains: List[Tuple[int, str, str]] = []
            for cpid in client_pids:
                client_chains.extend(_walk_ancestors(cpid, ps_table))
            if client_chains:
                effective_ancestors = client_chains
                has_reattached_tmux_client = True
        if client_ttys:
            our_tty = client_ttys[0]

    ancestor_pids = {pid for pid, _, _ in effective_ancestors}
    ancestor_bundles = _extract_bundle_paths(effective_ancestors)

    host_summary = {
        "agy_pid": agy_pid,
        "tty": our_tty,
        "env_bundle_id": env_bundle_id,
        "term_program": term_program,
        "ancestor_bundles": sorted(ancestor_bundles),
        "tmux": tmux_info,
    }

    if focus_mode == "always":
        return {
            "is_interactive": is_interactive,
            "focused": False,
            "reason": "focus_mode=always (notify regardless of focus)",
            "host": host_summary,
            "frontmost": _query_frontmost_app() if include_frontmost else {},
        }

    front = _query_frontmost_app()
    front_bid = front.get("bundle_id") or ""
    front_bpath = front.get("bundle_path") or ""
    front_pid = front.get("pid") or 0

    if not front_bid and not front_pid:
        return {
            "is_interactive": is_interactive,
            "focused": False,
            "reason": "unable to determine frontmost macOS app (or screen locked)",
            "host": host_summary,
            "frontmost": front,
        }

    if front_bid in LOCK_BUNDLE_IDS:
        return {
            "is_interactive": is_interactive,
            "focused": False,
            "reason": f"screen is locked or screensaver active ({front_bid})",
            "host": host_summary,
            "frontmost": front,
        }

    app_matches = False
    match_reason = ""

    if front_pid and front_pid in ancestor_pids:
        app_matches = True
        match_reason = f"frontmost pid {front_pid} is an ancestor of this CLI"
    elif front_bpath and front_bpath in ancestor_bundles:
        app_matches = True
        match_reason = f"frontmost bundle path ({front_bpath}) owns this CLI"
    elif not has_reattached_tmux_client or not ancestor_bundles:
        if env_bundle_id and front_bid == env_bundle_id:
            app_matches = True
            match_reason = f"frontmost bundleID ({front_bid}) matches __CFBundleIdentifier"
        elif not env_bundle_id and term_program:
            expected_bids = TERM_PROGRAM_BUNDLES.get(term_program.lower(), set())
            if front_bid in expected_bids:
                app_matches = True
                match_reason = f"frontmost bundleID ({front_bid}) matches TERM_PROGRAM={term_program}"

    if not app_matches:
        return {
            "is_interactive": is_interactive,
            "focused": False,
            "reason": (
                f"frontmost app is {front.get('name') or front_bid} ({front_bid}), "
                f"not the CLI host ({env_bundle_id or term_program or 'terminal'})"
            ),
            "host": host_summary,
            "frontmost": front,
        }

    if focus_mode == "tab":
        tab_check: Optional[Tuple[bool, str]] = None
        if front_bid == "com.apple.Terminal":
            tab_check = _check_apple_terminal_tab_focused(our_tty)
        elif front_bid == "com.googlecode.iterm2":
            iterm_sid = "" if has_reattached_tmux_client else os.environ.get("ITERM_SESSION_ID", "")
            tab_check = _check_iterm2_tab_focused(our_tty, iterm_sid)
        elif front_bid in TERM_PROGRAM_BUNDLES["wezterm"]:
            if not has_reattached_tmux_client:
                tab_check = _check_wezterm_pane_focused(os.environ.get("WEZTERM_PANE", ""))
        elif front_bid == "net.kovidgoyal.kitty":
            if not has_reattached_tmux_client:
                tab_check = _check_kitty_window_focused(
                    os.environ.get("KITTY_WINDOW_ID", ""),
                    os.environ.get("KITTY_LISTEN_ON", ""),
                )

        if tab_check is not None:
            is_tab_focused, tab_reason = tab_check
            return {
                "is_interactive": is_interactive,
                "focused": is_tab_focused,
                "reason": tab_reason,
                "host": host_summary,
                "frontmost": front,
            }

    return {
        "is_interactive": is_interactive,
        "focused": True,
        "reason": match_reason,
        "host": host_summary,
        "frontmost": front,
    }


def _find_ancestor_agy_pid() -> Tuple[int, bool]:
    """Return (agy_pid, is_interactive) for the owning `agy` CLI process."""
    mock_pid_str = os.environ.get("DONE_NOTIFY_MOCK_AGY_PID", "").strip()
    mock_pid = 0
    if mock_pid_str:
        try:
            mock_pid = int(mock_pid_str)
        except ValueError:
            mock_pid = 0

    ps_table = _read_ps_table()
    ancestors = _walk_ancestors(os.getpid(), ps_table)
    is_interactive = True
    agy_pid = mock_pid

    for pid, _, args in ancestors:
        headless = _is_agy_command_headless(args)
        if headless is not None:
            if not agy_pid:
                agy_pid = pid
            if headless:
                is_interactive = False
            break

    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        is_interactive = False
    if _parse_bool(os.environ.get("DONE_NOTIFY_FORCE_INTERACTIVE"), False):
        is_interactive = True

    return agy_pid, is_interactive


def _unescape_lsof_path(raw: str) -> str:
    if "\\x" not in raw:
        return raw

    def _repl(match: re.Match[str]) -> str:
        seq = match.group(0)
        try:
            return bytes(
                int(seq[i + 2 : i + 4], 16) for i in range(0, len(seq), 4)
            ).decode("utf-8", errors="replace")
        except ValueError:
            return seq

    return LSOF_HEX_ESCAPE_RE.sub(_repl, raw)


def _resolve_cli_log_path(agy_pid: int, allow_global_fallback: bool = True) -> str:
    """Resolve the active `cli-*.log` file held open by `agy_pid`."""
    override = os.environ.get("DONE_NOTIFY_CLI_LOG_FILE", "").strip()
    if override:
        return override

    if agy_pid > 1:
        try:
            env = dict(os.environ)
            env.pop("LC_ALL", None)
            env["LC_CTYPE"] = "UTF-8"
            out = subprocess.check_output(
                ["/usr/sbin/lsof", "-a", "-p", str(agy_pid), "-d", "1,2", "-Fn"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
                env=env,
            )
            for line in out.splitlines():
                if line.startswith("n/"):
                    candidate = _unescape_lsof_path(line[1:].strip())
                    if candidate.endswith(".log") and os.path.isfile(candidate):
                        return candidate
        except (OSError, subprocess.SubprocessError):
            pass

    if not allow_global_fallback:
        return ""

    log_dir = os.path.expanduser("~/.gemini/antigravity-cli/log")
    symlink_path = os.path.join(log_dir, "cli.log")
    if os.path.exists(symlink_path):
        resolved = _safe_realpath(symlink_path)
        if os.path.isfile(resolved):
            return resolved

    try:
        entries = [
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.startswith("cli-") and name.endswith(".log")
        ]
        entries = [p for p in entries if os.path.isfile(p)]
        if entries:
            entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            return entries[0]
    except OSError:
        pass

    return ""


def _inspect_prompt_watcher_status(agy_pid: int) -> Dict[str, Any]:
    if agy_pid <= 1:
        return {"running": False, "agy_pid": agy_pid}
    state = _read_json_file(_watcher_state_path(agy_pid))
    if not state:
        return {"running": False, "agy_pid": agy_pid}
    wpid = int(state.get("watcher_pid") or 0)
    alive = _pid_alive(wpid)
    if alive:
        rec_lstart = str(state.get("watcher_lstart") or "")
        cur_lstart = _process_lstart(wpid)
        if rec_lstart and cur_lstart and rec_lstart != cur_lstart:
            alive = False
    return {
        "running": alive,
        "agy_pid": agy_pid,
        "watcher_pid": wpid if alive else 0,
        "watcher_lstart": str(state.get("watcher_lstart") or ""),
        "script_version": str(state.get("script_version") or ""),
        "log_path": str(state.get("log_path") or ""),
    }


def _write_watcher_context(
    agy_pid: int,
    project: str,
    conv_id: str = "",
    conversation: str = "",
    app_data_dir: str = "",
) -> None:
    if agy_pid <= 1:
        return
    ctx: Dict[str, Any] = {
        "project": project,
        "updated_at": time.time(),
    }
    if conv_id:
        ctx["conversation_id"] = conv_id
    if conversation:
        ctx["conversation"] = conversation
    if app_data_dir:
        ctx["app_data_dir"] = app_data_dir
    try:
        _atomic_write_json(_watcher_context_path(agy_pid), ctx)
    except OSError:
        pass


def _stop_outdated_watcher(status: Dict[str, Any]) -> bool:
    """Stop a running prompt watcher started by an older script version.

    Returns True if the old watcher was stopped (or already exited), False if it
    could not be verified as our watcher process.
    """
    wpid = int(status.get("watcher_pid") or 0)
    rec_lstart = str(status.get("watcher_lstart") or "")
    if wpid <= 1 or wpid == os.getpid() or not rec_lstart:
        return False
    cur_lstart = _process_lstart(wpid)
    if not cur_lstart or cur_lstart != rec_lstart:
        return False
    ps_table = _read_ps_table()
    entry = ps_table.get(wpid)
    if not entry or "--watch-prompts" not in entry[2]:
        return False
    try:
        os.kill(wpid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(wpid)
    for _ in range(8):
        if not _pid_alive(wpid):
            return True
        time.sleep(0.05)
    return not _pid_alive(wpid)


def _ensure_prompt_watcher(
    agy_pid: int,
    project: str,
    conv_id: str = "",
    conversation: str = "",
    app_data_dir: str = "",
) -> None:
    if agy_pid <= 1 or not _pid_alive(agy_pid):
        return

    _write_watcher_context(
        agy_pid,
        project=project,
        conv_id=conv_id,
        conversation=conversation,
        app_data_dir=app_data_dir,
    )

    status = _inspect_prompt_watcher_status(agy_pid)
    if status.get("running"):
        if status.get("script_version") == SCRIPT_VERSION:
            return
        if not _stop_outdated_watcher(status):
            return

    cmd: List[str] = [
        sys.executable or "/usr/bin/python3",
        os.path.abspath(__file__),
        "--watch-prompts",
        "--agy-pid",
        str(agy_pid),
        "--project",
        project,
    ]
    override_log = os.environ.get("DONE_NOTIFY_CLI_LOG_FILE", "").strip()
    if override_log and os.path.isfile(override_log):
        try:
            cmd.extend(["--start-offset", str(os.path.getsize(override_log))])
        except OSError:
            pass

    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        pass


def _record_question_marker(agy_pid: int) -> None:
    if agy_pid <= 1:
        return
    try:
        _atomic_write_json(_question_marker_path(agy_pid), {"ts": time.time()})
    except OSError:
        pass


def _recent_question_marker(agy_pid: int) -> bool:
    if agy_pid <= 1:
        return False
    marker = _read_json_file(_question_marker_path(agy_pid))
    if not marker:
        return False
    try:
        ts = float(marker.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    return 0.0 <= (time.time() - ts) < PRETOOL_QUESTION_DEDUP_SEC


def _unescape_go_quoted(raw_inner: str) -> str:
    try:
        val = json.loads('"' + raw_inner + '"')
        if isinstance(val, str):
            return val
    except ValueError:
        pass
    return raw_inner


def _read_tail_jsonl_entries(path: str, max_bytes: int = 262144) -> List[Tuple[int, Dict[str, Any]]]:
    """Read trailing JSONL objects from `path` with their 0-based line indices if small, or -1."""
    if not path or not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size <= max_bytes:
                raw = fh.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()
                base_idx = 0
            else:
                fh.seek(size - max_bytes)
                raw = fh.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()
                if lines:
                    lines = lines[1:]  # drop partial leading line
                base_idx = -1
    except OSError:
        return []

    parsed: List[Tuple[int, Dict[str, Any]]] = []
    for offset, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict):
            line_idx = (base_idx + offset) if base_idx >= 0 else -1
            parsed.append((line_idx, obj))
    return parsed


def _resolve_full_step_content(
    transcript_path: str, line_idx: int, step_index: Any
) -> Optional[str]:
    full_path = os.path.join(os.path.dirname(transcript_path), "transcript_full.jsonl")
    if not os.path.isfile(full_path):
        return None
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh):
                if line_idx >= 0 and idx < line_idx:
                    continue
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    continue
                if isinstance(obj, dict) and (
                    (step_index is not None and obj.get("step_index") == step_index)
                    or (line_idx >= 0 and idx == line_idx)
                ):
                    content = obj.get("content")
                    return content if isinstance(content, str) else None
    except OSError:
        return None
    return None


def _is_swarm_wait_result_step(step: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(step, dict):
        return False
    if str(step.get("type") or "") != "GENERIC":
        return False
    content = step.get("content")
    return isinstance(content, str) and bool(SWARM_WAIT_RESULT_RE.search(content))


def _planner_called_swarm_wait(step: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(step, dict):
        return False
    if str(step.get("type") or "") != "PLANNER_RESPONSE":
        return False
    tool_calls = step.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    for tc in tool_calls:
        if isinstance(tc, dict) and str(tc.get("name") or "") == "swarm_wait":
            return True
    return False


def _step_ended_prior_turn(step: Optional[Dict[str, Any]]) -> bool:
    if step is None:
        return True
    if not isinstance(step, dict):
        return False
    if str(step.get("type") or "") == "PLANNER_RESPONSE" and not step.get("tool_calls"):
        return True
    if _is_swarm_wait_result_step(step) or _planner_called_swarm_wait(step):
        return True
    return False


def _read_tool_output_file(
    raw_content: str, transcript_path: str, step_index: Any
) -> Optional[str]:
    candidates: List[str] = []
    match = TOOL_OUTPUT_FILE_RE.search(raw_content)
    if match:
        candidates.append(unquote(match.group(1)))
    if (
        transcript_path
        and step_index is not None
        and re.fullmatch(r"\d+", str(step_index))
        and "output was truncated" in raw_content.lower()
    ):
        sysgen_dir = os.path.dirname(os.path.dirname(transcript_path))
        sibling = os.path.join(sysgen_dir, "steps", str(step_index), "output.txt")
        if sibling not in candidates:
            candidates.append(sibling)
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > TOOL_OUTPUT_MAX_BYTES:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def _extract_board_json_messages(raw_text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the `messages` list from a `read_board` tool output string, or None."""
    if not raw_text or '"messages"' not in raw_text:
        return None
    decoder = json.JSONDecoder()
    search_from = 0
    while True:
        idx = raw_text.find('{"messages"', search_from)
        if idx < 0:
            m = re.search(r'\{\s*"messages"\s*:', raw_text[search_from:])
            if not m:
                break
            idx = search_from + m.start()
        try:
            obj, _ = decoder.raw_decode(raw_text, idx)
            if isinstance(obj, dict) and isinstance(obj.get("messages"), list):
                return [item for item in obj["messages"] if isinstance(item, dict)]
        except ValueError:
            pass
        search_from = idx + 1
    return None


def _parse_board_messages_from_step(
    step: Dict[str, Any], transcript_path: str, line_idx: int
) -> List[Dict[str, Any]]:
    if str(step.get("type") or "") != "GENERIC":
        return []
    raw_content = step.get("content")
    if not isinstance(raw_content, str) or not raw_content:
        return []
    if '"messages"' not in raw_content and "output.txt" not in raw_content:
        return []

    step_index = step.get("step_index")
    full_from_file = _read_tool_output_file(raw_content, transcript_path, step_index)
    if full_from_file:
        parsed = _extract_board_json_messages(full_from_file)
        if parsed is not None:
            return parsed

    truncated = step.get("truncated_fields") or []
    if "content" not in truncated:
        parsed = _extract_board_json_messages(raw_content)
        if parsed is not None:
            return parsed

    resolved_content = raw_content
    if "content" in truncated and transcript_path:
        full_content = _resolve_full_step_content(transcript_path, line_idx, step_index)
        if full_content:
            resolved_content = full_content
            from_resolved_file = _read_tool_output_file(
                resolved_content, transcript_path, step_index
            )
            if from_resolved_file:
                parsed = _extract_board_json_messages(from_resolved_file)
                if parsed is not None:
                    return parsed
            parsed = _extract_board_json_messages(resolved_content)
            if parsed is not None:
                return parsed

    fallback_msgs: List[Dict[str, Any]] = []
    for match in SWARM_MSG_FALLBACK_RE.finditer(resolved_content):
        agent = _unescape_go_quoted(match.group("agent")).strip()
        text = _unescape_go_quoted(match.group("text"))
        if agent:
            fallback_msgs.append({"agent": agent, "text": text})
    return fallback_msgs


def _extract_swarm_turn_info(
    turn_entries: List[Tuple[int, Dict[str, Any]]], transcript_path: str
) -> Dict[str, Any]:
    """Inspect current-turn entries for `swarm_wait` parking and worker `[done]` posts."""
    last_planner: Optional[Dict[str, Any]] = None
    has_wait_result = False
    for _line_idx, step in reversed(turn_entries):
        step_type = str(step.get("type") or "")
        if step_type in MID_TURN_STEP_TYPES:
            continue
        if not has_wait_result and _is_swarm_wait_result_step(step):
            has_wait_result = True
        if step_type == "PLANNER_RESPONSE":
            last_planner = step
            break

    is_swarm_wait = has_wait_result or _planner_called_swarm_wait(last_planner)
    done_workers: List[Tuple[str, str]] = []
    if not is_swarm_wait:
        return {"is_swarm_wait": False, "done_workers": done_workers}

    done_by_agent: Dict[str, str] = {}
    done_order: List[str] = []
    for line_idx, step in turn_entries:
        for msg in _parse_board_messages_from_step(step, transcript_path, line_idx):
            agent = str(msg.get("agent") or "").strip()
            if not agent or agent.lower() == "coordinator":
                continue
            if str(msg.get("kind") or "").strip().lower() == "brief":
                continue
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            lead_match = SWARM_DONE_LEAD_RE.match(text)
            if not lead_match:
                continue
            mentions = {
                m.lower()
                for m in SWARM_MENTION_TOKEN_RE.findall(
                    (lead_match.group("lead") or "") + " " + (lead_match.group("after") or "")
                )
            }
            if mentions and "coordinator" not in mentions and "all" not in mentions:
                continue
            summary = text[lead_match.end():].strip()
            if agent not in done_by_agent:
                done_order.append(agent)
                done_by_agent[agent] = summary
            elif summary:
                done_by_agent[agent] = summary

    done_workers = [(agent, done_by_agent[agent]) for agent in done_order]
    return {"is_swarm_wait": True, "done_workers": done_workers}


def _extract_turn_details(
    payload: Dict[str, Any],
) -> Tuple[Optional[float], bool, str, Dict[str, Any]]:
    """Return (duration_sec, is_background_wakeup, model_answer_markdown, swarm_info)."""
    transcript_path = payload.get("transcriptPath") or ""
    artifact_dir = payload.get("artifactDirectoryPath") or ""
    if (not transcript_path or not os.path.isfile(transcript_path)) and artifact_dir:
        candidate = os.path.join(artifact_dir, ".system_generated", "logs", "transcript.jsonl")
        if os.path.isfile(candidate):
            transcript_path = candidate

    final_output = payload.get("finalModelOutput")
    answer_md = final_output.strip() if isinstance(final_output, str) else ""

    entries = _read_tail_jsonl_entries(transcript_path, max_bytes=1048576)
    duration_sec: Optional[float] = None
    is_bg_wakeup = False
    earliest_model_ts: Optional[float] = None
    turn_entries: List[Tuple[int, Dict[str, Any]]] = []
    seen_turn_work = False

    for pos in range(len(entries) - 1, -1, -1):
        line_idx, step = entries[pos]
        source = str(step.get("source") or "")
        step_type = str(step.get("type") or "")
        step_ts = _parse_iso_epoch(str(step.get("created_at") or ""))

        if step_type in ("USER_INPUT", "PLAN_INPUT"):
            if step_ts is not None:
                duration_sec = max(0.0, time.time() - step_ts)
            is_bg_wakeup = source != "USER_EXPLICIT"
            break

        if seen_turn_work and (
            _is_swarm_wait_result_step(step)
            or (step_type == "PLANNER_RESPONSE" and not step.get("tool_calls"))
        ):
            break

        if not answer_md and step_type == "PLANNER_RESPONSE":
            content = step.get("content")
            if isinstance(content, str) and content.strip():
                truncated = step.get("truncated_fields") or []
                if "content" in truncated and transcript_path:
                    full_content = _resolve_full_step_content(
                        transcript_path, line_idx, step.get("step_index")
                    )
                    if full_content:
                        content = full_content
                answer_md = content.strip()

        turn_entries.append((line_idx, step))

        if source == "MODEL" or step_type in MID_TURN_STEP_TYPES:
            if source == "MODEL":
                if step_ts is not None:
                    earliest_model_ts = step_ts
                if step_type not in MID_TURN_STEP_TYPES:
                    seen_turn_work = True
            continue

        prev_step = entries[pos - 1][1] if pos > 0 else None
        if _step_ended_prior_turn(prev_step):
            if step_ts is not None:
                duration_sec = max(0.0, time.time() - step_ts)
            is_bg_wakeup = True
            break

    if duration_sec is None and earliest_model_ts is not None:
        duration_sec = max(0.0, time.time() - earliest_model_ts)

    turn_entries.reverse()
    swarm_info = _extract_swarm_turn_info(turn_entries, transcript_path)
    return duration_sec, is_bg_wakeup, answer_md, swarm_info


def _normalize_compare_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    resolved = _safe_realpath(raw)
    norm = unicodedata.normalize("NFC", resolved)
    if len(norm) > 1:
        norm = norm.rstrip("/")
    return norm


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na = _normalize_compare_path(a)
    nb = _normalize_compare_path(b)
    if na == nb:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _containment_depth(child: str, parent: str) -> Optional[int]:
    """Return specificity score if `child` is strictly inside `parent`, else None."""
    nc = _normalize_compare_path(child)
    np = _normalize_compare_path(parent)
    if not nc or not np or np == "/":
        return None
    if nc.startswith(np + os.sep):
        return len(np)
    if os.path.exists(nc) and os.path.exists(np):
        cur = os.path.dirname(nc)
        while cur and cur != "/" and cur != os.path.dirname(cur):
            if _same_path(cur, np):
                return len(np)
            cur = os.path.dirname(cur)
    return None


def _get_process_cwd_darwin(pid: int) -> str:
    """Return the current working directory of `pid` on macOS via libproc or lsof."""
    global _LIBPROC_CACHE
    if pid <= 1 or sys.platform != "darwin":
        return ""
    if _LIBPROC_CACHE is None:
        try:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            fn = lib.proc_pidinfo
            fn.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            fn.restype = ctypes.c_int
            _LIBPROC_CACHE = fn
        except (OSError, AttributeError):
            _LIBPROC_CACHE = False

    if callable(_LIBPROC_CACHE):
        try:
            buf = ctypes.create_string_buffer(_PROC_VNODEPATHINFO_SIZE)
            ret = _LIBPROC_CACHE(
                pid,
                _PROC_PIDVNODEPATHINFO,
                0,
                buf,
                _PROC_VNODEPATHINFO_SIZE,
            )
            if ret == _PROC_VNODEPATHINFO_SIZE:
                raw_path = buf.raw[
                    _PVI_CDIR_PATH_OFFSET : _PVI_CDIR_PATH_OFFSET + _MAXPATHLEN
                ].split(b"\x00", 1)[0]
                decoded = raw_path.decode("utf-8", errors="replace").strip()
                if decoded.startswith("/"):
                    return decoded
        except (OSError, ValueError):
            pass

    try:
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env["LC_CTYPE"] = "UTF-8"
        out = subprocess.check_output(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
            env=env,
        )
        for line in out.splitlines():
            if line.startswith("n/"):
                candidate = _unescape_lsof_path(line[1:].strip())
                if candidate.startswith("/"):
                    return candidate
    except (OSError, subprocess.SubprocessError):
        pass

    return ""


def _read_cli_log_workspace(agy_pid: int) -> str:
    """Extract the primary launch workspace recorded at startup in `agy`'s CLI log."""
    log_path = _resolve_cli_log_path(agy_pid, allow_global_fallback=False)
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path, "rb") as fh:
            head = fh.read(65536).decode("utf-8", errors="replace")
    except OSError:
        return ""
    for raw_line in head.splitlines():
        gmatch = GLOG_LINE_RE.match(raw_line.rstrip("\r"))
        if not gmatch:
            continue
        wmatch = CLI_STORE_WORKSPACE_RE.match(gmatch.group("msg"))
        if wmatch:
            candidate = wmatch.group("path").strip()
            if candidate.startswith("/"):
                return candidate
    return ""


def _get_agy_launch_cwd(agy_pid: int) -> str:
    """Return the owning `agy` CLI process's launch workspace directory."""
    if "DONE_NOTIFY_MOCK_AGY_CWD" in os.environ:
        return os.environ.get("DONE_NOTIFY_MOCK_AGY_CWD", "").strip()
    if agy_pid > 1:
        cwd = _get_process_cwd_darwin(agy_pid)
        if cwd:
            return cwd
    return _read_cli_log_workspace(agy_pid)


def _default_cli_project_path() -> str:
    if "DONE_NOTIFY_DEFAULT_PROJECT_JSON" in os.environ:
        return os.environ.get("DONE_NOTIFY_DEFAULT_PROJECT_JSON", "").strip()
    return os.path.expanduser("~/.gemini/config/projects/default-cli-project.json")


def _file_uri_to_path(uri: str) -> str:
    raw = (uri or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return raw
    parsed = urlparse(raw)
    if parsed.scheme != "file":
        return ""
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in ("localhost",):
        path = f"/{parsed.netloc}{path}"
    return path


def _default_cli_project_folders() -> List[str]:
    """Return folder paths statically attached to `default-cli-project.json`."""
    path = _default_cli_project_path()
    if not path or not os.path.isfile(path):
        return []
    data = _read_json_file(path)
    if not data:
        return []
    proj_res = data.get("projectResources")
    if not isinstance(proj_res, dict):
        return []
    resources = proj_res.get("resources")
    if not isinstance(resources, list):
        return []
    folders: List[str] = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        uri = str(item.get("folderUri") or "").strip()
        local_path = _file_uri_to_path(uri)
        if local_path:
            folders.append(local_path)
    return folders


def _collect_payload_workspaces(payload: Dict[str, Any]) -> Tuple[List[str], str]:
    candidates: List[str] = []
    seen_norm: Set[str] = set()

    def _add(raw_path: Any) -> None:
        if not isinstance(raw_path, str):
            return
        cleaned = raw_path.strip()
        if not cleaned:
            return
        norm = _normalize_compare_path(cleaned)
        if norm and norm not in seen_norm:
            seen_norm.add(norm)
            candidates.append(cleaned)

    ws = payload.get("workspacePaths")
    if isinstance(ws, list):
        for entry in ws:
            _add(entry)

    payload_cwd = str(payload.get("cwd") or "").strip()
    if not candidates and payload_cwd:
        _add(payload_cwd)

    return candidates, payload_cwd


def _resolve_project_dir(
    payload: Dict[str, Any], agy_pid: Optional[int] = None
) -> str:
    """Resolve the active project directory for a hook payload or CLI invocation.

    In multi-workspace `agy` sessions (such as `--add-dir` or extra folders stored in
    `default-cli-project.json`), `agy` sorts `WorkspaceInfos` lexicographically and
    populates `payload["cwd"]` from `workspacePaths[0]`, which can point to a secondary
    workspace instead of the launched CLI project. Prefer the workspace matching the
    owning `agy` process's working directory while preserving cross-project `/resume`.
    """
    candidates, payload_cwd = _collect_payload_workspaces(payload)

    if len(candidates) == 1:
        return candidates[0]

    pid = agy_pid
    if pid is None:
        pid, _ = _find_ancestor_agy_pid()
    agy_cwd = _get_agy_launch_cwd(pid)

    if candidates:
        if agy_cwd:
            for cand in candidates:
                if _same_path(agy_cwd, cand):
                    return cand

            best_cand = ""
            best_depth = -1
            for cand in candidates:
                depth = _containment_depth(agy_cwd, cand)
                if depth is not None and depth > best_depth:
                    best_depth = depth
                    best_cand = cand
            if best_cand:
                return best_cand

        default_folders = _default_cli_project_folders()
        if default_folders:
            specific = [
                cand
                for cand in candidates
                if not any(_same_path(cand, df) for df in default_folders)
            ]
            if specific:
                candidates = specific

        if payload_cwd and any(_same_path(payload_cwd, cand) for cand in candidates):
            return payload_cwd
        return candidates[0]

    if agy_cwd:
        return agy_cwd

    cur_cwd = os.getcwd()
    plugin_root = os.environ.get("PLUGIN_ROOT", "").strip()
    if plugin_root and _same_path(cur_cwd, plugin_root):
        return ""
    return cur_cwd


def _project_name(
    payload: Dict[str, Any], agy_pid: Optional[int] = None
) -> str:
    proj_dir = _resolve_project_dir(payload, agy_pid=agy_pid)
    if not proj_dir:
        return "agy"
    base = os.path.basename(proj_dir.rstrip("/"))
    return base or proj_dir


def _sanitize_single_line(text: str) -> str:
    """Strip ANSI escape sequences and control characters, collapsing whitespace."""
    if not text:
        return ""
    plain = ANSI_RE.sub("", text)
    chars: List[str] = []
    for ch in plain:
        code = ord(ch)
        if ch in ("\r", "\n", "\t"):
            chars.append(" ")
        elif code < 32 or 0x7F <= code < 0xA0:
            continue
        else:
            chars.append(ch)
    return WHITESPACE_RE.sub(" ", "".join(chars)).strip()


def _unescape_pbtxt_string(raw: str) -> str:
    """Decode protobuf text-format string escapes (including octal/hex UTF-8 bytes)."""
    if "\\" not in raw:
        return raw
    out = bytearray()
    i = 0
    n = len(raw)
    simple_escapes = {
        "a": 0x07,
        "b": 0x08,
        "f": 0x0C,
        "n": 0x0A,
        "r": 0x0D,
        "t": 0x09,
        "v": 0x0B,
        "\\": 0x5C,
        '"': 0x22,
        "'": 0x27,
        "?": 0x3F,
    }
    while i < n:
        ch = raw[i]
        if ch != "\\" or i + 1 >= n:
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in simple_escapes:
            out.append(simple_escapes[nxt])
            i += 2
        elif nxt in "01234567":
            j = i + 1
            while j < n and j < i + 4 and raw[j] in "01234567":
                j += 1
            out.append(int(raw[i + 1 : j], 8) & 0xFF)
            i = j
        elif nxt in ("x", "X") and i + 3 < n:
            hex_part = raw[i + 2 : i + 4]
            try:
                out.append(int(hex_part, 16))
                i += 4
            except ValueError:
                out.extend(nxt.encode("utf-8"))
                i += 2
        elif nxt == "u" and i + 5 < n:
            try:
                out.extend(chr(int(raw[i + 2 : i + 6], 16)).encode("utf-8"))
                i += 6
            except ValueError:
                out.extend(nxt.encode("utf-8"))
                i += 2
        elif nxt == "U" and i + 9 < n:
            try:
                out.extend(chr(int(raw[i + 2 : i + 10], 16)).encode("utf-8"))
                i += 10
            except ValueError:
                out.extend(nxt.encode("utf-8"))
                i += 2
        else:
            out.extend(nxt.encode("utf-8"))
            i += 2
    return out.decode("utf-8", errors="replace")


def _extract_conversation_id(payload: Dict[str, Any]) -> str:
    for key in ("conversationId", "conversation_id", "sessionId", "session_id"):
        val = str(payload.get(key) or "").strip()
        if val:
            return val
    artifact_dir = str(payload.get("artifactDirectoryPath") or "").strip().rstrip("/")
    if artifact_dir:
        parent = os.path.dirname(artifact_dir)
        if os.path.basename(parent) == "brain":
            cand = os.path.basename(artifact_dir)
            if cand and CONV_ID_SAFE_RE.match(cand):
                return cand
    return ""


def _resolve_app_data_dir(payload: Dict[str, Any], explicit: str = "") -> str:
    if explicit:
        return os.path.expanduser(explicit)

    artifact_dir = str(payload.get("artifactDirectoryPath") or "").strip().rstrip("/")
    if artifact_dir:
        brain_dir = os.path.dirname(artifact_dir)
        if os.path.basename(brain_dir) == "brain":
            app_dir = os.path.dirname(brain_dir)
            if app_dir:
                return app_dir

    transcript_path = str(payload.get("transcriptPath") or "").strip()
    if transcript_path:
        cur = os.path.dirname(transcript_path)
        for _ in range(5):
            if os.path.basename(cur) == "brain":
                app_dir = os.path.dirname(cur)
                if app_dir:
                    return app_dir
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent

    for env_key in ("ANTIGRAVITY_APP_DATA_DIR", "JETSKI_APP_DATA_DIR"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            return os.path.expanduser(raw)
    return os.path.expanduser("~/.gemini/antigravity-cli")


def _summaries_db_path(app_data_dir: str) -> str:
    if "DONE_NOTIFY_SUMMARIES_DB" in os.environ:
        return os.environ.get("DONE_NOTIFY_SUMMARIES_DB", "").strip()
    return os.path.join(app_data_dir, "conversation_summaries.db")


def _annotations_pbtxt_path(app_data_dir: str, conv_id: str) -> str:
    if not conv_id or not CONV_ID_SAFE_RE.match(conv_id):
        return ""
    if "DONE_NOTIFY_ANNOTATIONS_DIR" in os.environ:
        ann_dir = os.environ.get("DONE_NOTIFY_ANNOTATIONS_DIR", "").strip()
        return os.path.join(ann_dir, f"{conv_id}.pbtxt") if ann_dir else ""
    return os.path.join(app_data_dir, "annotations", f"{conv_id}.pbtxt")


def _metadata_cache_path(app_data_dir: str) -> str:
    if "DONE_NOTIFY_METADATA_PATH" in os.environ:
        return os.environ.get("DONE_NOTIFY_METADATA_PATH", "").strip()
    return os.path.join(app_data_dir, "cache", "conversation_metadata.json")


def _query_summaries_db_title(db_path: str, conv_id: str) -> str:
    """Read live conversation title from agy's SQLite summaries store."""
    if not db_path or not conv_id or not os.path.isfile(db_path):
        return ""
    encoded = _url_quote(os.path.abspath(db_path), safe="/:")
    for query_suffix in ("mode=ro", "immutable=1"):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(
                f"file:{encoded}?{query_suffix}",
                uri=True,
                timeout=0.15,
            )
            row = conn.execute(
                "SELECT title FROM conversation_summaries WHERE conversation_id = ? LIMIT 1",
                (conv_id,),
            ).fetchone()
            if row and isinstance(row[0], str):
                cleaned = _sanitize_single_line(row[0])
                if cleaned:
                    return cleaned
            row_fts = conn.execute(
                "SELECT title FROM conversations_fts WHERE chunk_index = -1 AND conversation_id = ? LIMIT 1",
                (conv_id,),
            ).fetchone()
            if row_fts and isinstance(row_fts[0], str):
                cleaned = _sanitize_single_line(row_fts[0])
                if cleaned:
                    return cleaned
            return ""
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    return ""


def _read_annotation_title(pbtxt_path: str) -> str:
    """Read live conversation title from `annotations/<conv_id>.pbtxt`."""
    if not pbtxt_path or not os.path.isfile(pbtxt_path):
        return ""
    try:
        with open(pbtxt_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(16384)
    except OSError:
        return ""
    match = PBTXT_TITLE_RE.search(raw)
    if not match:
        return ""
    return _sanitize_single_line(_unescape_pbtxt_string(match.group(1)))


def _read_metadata_cache_title(meta_path: str, conv_id: str) -> str:
    if not meta_path or not conv_id or not os.path.isfile(meta_path):
        return ""
    meta = _read_json_file(meta_path)
    if not meta:
        return ""
    convs = meta.get("conversations")
    if not isinstance(convs, dict):
        return ""
    entry = convs.get(conv_id)
    if not isinstance(entry, dict):
        return ""
    summary = entry.get("summary")
    if not isinstance(summary, dict):
        return ""
    raw_title = summary.get("Title") or summary.get("title")
    if isinstance(raw_title, str):
        return _sanitize_single_line(raw_title)
    return ""


def _conversation_title(payload: Dict[str, Any], app_data_dir: str = "") -> str:
    """Resolve the active conversation name from hook payload or agy's local stores."""
    for key in (
        "conversationTitle",
        "conversationName",
        "sessionName",
        "conversation_title",
        "conversation_name",
        "session_name",
    ):
        val = payload.get(key)
        if isinstance(val, str):
            cleaned = _sanitize_single_line(val)
            if cleaned:
                return cleaned

    conv = payload.get("conversation")
    if isinstance(conv, dict):
        for key in ("title", "name"):
            val = conv.get(key)
            if isinstance(val, str):
                cleaned = _sanitize_single_line(val)
                if cleaned:
                    return cleaned

    conv_id = _extract_conversation_id(payload)
    if not conv_id:
        return ""

    data_dir = _resolve_app_data_dir(payload, explicit=app_data_dir)
    db_title = _query_summaries_db_title(_summaries_db_path(data_dir), conv_id)
    if db_title:
        return db_title

    ann_title = _read_annotation_title(_annotations_pbtxt_path(data_dir, conv_id))
    if ann_title:
        return ann_title

    return _read_metadata_cache_title(_metadata_cache_path(data_dir), conv_id)


def _format_body_with_conversation(
    body: str, conversation: str, cfg: Dict[str, Any]
) -> str:
    """Prepend `[<conversation>]` on its own leading line when available and enabled."""
    if not cfg.get("show_conversation", True):
        return clip_text(body, cfg["body_max"])
    clean_conv = _sanitize_single_line(conversation)
    if not clean_conv:
        return clip_text(body, cfg["body_max"])
    clipped_conv = clip_text(clean_conv, CONV_TITLE_MAX)
    prefix = f"[{clipped_conv}]\n"
    rem_budget = max(40, cfg["body_max"] - len(prefix))
    clipped_body = clip_text(body, rem_budget)
    return f"{prefix}{clipped_body}" if clipped_body else f"[{clipped_conv}]"


def _resolve_context_conversation(ctx: Optional[Dict[str, Any]]) -> str:
    """Resolve live conversation title from a watcher context dict."""
    if not isinstance(ctx, dict):
        return ""
    conv_id = str(ctx.get("conversation_id") or "").strip()
    app_data_dir = str(ctx.get("app_data_dir") or "").strip()
    if conv_id:
        live = _conversation_title({"conversationId": conv_id}, app_data_dir=app_data_dir)
        if live:
            return live
    return _sanitize_single_line(str(ctx.get("conversation") or ""))


def _build_stop_notification(
    payload: Dict[str, Any], cfg: Dict[str, Any], agy_pid: Optional[int] = None
) -> Optional[Tuple[str, str, str]]:
    """Build (subtitle, body, sound_name) for a Stop event, or None if filtered."""
    reason = str(payload.get("terminationReason") or "NO_TOOL_CALL")
    if reason == "USER_CANCELED":
        return None

    fully_idle = bool(payload.get("fullyIdle", True))
    is_failure = reason in FAILURE_STOP_REASONS
    if not is_failure and not fully_idle and not cfg["notify_when_bg_running"]:
        return None

    error_raw = str(payload.get("error") or "").strip()
    cleaned_error = plain_text(error_raw)

    duration_sec, is_bg_wakeup, answer_md, swarm_info = _extract_turn_details(payload)
    cleaned_answer = plain_text(answer_md)

    project = _project_name(payload, agy_pid=agy_pid)
    conversation = _conversation_title(payload)
    zh = cfg["lang"] == "zh"

    # `/swarm` coordinator turns end with `swarm_wait` via a terminal custom hook
    # (`TERMINAL_CUSTOM_HOOK` with no error). Treat that as a normal park, not an error.
    if reason == "TERMINAL_CUSTOM_HOOK" and not cleaned_error and swarm_info.get("is_swarm_wait"):
        done_workers = swarm_info.get("done_workers") or []
        if done_workers:
            if not cfg.get("notify_on_swarm_done", True):
                return None
            if len(done_workers) == 1:
                agent, summary = done_workers[0]
                status = f"@{agent} 已完成" if zh else f"@{agent} done"
                cleaned_summary = plain_text(summary)
                raw_body = cleaned_summary or (
                    f"@{agent} 已完成任务。" if zh else f"@{agent} finished its task."
                )
            else:
                count = len(done_workers)
                status = f"{count}个子代理已完成" if zh else f"{count} workers done"
                names = ("、" if zh else ", ").join(f"@{agent}" for agent, _ in done_workers)
                latest_summary = ""
                for _agent, summary in reversed(done_workers):
                    cleaned_summary = plain_text(summary)
                    if cleaned_summary:
                        latest_summary = cleaned_summary
                        break
                if latest_summary:
                    raw_body = f"{names}：{latest_summary}" if zh else f"{names}: {latest_summary}"
                else:
                    raw_body = (
                        f"{names} 已完成任务。" if zh else f"{names} finished their tasks."
                    )
            subtitle = f"{project} · {status}"
            body = _format_body_with_conversation(raw_body, conversation, cfg)
            return subtitle, body, cfg["sound"]

        if not cleaned_answer:
            return None

        is_failure = False
        if not fully_idle and not cfg["notify_when_bg_running"]:
            return None

    if (
        not is_failure
        and cfg["min_duration_sec"] > 0
        and duration_sec is not None
        and duration_sec < cfg["min_duration_sec"]
    ):
        return None

    took = format_duration(duration_sec)

    if is_failure:
        if reason == "MAX_TOKEN_BUDGET_EXCEEDED":
            status = (
                f"超出Token预算（{took}）" if (zh and took)
                else ("超出Token预算" if zh else (f"token budget exceeded after {took}" if took else "token budget exceeded"))
            )
        elif reason in ("MAX_INVOCATIONS", "MAX_FORCED_INVOCATIONS"):
            status = (
                f"达到步数上限（{took}）" if (zh and took)
                else ("达到步数上限" if zh else (f"max steps reached after {took}" if took else "max steps reached"))
            )
        elif reason == "TERMINAL_CUSTOM_HOOK":
            status = (
                f"被钩子中止（{took}）" if (zh and took)
                else ("被钩子中止" if zh else (f"stopped by hook after {took}" if took else "stopped by hook"))
            )
        else:
            status = (
                f"任务失败（{took}）" if (zh and took)
                else ("任务失败" if zh else (f"turn failed after {took}" if took else "turn failed"))
            )
    else:
        if is_bg_wakeup:
            status = (
                f"后台任务完成（{took}）" if (zh and took)
                else ("后台任务完成" if zh else (f"task done in {took}" if took else "task done"))
            )
        elif not fully_idle:
            status = (
                f"已回复（{took}）· 后台运行中" if (zh and took)
                else ("已回复 · 后台运行中" if zh else (f"replied in {took} · bg running" if took else "replied · bg running"))
            )
        else:
            status = (
                f"已完成（{took}）" if (zh and took)
                else ("已完成" if zh else (f"done in {took}" if took else "done"))
            )

    subtitle = f"{project} · {status}"

    if is_failure:
        raw_body = cleaned_error or cleaned_answer or (
            "本轮执行异常中止。" if zh else "The turn ended with an error."
        )
        sound = cfg["error_sound"]
    else:
        raw_body = cleaned_answer or ("回答完毕。" if zh else "Response finished.")
        sound = cfg["sound"]

    body = _format_body_with_conversation(raw_body, conversation, cfg)
    return subtitle, body, sound


def _build_question_notification(
    payload: Dict[str, Any], cfg: Dict[str, Any], agy_pid: Optional[int] = None
) -> Optional[Tuple[str, str, str]]:
    if not cfg["notify_on_question"]:
        return None
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict) or tool_call.get("name") != "ask_question":
        return None

    args = tool_call.get("args")
    question_text = ""
    if isinstance(args, dict):
        questions = args.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, dict):
                question_text = str(first.get("question") or "").strip()

    zh = cfg["lang"] == "zh"
    project = _project_name(payload, agy_pid=agy_pid)
    conversation = _conversation_title(payload)
    status = "等待你的确认" if zh else "waiting for input"
    subtitle = f"{project} · {status}"
    raw_body = plain_text(question_text) or (
        "需要你选择一项后继续执行。" if zh else "Waiting for your answer to continue."
    )
    body = _format_body_with_conversation(raw_body, conversation, cfg)
    return subtitle, body, cfg["prompt_sound"]


def _build_permission_notification(
    project: str,
    label: str,
    cfg: Dict[str, Any],
    is_subagent: bool = False,
    conversation: str = "",
) -> Optional[Tuple[str, str, str]]:
    if not cfg["notify_on_permission"]:
        return None

    zh = cfg["lang"] == "zh"
    status = "等待权限确认" if zh else "waiting for permission"
    subtitle = f"{project} · {status}"

    if is_subagent:
        raw_body = (
            "子代理操作需要你确认权限后继续执行。"
            if zh
            else "A subagent is waiting for your approval to continue."
        )
    else:
        clean_label = plain_text(label)
        if zh:
            raw_body = PERMISSION_LABELS_ZH.get(
                clean_label, f"操作“{clean_label}”需要你确认权限。"
            )
        else:
            raw_body = PERMISSION_LABELS_EN.get(
                clean_label, f"Action '{clean_label}' is waiting for your permission."
            )

    body = _format_body_with_conversation(raw_body, conversation, cfg)
    return subtitle, body, cfg["prompt_sound"]


def send_macos_notification(
    title: str, subtitle: str, body: str, sound: str, sound_mode: str = "notification"
) -> Tuple[bool, str]:
    dry_run_file = os.environ.get("DONE_NOTIFY_DRY_RUN_FILE")
    dry_run_jsonl = os.environ.get("DONE_NOTIFY_DRY_RUN_JSONL")
    if dry_run_file or dry_run_jsonl:
        record = {
            "title": title,
            "subtitle": subtitle,
            "body": body,
            "sound": sound,
            "sound_mode": sound_mode,
        }
        if dry_run_file:
            with open(dry_run_file, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False)
        if dry_run_jsonl:
            with open(dry_run_jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True, "dry-run recorded"

    notif_sound = sound if sound_mode in ("notification", "both") else ""
    cmd: List[str] = ["/usr/bin/osascript"]
    for line in OSASCRIPT_LINES:
        cmd.extend(["-e", line])
    cmd.extend(["--", body, title, subtitle, notif_sound])

    err_msg = ""
    ok = True
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            ok = False
            err_msg = (proc.stderr or "").strip() or f"osascript exited {proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        ok = False
        err_msg = str(exc)

    if sound and sound_mode in ("afplay", "both"):
        sound_file = sound if os.path.isfile(sound) else f"/System/Library/Sounds/{sound}.aiff"
        if os.path.isfile(sound_file):
            try:
                subprocess.Popen(
                    ["/usr/bin/afplay", sound_file],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                pass

    return ok, err_msg


def _run_prompt_watcher(
    agy_pid: int, initial_project: str, start_offset: Optional[int] = None
) -> int:
    """Tail the owning `agy` CLI log for surfaced permission and question prompts."""
    if sys.platform != "darwin" or agy_pid <= 1 or not _pid_alive(agy_pid):
        return 0

    existing = _inspect_prompt_watcher_status(agy_pid)
    if existing.get("running") and int(existing.get("watcher_pid") or 0) != os.getpid():
        return 0

    stop_requested = False

    def _on_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    agy_lstart = _process_lstart(agy_pid)
    watcher_lstart = _process_lstart(os.getpid())

    log_path = ""
    for _ in range(10):
        if stop_requested or not _pid_alive(agy_pid):
            return 0
        log_path = _resolve_cli_log_path(agy_pid)
        if log_path and os.path.isfile(log_path):
            break
        time.sleep(WATCHER_POLL_SEC)

    if not log_path or not os.path.isfile(log_path):
        return 0

    state_path = _watcher_state_path(agy_pid)
    try:
        _atomic_write_json(
            state_path,
            {
                "watcher_pid": os.getpid(),
                "watcher_lstart": watcher_lstart,
                "script_version": SCRIPT_VERSION,
                "agy_pid": agy_pid,
                "agy_lstart": agy_lstart,
                "log_path": log_path,
                "started_at": time.time(),
            },
        )
    except OSError:
        return 0

    seen_events: Set[Tuple[str, str, str]] = set()
    partial_buf = b""
    deadline = time.monotonic() + WATCHER_MAX_LIFETIME_SEC
    last_lstart_check = time.monotonic()

    try:
        with open(log_path, "rb") as fh:
            try:
                init_size = os.path.getsize(log_path)
            except OSError:
                init_size = 0
            if start_offset is not None and 0 <= start_offset <= init_size:
                fh.seek(start_offset)
            else:
                fh.seek(init_size)

            while not stop_requested and time.monotonic() < deadline:
                if not _pid_alive(agy_pid):
                    break

                now_mono = time.monotonic()
                if now_mono - last_lstart_check >= WATCHER_LSTART_CHECK_SEC:
                    last_lstart_check = now_mono
                    if agy_lstart:
                        cur_lstart = _process_lstart(agy_pid)
                        if cur_lstart and cur_lstart != agy_lstart:
                            break

                try:
                    cur_size = os.path.getsize(log_path)
                    if cur_size < fh.tell():
                        fh.seek(0)
                        partial_buf = b""
                except OSError:
                    break

                chunk = fh.read()
                if not chunk:
                    time.sleep(WATCHER_POLL_SEC)
                    continue

                data = partial_buf + chunk
                raw_lines = data.split(b"\n")
                partial_buf = raw_lines.pop()

                for raw_line in raw_lines:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                    gmatch = GLOG_LINE_RE.match(line)
                    if not gmatch:
                        continue
                    msg = gmatch.group("msg")

                    if msg.startswith("CLI program exited, shutting down") or msg.startswith(
                        "Terminal gone, shutting down"
                    ):
                        stop_requested = True
                        break

                    event_kind = ""
                    target_id = ""
                    step_str = ""
                    label = ""

                    m_tool = TOOL_CONFIRM_RE.match(msg)
                    if m_tool:
                        event_kind = "tool"
                        label = _unescape_go_quoted(m_tool.group("label"))
                        step_str = m_tool.group("step")
                    else:
                        m_sub = SUBAGENT_QUEUE_RE.match(msg)
                        if m_sub:
                            event_kind = "subagent"
                            target_id = m_sub.group("subagent")
                            step_str = m_sub.group("step")
                        else:
                            m_ask = ASK_QUESTION_LOG_RE.match(msg)
                            if m_ask:
                                event_kind = "question"
                                step_str = m_ask.group("step")

                    if not event_kind:
                        continue

                    dedup_key = (event_kind, target_id, step_str)
                    if dedup_key in seen_events:
                        continue
                    if len(seen_events) >= 1024:
                        seen_events.clear()
                    seen_events.add(dedup_key)

                    cfg = load_config()
                    if not cfg["enabled"]:
                        continue

                    ctx = _read_json_file(_watcher_context_path(agy_pid))
                    project = (
                        str((ctx or {}).get("project") or "").strip()
                        or initial_project
                        or _project_name({}, agy_pid=agy_pid)
                    )
                    conversation = _resolve_context_conversation(ctx)

                    notif: Optional[Tuple[str, str, str]] = None
                    if event_kind == "question":
                        if not cfg["notify_on_question"] or _recent_question_marker(agy_pid):
                            continue
                        zh = cfg["lang"] == "zh"
                        subtitle = f"{project} · {'等待你的确认' if zh else 'waiting for input'}"
                        raw_body = (
                            "需要你回答问题后继续执行。"
                            if zh
                            else "Waiting for your answer to continue."
                        )
                        body = _format_body_with_conversation(raw_body, conversation, cfg)
                        notif = (subtitle, body, cfg["prompt_sound"])
                    elif event_kind == "subagent":
                        notif = _build_permission_notification(
                            project, "", cfg, is_subagent=True, conversation=conversation
                        )
                    else:
                        notif = _build_permission_notification(
                            project, label, cfg, is_subagent=False, conversation=conversation
                        )

                    if notif is None:
                        continue

                    focus = inspect_focus_state(
                        focus_mode=cfg["prompt_focus_mode"], target_pid=agy_pid
                    )
                    if not focus.get("is_interactive", True) or focus.get("focused", False):
                        continue

                    subtitle, body, sound = notif
                    send_macos_notification(
                        title=cfg["title"],
                        subtitle=subtitle,
                        body=body,
                        sound=sound,
                        sound_mode=cfg["sound_mode"],
                    )
    finally:
        cur_state = _read_json_file(state_path)
        if cur_state and int(cur_state.get("watcher_pid") or 0) == os.getpid():
            try:
                os.unlink(state_path)
            except OSError:
                pass

    return 0


def _is_subagent_payload(payload: Dict[str, Any]) -> bool:
    parent_id = str(payload.get("parentConversationId") or "").strip()
    if parent_id:
        return True
    conv_id = str(payload.get("conversationId") or "").strip()
    root_id = str(payload.get("rootConversationId") or "").strip()
    if root_id and conv_id and root_id != conv_id:
        return True
    return False


def _is_non_cli_surface(payload: Dict[str, Any]) -> bool:
    paths = [
        str(payload.get("artifactDirectoryPath") or ""),
        str(payload.get("transcriptPath") or ""),
        os.environ.get("ANTIGRAVITY_APP_DATA_DIR", ""),
    ]
    non_empty = [p for p in paths if p]
    if not non_empty:
        return False
    if any("antigravity-cli" in p for p in non_empty):
        return False
    if any("/antigravity/" in p or p.endswith("/antigravity") or "antigravity-ide" in p for p in non_empty):
        return True
    return False


def _run_hook() -> int:
    raw = sys.stdin.read()
    payload: Dict[str, Any] = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}

    event = str(payload.get("hookEventName") or "")
    if event == "Stop":
        response: Dict[str, Any] = {"decision": "stop"}
    elif event == "PreToolUse":
        response = {"decision": "allow"}
    else:
        response = {}

    try:
        if sys.platform != "darwin":
            return _emit(response)

        cfg = load_config()
        if not cfg["enabled"]:
            return _emit(response)

        if _is_subagent_payload(payload):
            return _emit(response)

        if cfg["cli_only"] and _is_non_cli_surface(payload):
            return _emit(response)

        if event in ("SessionStart", "PreInvocation"):
            if cfg["notify_on_permission"] or cfg["notify_on_question"]:
                agy_pid, is_interactive = _find_ancestor_agy_pid()
                if is_interactive and agy_pid > 1:
                    _ensure_prompt_watcher(
                        agy_pid,
                        _project_name(payload, agy_pid=agy_pid),
                        conv_id=_extract_conversation_id(payload),
                        conversation=_conversation_title(payload),
                        app_data_dir=_resolve_app_data_dir(payload),
                    )
            return _emit(response)

        agy_pid = 0
        if event == "Stop":
            reason = str(payload.get("terminationReason") or "NO_TOOL_CALL")
            if reason == "USER_CANCELED":
                return _emit(response)
            active_focus_mode = cfg["focus_mode"]
        elif event == "PreToolUse":
            tool_name = str(((payload.get("toolCall") or {}).get("name") or ""))
            if tool_name != "ask_question" or not cfg["notify_on_question"]:
                return _emit(response)
            agy_pid, _ = _find_ancestor_agy_pid()
            _record_question_marker(agy_pid)
            active_focus_mode = cfg["prompt_focus_mode"]
        else:
            return _emit(response)

        focus = inspect_focus_state(focus_mode=active_focus_mode)
        if not focus.get("is_interactive", True) or focus.get("focused", False):
            return _emit(response)

        resolved_agy_pid = int((focus.get("host") or {}).get("agy_pid") or 0) or agy_pid
        if resolved_agy_pid > 1 and _pid_alive(resolved_agy_pid):
            _write_watcher_context(
                resolved_agy_pid,
                project=_project_name(payload, agy_pid=resolved_agy_pid),
                conv_id=_extract_conversation_id(payload),
                conversation=_conversation_title(payload),
                app_data_dir=_resolve_app_data_dir(payload),
            )
        notif = (
            _build_stop_notification(payload, cfg, agy_pid=resolved_agy_pid)
            if event == "Stop"
            else _build_question_notification(payload, cfg, agy_pid=resolved_agy_pid)
        )
        if notif is not None:
            subtitle, body, sound = notif
            send_macos_notification(
                title=cfg["title"],
                subtitle=subtitle,
                body=body,
                sound=sound,
                sound_mode=cfg["sound_mode"],
            )
    except Exception:
        # Never block or fail the agent loop on notification errors.
        pass

    return _emit(response)


def _emit(obj: Dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    return 0


def _run_cli(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Configure, inspect, or test Done Notify for Antigravity CLI (agy)."
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current terminal host detection, watcher state, and macOS focus status.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a test macOS Notification Center banner with sound immediately.",
    )
    parser.add_argument(
        "--error",
        action="store_true",
        help="With --test, send a failure-style notification using error_sound.",
    )
    parser.add_argument(
        "--question",
        action="store_true",
        help="With --test, send a question-prompt notification using prompt_sound.",
    )
    parser.add_argument(
        "--permission",
        action="store_true",
        help="With --test, send a permission-prompt notification using prompt_sound.",
    )
    parser.add_argument(
        "--swarm-done",
        action="store_true",
        help="With --test, send a /swarm worker [done] completion notification using sound.",
    )
    parser.add_argument(
        "--respect-focus",
        action="store_true",
        help="With --test, suppress the test banner if the current CLI is focused.",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the active configuration JSON.",
    )
    parser.add_argument(
        "--set",
        dest="updates",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Persist a configuration setting in ~/.config/agy-done-notify/config.json.",
    )
    parser.add_argument(
        "--reset-config",
        action="store_true",
        help="Reset configuration back to defaults.",
    )
    parser.add_argument("--watch-prompts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--agy-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--project", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--start-offset", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.watch_prompts:
        return _run_prompt_watcher(
            agy_pid=args.agy_pid,
            initial_project=args.project,
            start_offset=args.start_offset,
        )

    cfg = dict(DEFAULT_CONFIG) if args.reset_config else load_config()
    if args.reset_config and not args.updates:
        saved_path = save_config(cfg)
        print(f"Reset config at {saved_path}")

    if args.updates:
        raw_updates = dict(cfg)
        for item in args.updates:
            if "=" not in item:
                print(f"Invalid --set argument (expected KEY=VALUE): {item}", file=sys.stderr)
                return 2
            key, val = item.split("=", 1)
            key = key.strip()
            if key not in DEFAULT_CONFIG:
                valid = ", ".join(sorted(DEFAULT_CONFIG.keys()))
                print(f"Unknown config key '{key}'. Valid keys: {valid}", file=sys.stderr)
                return 2
            raw_updates[key] = val.strip()
        cfg = _normalize_config(raw_updates)
        saved_path = save_config(cfg)
        print(f"Saved config to {saved_path}")

    if args.show_config or args.updates or args.reset_config:
        print(json.dumps({"config_path": _config_path(), "config": cfg}, indent=2, ensure_ascii=False))

    if args.status:
        state = inspect_focus_state(
            focus_mode=cfg["focus_mode"], include_frontmost=True
        )
        agy_pid = int((state.get("host") or {}).get("agy_pid") or 0)
        agy_cwd = _get_agy_launch_cwd(agy_pid)
        ctx = _read_json_file(_watcher_context_path(agy_pid)) if agy_pid > 1 else None
        live_conv = _resolve_context_conversation(ctx)
        if isinstance(state.get("host"), dict):
            state["host"]["agy_cwd"] = agy_cwd
            state["host"]["project"] = _project_name({}, agy_pid=agy_pid)
            state["host"]["conversation"] = live_conv
        watcher_status = _inspect_prompt_watcher_status(agy_pid)
        if ctx:
            if ctx.get("project"):
                watcher_status["project"] = str(ctx["project"])
            if ctx.get("conversation_id"):
                watcher_status["conversation_id"] = str(ctx["conversation_id"])
            if live_conv:
                watcher_status["conversation"] = live_conv
        state["prompt_watcher"] = watcher_status
        state["config_path"] = _config_path()
        state["config"] = cfg
        print(json.dumps(state, indent=2, ensure_ascii=False))

    if args.test:
        is_prompt_test = bool(args.question or args.permission)
        agy_pid = 0
        if args.respect_focus:
            mode_to_check = cfg["prompt_focus_mode"] if is_prompt_test else cfg["focus_mode"]
            state = inspect_focus_state(focus_mode=mode_to_check)
            if state.get("focused"):
                print(f"Skipped test notification because CLI is currently focused ({state.get('reason')}).")
                return 0
            agy_pid = int((state.get("host") or {}).get("agy_pid") or 0)
        else:
            agy_pid, _ = _find_ancestor_agy_pid()
        project = _project_name({}, agy_pid=agy_pid)
        ctx = _read_json_file(_watcher_context_path(agy_pid)) if agy_pid > 1 else None
        zh = cfg["lang"] == "zh"
        conversation = _resolve_context_conversation(ctx) or (
            "测试会话" if zh else "Test Session"
        )
        if args.permission:
            subtitle = f"{project} · {'等待权限确认' if zh else 'waiting for permission'}"
            raw_body = (
                "测试通知：终端命令执行需要你确认权限。"
                if zh
                else "Test notification: A command is waiting for your permission."
            )
            sound = cfg["prompt_sound"]
        elif args.question:
            subtitle = f"{project} · {'等待你的确认' if zh else 'waiting for input'}"
            raw_body = (
                "测试通知：需要你回答问题后继续执行。"
                if zh
                else "Test notification: Waiting for your answer to continue."
            )
            sound = cfg["prompt_sound"]
        elif args.swarm_done:
            subtitle = f"{project} · {'@worker-1 已完成' if zh else '@worker-1 done'}"
            raw_body = (
                "测试通知：子代理已完成分配任务并回报汇总结果。"
                if zh
                else "Test notification: swarm worker completed its assigned task and posted [done]."
            )
            sound = cfg["sound"]
        elif args.error:
            subtitle = f"{project} · {'任务失败（3s）' if zh else 'turn failed after 3s'}"
            raw_body = "测试通知：模拟任务失败提醒。" if zh else "Test notification: simulated failed turn alert."
            sound = cfg["error_sound"]
        else:
            subtitle = f"{project} · {'已完成（3s）' if zh else 'done in 3s'}"
            raw_body = "测试通知：回答完毕提醒已正常工作。" if zh else "Test notification: Done Notify banner and sound are active."
            sound = cfg["sound"]
        body = _format_body_with_conversation(raw_body, conversation, cfg)
        ok, err = send_macos_notification(
            title=cfg["title"],
            subtitle=subtitle,
            body=body,
            sound=sound,
            sound_mode=cfg["sound_mode"],
        )
        if not ok:
            print(f"Notification failed: {err}", file=sys.stderr)
            return 1
        print(f"Sent notification: [{cfg['title']}] {subtitle} — {body} (sound={sound}, mode={cfg['sound_mode']})")

    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return _run_cli(sys.argv[1:])
    return _run_hook()


if __name__ == "__main__":
    sys.exit(main())
