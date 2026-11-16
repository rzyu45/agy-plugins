#!/usr/bin/env python3
"""Antigravity CLI (agy) statusLine renderer inspired by Claude Code's token-weather."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
O_SERIES_RE = re.compile(r"\bo[134]\b")

COLAB_BALANCE_RE = re.compile(
    r"^Current balance:\s*([0-9]+(?:\.[0-9]+)?)\s*compute units\b",
    re.MULTILINE | re.IGNORECASE,
)
COLAB_RATE_RE = re.compile(
    r"^Usage rate:\s*([0-9]+(?:\.[0-9]+)?)\s*/hr\b",
    re.MULTILINE | re.IGNORECASE,
)
COLAB_ASSIGN_RE = re.compile(
    r"^Active assignments:\s*(\d+)\b",
    re.MULTILINE | re.IGNORECASE,
)

SPARK_UNICODE = "▁▂▃▄▅▆▇█"
SPARK_ASCII = "._-=+*#%"

FIVE_HOUR_WINDOW_SEC = 5 * 3600
WEEKLY_WINDOW_SEC = 7 * 86400
QUOTA_CACHE_TTL_SEC = 900

COLAB_ACTIVE_TTL_SEC = 60
COLAB_MAX_STALE_SEC = 7 * 86400
COLAB_STALE_ACTIVE_SEC = 600
COLAB_REFRESH_COOLDOWN_SEC = 30

WEATHER_TIERS = (
    # (upper_bound_exclusive, glyph, ascii_glyph, en_label, zh_label, fg_256, bar_256, bold)
    (25.0, "☀", "*", "Clear", "晴朗", 220, 114, False),
    (50.0, "☁", "~", "Cloudy", "多云", 117, 117, False),
    (75.0, "☂", "/", "Showers", "阵雨", 214, 214, False),
    (90.0, "☇", "!", "Storm", "雷暴", 203, 203, False),
    (1000.0, "↯", "!!", "Compact soon", "建议压缩", 196, 196, True),
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "lang": "en",
    "ascii": False,
    "cache_ttl": 300,
    "bar_width": 8,
    "show_git": True,
    "show_quota": True,
    "quota_metric": "used",
    "show_colab": True,
    "colab_ttl": 180,
    "colab_auth": "auto",
    "lines": 3,
    "show_mode": "always",
}


def _default_config_path() -> str:
    return os.environ.get(
        "TOKEN_WEATHER_CONFIG",
        os.path.expanduser("~/.config/agy-token-weather/config.json"),
    )


def _state_dir() -> str:
    return os.environ.get(
        "TOKEN_WEATHER_STATE_DIR",
        os.path.join(tempfile.gettempdir(), "agy-token-weather"),
    )


def _colab_cache_paths() -> List[str]:
    state_cache = os.path.join(_state_dir(), "_colab_cache.json")
    if "TOKEN_WEATHER_STATE_DIR" in os.environ:
        return [state_cache]
    persistent_cache = os.path.join(os.path.dirname(_default_config_path()), "colab_cache.json")
    return [persistent_cache, state_cache]


def _colab_refresh_stamp_path() -> str:
    return os.path.join(_state_dir(), "_colab_refresh.json")


def _to_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


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


def _normalize_show_mode(val: Any, default: str = "always") -> str:
    if isinstance(val, bool):
        return "always" if val else "off"
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm in ("always", "all", "1", "true", "yes", "on"):
            return "always"
        if norm in ("non-default", "non_default", "nondefault"):
            return "non-default"
        if norm in ("off", "none", "0", "false", "no"):
            return "off"
    return default


def _normalize_colab_auth(val: Any, default: str = "auto") -> str:
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm in ("auto", "oauth2", "adc"):
            return norm
    return default


def _c256(text: str, fg: Optional[int] = None, bold: bool = False, dim: bool = False) -> str:
    codes: List[str] = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if fg is not None:
        codes.append(f"38;5;{fg}")
    if not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def display_width(text: str) -> int:
    """Return rendered terminal column width of a string, ignoring ANSI SGR codes."""
    plain = ANSI_RE.sub("", text)
    width = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


def format_tokens(n: int) -> str:
    """Format token count concisely (e.g., 850, 20.9k, 149.3k, 1.0M)."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        val = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{val}k"
    return f"{n / 1_000_000:.1f}M"


def format_percent(pct: float) -> str:
    """Format percentage concisely (0%, 14.2%, 95%, 100%)."""
    pct = max(0.0, float(pct))
    if pct < 0.05:
        return "0%"
    if pct >= 99.95:
        return f"{pct:.0f}%"
    formatted = f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}%"


def format_cu(val: float) -> str:
    """Format Colab compute units concisely (1000, 986.5, 999.92, 0)."""
    val = max(0.0, float(val))
    if val < 0.005:
        return "0"
    formatted = f"{val:.2f}".rstrip("0").rstrip(".")
    return formatted or "0"


def format_rate(val: float) -> str:
    """Format hourly compute-unit burn rate concisely (13.1, 1.9, 0.07)."""
    val = max(0.0, float(val))
    if val < 0.1:
        formatted = f"{val:.2f}".rstrip("0").rstrip(".")
    else:
        formatted = f"{val:.1f}".rstrip("0").rstrip(".")
    return formatted or "0"


