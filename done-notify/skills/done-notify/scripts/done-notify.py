#!/usr/bin/env python3
"""macOS Notification Center + sound notifier for Antigravity CLI (agy).

Modeled on Claude Code's local `done-notify` plugin, extended with terminal and
tab focus detection so notifications only fire when the user is not currently
focused on the active `agy` CLI session.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

OSASCRIPT_LINES = (
    "on run argv",
    "if (count of argv) >= 4 and (item 4 of argv) is not \"\" then",
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

APP_BUNDLE_RE = re.compile(r"(/[^:\n]*?\.app)(?=/Contents/)")
FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
MD_BLOCK_PREFIX_RE = re.compile(r"^\s{0,3}(?:#{1,6}|[-*+]|\d+\.|>)\s+", re.MULTILINE)
MD_EMPHASIS_RE = re.compile(r"\*\*|~~|__")
HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
WHITESPACE_RE = re.compile(r"\s+")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "focus_mode": "tab",          # "tab" | "app" | "always"
    "sound": "Glass",             # macOS sound in /System/Library/Sounds
    "error_sound": "Sosumi",      # macOS sound on failed turns
    "sound_mode": "notification", # "notification" | "afplay" | "both" | "off"
    "lang": "en",                 # "en" | "zh"
    "title": "Antigravity CLI",
    "body_max": 180,
    "min_duration_sec": 0.0,
    "cli_only": True,
    "notify_on_question": True,
    "notify_when_bg_running": True,
}


def _config_path() -> str:
    return os.environ.get(
        "DONE_NOTIFY_CONFIG",
        os.path.expanduser("~/.config/agy-done-notify/config.json"),
    )


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

    focus_mode = str(raw.get("focus_mode", cfg["focus_mode"])).strip().lower()
    if focus_mode in ("tab", "app", "always"):
        cfg["focus_mode"] = focus_mode

    for key in ("sound", "error_sound", "title"):
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

    for key in ("cli_only", "notify_on_question", "notify_when_bg_running"):
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
        'return tty of selected tab of front window\n'
        'end tell'
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
        'tell current session of current window\n'
        'return (unique ID) & "\\t" & (tty)\n'
        'end tell\n'
        'end tell'
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


def inspect_focus_state(focus_mode: str = "tab") -> Dict[str, Any]:
    """Inspect CLI interactivity, terminal ownership, and current macOS focus."""
    ps_table = _read_ps_table()
    ancestors = _walk_ancestors(os.getpid(), ps_table)

    is_interactive = True
    agy_pid = 0
    for pid, _, args in ancestors:
        headless = _is_agy_command_headless(args)
        if headless is not None:
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

    if tmux_pane:
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
    front = _query_frontmost_app()

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
            "frontmost": front,
        }

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


def _extract_turn_details(payload: Dict[str, Any]) -> Tuple[Optional[float], bool, str]:
    """Return (duration_sec, is_background_wakeup, model_answer_markdown)."""
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

    for pos in range(len(entries) - 1, -1, -1):
        line_idx, step = entries[pos]
        source = str(step.get("source") or "")
        step_type = str(step.get("type") or "")

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

        step_ts = _parse_iso_epoch(str(step.get("created_at") or ""))

        if step_type in ("USER_INPUT", "PLAN_INPUT"):
            if step_ts is not None:
                duration_sec = max(0.0, time.time() - step_ts)
            is_bg_wakeup = source != "USER_EXPLICIT"
            break

        if source == "MODEL" or step_type in MID_TURN_STEP_TYPES:
            if source == "MODEL" and step_ts is not None:
                earliest_model_ts = step_ts
            continue

        # Check if a SYSTEM_MESSAGE / non-MODEL step started a reactive wakeup
        # right after a completed prior turn (a PLANNER_RESPONSE without tool_calls).
        prev_step = entries[pos - 1][1] if pos > 0 else None
        prev_ended_turn = (
            prev_step is None
            or (
                str(prev_step.get("type") or "") == "PLANNER_RESPONSE"
                and not prev_step.get("tool_calls")
            )
        )
        if prev_ended_turn:
            if step_ts is not None:
                duration_sec = max(0.0, time.time() - step_ts)
            is_bg_wakeup = True
            break

    if duration_sec is None and earliest_model_ts is not None:
        duration_sec = max(0.0, time.time() - earliest_model_ts)

    return duration_sec, is_bg_wakeup, answer_md


def _project_name(payload: Dict[str, Any]) -> str:
    cwd = str(payload.get("cwd") or "").strip()
    if not cwd:
        ws = payload.get("workspacePaths")
        if isinstance(ws, list) and ws:
            cwd = str(ws[0] or "").strip()
    if not cwd:
        cwd = os.getcwd()
    base = os.path.basename(cwd.rstrip("/"))
    return base or cwd


def _build_stop_notification(
    payload: Dict[str, Any], cfg: Dict[str, Any]
) -> Optional[Tuple[str, str, str]]:
    """Build (subtitle, body, sound_name) for a Stop event, or None if filtered."""
    reason = str(payload.get("terminationReason") or "NO_TOOL_CALL")
    if reason == "USER_CANCELED":
        return None

    fully_idle = bool(payload.get("fullyIdle", True))
    is_failure = reason in FAILURE_STOP_REASONS
    if not is_failure and not fully_idle and not cfg["notify_when_bg_running"]:
        return None

    duration_sec, is_bg_wakeup, answer_md = _extract_turn_details(payload)
    if (
        not is_failure
        and cfg["min_duration_sec"] > 0
        and duration_sec is not None
        and duration_sec < cfg["min_duration_sec"]
    ):
        return None

    project = _project_name(payload)
    took = format_duration(duration_sec)
    zh = cfg["lang"] == "zh"

    if is_failure:
        if reason == "MAX_TOKEN_BUDGET_EXCEEDED":
            status = (
                f"超出Token预算 ({took})" if (zh and took)
                else ("超出Token预算" if zh else (f"token budget exceeded after {took}" if took else "token budget exceeded"))
            )
        elif reason in ("MAX_INVOCATIONS", "MAX_FORCED_INVOCATIONS"):
            status = (
                f"达到步数上限 ({took})" if (zh and took)
                else ("达到步数上限" if zh else (f"max steps reached after {took}" if took else "max steps reached"))
            )
        elif reason == "TERMINAL_CUSTOM_HOOK":
            status = (
                f"被钩子中止 ({took})" if (zh and took)
                else ("被钩子中止" if zh else (f"stopped by hook after {took}" if took else "stopped by hook"))
            )
        else:
            status = (
                f"任务失败 ({took})" if (zh and took)
                else ("任务失败" if zh else (f"turn failed after {took}" if took else "turn failed"))
            )
    else:
        if is_bg_wakeup:
            status = (
                f"后台任务完成 ({took})" if (zh and took)
                else ("后台任务完成" if zh else (f"task done in {took}" if took else "task done"))
            )
        elif not fully_idle:
            status = (
                f"已回复 ({took}) · 后台运行中" if (zh and took)
                else ("已回复 · 后台运行中" if zh else (f"replied in {took} · bg running" if took else "replied · bg running"))
            )
        else:
            status = (
                f"已完成 ({took})" if (zh and took)
                else ("已完成" if zh else (f"done in {took}" if took else "done"))
            )

    subtitle = f"{project} · {status}"

    cleaned_answer = clip_text(plain_text(answer_md), cfg["body_max"])
    error_raw = str(payload.get("error") or "").strip()
    cleaned_error = clip_text(plain_text(error_raw), cfg["body_max"])

    if is_failure:
        body = cleaned_error or cleaned_answer or (
            "本轮执行异常中止。" if zh else "The turn ended with an error."
        )
        sound = cfg["error_sound"]
    else:
        body = cleaned_answer or ("回答完毕。" if zh else "Response finished.")
        sound = cfg["sound"]

    return subtitle, body, sound


def _build_question_notification(
    payload: Dict[str, Any], cfg: Dict[str, Any]
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
    project = _project_name(payload)
    status = "等待你的确认" if zh else "waiting for input"
    subtitle = f"{project} · {status}"
    body = clip_text(
        plain_text(question_text) or ("需要你选择一项后继续执行。" if zh else "Waiting for your answer to continue."),
        cfg["body_max"],
    )
    return subtitle, body, cfg["sound"]


def send_macos_notification(
    title: str, subtitle: str, body: str, sound: str, sound_mode: str = "notification"
) -> Tuple[bool, str]:
    dry_run_file = os.environ.get("DONE_NOTIFY_DRY_RUN_FILE")
    if dry_run_file:
        record = {
            "title": title,
            "subtitle": subtitle,
            "body": body,
            "sound": sound,
            "sound_mode": sound_mode,
        }
        with open(dry_run_file, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
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

        if event == "Stop":
            reason = str(payload.get("terminationReason") or "NO_TOOL_CALL")
            if reason == "USER_CANCELED":
                return _emit(response)
        elif event == "PreToolUse":
            tool_name = str(((payload.get("toolCall") or {}).get("name") or ""))
            if tool_name != "ask_question" or not cfg["notify_on_question"]:
                return _emit(response)
        else:
            return _emit(response)

        focus = inspect_focus_state(focus_mode=cfg["focus_mode"])
        if not focus.get("is_interactive", True) or focus.get("focused", False):
            return _emit(response)

        notif = (
            _build_stop_notification(payload, cfg)
            if event == "Stop"
            else _build_question_notification(payload, cfg)
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
        help="Show current terminal host detection and macOS focus status.",
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
    args = parser.parse_args(argv)

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
        state = inspect_focus_state(focus_mode=cfg["focus_mode"])
        state["config_path"] = _config_path()
        state["config"] = cfg
        print(json.dumps(state, indent=2, ensure_ascii=False))

    if args.test:
        if args.respect_focus:
            state = inspect_focus_state(focus_mode=cfg["focus_mode"])
            if state.get("focused"):
                print(f"Skipped test notification because CLI is currently focused ({state.get('reason')}).")
                return 0
        project = os.path.basename(os.getcwd().rstrip("/")) or "agy"
        zh = cfg["lang"] == "zh"
        if args.error:
            subtitle = f"{project} · {'任务失败 (3s)' if zh else 'turn failed after 3s'}"
            body = "测试通知：模拟任务失败提醒。" if zh else "Test notification: simulated failed turn alert."
            sound = cfg["error_sound"]
        else:
            subtitle = f"{project} · {'已完成 (3s)' if zh else 'done in 3s'}"
            body = "测试通知：回答完毕提醒已正常工作。" if zh else "Test notification: Done Notify banner and sound are active."
            sound = cfg["sound"]
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