def load_saved_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load persisted user preferences if available."""
    cfg_path = path or _default_config_path()
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def resolve_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Merge default config, persisted file, environment variables, and CLI flags."""
    cfg = dict(DEFAULT_CONFIG)
    saved = load_saved_config()

    for key in DEFAULT_CONFIG:
        if key in saved:
            cfg[key] = saved[key]

    env_lang = os.environ.get("TOKEN_WEATHER_LANG")
    if env_lang:
        cfg["lang"] = env_lang.strip().lower()
    if "TOKEN_WEATHER_ASCII" in os.environ:
        cfg["ascii"] = _parse_bool(os.environ["TOKEN_WEATHER_ASCII"], cfg["ascii"])
    if "TOKEN_WEATHER_CACHE_TTL" in os.environ:
        cfg["cache_ttl"] = max(0, _to_int(os.environ["TOKEN_WEATHER_CACHE_TTL"], cfg["cache_ttl"]))
    if "TOKEN_WEATHER_BAR_WIDTH" in os.environ:
        cfg["bar_width"] = max(1, _to_int(os.environ["TOKEN_WEATHER_BAR_WIDTH"], cfg["bar_width"]))
    if "TOKEN_WEATHER_SHOW_GIT" in os.environ:
        cfg["show_git"] = _parse_bool(os.environ["TOKEN_WEATHER_SHOW_GIT"], cfg["show_git"])
    if "TOKEN_WEATHER_SHOW_QUOTA" in os.environ:
        cfg["show_quota"] = _parse_bool(os.environ["TOKEN_WEATHER_SHOW_QUOTA"], cfg["show_quota"])
    env_qmetric = os.environ.get("TOKEN_WEATHER_QUOTA_METRIC")
    if env_qmetric:
        cfg["quota_metric"] = env_qmetric.strip().lower()
    if "TOKEN_WEATHER_SHOW_COLAB" in os.environ:
        cfg["show_colab"] = _parse_bool(os.environ["TOKEN_WEATHER_SHOW_COLAB"], cfg["show_colab"])
    if "TOKEN_WEATHER_COLAB_TTL" in os.environ:
        cfg["colab_ttl"] = max(15, _to_int(os.environ["TOKEN_WEATHER_COLAB_TTL"], cfg["colab_ttl"]))
    env_cauth = os.environ.get("TOKEN_WEATHER_COLAB_AUTH")
    if env_cauth:
        cfg["colab_auth"] = _normalize_colab_auth(env_cauth, str(cfg["colab_auth"]))
    if "TOKEN_WEATHER_LINES" in os.environ:
        cfg["lines"] = _to_int(os.environ["TOKEN_WEATHER_LINES"], cfg["lines"])
    if "TOKEN_WEATHER_SHOW_MODE" in os.environ:
        cfg["show_mode"] = _normalize_show_mode(
            os.environ["TOKEN_WEATHER_SHOW_MODE"], str(cfg["show_mode"])
        )

    if getattr(args, "lang", None):
        cfg["lang"] = args.lang
    if getattr(args, "ascii", False):
        cfg["ascii"] = True
    if getattr(args, "cache_ttl", None) is not None:
        cfg["cache_ttl"] = max(0, args.cache_ttl)
    if getattr(args, "bar_width", None) is not None:
        cfg["bar_width"] = max(1, args.bar_width)
    if getattr(args, "no_git", False):
        cfg["show_git"] = False
    if getattr(args, "no_quota", False):
        cfg["show_quota"] = False
    if getattr(args, "quota_metric", None):
        cfg["quota_metric"] = args.quota_metric
    if getattr(args, "no_colab", False):
        cfg["show_colab"] = False
    if getattr(args, "colab_ttl", None) is not None:
        cfg["colab_ttl"] = max(15, args.colab_ttl)
    if getattr(args, "colab_auth", None):
        cfg["colab_auth"] = args.colab_auth
    if getattr(args, "lines", None) is not None:
        cfg["lines"] = args.lines
    if getattr(args, "no_mode", False):
        cfg["show_mode"] = "off"
    elif getattr(args, "show_mode", None):
        cfg["show_mode"] = args.show_mode

    if cfg.get("lang") not in ("en", "zh"):
        cfg["lang"] = "en"
    cfg["ascii"] = _parse_bool(cfg.get("ascii"), False)
    cfg["cache_ttl"] = max(0, _to_int(cfg.get("cache_ttl"), 300))
    cfg["bar_width"] = max(1, min(32, _to_int(cfg.get("bar_width"), 8)))
    cfg["show_git"] = _parse_bool(cfg.get("show_git"), True)
    cfg["show_quota"] = _parse_bool(cfg.get("show_quota"), True)
    if cfg.get("quota_metric") not in ("used", "remaining"):
        cfg["quota_metric"] = "used"
    cfg["show_colab"] = _parse_bool(cfg.get("show_colab"), True)
    cfg["colab_ttl"] = max(15, _to_int(cfg.get("colab_ttl"), 180))
    cfg["colab_auth"] = _normalize_colab_auth(cfg.get("colab_auth"), "auto")
    lines_val = _to_int(cfg.get("lines"), 3)
    cfg["lines"] = lines_val if lines_val in (1, 2, 3) else 3
    cfg["show_mode"] = _normalize_show_mode(cfg.get("show_mode"), "always")
    return cfg


def write_atomic_json(path: str, data: Dict[str, Any]) -> None:
    """Write JSON atomically via a temporary file in the target directory."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tw_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_config_key(updates: List[str]) -> Tuple[int, str]:
    """Update persistent config keys from KEY=VALUE pairs."""
    cfg_path = _default_config_path()
    saved = load_saved_config(cfg_path)

    for item in updates:
        if "=" not in item:
            return 2, f"Invalid --set format '{item}'; expected KEY=VALUE"
        key, raw_val = item.split("=", 1)
        key = key.strip()
        val_str = raw_val.strip()

        if key == "lang":
            if val_str.lower() not in ("en", "zh"):
                return 2, f"Invalid lang '{val_str}'; expected 'en' or 'zh'"
            saved[key] = val_str.lower()
        elif key in ("ascii", "show_git", "show_quota", "show_colab"):
            if val_str.lower() not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
                return 2, f"Invalid boolean '{val_str}' for {key}"
            saved[key] = _parse_bool(val_str)
        elif key == "cache_ttl":
            parsed = _to_int(val_str, -1)
            if parsed < 0:
                return 2, f"Invalid cache_ttl '{val_str}'; expected integer >= 0"
            saved[key] = parsed
        elif key == "bar_width":
            parsed = _to_int(val_str, -1)
            if parsed < 1 or parsed > 32:
                return 2, f"Invalid bar_width '{val_str}'; expected integer in 1..32"
            saved[key] = parsed
        elif key == "quota_metric":
            if val_str.lower() not in ("used", "remaining"):
                return 2, f"Invalid quota_metric '{val_str}'; expected 'used' or 'remaining'"
            saved[key] = val_str.lower()
        elif key == "colab_ttl":
            parsed = _to_int(val_str, -1)
            if parsed < 15:
                return 2, f"Invalid colab_ttl '{val_str}'; expected integer >= 15"
            saved[key] = parsed
        elif key == "colab_auth":
            norm = _normalize_colab_auth(val_str, "")
            if not norm:
                return 2, f"Invalid colab_auth '{val_str}'; expected 'auto', 'oauth2', or 'adc'"
            saved[key] = norm
        elif key == "lines":
            parsed = _to_int(val_str, -1)
            if parsed not in (1, 2, 3):
                return 2, f"Invalid lines '{val_str}'; expected 1, 2, or 3"
            saved[key] = parsed
        elif key == "show_mode":
            norm = _normalize_show_mode(val_str, "")
            if not norm:
                return 2, f"Invalid show_mode '{val_str}'; expected 'always', 'non-default', or 'off'"
            saved[key] = norm
        else:
            return 2, f"Unknown config key '{key}'; allowed: {', '.join(sorted(DEFAULT_CONFIG))}"

    write_atomic_json(cfg_path, saved)
    return 0, f"Updated {cfg_path}: {json.dumps(saved, ensure_ascii=False)}"


def _find_colab_bin() -> Optional[str]:
    override = os.environ.get("TOKEN_WEATHER_COLAB_BIN", "").strip()
    if override:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None

    found = shutil.which("colab")
    if found:
        return found

    for candidate in (
        os.path.expanduser("~/.local/bin/colab"),
        "/opt/homebrew/bin/colab",
        "/usr/local/bin/colab",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _read_colab_cache() -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_ts = -1.0
    for path in _colab_cache_paths():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or data.get("balance") is None:
                continue
            ts = _to_float(data.get("ts"), -1.0)
            if ts > best_ts:
                best_ts = ts
                best = data
        except (OSError, ValueError):
            continue
    return best


def _write_colab_cache(entry: Dict[str, Any]) -> None:
    for path in _colab_cache_paths():
        try:
            write_atomic_json(path, entry)
        except OSError:
            continue


def _parse_colab_usage_output(stdout: str) -> Optional[Dict[str, Any]]:
    m_bal = COLAB_BALANCE_RE.search(stdout)
    if not m_bal:
        return None
    m_rate = COLAB_RATE_RE.search(stdout)
    m_asgn = COLAB_ASSIGN_RE.search(stdout)
    return {
        "balance": max(0.0, _to_float(m_bal.group(1))),
        "rate": max(0.0, _to_float(m_rate.group(1))) if m_rate else 0.0,
        "assignments": max(0, _to_int(m_asgn.group(1))) if m_asgn else 0,
    }


def refresh_colab_cache(cfg: Dict[str, Any], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Query `colab usage` non-interactively and update cached compute units."""
    ts_now = time.time() if now is None else now
    colab_bin = _find_colab_bin()
    if not colab_bin:
        return None

    stamp_path = _colab_refresh_stamp_path()
    try:
        write_atomic_json(stamp_path, {"ts": ts_now, "pid": os.getpid()})
    except OSError:
        pass

    auth_cfg = _normalize_colab_auth(cfg.get("colab_auth"), "auto")
    if auth_cfg in ("oauth2", "adc"):
        providers = [auth_cfg]
    else:
        providers = []
        cached = _read_colab_cache()
        if cached and cached.get("auth") in ("oauth2", "adc"):
            providers.append(str(cached["auth"]))
        for cand in ("oauth2", "adc"):
            if cand not in providers:
                providers.append(cand)

    env = dict(os.environ)
    env.setdefault("COLAB_CLI_REQUEST_TIMEOUT", "8")

    for provider in providers:
        try:
            proc = subprocess.run(
                [colab_bin, f"--auth={provider}", "usage"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=12,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        if proc.returncode != 0:
            continue

        parsed = _parse_colab_usage_output(proc.stdout or "")
        if parsed is not None:
            entry = {
                "ts": time.time(),
                "balance": parsed["balance"],
                "rate": parsed["rate"],
                "assignments": parsed["assignments"],
                "auth": provider,
            }
            _write_colab_cache(entry)
            return entry

    return None


def _trigger_colab_refresh_async(now: float) -> None:
    """Spawn a detached background process to refresh Colab usage without blocking statusLine."""
    stamp_path = _colab_refresh_stamp_path()
    try:
        with open(stamp_path, "r", encoding="utf-8") as fh:
            stamp = json.load(fh)
        if isinstance(stamp, dict):
            last_attempt = _to_float(stamp.get("ts"), -1.0)
            if 0.0 <= now - last_attempt < COLAB_REFRESH_COOLDOWN_SEC:
                return
    except (OSError, ValueError):
        pass

    try:
        write_atomic_json(stamp_path, {"ts": now, "pid": os.getpid()})
    except OSError:
        pass

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh-colab", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        pass


def _extract_colab_usage(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    now: float,
) -> Optional[Dict[str, Any]]:
    """Extract Colab compute-unit balance from payload or non-blocking cache."""
    raw_colab = payload.get("colab")
    if isinstance(raw_colab, dict) and raw_colab.get("balance") is not None:
        return {
            "balance": max(0.0, _to_float(raw_colab.get("balance"))),
            "rate": max(0.0, _to_float(raw_colab.get("rate"))),
            "assignments": max(0, _to_int(raw_colab.get("assignments"))),
        }

    if _find_colab_bin() is None:
        return None

    cached = _read_colab_cache()
    if cached is None:
        _trigger_colab_refresh_async(now)
        return None

    ts = _to_float(cached.get("ts"), -1.0)
    age = now - ts
    balance = max(0.0, _to_float(cached.get("balance")))
    rate = max(0.0, _to_float(cached.get("rate")))
    assignments = max(0, _to_int(cached.get("assignments")))

    is_active = rate > 0.005 or assignments > 0
    effective_ttl = (
        min(cfg["colab_ttl"], COLAB_ACTIVE_TTL_SEC) if is_active else cfg["colab_ttl"]
    )

    if age < 0.0 or age > effective_ttl:
        _trigger_colab_refresh_async(now)

    if age > COLAB_MAX_STALE_SEC:
        return None

    if age > COLAB_STALE_ACTIVE_SEC:
        rate = 0.0
        assignments = 0

    return {
        "balance": balance,
        "rate": rate,
        "assignments": assignments,
    }


def _session_file(payload: Dict[str, Any]) -> Optional[str]:
    workspace = payload.get("workspace")
    ws_dir = ""
    if isinstance(workspace, dict):
        ws_dir = str(workspace.get("current_dir") or workspace.get("project_dir") or "")

    raw_id = (
        payload.get("conversation_id")
        or payload.get("session_id")
        or payload.get("transcript_path")
        or payload.get("cwd")
        or ws_dir
    )
    if not raw_id:
        return None
    digest = hashlib.sha256(str(raw_id).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_state_dir(), f"{digest}.json")


def _update_session_state(
    payload: Dict[str, Any],
    used_tokens: int,
    cum_tokens: int,
    now: float,
) -> Dict[str, Any]:
    """Persist per-session turn magnitudes and latest API timestamp."""
    state_path = _session_file(payload)
    state: Optional[Dict[str, Any]] = None
    if state_path:
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError):
            state = None

    if state is None:
        initial_mags: List[int] = []
        if used_tokens > 0:
            initial_mags = [used_tokens]
        elif cum_tokens > 0:
            initial_mags = [cum_tokens]
        new_state = {
            "cum_tokens": cum_tokens,
            "used_tokens": used_tokens,
            "last_api_ts": now if (used_tokens > 0 or cum_tokens > 0) else 0.0,
            "turn_magnitudes": initial_mags,
            "last_delta": 0,
        }
        if state_path:
            try:
                write_atomic_json(state_path, new_state)
            except OSError:
                pass
        return new_state

    prev_cum = _to_int(state.get("cum_tokens"))
    prev_used = _to_int(state.get("used_tokens"))
    mags = [_to_int(x) for x in (state.get("turn_magnitudes") or []) if _to_int(x) >= 0]
    last_api_ts = _to_float(state.get("last_api_ts"))
    last_delta = _to_int(state.get("last_delta"))

    if cum_tokens == 0 and used_tokens == 0 and (prev_cum > 0 or prev_used > 0):
        new_state = {
            "cum_tokens": 0,
            "used_tokens": 0,
            "last_api_ts": 0.0,
            "turn_magnitudes": [],
            "last_delta": 0,
        }
    elif cum_tokens != prev_cum or used_tokens != prev_used:
        delta = used_tokens - prev_used
        turn_step = abs(delta) if delta != 0 else max(0, cum_tokens - prev_cum)
        mags.append(turn_step)
        mags = mags[-8:]
        new_state = {
            "cum_tokens": cum_tokens,
            "used_tokens": used_tokens,
            "last_api_ts": now,
            "turn_magnitudes": mags,
            "last_delta": delta,
        }
    else:
        return {
            "cum_tokens": prev_cum,
            "used_tokens": prev_used,
            "last_api_ts": last_api_ts,
            "turn_magnitudes": mags[-8:],
            "last_delta": last_delta,
        }

    if state_path:
        try:
            write_atomic_json(state_path, new_state)
        except OSError:
            pass
    return new_state


def _parse_reset_seconds(val: Any, now: float) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        return max(0, int(round(ts - now)))
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        try:
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            return max(0, int(round(ts - now)))
        except ValueError:
            pass
        try:
            iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int(round(dt.timestamp() - now)))
        except ValueError:
            return None
    return None


def _parse_quota_bucket(bucket: Any, now: float) -> Optional[Dict[str, Any]]:
    if not isinstance(bucket, dict):
        return None
    if bucket.get("disabled") is True:
        return None

    used_pct: Optional[float] = None
    rem_frac = bucket.get("remaining_fraction")
    used_pct_raw = bucket.get("used_percentage")
    used_frac = bucket.get("used_fraction")

    if rem_frac is not None and not isinstance(rem_frac, bool):
        try:
            used_pct = (1.0 - float(rem_frac)) * 100.0
        except (TypeError, ValueError):
            used_pct = None
    elif used_pct_raw is not None and not isinstance(used_pct_raw, bool):
        try:
            used_pct = float(used_pct_raw)
        except (TypeError, ValueError):
            used_pct = None
    elif used_frac is not None and not isinstance(used_frac, bool):
        try:
            used_pct = float(used_frac) * 100.0
        except (TypeError, ValueError):
            used_pct = None

    if used_pct is None:
        return None

    used_pct = min(100.0, max(0.0, used_pct))

    reset_sec: Optional[int] = None
    ris = bucket.get("reset_in_seconds")
    if ris is not None and not isinstance(ris, bool):
        try:
            reset_sec = max(0, int(round(float(ris))))
        except (TypeError, ValueError):
            reset_sec = None
    if reset_sec is None:
        for key in ("reset_time", "resets_at"):
            if bucket.get(key) is not None:
                reset_sec = _parse_reset_seconds(bucket.get(key), now)
                if reset_sec is not None:
                    break

    return {"used_pct": used_pct, "reset_sec": reset_sec}


def _extract_quotas(payload: Dict[str, Any], now: float) -> Dict[str, Dict[str, Any]]:
    """Extract active quota buckets from payload or fall back to recent quota cache."""
    buckets: Dict[str, Dict[str, Any]] = {}

    for source_key in ("quota", "rate_limits"):
        raw_map = payload.get(source_key)
        if not isinstance(raw_map, dict):
            continue
        for key, item in raw_map.items():
            parsed = _parse_quota_bucket(item, now)
            if parsed is not None:
                buckets[str(key)] = parsed
        if buckets:
            break

    cache_path = os.path.join(_state_dir(), "_quota_cache.json")
    if buckets:
        try:
            write_atomic_json(cache_path, {"ts": now, "buckets": buckets})
        except OSError:
            pass
        return buckets

    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict):
            ts = _to_float(cached.get("ts"), -1.0)
            age = now - ts
            raw_buckets = cached.get("buckets")
            if 0.0 <= age <= QUOTA_CACHE_TTL_SEC and isinstance(raw_buckets, dict):
                elapsed = int(age)
                restored: Dict[str, Dict[str, Any]] = {}
                for key, item in raw_buckets.items():
                    if not isinstance(item, dict) or item.get("used_pct") is None:
                        continue
                    r_sec = item.get("reset_sec")
                    if r_sec is not None:
                        r_sec = max(0, _to_int(r_sec) - elapsed)
                    restored[str(key)] = {
                        "used_pct": min(100.0, max(0.0, _to_float(item.get("used_pct")))),
                        "reset_sec": r_sec,
                    }
                return restored
    except (OSError, ValueError):
        pass
    return {}


def _pick_best_bucket(
    candidates: List[Tuple[str, Dict[str, Any]]],
    predicates: List[Any],
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    for pred in predicates:
        for key, val in candidates:
            if pred(key.lower()):
                return val
    return candidates[0][1]


def _pick_quota_pair(
    buckets: Dict[str, Dict[str, Any]],
    model_id_and_name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Select the 5-hour and weekly quota buckets matching the active model pool."""
    if not buckets:
        return None, None

    cand_5h: List[Tuple[str, Dict[str, Any]]] = []
    cand_wk: List[Tuple[str, Dict[str, Any]]] = []
    for key, val in buckets.items():
        lk = key.lower()
        if any(tok in lk for tok in ("5h", "five_hour", "5-hour", "5_hour")):
            cand_5h.append((key, val))
        elif any(tok in lk for tok in ("weekly", "7d", "seven_day", "week")):
            cand_wk.append((key, val))

    m = model_id_and_name.lower()
    is_3p = any(
        tok in m for tok in ("claude", "sonnet", "opus", "haiku", "anthropic", "gpt", "openai", "3p")
    ) or bool(O_SERIES_RE.search(m))

    if is_3p:
        preds_5h = [
            lambda k: any(tok in k for tok in ("3p", "claude", "anthropic", "gpt", "openai")),
        ]
        preds_wk = preds_5h
    elif "argon" in m:
        preds_5h = [
            lambda k: "argon" in k,
            lambda k: k == "gemini-5h",
            lambda k: "gemini" in k,
        ]
        preds_wk = [
            lambda k: "argon" in k,
            lambda k: k == "gemini-weekly",
            lambda k: "gemini" in k,
        ]
    elif "carbon" in m or "orchestrator" in m:
        preds_5h = [
            lambda k: "carbon" in k,
            lambda k: k == "gemini-5h",
            lambda k: "gemini" in k and "argon" not in k,
        ]
        preds_wk = [
            lambda k: "carbon" in k,
            lambda k: k == "gemini-weekly",
            lambda k: "gemini" in k and "argon" not in k,
        ]
    else:
        preds_5h = [
            lambda k: k == "gemini-5h",
            lambda k: "gemini" in k and "argon" not in k and "carbon" not in k,
        ]
        preds_wk = [
            lambda k: k == "gemini-weekly",
            lambda k: "gemini" in k and "argon" not in k and "carbon" not in k,
        ]

    return _pick_best_bucket(cand_5h, preds_5h), _pick_best_bucket(cand_wk, preds_wk)


def _get_weather_tier(used_pct: float) -> Tuple[str, str, str, str, int, int, bool]:
    for limit, glyph, ascii_glyph, en_lbl, zh_lbl, fg, bar_fg, bold in WEATHER_TIERS:
        if used_pct < limit:
            return glyph, ascii_glyph, en_lbl, zh_lbl, fg, bar_fg, bold
    _, glyph, ascii_glyph, en_lbl, zh_lbl, fg, bar_fg, bold = WEATHER_TIERS[-1]
    return glyph, ascii_glyph, en_lbl, zh_lbl, fg, bar_fg, bold


def _extract_exec_mode(payload: Dict[str, Any]) -> str:
    for key in ("cycle_mode", "mode", "execution_mode", "permission_mode"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            norm = val.strip().lower()
            if norm in ("accept_edits", "acceptedits"):
                return "accept-edits"
            return norm
    return "default"


def _extract_vim_mode(payload: Dict[str, Any]) -> str:
    vim_raw = payload.get("vim")
    if isinstance(vim_raw, dict):
        mode = vim_raw.get("mode")
        if isinstance(mode, str) and mode.strip():
            return mode.strip().upper()
    return ""


def _extract_telemetry(payload: Dict[str, Any]) -> Dict[str, Any]:
    cw = payload.get("context_window")
    if not isinstance(cw, dict):
        cw = {}

    win_size = max(0, _to_int(cw.get("context_window_size")))
    total_in = max(0, _to_int(cw.get("total_input_tokens")))
    total_out = max(0, _to_int(cw.get("total_output_tokens")))
    cu = cw.get("current_usage")
    cu_dict = cu if isinstance(cu, dict) else None
    raw_pct = cw.get("used_percentage")

    if win_size > 0 and raw_pct is not None and not isinstance(raw_pct, bool):
        used_tokens = max(0, int(round(win_size * max(0.0, float(raw_pct)) / 100.0)))
    elif cu_dict is not None:
        used_tokens = max(
            0,
            _to_int(cu_dict.get("input_tokens"))
            + _to_int(cu_dict.get("output_tokens"))
            + _to_int(cu_dict.get("cache_creation_input_tokens"))
            + _to_int(cu_dict.get("cache_read_input_tokens")),
        )
    else:
        used_tokens = total_in + total_out

    if raw_pct is not None and not isinstance(raw_pct, bool):
        used_pct = max(0.0, _to_float(raw_pct))
    elif win_size > 0:
        used_pct = max(0.0, used_tokens / win_size * 100.0)
    else:
        used_pct = 0.0

    cache_read = _to_int(cu_dict.get("cache_read_input_tokens")) if cu_dict else 0
    cache_write = _to_int(cu_dict.get("cache_creation_input_tokens")) if cu_dict else 0

    model_raw = payload.get("model")
    model_name = ""
    model_id = ""
    effort = ""
    if isinstance(model_raw, dict):
        model_id = str(model_raw.get("id") or "").strip()
        model_name = str(model_raw.get("display_name") or model_id or "").strip()
        effort = str(model_raw.get("effort") or "").strip()
    elif isinstance(model_raw, str):
        model_name = model_raw.strip()
        model_id = model_name

    model_match_str = f"{model_id} {model_name}".strip()

    for prefix in ("Google ", "Anthropic "):
        if model_name.startswith(prefix):
            model_name = model_name[len(prefix) :]
            break

    if model_name and effort and effort.lower() not in model_name.lower():
        model_display = f"{model_name} ({effort})"
    else:
        model_display = model_name

    agent_state = str(payload.get("agent_state") or "").strip()
    exec_mode = _extract_exec_mode(payload)
    vim_mode = _extract_vim_mode(payload)

    cost_raw = payload.get("cost")
    total_usd = 0.0
    if isinstance(cost_raw, dict):
        total_usd = max(0.0, _to_float(cost_raw.get("total_usd")))
    elif isinstance(cost_raw, (int, float)):
        total_usd = max(0.0, float(cost_raw))

    sub_raw = payload.get("subagents")
    subagents = len(sub_raw) if isinstance(sub_raw, list) else max(0, _to_int(sub_raw))

    tasks_raw = None
    for key in ("task_count", "background_tasks", "bg_tasks"):
        if payload.get(key) is not None:
            tasks_raw = payload.get(key)
            break
    tasks = len(tasks_raw) if isinstance(tasks_raw, list) else max(0, _to_int(tasks_raw))

    vcs_raw = payload.get("vcs")
    if not isinstance(vcs_raw, dict):
        vcs_raw = payload.get("worktree")
    branch = ""
    dirty = False
    if isinstance(vcs_raw, dict):
        branch = str(vcs_raw.get("branch") or "").strip()
        dirty = bool(vcs_raw.get("dirty"))

    term_width = _to_int(payload.get("terminal_width")) or _to_int(payload.get("render_width_chars"))
    if term_width <= 0:
        term_width = _to_int(os.environ.get("COLUMNS"), 120)

    return {
        "win_size": win_size,
        "used_tokens": used_tokens,
        "cum_tokens": total_in + total_out,
        "used_pct": used_pct,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "model_display": model_display,
        "model_match_str": model_match_str,
        "agent_state": agent_state,
        "exec_mode": exec_mode,
        "vim_mode": vim_mode,
        "total_usd": total_usd,
        "subagents": subagents,
        "tasks": tasks,
        "branch": branch,
        "dirty": dirty,
        "term_width": term_width,
    }


def _render_mode_badge(
    exec_mode: str,
    vim_mode: str,
    show_mode_cfg: str,
    compact: bool = False,
) -> str:
    """Render active session execution mode (auto, plan, accept-edits, default) and Vim mode."""
    if show_mode_cfg == "off":
        return ""

    badges: List[str] = []
    if not (show_mode_cfg == "non-default" and exec_mode == "default"):
        if exec_mode == "plan":
            badges.append(_c256("[plan]", fg=81, bold=True))
        elif exec_mode == "auto":
            badges.append(_c256("[auto]", fg=114, bold=True))
        elif exec_mode == "accept-edits":
            lbl = "[edits]" if compact else "[accept-edits]"
            badges.append(_c256(lbl, fg=214, bold=True))
        elif exec_mode in ("bypass", "yolo", "dangerously-skip-permissions"):
            badges.append(_c256("[bypass]", fg=196, bold=True))
        elif exec_mode == "default":
            badges.append(_c256("[default]", fg=245))
        else:
            badges.append(_c256(f"[{exec_mode}]", fg=183, bold=True))

    if vim_mode:
        vim_map = {
            "NORMAL": "VIM:N",
            "INSERT": "VIM:I",
            "VISUAL": "VIM:V",
            "VISUAL LINE": "VIM:VL",
        }
        vim_tag = vim_map.get(vim_mode, f"VIM:{vim_mode[:3]}")
        badges.append(_c256(f"[{vim_tag}]", fg=183))

    return " ".join(badges)


def _render_bar(used_pct: float, bar_width: int, bar_fg: int, ascii_mode: bool) -> str:
    if used_pct <= 0.0:
        filled = 0
    else:
        filled = min(bar_width, max(1, int(round(used_pct / 100.0 * bar_width))))
    empty = bar_width - filled
    f_ch = "#" if ascii_mode else "▰"
    e_ch = "-" if ascii_mode else "▱"
    filled_part = _c256(f_ch * filled, fg=bar_fg) if filled else ""
    empty_part = _c256(e_ch * empty, fg=240) if empty else ""
    return filled_part + empty_part


def _format_duration_short(sec: int, lang: str) -> str:
    if sec <= 0:
        return "即将" if lang == "zh" else "now"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        h, m = divmod(sec // 60, 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, rem_h = divmod(sec // 3600, 24)
    return f"{d}d{rem_h}h" if rem_h else f"{d}d"


def _render_quota_item(
    label: str,
    bucket: Dict[str, Any],
    window_sec: int,
    cfg: Dict[str, Any],
    include_reset: bool,
    include_bars: bool,
) -> str:
    used_pct = float(bucket["used_pct"])
    reset_sec = bucket.get("reset_sec")

    over_pace = False
    if reset_sec is not None and 0 < reset_sec <= window_sec:
        elapsed_pct = (window_sec - reset_sec) / window_sec * 100.0
        over_pace = used_pct >= 20.0 and (used_pct - elapsed_pct) >= 15.0

    bold = False
    if used_pct >= 90.0:
        fg = 196
        bold = True
    elif used_pct >= 75.0:
        fg = 203
    elif used_pct >= 50.0 or over_pace:
        fg = 214
    else:
        fg = 114

    if cfg["quota_metric"] == "remaining":
        rem_pct = max(0.0, 100.0 - used_pct)
        pct_text = (
            f"余{format_percent(rem_pct)}"
            if cfg["lang"] == "zh"
            else f"{format_percent(rem_pct)} left"
        )
        bar_pct = rem_pct
    else:
        pct_text = format_percent(used_pct)
        bar_pct = used_pct

    arrow = ("!" if cfg["ascii"] else "↑") if over_pace else ""
    parts: List[str] = [_c256(label, fg=247)]
    if include_bars:
        parts.append(_render_bar(bar_pct, 5, fg, cfg["ascii"]))
    parts.append(_c256(f"{pct_text}{arrow}", fg=fg, bold=bold))
    if include_reset and reset_sec is not None:
        parts.append(_c256(_format_duration_short(reset_sec, cfg["lang"]), fg=244))
    return " ".join(parts)


def _render_quota_segment(
    q5h: Optional[Dict[str, Any]],
    qwk: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
    include_reset: bool = True,
    include_bars: bool = False,
) -> str:
    items: List[str] = []
    if q5h is not None:
        items.append(
            _render_quota_item("5h", q5h, FIVE_HOUR_WINDOW_SEC, cfg, include_reset, include_bars)
        )
    if qwk is not None:
        wk_lbl = "周" if cfg["lang"] == "zh" else "wk"
        items.append(
            _render_quota_item(wk_lbl, qwk, WEEKLY_WINDOW_SEC, cfg, include_reset, include_bars)
        )
    if not items:
        return ""
    dot = " * " if cfg["ascii"] else " · "
    return _c256(dot, fg=240).join(items)


def _render_colab_segment(
    colab_info: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
    include_active: bool = True,
) -> str:
    """Render Google Colab remaining compute units and optional active burn rate / VMs."""
    if colab_info is None:
        return ""

    balance = max(0.0, _to_float(colab_info.get("balance")))
    rate = max(0.0, _to_float(colab_info.get("rate")))
    assignments = max(0, _to_int(colab_info.get("assignments")))

    bold = False
    if balance <= 10.0:
        fg = 196
        bold = True
    elif balance <= 30.0:
        fg = 203
    elif balance <= 100.0:
        fg = 214
    else:
        fg = 114

    cu_num = format_cu(balance)
    cu_text = f"余{cu_num} CU" if cfg["lang"] == "zh" else f"{cu_num} CU"
    head = f"{_c256('Colab', fg=247)} {_c256(cu_text, fg=fg, bold=bold)}"

    if not include_active or (rate <= 0.005 and assignments <= 0):
        return head

    sub_parts: List[str] = [head]
    if rate > 0.005:
        sub_parts.append(_c256(f"{format_rate(rate)}/h", fg=214))
    if assignments > 0:
        if cfg["lang"] == "zh":
            vm_lbl = f"{assignments}实例"
        else:
            vm_lbl = "1 VM" if assignments == 1 else f"{assignments} VMs"
        sub_parts.append(_c256(vm_lbl, fg=81))

    dot = " * " if cfg["ascii"] else " · "
    return _c256(dot, fg=240).join(sub_parts)


def _build_usage_line(
    q5h: Optional[Dict[str, Any]],
    qwk: Optional[Dict[str, Any]],
    colab_info: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
    sep: str,
    sep_width: int,
    max_width: int,
) -> str:
    """Combine Model quota usage and Colab remaining compute units within max_width."""
    q_variants: List[str] = []
    if q5h is not None or qwk is not None:
        for inc_reset, inc_bars in ((True, True), (True, False), (False, False)):
            cand = _render_quota_segment(
                q5h, qwk, cfg, include_reset=inc_reset, include_bars=inc_bars
            )
            if cand and cand not in q_variants:
                q_variants.append(cand)

    c_variants: List[str] = []
    if colab_info is not None:
        for inc_active in (True, False):
            cand = _render_colab_segment(colab_info, cfg, include_active=inc_active)
            if cand and cand not in c_variants:
                c_variants.append(cand)

    if q_variants and c_variants:
        for q_cand in q_variants:
            for c_cand in c_variants:
                if display_width(q_cand) + sep_width + display_width(c_cand) <= max_width:
                    return f"{q_cand}{sep}{c_cand}"
        return _append_fitting_segments("", [q_variants[-1], c_variants[-1]], sep, sep_width, max_width)

    if q_variants:
        for q_cand in q_variants:
            if display_width(q_cand) <= max_width:
                return q_cand
        return q_variants[-1]

    if c_variants:
        for c_cand in c_variants:
            if display_width(c_cand) <= max_width:
                return c_cand
        return c_variants[-1]

    return ""


def _render_sparkline(mags: List[int], last_delta: int, ascii_mode: bool) -> str:
    if not mags:
        return ""
    palette = SPARK_ASCII if ascii_mode else SPARK_UNICODE
    pad_ch = "." if ascii_mode else "·"
    max_v = max(mags)
    bars: List[str] = []
    for val in mags[-8:]:
        if max_v <= 0:
            idx = 0
        else:
            idx = min(7, max(0, int(round((val / max_v) * 7))))
        bars.append(palette[idx])
    pad_len = max(0, 8 - len(bars))
    spark = _c256(pad_ch * pad_len, fg=240) + _c256("".join(bars), fg=250)

    if last_delta > 0:
        prefix = "d+" if ascii_mode else "Δ+"
        spark += " " + _c256(f"{prefix}{format_tokens(last_delta)}", fg=248)
    elif last_delta < 0:
        prefix = "d-" if ascii_mode else "Δ-"
        spark += " " + _c256(f"{prefix}{format_tokens(abs(last_delta))}", fg=114)
    return spark


def _render_state_badge(agent_state: str, lang: str) -> str:
    norm = agent_state.lower()
    if norm in ("", "idle", "ready"):
        return ""
    if lang == "zh":
        mapping = {
            "thinking": "思考",
            "working": "运行",
            "running": "运行",
            "tool_use": "工具",
            "tool": "工具",
        }
        label = mapping.get(norm, agent_state)
    else:
        label = agent_state
    return _c256(label, fg=81)


def _render_cache_segment(
    cache_read: int,
    cache_write: int,
    last_api_ts: float,
    agent_state: str,
    cache_ttl: int,
    lang: str,
    now: float,
    ascii_mode: bool,
) -> str:
    parts: List[str] = []
    cache_lbl = "缓存" if lang == "zh" else "cache"
    if cache_read > 0:
        parts.append(_c256(f"{cache_lbl} {format_tokens(cache_read)}", fg=247))
    elif cache_write > 0:
        parts.append(_c256(f"{cache_lbl} +{format_tokens(cache_write)}", fg=247))

    if cache_ttl > 0 and last_api_ts > 0:
        if agent_state.lower() not in ("", "idle", "ready"):
            ttl_lbl = "TTL 活跃" if lang == "zh" else "TTL live"
            parts.append(_c256(ttl_lbl, fg=81))
        else:
            rem = cache_ttl - int(max(0.0, now - last_api_ts))
            if rem > 60:
                parts.append(_c256(f"TTL {rem // 60}m{rem % 60:02d}s", fg=114))
            elif rem > 0:
                parts.append(_c256(f"TTL {rem}s", fg=214))
            else:
                cold_lbl = "TTL 已过期" if lang == "zh" else "TTL cold"
                parts.append(_c256(cold_lbl, fg=244))

    dot = " * " if ascii_mode else " · "
    return _c256(dot, fg=240).join(parts) if parts else ""


def _render_meta_segment(tel: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    parts: List[str] = []
    lang = cfg["lang"]
    if tel["subagents"] > 0:
        lbl = f"子代理:{tel['subagents']}" if lang == "zh" else f"sub:{tel['subagents']}"
        parts.append(_c256(lbl, fg=183))
    if tel["tasks"] > 0:
        lbl = f"任务:{tel['tasks']}" if lang == "zh" else f"tasks:{tel['tasks']}"
        parts.append(_c256(lbl, fg=183))
    if tel["total_usd"] > 0.0:
        parts.append(_c256(f"${tel['total_usd']:.2f}", fg=247))
    if cfg["show_git"] and tel["branch"]:
        dirty_mark = "*" if tel["dirty"] else ""
        parts.append(_c256(f"{tel['branch']}{dirty_mark}", fg=110))
    return " ".join(parts)


def _append_fitting_segments(
    base_line: str,
    segments: List[str],
    sep: str,
    sep_width: int,
    max_width: int,
) -> str:
    line = base_line
    cur_width = display_width(line) if line else 0
    for seg in segments:
        if not seg:
            continue
        seg_w = display_width(seg)
        if not line:
            if seg_w <= max_width:
                line = seg
                cur_width = seg_w
        elif cur_width + sep_width + seg_w <= max_width:
            line = f"{line}{sep}{seg}"
            cur_width += sep_width + seg_w
    return line


def render_status_line(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    now: Optional[float] = None,
) -> str:
    """Render 1-, 2-, or 3-line weather-themed status band for Antigravity CLI."""
    ts_now = time.time() if now is None else now
    tel = _extract_telemetry(payload)
    state = _update_session_state(payload, tel["used_tokens"], tel["cum_tokens"], ts_now)

    q5h: Optional[Dict[str, Any]] = None
    qwk: Optional[Dict[str, Any]] = None
    if cfg["show_quota"]:
        quota_buckets = _extract_quotas(payload, ts_now)
        q5h, qwk = _pick_quota_pair(quota_buckets, tel["model_match_str"])

    colab_info: Optional[Dict[str, Any]] = None
    if cfg["show_colab"]:
        colab_info = _extract_colab_usage(payload, cfg, ts_now)

    glyph, ascii_glyph, en_lbl, zh_lbl, fg, bar_fg, bold = _get_weather_tier(tel["used_pct"])
    icon = ascii_glyph if cfg["ascii"] else glyph
    label = zh_lbl if cfg["lang"] == "zh" else en_lbl
    weather_str = _c256(f"{icon} {label}", fg=fg, bold=bold)
    pct_str = _c256(format_percent(tel["used_pct"]), fg=fg, bold=bold)

    if tel["win_size"] > 0:
        tokens_text = f"{format_tokens(tel['used_tokens'])}/{format_tokens(tel['win_size'])}"
    elif tel["used_tokens"] > 0:
        tokens_text = format_tokens(tel["used_tokens"])
    else:
        tokens_text = "就绪" if cfg["lang"] == "zh" else "ready"
    tokens_str = _c256(tokens_text, fg=247)

    bar_str = _render_bar(tel["used_pct"], cfg["bar_width"], bar_fg, cfg["ascii"])

    term_width = tel["term_width"]
    max_width = max(40, term_width - 2) if term_width >= 42 else max(20, term_width - 2)

    mode_full = _render_mode_badge(
        tel["exec_mode"], tel["vim_mode"], cfg["show_mode"], compact=(term_width < 90)
    )
    mode_compact = _render_mode_badge(
        tel["exec_mode"], tel["vim_mode"], cfg["show_mode"], compact=True
    )

    sep = _c256(" | " if cfg["ascii"] else " │ ", fg=239)
    sep_width = display_width(sep)

    spark = _render_sparkline(state["turn_magnitudes"], state["last_delta"], cfg["ascii"])

    model_parts = []
    if tel["model_display"]:
        model_parts.append(_c256(tel["model_display"], fg=252))
    badge = _render_state_badge(tel["agent_state"], cfg["lang"])
    if badge:
        model_parts.append(badge)
    dot = " * " if cfg["ascii"] else " · "
    model_seg = _c256(dot, fg=240).join(model_parts) if model_parts else ""

    cache_seg = _render_cache_segment(
        tel["cache_read"],
        tel["cache_write"],
        state["last_api_ts"],
        tel["agent_state"],
        cfg["cache_ttl"],
        cfg["lang"],
        ts_now,
        cfg["ascii"],
    )

    meta_seg = _render_meta_segment(tel, cfg)

    seg1_base_full = f"{weather_str} {pct_str} {bar_str} {tokens_str}"
    seg1_base_compact = f"{weather_str} {pct_str} {tokens_str}"
    seg1_base_tiny = f"{weather_str} {pct_str}"

    if cfg["lines"] == 3:
        # Line 1: Mode and model (+ auxiliary session metadata)
        line1 = _append_fitting_segments(
            "", [mode_full, model_seg, meta_seg], sep, sep_width, max_width
        )
        if mode_full and model_seg and model_seg not in line1:
            line1 = _append_fitting_segments(
                "", [mode_compact, model_seg, meta_seg], sep, sep_width, max_width
            )
        if not line1 and (mode_compact or model_seg):
            line1 = mode_compact or model_seg

        # Line 2: Model usage and Colab usage
        line2 = _build_usage_line(q5h, qwk, colab_info, cfg, sep, sep_width, max_width)

        # Line 3: Context usage
        if display_width(seg1_base_full) <= max_width:
            ctx_base = seg1_base_full
            allow_ctx_tail = True
        elif display_width(seg1_base_compact) <= max_width:
            ctx_base = seg1_base_compact
            allow_ctx_tail = True
        else:
            ctx_base = seg1_base_tiny
            allow_ctx_tail = False

        line3 = (
            _append_fitting_segments(ctx_base, [spark, cache_seg], sep, sep_width, max_width)
            if allow_ctx_tail
            else ctx_base
        )

        return "\n".join(line for line in (line1, line2, line3) if line)

    seg1_full = f"{mode_full} {seg1_base_full}" if mode_full else seg1_base_full
    seg1_compact_mode = (
        f"{mode_compact} {seg1_base_compact}" if mode_compact else seg1_base_compact
    )
    seg1_tiny_mode = f"{mode_compact} {seg1_base_tiny}" if mode_compact else seg1_base_tiny

    if display_width(seg1_full) <= max_width:
        seg1 = seg1_full
        allow_tail = True
    elif display_width(seg1_compact_mode) <= max_width:
        seg1 = seg1_compact_mode
        allow_tail = True
    elif display_width(seg1_base_compact) <= max_width:
        seg1 = seg1_base_compact
        allow_tail = True
    elif display_width(seg1_tiny_mode) <= max_width:
        seg1 = seg1_tiny_mode
        allow_tail = False
    else:
        seg1 = seg1_base_tiny
        allow_tail = False

    if cfg["lines"] == 2:
        line1 = (
            _append_fitting_segments(seg1, [spark, cache_seg], sep, sep_width, max_width)
            if allow_tail
            else seg1
        )
        usage_seg = _build_usage_line(q5h, qwk, colab_info, cfg, sep, sep_width, max_width)
        line2 = _append_fitting_segments(
            "", [usage_seg, model_seg, meta_seg], sep, sep_width, max_width
        )
        return f"{line1}\n{line2}" if line2 else line1

    if not allow_tail:
        return seg1

    line = seg1
    cur_width = display_width(line)
    if q5h is not None or qwk is not None:
        for include_reset in (True, False):
            q_seg = _render_quota_segment(q5h, qwk, cfg, include_reset=include_reset)
            q_w = display_width(q_seg)
            if q_seg and cur_width + sep_width + q_w <= max_width:
                line = f"{line}{sep}{q_seg}"
                cur_width = display_width(line)
                break

    colab_seg_1l = ""
    if colab_info is not None:
        for inc_active in (True, False):
            cand = _render_colab_segment(colab_info, cfg, include_active=inc_active)
            if cand and cur_width + sep_width + display_width(cand) <= max_width:
                colab_seg_1l = cand
                break

    return _append_fitting_segments(
        line,
        [colab_seg_1l, spark, model_seg, cache_seg, meta_seg],
        sep,
        sep_width,
        max_width,
    )


def run_preview(cfg: Dict[str, Any]) -> int:
    """Print realistic preview lines across all five weather tiers and modes."""
    now = time.time()
    samples = [
        (
            14.2,
            900_000,
            [12_000, 18_400, 24_100],
            24_100,
            20_857,
            "idle",
            "default",
            45,
            0.08,
            "main",
            False,
            0.876,
            13_800,
            0.810,
            154_800,
            1000.0,
            0.0,
            0,
        ),
        (
            36.8,
            900_000,
            [8_200, 14_500, 19_100, 11_400],
            11_400,
            41_200,
            "idle",
            "accept-edits",
            128,
            0.24,
            "feat/status",
            True,
            0.580,
            15_000,
            0.650,
            432_000,
            942.5,
            1.9,
            1,
        ),
        (
            62.5,
            900_000,
            [9_000, 22_000, 15_300, 31_200, 18_700],
            18_700,
            88_500,
            "thinking",
            "auto",
            5,
            0.49,
            "main",
            True,
            0.360,
            7_800,
            0.480,
            302_400,
            618.4,
            13.1,
            1,
        ),
        (
            82.0,
            900_000,
            [14_000, 29_500, 18_200, 42_100, 25_000, 19_800],
            19_800,
            121_000,
            "idle",
            "plan",
            262,
            0.85,
            "main",
            False,
            0.180,
            3_900,
            0.250,
            172_800,
            74.2,
            0.0,
            0,
        ),
        (
            94.5,
            900_000,
            [21_000, 35_000, 48_000, 19_000, 27_400, 38_600],
            -42_000,
            140_200,
            "idle",
            "auto",
            340,
            1.24,
            "release",
            True,
            0.060,
            1_500,
            0.080,
            86_400,
            8.5,
            13.1,
            2,
        ),
    ]

    with tempfile.TemporaryDirectory(prefix="tw_preview_") as tmp_dir:
        prev_state_dir = os.environ.get("TOKEN_WEATHER_STATE_DIR")
        os.environ["TOKEN_WEATHER_STATE_DIR"] = tmp_dir
        try:
            for idx, (
                pct,
                win,
                mags,
                delta,
                cache_r,
                a_state,
                c_mode,
                age_s,
                usd,
                branch,
                dirty,
                rem_5h,
                rst_5h,
                rem_wk,
                rst_wk,
                colab_bal,
                colab_rate,
                colab_vms,
            ) in enumerate(samples):
                conv_id = f"preview-{idx}"
                exact_used = int(round(win * pct / 100.0))
                payload: Dict[str, Any] = {
                    "conversation_id": conv_id,
                    "agent_state": a_state,
                    "cycle_mode": c_mode,
                    "terminal_width": 170,
                    "model": {
                        "id": "gemini-4-carbon",
                        "display_name": "Gemini 4 Carbon",
                        "effort": "High",
                    },
                    "context_window": {
                        "total_input_tokens": exact_used,
                        "total_output_tokens": 4200,
                        "context_window_size": win,
                        "used_percentage": pct,
                        "current_usage": {
                            "input_tokens": max(0, exact_used - cache_r),
                            "output_tokens": 600,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": cache_r,
                        },
                    },
                    "quota": {
                        "carbon-5h": {
                            "remaining_fraction": rem_5h,
                            "reset_in_seconds": rst_5h,
                        },
                        "carbon-weekly": {
                            "remaining_fraction": rem_wk,
                            "reset_in_seconds": rst_wk,
                        },
                    },
                    "colab": {
                        "balance": colab_bal,
                        "rate": colab_rate,
                        "assignments": colab_vms,
                    },
                    "cost": {"total_usd": usd},
                    "vcs": {"branch": branch, "dirty": dirty},
                }
                state_path = _session_file(payload)
                if state_path:
                    write_atomic_json(
                        state_path,
                        {
                            "cum_tokens": exact_used + 4200,
                            "used_tokens": exact_used,
                            "last_api_ts": now - age_s,
                            "turn_magnitudes": mags,
                            "last_delta": delta,
                        },
                    )
                if idx > 0 and cfg["lines"] >= 2:
                    print()
                print(render_status_line(payload, cfg, now=now))
        finally:
            if prev_state_dir is None:
                os.environ.pop("TOKEN_WEATHER_STATE_DIR", None)
            else:
                os.environ["TOKEN_WEATHER_STATE_DIR"] = prev_state_dir
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weather-themed statusLine renderer for Antigravity CLI (agy)."
    )
    parser.add_argument("--lang", choices=("en", "zh"), help="Display language (en or zh)")
    parser.add_argument("--ascii", action="store_true", help="Use pure ASCII symbols")
    parser.add_argument("--cache-ttl", type=int, help="Prompt cache TTL in seconds (0 disables)")
    parser.add_argument("--bar-width", type=int, help="Progress bar width in cells (1-32)")
    parser.add_argument("--no-git", action="store_true", help="Hide git branch indicator")
    parser.add_argument("--no-quota", action="store_true", help="Hide 5h and weekly quota usage")
    parser.add_argument(
        "--quota-metric",
        choices=("used", "remaining"),
        help="Show quota as used or remaining percentage",
    )
    parser.add_argument("--no-colab", action="store_true", help="Hide Google Colab compute-unit usage")
    parser.add_argument(
        "--colab-auth",
        choices=("auto", "oauth2", "adc"),
        help="Authentication mode for `colab usage` (auto, oauth2, or adc)",
    )
    parser.add_argument(
        "--colab-ttl",
        type=int,
        help="Colab usage cache TTL in seconds (minimum 15)",
    )
    parser.add_argument(
        "--refresh-colab",
        action="store_true",
        help="Refresh cached Google Colab compute-unit usage immediately",
    )
    parser.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--lines",
        type=int,
        choices=(1, 2, 3),
        help="Number of status lines (1, 2, or 3; default 3)",
    )
    parser.add_argument("--no-mode", action="store_true", help="Hide execution mode badge")
    parser.add_argument(
        "--show-mode",
        choices=("always", "non-default", "off"),
        help="Control execution mode badge visibility",
    )
    parser.add_argument("--preview", action="store_true", help="Print sample lines for all 5 tiers")
    parser.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="Persist a config option to ~/.config/agy-token-weather/config.json",
    )
    parser.add_argument("--show-config", action="store_true", help="Print effective configuration")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.set:
        code, msg = update_config_key(args.set)
        print(msg)
        return code

    cfg = resolve_config(args)

    if args.show_config:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    if args.refresh_colab:
        entry = refresh_colab_cache(cfg)
        if entry is None:
            if not args.quiet:
                print("Failed to refresh Colab usage", file=sys.stderr)
            return 1
        if not args.quiet:
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    if args.preview:
        return run_preview(cfg)

    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        print(render_status_line(payload, cfg))
    except Exception:
        print("☀ Clear  0%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
