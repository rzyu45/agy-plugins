#!/usr/bin/env python3
"""Diagnose and restore disappeared Antigravity CLI (agy) conversations.

Zero-dependency Python 3 standard-library utility covering all four known
agy-cli conversation disappearance modes:

- Mode A (upstream #1657): ``default-cli-project.json`` workspace pollution
  causing lexicographical URI sorting to hide conversations under ``/resume``.
- Mode B (upstream #1602): 0-step SQLite ``.db`` stubs shadowing non-empty
  ``.pb`` conversations in ``conversations/`` (CLI or Hub stores).
- Mode C: Stale, missing, or zeroed summary rows in
  ``conversation_summaries.db`` / ``jetbox_summaries_proto.pb`` for healthy
  non-empty ``.db`` or ``.pb`` conversations.
- Mode D: Conversations hidden by ``archived: true`` in
  ``annotations/<cid>.pbtxt``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"

UUID_LIKE_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ARCHIVED_TRUE_RE = re.compile(r"^\s*archived\s*:\s*true\b", re.MULTILINE)
TITLE_PBTXT_RE = re.compile(r'^\s*title\s*:\s*"((?:[^"\\]|\\.)*)"', re.MULTILINE)


class ProtobufError(ValueError):
    """Raised when wire-level Protobuf decoding or validation fails."""


# ---------------------------------------------------------------------------
# Wire-level Protobuf parser & encoder (zero third-party dependencies)
# ---------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as an unsigned Protobuf base-128 varint."""
    if value < 0:
        raise ProtobufError(f"negative varint value: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read an unsigned base-128 varint from ``buf`` starting at ``pos``."""
    val = 0
    shift = 0
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            raise ProtobufError("varint exceeds 10 bytes")
    raise ProtobufError("truncated varint")


def encode_field(field_num: int, wire_type: int, payload: Union[int, bytes]) -> bytes:
    """Encode a single Protobuf field."""
    tag = encode_varint((field_num << 3) | wire_type)
    if wire_type == 0:
        if not isinstance(payload, int):
            raise ProtobufError("wire type 0 requires int payload")
        return tag + encode_varint(payload)
    if wire_type == 1:
        if not isinstance(payload, (bytes, bytearray)) or len(payload) != 8:
            raise ProtobufError("wire type 1 requires 8 bytes")
        return tag + bytes(payload)
    if wire_type == 2:
        if not isinstance(payload, (bytes, bytearray)):
            raise ProtobufError("wire type 2 requires bytes payload")
        b = bytes(payload)
        return tag + encode_varint(len(b)) + b
    if wire_type == 5:
        if not isinstance(payload, (bytes, bytearray)) or len(payload) != 4:
            raise ProtobufError("wire type 5 requires 4 bytes")
        return tag + bytes(payload)
    raise ProtobufError(f"unsupported wire type: {wire_type}")


def parse_pb(buf: bytes) -> List[Tuple[int, int, bytes, Union[int, bytes]]]:
    """Parse a Protobuf message into ``(field_num, wire_type, raw_bytes, payload)``.

    Preserves ``raw_bytes`` for every field so untouched fields can be
    re-emitted byte-for-byte identically.
    """
    out: List[Tuple[int, int, bytes, Union[int, bytes]]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        start = pos
        tag, pos = read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num <= 0:
            raise ProtobufError(f"invalid field number {field_num} at offset {start}")
        if wire_type == 0:
            val, pos = read_varint(buf, pos)
            out.append((field_num, wire_type, buf[start:pos], val))
        elif wire_type == 1:
            if pos + 8 > n:
                raise ProtobufError(f"truncated 64-bit field {field_num} at offset {start}")
            val_b = buf[pos : pos + 8]
            pos += 8
            out.append((field_num, wire_type, buf[start:pos], val_b))
        elif wire_type == 2:
            length, pos = read_varint(buf, pos)
            if pos + length > n:
                raise ProtobufError(
                    f"truncated length-delimited field {field_num} (need {length}, have {n - pos})"
                )
            val_b = buf[pos : pos + length]
            pos += length
            out.append((field_num, wire_type, buf[start:pos], val_b))
        elif wire_type == 5:
            if pos + 4 > n:
                raise ProtobufError(f"truncated 32-bit field {field_num} at offset {start}")
            val_b = buf[pos : pos + 4]
            pos += 4
            out.append((field_num, wire_type, buf[start:pos], val_b))
        else:
            raise ProtobufError(f"unsupported wire type {wire_type} for field {field_num}")
    return out


# ---------------------------------------------------------------------------
# URI & path helpers
# ---------------------------------------------------------------------------


def normalize_uri(uri_or_path: str) -> str:
    """Normalize a local path or ``file://`` URI into canonical percent-encoded form."""
    text = (uri_or_path or "").strip()
    if not text:
        return ""
    if text.startswith("file://"):
        parsed = urllib.parse.urlparse(text)
        raw_path = urllib.parse.unquote(parsed.path)
        if len(raw_path) > 1:
            raw_path = raw_path.rstrip("/")
        quoted = urllib.parse.quote(raw_path, safe="/:")
        netloc = parsed.netloc or ""
        return f"file://{netloc}{quoted}"
    expanded = os.path.abspath(os.path.expanduser(text))
    if len(expanded) > 1:
        expanded = expanded.rstrip("/")
    quoted = urllib.parse.quote(expanded, safe="/:")
    return f"file://{quoted}"


def uri_basename_encoded(uri: str) -> str:
    """Return the percent-encoded workspace basename matching ``conversations_fts.workspace``."""
    norm = normalize_uri(uri)
    if not norm:
        return ""
    return norm.rstrip("/").rsplit("/", 1)[-1]


def uri_display(uri: str) -> str:
    """Return a human-readable decoded filesystem path for display."""
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urllib.parse.urlparse(uri)
        return urllib.parse.unquote(parsed.path)
    return uri


def matches_project_filter(uris: Sequence[str], filt: Optional[str]) -> bool:
    """Check whether any workspace URI matches a user-supplied filter."""
    if not filt:
        return True
    needle = filt.strip()
    if not needle:
        return True
    needle_lower = needle.lower()
    norm_needle = normalize_uri(needle).lower() if (needle.startswith("/") or needle.startswith("~") or needle.startswith("file://")) else ""
    for u in uris:
        nu = normalize_uri(u).lower()
        du = uri_display(u).lower()
        if norm_needle and nu == norm_needle:
            return True
        if needle_lower in nu or needle_lower in du:
            return True
    return False


# ---------------------------------------------------------------------------
# Protobuf workspace stripping & validation
# ---------------------------------------------------------------------------


def extract_workspace_msg_uri(ws_bytes: bytes) -> Optional[str]:
    """Extract normalized ``file://`` URI from a Workspace submessage (field 1)."""
    fields = parse_pb(ws_bytes)
    for fnum, wt, _raw, payload in fields:
        if fnum == 1 and wt == 2 and isinstance(payload, bytes):
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def extract_trajectory_metadata_uris(blob: bytes) -> Tuple[List[str], List[str]]:
    """Extract workspace URIs from ``TrajectoryMetadata`` (field 1 and field 7)."""
    if not blob:
        return [], []
    fields = parse_pb(blob)
    f1_uris: List[str] = []
    f7_uris: List[str] = []
    for fnum, wt, _raw, payload in fields:
        if fnum == 1 and wt == 2 and isinstance(payload, bytes):
            u = extract_workspace_msg_uri(payload)
            if u:
                f1_uris.append(u)
        elif fnum == 7 and wt == 2 and isinstance(payload, bytes):
            try:
                f7_uris.append(payload.decode("utf-8"))
            except UnicodeDecodeError:
                pass
    return f1_uris, f7_uris


def strip_trajectory_metadata_blob(
    blob: bytes,
    polluter_norms: Set[str],
) -> Tuple[bytes, bool, List[str]]:
    """Strip polluter URIs from ``TrajectoryMetadata`` with round-trip verification.

    Returns ``(new_blob, changed, surviving_uris)``. Never strips if doing so
    would leave zero workspace URIs.
    """
    if not blob or not polluter_norms:
        f1, f7 = extract_trajectory_metadata_uris(blob)
        return blob, False, f1 or f7

    fields = parse_pb(blob)
    orig_f1_indices: List[int] = []
    keep_f1_indices: Set[int] = set()
    orig_f7_indices: List[int] = []
    keep_f7_indices: Set[int] = set()
    surviving_f1: List[str] = []
    surviving_f7: List[str] = []

    for idx, (fnum, wt, _raw, payload) in enumerate(fields):
        if fnum == 1 and wt == 2 and isinstance(payload, bytes):
            orig_f1_indices.append(idx)
            u = extract_workspace_msg_uri(payload)
            if u is None or normalize_uri(u) not in polluter_norms:
                keep_f1_indices.add(idx)
                if u:
                    surviving_f1.append(u)
        elif fnum == 7 and wt == 2 and isinstance(payload, bytes):
            orig_f7_indices.append(idx)
            try:
                u = payload.decode("utf-8")
            except UnicodeDecodeError:
                keep_f7_indices.add(idx)
                continue
            if normalize_uri(u) not in polluter_norms:
                keep_f7_indices.add(idx)
                surviving_f7.append(u)

    # Guard: never leave 0 workspaces in either field 1 or field 7
    if orig_f1_indices and not keep_f1_indices:
        f1, f7 = extract_trajectory_metadata_uris(blob)
        return blob, False, f1 or f7
    if orig_f7_indices and not keep_f7_indices:
        f1, f7 = extract_trajectory_metadata_uris(blob)
        return blob, False, f1 or f7

    dropped_f1 = len(orig_f1_indices) - len(keep_f1_indices)
    dropped_f7 = len(orig_f7_indices) - len(keep_f7_indices)
    if dropped_f1 == 0 and dropped_f7 == 0:
        return blob, False, surviving_f1 or surviving_f7

    out_chunks: List[bytes] = []
    expected_other: List[bytes] = []
    for idx, (fnum, wt, raw, _payload) in enumerate(fields):
        if fnum == 1 and wt == 2:
            if idx in keep_f1_indices:
                out_chunks.append(raw)
        elif fnum == 7 and wt == 2:
            if idx in keep_f7_indices:
                out_chunks.append(raw)
        else:
            out_chunks.append(raw)
            expected_other.append(raw)

    new_blob = b"".join(out_chunks)

    # Round-trip validation before returning
    reparsed = parse_pb(new_blob)
    reparsed_other = [raw for fnum, wt, raw, _p in reparsed if not ((fnum in (1, 7)) and wt == 2)]
    if reparsed_other != expected_other:
        raise ProtobufError("round-trip check failed: non-workspace fields diverged")
    new_f1, new_f7 = extract_trajectory_metadata_uris(new_blob)
    if orig_f1_indices and not new_f1:
        raise ProtobufError("round-trip check failed: empty field 1 workspaces")
    if orig_f7_indices and not new_f7:
        raise ProtobufError("round-trip check failed: empty field 7 workspaces")
    for u in new_f1 + new_f7:
        if normalize_uri(u) in polluter_norms:
            raise ProtobufError(f"round-trip check failed: polluter URI still present: {u}")

    return new_blob, True, new_f1 or new_f7


def strip_raw_summary_blob(blob: bytes, polluter_norms: Set[str]) -> Tuple[bytes, bool]:
    """Strip polluter URIs from ``CascadeTrajectorySummary`` (field 9 + nested field 17)."""
    if not blob or not polluter_norms:
        return blob, False

    fields = parse_pb(blob)
    orig_f9_indices: List[int] = []
    keep_f9_indices: Set[int] = set()
    f17_replacements: Dict[int, bytes] = {}
    any_changed = False

    for idx, (fnum, wt, _raw, payload) in enumerate(fields):
        if fnum == 9 and wt == 2 and isinstance(payload, bytes):
            orig_f9_indices.append(idx)
            u = extract_workspace_msg_uri(payload)
            if u is None or normalize_uri(u) not in polluter_norms:
                keep_f9_indices.add(idx)
        elif fnum == 17 and wt == 2 and isinstance(payload, bytes):
            new_sub, sub_changed, _surv = strip_trajectory_metadata_blob(payload, polluter_norms)
            if sub_changed:
                f17_replacements[idx] = new_sub
                any_changed = True

    if orig_f9_indices and not keep_f9_indices:
        # Never leave zero workspaces in field 9
        return blob, False

    if len(orig_f9_indices) != len(keep_f9_indices):
        any_changed = True

    if not any_changed:
        return blob, False

    out_chunks: List[bytes] = []
    expected_other: List[bytes] = []
    for idx, (fnum, wt, raw, _payload) in enumerate(fields):
        if fnum == 9 and wt == 2:
            if idx in keep_f9_indices:
                out_chunks.append(raw)
        elif fnum == 17 and wt == 2 and idx in f17_replacements:
            out_chunks.append(encode_field(17, 2, f17_replacements[idx]))
        else:
            out_chunks.append(raw)
            if not (fnum == 17 and wt == 2):
                expected_other.append(raw)

    new_blob = b"".join(out_chunks)

    # Round-trip validation
    reparsed = parse_pb(new_blob)
    reparsed_other = [raw for fnum, wt, raw, _p in reparsed if not ((fnum in (9, 17)) and wt == 2)]
    if reparsed_other != expected_other:
        raise ProtobufError("raw_summary round-trip check failed: unrelated fields diverged")
    new_f9_uris: List[str] = []
    for fnum, wt, _raw, payload in reparsed:
        if fnum == 9 and wt == 2 and isinstance(payload, bytes):
            u = extract_workspace_msg_uri(payload)
            if u:
                new_f9_uris.append(u)
                if normalize_uri(u) in polluter_norms:
                    raise ProtobufError(f"raw_summary round-trip failed: polluter in field 9: {u}")
        elif fnum == 17 and wt == 2 and isinstance(payload, bytes):
            sub_f1, sub_f7 = extract_trajectory_metadata_uris(payload)
            for u in sub_f1 + sub_f7:
                if normalize_uri(u) in polluter_norms:
                    raise ProtobufError(f"raw_summary round-trip failed: polluter in field 17: {u}")
    if orig_f9_indices and not new_f9_uris:
        raise ProtobufError("raw_summary round-trip failed: empty field 9 workspaces")

    return new_blob, True


def patch_jetbox_summaries_proto(
    raw_bytes: bytes,
    updated_summaries: Dict[str, bytes],
) -> Tuple[bytes, int]:
    """Update selected CID entries inside ``jetbox_summaries_proto.pb``."""
    if not raw_bytes or not updated_summaries:
        return raw_bytes, 0

    top_fields = parse_pb(raw_bytes)
    out_chunks: List[bytes] = []
    patched = 0

    for fnum, wt, raw, payload in top_fields:
        if fnum != 1 or wt != 2 or not isinstance(payload, bytes):
            out_chunks.append(raw)
            continue
        entry_fields = parse_pb(payload)
        cid: Optional[str] = None
        for efnum, ewt, _eraw, epayload in entry_fields:
            if efnum == 1 and ewt == 2 and isinstance(epayload, bytes):
                try:
                    cid = epayload.decode("utf-8")
                except UnicodeDecodeError:
                    cid = None
                break
        if cid and cid in updated_summaries:
            new_entry_chunks: List[bytes] = []
            replaced_val = False
            for efnum, ewt, eraw, _epayload in entry_fields:
                if efnum == 2 and ewt == 2:
                    new_entry_chunks.append(encode_field(2, 2, updated_summaries[cid]))
                    replaced_val = True
                else:
                    new_entry_chunks.append(eraw)
            if not replaced_val:
                new_entry_chunks.append(encode_field(2, 2, updated_summaries[cid]))
            out_chunks.append(encode_field(1, 2, b"".join(new_entry_chunks)))
            patched += 1
        else:
            out_chunks.append(raw)

    new_bytes = b"".join(out_chunks)
    reparsed = parse_pb(new_bytes)
    if len(reparsed) != len(top_fields):
        raise ProtobufError(
            f"jetbox_summaries_proto entry count mismatch: {len(reparsed)} != {len(top_fields)}"
        )
    return new_bytes, patched


def inspect_jetbox_for_polluters(path: str, polluter_norms: Set[str]) -> Dict[str, Any]:
    """Read-only check of ``jetbox_summaries_proto.pb`` for polluter URIs."""
    result: Dict[str, Any] = {
        "exists": os.path.exists(path),
        "entry_count": 0,
        "polluted_entries": 0,
        "error": None,
    }
    if not result["exists"]:
        return result
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        top_fields = parse_pb(data)
        result["entry_count"] = len(top_fields)
        if polluter_norms:
            for fnum, wt, _raw, payload in top_fields:
                if fnum != 1 or wt != 2 or not isinstance(payload, bytes):
                    continue
                for efnum, ewt, _eraw, epayload in parse_pb(payload):
                    if efnum == 2 and ewt == 2 and isinstance(epayload, bytes):
                        uris = _extract_raw_summary_uris(epayload)
                        norms = [normalize_uri(u) for u in uris]
                        if len(norms) > 1 and any(u in polluter_norms for u in norms):
                            result["polluted_entries"] += 1
                        break
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _extract_raw_summary_uris(raw_summary: bytes) -> List[str]:
    uris: List[str] = []
    for fnum, wt, _raw, payload in parse_pb(raw_summary):
        if fnum == 9 and wt == 2 and isinstance(payload, bytes):
            u = extract_workspace_msg_uri(payload)
            if u:
                uris.append(u)
    return uris


# ---------------------------------------------------------------------------
# WAL-safe SQLite helpers & atomic file operations
# ---------------------------------------------------------------------------


@contextmanager
def sqlite_readonly_copy(db_path: str, force_copy: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a SQLite DB safely read-only, copying ``.db``/``-wal``/``-shm`` when needed.

    When ``force_copy`` is False and no ``-wal`` sidecar exists, attempts a fast
    URI ``mode=ro`` open first and verifies readability; if a WAL header without
    ``-shm`` or lock triggers ``sqlite3.Error``, falls back to copying the DB
    triplet into a temporary directory so live files are never mutated or locked.
    Pass ``force_copy=True`` when running SQLite FTS5 ``integrity-check`` (which
    uses ``INSERT`` syntax and therefore requires a writable temp copy).
    """
    wal_exists = os.path.exists(db_path + "-wal")
    if not force_copy and not wal_exists:
        direct_conn: Optional[sqlite3.Connection] = None
        try:
            uri = f"file:{urllib.parse.quote(os.path.abspath(db_path), safe='/')}?mode=ro"
            direct_conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            direct_conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error:
            if direct_conn is not None:
                try:
                    direct_conn.close()
                except Exception:
                    pass
                direct_conn = None
        if direct_conn is not None:
            try:
                yield direct_conn
            finally:
                direct_conn.close()
            return

    with tempfile.TemporaryDirectory(prefix="agy-conv-ro-") as tmp_dir:
        base = os.path.basename(db_path)
        dst = os.path.join(tmp_dir, base)
        shutil.copy2(db_path, dst)
        for suffix in ("-wal", "-shm"):
            side = db_path + suffix
            if os.path.exists(side):
                shutil.copy2(side, dst + suffix)
        conn = sqlite3.connect(dst)
        try:
            yield conn
        finally:
            conn.close()


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomically write bytes to ``path`` using temp file + ``os.replace``."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".agy-conv-tmp-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_write_json(path: str, obj: Any) -> None:
    """Atomically write formatted JSON with trailing newline."""
    payload = (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


class BackupManager:
    """Manages timestamped backups and rollback manifests before any mutation."""

    def __init__(self, backup_dir: str) -> None:
        self.backup_dir = os.path.abspath(backup_dir)
        self.backed_up: Dict[str, str] = {}
        self.moved_stubs: List[Dict[str, str]] = []
        self._created = False

    def ensure_dir(self) -> None:
        if not self._created:
            os.makedirs(self.backup_dir, exist_ok=True)
            self._created = True

    def backup_file(self, src: str, rel_dest: str) -> Optional[str]:
        """Copy ``src`` into ``<backup_dir>/<rel_dest>`` once."""
        src_abs = os.path.abspath(src)
        if not os.path.exists(src_abs):
            return None
        if src_abs in self.backed_up:
            return self.backed_up[src_abs]
        self.ensure_dir()
        dst = os.path.join(self.backup_dir, rel_dest)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src_abs, dst)
        self.backed_up[src_abs] = dst
        return dst

    def backup_sqlite_triplet(self, db_path: str, rel_dir: str) -> List[str]:
        """Back up ``.db`` plus ``-wal`` and ``-shm`` if present."""
        base = os.path.basename(db_path)
        saved: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                rel = os.path.join(rel_dir, base + suffix) if rel_dir else (base + suffix)
                dst = self.backup_file(src, rel)
                if dst:
                    saved.append(dst)
        return saved

    def restore_sqlite_triplet(self, db_path: str) -> List[str]:
        """Roll back a SQLite DB triplet from the backup directory without raising on OSError."""
        db_abs = os.path.abspath(db_path)
        errors: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            target = db_abs + suffix
            src = self.backed_up.get(target)
            if src and os.path.exists(src):
                try:
                    shutil.copy2(src, target)
                except OSError as exc:
                    errors.append(f"{target}: {exc}")
            elif suffix and os.path.exists(target):
                try:
                    os.unlink(target)
                except OSError as exc:
                    errors.append(f"{target}: {exc}")
        return errors

    def move_stub_triplet(self, db_path: str, stub_subdir: str) -> List[str]:
        """Move a verified 0-step stub ``.db`` (+ WAL/SHM) into ``<backup_dir>/<stub_subdir>/``."""
        self.ensure_dir()
        dst_dir = os.path.join(self.backup_dir, stub_subdir)
        os.makedirs(dst_dir, exist_ok=True)
        moved: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                dst = os.path.join(dst_dir, os.path.basename(src))
                shutil.move(src, dst)
                self.moved_stubs.append({"from": os.path.abspath(src), "to": dst})
                moved.append(dst)
        return moved

    def write_manifest(self, metadata: Dict[str, Any]) -> str:
        """Write ``manifest.json`` describing every backed-up and moved file."""
        self.ensure_dir()
        manifest_path = os.path.join(self.backup_dir, "manifest.json")
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool_version": VERSION,
            "backup_dir": self.backup_dir,
            "backed_up_files": self.backed_up,
            "moved_stubs": self.moved_stubs,
            **metadata,
        }
        atomic_write_json(manifest_path, payload)
        return manifest_path


# ---------------------------------------------------------------------------
# Process & open-file detection
# ---------------------------------------------------------------------------


def detect_running_agy_processes() -> List[Dict[str, Any]]:
    """Return running ``agy`` CLI / hub processes (excluding staging checks)."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if proc.returncode != 0:
        return []

    my_pid = os.getpid()
    found: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid == my_pid:
            continue
        cmd = parts[2].strip()
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        if not tokens:
            continue
        exe_base = os.path.basename(tokens[0])
        if exe_base == "agy":
            found.append({"pid": pid, "ppid": ppid, "command": cmd})
    return found


def detect_open_cids_lsof(conv_dir: str) -> Set[str]:
    """Use ``lsof`` to detect conversation DBs currently held open."""
    if not os.path.isdir(conv_dir):
        return set()
    lsof_bin = shutil.which("lsof")
    if not lsof_bin:
        return set()
    try:
        proc = subprocess.run(
            [lsof_bin, "+D", conv_dir],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()

    open_cids: Set[str] = set()
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        path = parts[-1]
        base = os.path.basename(path)
        for suffix in (".db-wal", ".db-shm", ".db", ".pb"):
            if base.endswith(suffix):
                stem = base[: -len(suffix)]
                if UUID_LIKE_RE.match(stem):
                    open_cids.add(stem)
                break
    return open_cids


def expand_active_swarm_cids(
    seed_cids: Set[str],
    summaries_db: str,
) -> Set[str]:
    """Expand seed active CIDs to include parent, root, children, and siblings."""
    active = set(seed_cids)
    env_cid = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
    if env_cid:
        active.add(env_cid)
    if not active or not os.path.exists(summaries_db):
        return active

    parent_of: Dict[str, str] = {}
    children_of: Dict[str, Set[str]] = {}
    try:
        with sqlite_readonly_copy(summaries_db) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "conversation_parents" in tables:
                for cid, pid in conn.execute(
                    "SELECT conversation_id, parent_id FROM conversation_parents"
                ):
                    if cid and pid:
                        parent_of[cid] = pid
                        children_of.setdefault(pid, set()).add(cid)
            if "conversation_summaries" in tables:
                for cid, pid in conn.execute(
                    "SELECT conversation_id, parent_conversation_id FROM conversation_summaries WHERE parent_conversation_id != ''"
                ):
                    if cid and pid:
                        parent_of.setdefault(cid, pid)
                        children_of.setdefault(pid, set()).add(cid)
    except sqlite3.Error:
        return active

    # Walk up to roots, then down to all descendants in the connected tree
    queue = list(active)
    seen = set(active)
    while queue:
        cur = queue.pop()
        p = parent_of.get(cur)
        if p and p not in seen:
            seen.add(p)
            queue.append(p)
        for child in children_of.get(cur, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


# ---------------------------------------------------------------------------
# Inspection / Diagnosis (read-only)
# ---------------------------------------------------------------------------


def read_default_project_polluters(default_project_json: str) -> Tuple[bool, List[str], Optional[str]]:
    """Read ``projectResources.resources[*].folderUri`` from ``default-cli-project.json``."""
    if not os.path.exists(default_project_json):
        return False, [], None
    try:
        with open(default_project_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return True, [], str(exc)

    uris: List[str] = []
    pr = data.get("projectResources")
    if isinstance(pr, dict):
        resources = pr.get("resources")
        if isinstance(resources, list):
            for item in resources:
                if isinstance(item, dict):
                    furi = item.get("folderUri")
                    if isinstance(furi, str) and furi.strip():
                        uris.append(normalize_uri(furi))
    return True, list(dict.fromkeys(uris)), None


def inspect_conversation_db(db_path: str) -> Dict[str, Any]:
    """Inspect a single conversation ``.db`` via WAL-safe temp copy."""
    info: Dict[str, Any] = {
        "steps_count": None,
        "trajectory_meta": [],
        "blob_lengths": [],
        "blob_main": b"",
        "blob_uris_f1": [],
        "blob_uris_f7": [],
        "is_zero_step_stub": False,
        "error": None,
    }
    try:
        with sqlite_readonly_copy(db_path) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "steps" in tables:
                info["steps_count"] = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            if "trajectory_meta" in tables:
                info["trajectory_meta"] = [
                    (r[0], r[1])
                    for r in conn.execute(
                        "SELECT trajectory_type, source FROM trajectory_meta"
                    ).fetchall()
                ]
            if "trajectory_metadata_blob" in tables:
                rows = conn.execute(
                    "SELECT id, data FROM trajectory_metadata_blob"
                ).fetchall()
                info["blob_lengths"] = [
                    0 if data is None else len(data) for _bid, data in rows
                ]
                for bid, data in rows:
                    if bid == "main" and data:
                        info["blob_main"] = bytes(data)
                        break
                if info["blob_main"]:
                    f1, f7 = extract_trajectory_metadata_uris(info["blob_main"])
                    info["blob_uris_f1"] = f1
                    info["blob_uris_f7"] = f7

            steps_zero = info["steps_count"] == 0
            meta_zero = (not info["trajectory_meta"]) or all(
                (t == 0 and s == 0) for t, s in info["trajectory_meta"]
            )
            blob_empty = (not info["blob_lengths"]) or all(
                ln == 0 for ln in info["blob_lengths"]
            )
            info["is_zero_step_stub"] = bool(steps_zero and meta_zero and blob_empty)
    except Exception as exc:
        info["error"] = str(exc)
    return info


def scan_annotations_archived(app_dir: str) -> List[Dict[str, Any]]:
    """Scan ``annotations/*.pbtxt`` for ``archived: true`` entries."""
    ann_dir = os.path.join(app_dir, "annotations")
    if not os.path.isdir(ann_dir):
        return []
    archived: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(ann_dir)):
        if not fn.endswith(".pbtxt"):
            continue
        cid = fn[:-6]
        path = os.path.join(ann_dir, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if ARCHIVED_TRUE_RE.search(text):
            m = TITLE_PBTXT_RE.search(text)
            title = m.group(1) if m else ""
            archived.append({"cid": cid, "annotation_title": title, "path": path})
    return archived


def query_ls_conversations_optional() -> Optional[Dict[str, Any]]:
    """Best-effort query to local ``ConversationService/ListConversations`` if env vars exist."""
    addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if not addr or not csrf:
        return None
    url = f"http://{addr}/product.v1.ConversationService/ListConversations"
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-antigravity-csrf-token": csrf,
    }
    try:
        all_convs: List[Dict[str, Any]] = []
        page_token = ""
        for _ in range(20):
            body: Dict[str, Any] = {"pageSize": 100, "includeArchived": True}
            if page_token:
                body["pageToken"] = page_token
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            batch = data.get("conversations") or []
            all_convs.extend(batch)
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
        archived_ids = [
            c.get("id")
            for c in all_convs
            if isinstance(c.get("annotations"), dict) and c["annotations"].get("archived") is True
        ]
        return {
            "reachable": True,
            "total_listed": len(all_convs),
            "archived_ids": [x for x in archived_ids if x],
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def call_ls_unarchive(cid: str) -> Tuple[bool, str]:
    """Call ``ConversationService/UpdateConversation`` to set ``archived: false``."""
    addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if not addr or not csrf:
        return (
            False,
            "ANTIGRAVITY_LS_ADDRESS / ANTIGRAVITY_CSRF_TOKEN not set; run inside an active agy session or pass LS env vars.",
        )
    url = f"http://{addr}/product.v1.ConversationService/UpdateConversation"
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-antigravity-csrf-token": csrf,
    }
    body = {"conversationId": cid, "annotations": {"archived": False}}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp.read()
        return True, "unarchived via ConversationService/UpdateConversation"
    except Exception as exc:
        return False, str(exc)


def scan_hub_mode_b(hub_dir: str) -> Dict[str, Any]:
    """Inspect optional Hub store (``~/.gemini/antigravity``) for Mode B 0-step stubs."""
    info: Dict[str, Any] = {
        "hub_dir": hub_dir,
        "exists": os.path.isdir(hub_dir),
        "mode_b_stubs": [],
        "dual_nonempty": [],
        "hub_summaries_pb_exists": os.path.exists(
            os.path.join(hub_dir, "agyhub_summaries_proto.pb")
        ),
    }
    conv_dir = os.path.join(hub_dir, "conversations")
    if not os.path.isdir(conv_dir):
        return info

    entries = os.listdir(conv_dir)
    dbs = {fn[:-3] for fn in entries if fn.endswith(".db")}
    pbs = {
        fn[:-3]
        for fn in entries
        if fn.endswith(".pb") and os.path.getsize(os.path.join(conv_dir, fn)) > 0
    }
    for cid in sorted(dbs & pbs):
        db_path = os.path.join(conv_dir, f"{cid}.db")
        pb_path = os.path.join(conv_dir, f"{cid}.pb")
        db_info = inspect_conversation_db(db_path)
        item = {
            "cid": cid,
            "db_path": db_path,
            "pb_path": pb_path,
            "pb_size": os.path.getsize(pb_path),
            "steps_count": db_info["steps_count"],
            "trajectory_meta": db_info["trajectory_meta"],
            "error": db_info["error"],
        }
        if db_info["is_zero_step_stub"]:
            info["mode_b_stubs"].append(item)
        else:
            info["dual_nonempty"].append(item)
    return info


def diagnose_store(
    app_dir: str,
    default_project_json: str,
    hub_dir: Optional[str] = None,
    extra_strip_uris: Sequence[str] = (),
    project_filter: Optional[str] = None,
    cid_filter: Optional[Set[str]] = None,
    query_ls: bool = False,
) -> Dict[str, Any]:
    """Run full read-only WAL-safe diagnosis across all four disappearance modes."""
    app_dir = os.path.abspath(os.path.expanduser(app_dir))
    default_project_json = os.path.abspath(os.path.expanduser(default_project_json))
    conv_dir = os.path.join(app_dir, "conversations")
    summaries_db = os.path.join(app_dir, "conversation_summaries.db")
    jetbox_pb = os.path.join(app_dir, "jetbox_summaries_proto.pb")
    metadata_json = os.path.join(app_dir, "cache", "conversation_metadata.json")

    dp_exists, dp_polluters, dp_err = read_default_project_polluters(default_project_json)
    explicit_polluters = [normalize_uri(u) for u in extra_strip_uris if u and u.strip()]
    polluter_list = list(dict.fromkeys(dp_polluters + explicit_polluters))
    polluter_norms: Set[str] = set(polluter_list)

    running_procs = detect_running_agy_processes()
    lsof_open = detect_open_cids_lsof(conv_dir)
    active_cids = expand_active_swarm_cids(lsof_open, summaries_db)

    # Load summaries DB (WAL-safe copy)
    summaries: Dict[str, Dict[str, Any]] = {}
    summaries_err: Optional[str] = None
    if os.path.exists(summaries_db):
        try:
            with sqlite_readonly_copy(summaries_db) as conn:
                rows = conn.execute(
                    """
                    SELECT conversation_id, title, step_count, last_modified_time,
                           workspace_uris, parent_conversation_id
                    FROM conversation_summaries
                    """
                ).fetchall()
                for cid, title, step_count, lmt, uris_raw, parent_id in rows:
                    try:
                        uris = json.loads(uris_raw) if (uris_raw and uris_raw.startswith("[")) else []
                    except ValueError:
                        uris = []
                    summaries[cid] = {
                        "title": title or "",
                        "step_count": step_count,
                        "last_modified_time": str(lmt or ""),
                        "workspace_uris": uris,
                        "parent_conversation_id": parent_id or "",
                    }
        except Exception as exc:
            summaries_err = str(exc)

    # Catalog conversation files (detect permission errors such as sandbox restrictions)
    db_cids: Set[str] = set()
    pb_cids: Set[str] = set()
    pb_sizes: Dict[str, int] = {}
    conv_access_err: Optional[str] = None
    try:
        entries = os.listdir(conv_dir)
    except FileNotFoundError:
        entries = []
    except PermissionError as exc:
        entries = []
        conv_access_err = (
            f"permission denied reading {conv_dir} ({exc}); "
            "if running inside an agent sandbox, re-run with unsandboxed access"
        )
    except OSError as exc:
        entries = []
        conv_access_err = str(exc)

    for fn in entries:
        full = os.path.join(conv_dir, fn)
        if not os.path.isfile(full):
            continue
        if fn.endswith(".db"):
            db_cids.add(fn[:-3])
        elif fn.endswith(".pb"):
            sz = os.path.getsize(full)
            pb_sizes[fn[:-3]] = sz
            if sz > 0:
                pb_cids.add(fn[:-3])

    mode_a_hidden: List[Dict[str, Any]] = []
    mode_a_secondary: List[Dict[str, Any]] = []
    mode_a_pb_only_skipped: List[Dict[str, Any]] = []
    mode_a_genuine_preserved: List[str] = []
    mode_a_summary_only_stale: List[Dict[str, Any]] = []

    mode_b_stubs: List[Dict[str, Any]] = []
    mode_b_dual_nonempty: List[Dict[str, Any]] = []

    mode_c_anomalies: List[Dict[str, Any]] = []
    db_read_errors: List[Dict[str, Any]] = []
    multi_ws_uri_counter: Counter = Counter()
    multi_ws_count = 0

    for cid in sorted(db_cids):
        if cid_filter and cid not in cid_filter:
            continue
        db_path = os.path.join(conv_dir, f"{cid}.db")
        db_info = inspect_conversation_db(db_path)
        sum_row = summaries.get(cid)
        title = (sum_row["title"] if sum_row else "") or ""
        parent_id = (sum_row["parent_conversation_id"] if sum_row else "") or ""
        is_active = cid in active_cids

        if db_info["error"]:
            db_read_errors.append({"cid": cid, "error": db_info["error"]})
            continue

        # Mode B check: .db + non-empty .pb
        if cid in pb_cids:
            pair_entry = {
                "cid": cid,
                "title": title,
                "db_path": db_path,
                "pb_path": os.path.join(conv_dir, f"{cid}.pb"),
                "pb_size": pb_sizes.get(cid, 0),
                "steps_count": db_info["steps_count"],
                "trajectory_meta": db_info["trajectory_meta"],
                "is_active": is_active,
            }
            if db_info["is_zero_step_stub"]:
                mode_b_stubs.append(pair_entry)
                continue
            mode_b_dual_nonempty.append(pair_entry)

        # Mode C check: healthy .db with steps > 0 but missing/zeroed summary
        actual_steps = db_info["steps_count"] or 0
        if actual_steps > 0:
            if sum_row is None:
                mode_c_anomalies.append(
                    {
                        "cid": cid,
                        "reason": "missing_summary",
                        "storage": "db",
                        "actual_steps": actual_steps,
                        "summary_steps": None,
                        "last_modified_time": "",
                        "title": title,
                        "parent_conversation_id": parent_id,
                        "is_active": is_active,
                    }
                )
            elif (
                sum_row["step_count"] == 0
                or sum_row["last_modified_time"].startswith("0001-")
            ):
                mode_c_anomalies.append(
                    {
                        "cid": cid,
                        "reason": "zeroed_summary",
                        "storage": "db",
                        "actual_steps": actual_steps,
                        "summary_steps": sum_row["step_count"],
                        "last_modified_time": sum_row["last_modified_time"],
                        "title": title,
                        "parent_conversation_id": parent_id,
                        "is_active": is_active,
                    }
                )

        # Mode A check: inspect authoritative blob URIs (both field 1 and field 7) + summary URIs
        norm_f1 = list(dict.fromkeys(normalize_uri(u) for u in db_info["blob_uris_f1"] if u))
        norm_f7 = list(dict.fromkeys(normalize_uri(u) for u in db_info["blob_uris_f7"] if u))
        norm_blob_all = list(dict.fromkeys(norm_f1 + norm_f7))
        sum_uris = sum_row["workspace_uris"] if sum_row else []
        norm_sum_uris = list(dict.fromkeys(normalize_uri(u) for u in sum_uris if u))
        effective_uris = sorted(norm_blob_all or norm_sum_uris)

        if len(effective_uris) > 1:
            multi_ws_count += 1
            for u in effective_uris:
                multi_ws_uri_counter[u] += 1

        if polluter_norms:
            f1_has_polluter = any(u in polluter_norms for u in norm_f1)
            f7_has_polluter = any(u in polluter_norms for u in norm_f7)
            blob_has_polluter = f1_has_polluter or f7_has_polluter
            surviving_f1 = [u for u in norm_f1 if u not in polluter_norms]
            surviving_f7 = [u for u in norm_f7 if u not in polluter_norms]
            # Exact same non-empty invariant enforced by strip_trajectory_metadata_blob()
            blob_would_empty = (
                (bool(norm_f1) and not surviving_f1)
                or (bool(norm_f7) and not surviving_f7)
            )
            blob_strippable = blob_has_polluter and not blob_would_empty

            sum_has_polluter = any(u in polluter_norms for u in norm_sum_uris)
            surviving_sum = [u for u in norm_sum_uris if u not in polluter_norms]
            sum_strippable = sum_has_polluter and bool(surviving_sum)

            if blob_strippable:
                surviving = sorted(dict.fromkeys(surviving_f1 or surviving_f7))
                sorted_all = sorted(set(norm_blob_all))
                if not matches_project_filter(sorted_all + surviving, project_filter):
                    continue
                is_hidden = bool(sorted_all) and (sorted_all[0] in polluter_norms)
                entry = {
                    "cid": cid,
                    "title": title,
                    "steps_count": actual_steps,
                    "parent_conversation_id": parent_id,
                    "is_top_level": not bool(parent_id),
                    "is_active": is_active,
                    "storage": "db",
                    "blob_polluted": True,
                    "summary_polluted": sum_has_polluter,
                    "current_uris": sorted_all,
                    "restored_uris": surviving,
                    "primary_before": sorted_all[0] if sorted_all else "",
                    "primary_after": surviving[0] if surviving else "",
                }
                if is_hidden:
                    mode_a_hidden.append(entry)
                else:
                    mode_a_secondary.append(entry)
            elif sum_strippable:
                # .db blob is already clean or single-workspace preserved; only summary/cache is strippable
                sorted_all = sorted(set(norm_sum_uris))
                surviving = sorted(dict.fromkeys(surviving_sum))
                if not matches_project_filter(sorted_all + surviving, project_filter):
                    continue
                entry = {
                    "cid": cid,
                    "title": title,
                    "steps_count": actual_steps,
                    "parent_conversation_id": parent_id,
                    "is_top_level": not bool(parent_id),
                    "is_active": is_active,
                    "storage": "db",
                    "blob_polluted": False,
                    "summary_polluted": True,
                    "current_uris": sorted_all,
                    "restored_uris": surviving,
                    "primary_before": sorted_all[0] if sorted_all else "",
                    "primary_after": surviving[0] if surviving else "",
                }
                mode_a_summary_only_stale.append(entry)
            elif blob_has_polluter or sum_has_polluter:
                mode_a_genuine_preserved.append(cid)

    # .pb-only legacy conversations (never mutate .pb)
    for cid in sorted(pb_cids - db_cids):
        if cid_filter and cid not in cid_filter:
            continue
        sum_row = summaries.get(cid)
        if sum_row is None:
            mode_c_anomalies.append(
                {
                    "cid": cid,
                    "reason": "missing_summary",
                    "storage": "pb_only",
                    "actual_steps": None,
                    "summary_steps": None,
                    "last_modified_time": "",
                    "title": "",
                    "parent_conversation_id": "",
                    "is_active": cid in active_cids,
                }
            )
            continue
        if sum_row["step_count"] == 0 or sum_row["last_modified_time"].startswith("0001-"):
            mode_c_anomalies.append(
                {
                    "cid": cid,
                    "reason": "zeroed_summary",
                    "storage": "pb_only",
                    "actual_steps": None,
                    "summary_steps": sum_row["step_count"],
                    "last_modified_time": sum_row["last_modified_time"],
                    "title": sum_row["title"],
                    "parent_conversation_id": sum_row["parent_conversation_id"],
                    "is_active": cid in active_cids,
                }
            )
        norm_sum_uris = sorted(
            dict.fromkeys(normalize_uri(u) for u in sum_row["workspace_uris"] if u)
        )
        if len(norm_sum_uris) > 1:
            multi_ws_count += 1
            for u in norm_sum_uris:
                multi_ws_uri_counter[u] += 1
        if polluter_norms and any(u in polluter_norms for u in norm_sum_uris):
            surviving = [u for u in norm_sum_uris if u not in polluter_norms]
            if surviving and matches_project_filter(norm_sum_uris, project_filter):
                mode_a_pb_only_skipped.append(
                    {
                        "cid": cid,
                        "title": sum_row["title"],
                        "current_uris": norm_sum_uris,
                        "primary_before": norm_sum_uris[0],
                        "is_hidden": norm_sum_uris[0] in polluter_norms,
                    }
                )

    # Mode D: archived annotations
    archived_raw = scan_annotations_archived(app_dir)
    mode_d_archived: List[Dict[str, Any]] = []
    for item in archived_raw:
        cid = item["cid"]
        if cid_filter and cid not in cid_filter:
            continue
        sum_row = summaries.get(cid)
        uris = [normalize_uri(u) for u in (sum_row["workspace_uris"] if sum_row else []) if u]
        if project_filter and uris and not matches_project_filter(uris, project_filter):
            continue
        mode_d_archived.append(
            {
                "cid": cid,
                "title": item["annotation_title"] or (sum_row["title"] if sum_row else ""),
                "step_count": sum_row["step_count"] if sum_row else None,
                "last_modified_time": sum_row["last_modified_time"] if sum_row else "",
                "workspace_uris": uris,
            }
        )

    jetbox_info = inspect_jetbox_for_polluters(jetbox_pb, polluter_norms)
    metadata_polluted = 0
    if os.path.exists(metadata_json) and polluter_norms:
        try:
            with open(metadata_json, "r", encoding="utf-8") as fh:
                cm = json.load(fh)
            for _cid, entry in (cm.get("conversations") or {}).items():
                if not isinstance(entry, dict):
                    continue
                s = entry.get("summary")
                if not isinstance(s, dict):
                    continue
                wuris = [normalize_uri(u) for u in (s.get("WorkspaceURIs") or []) if isinstance(u, str)]
                if len(wuris) > 1 and any(u in polluter_norms for u in wuris):
                    metadata_polluted += 1
        except (OSError, ValueError):
            pass

    hub_info = scan_hub_mode_b(hub_dir) if hub_dir else None
    ls_info = query_ls_conversations_optional() if query_ls else None

    suggested_polluters: List[Dict[str, Any]] = []
    if not polluter_norms and multi_ws_count > 0:
        for uri, cnt in multi_ws_uri_counter.most_common(5):
            if cnt >= 2:
                suggested_polluters.append(
                    {"uri": uri, "path": uri_display(uri), "multi_ws_conversations": cnt}
                )

    return {
        "paths": {
            "app_dir": app_dir,
            "default_project_json": default_project_json,
            "summaries_db": summaries_db,
            "jetbox_pb": jetbox_pb,
            "metadata_json": metadata_json,
            "hub_dir": hub_dir,
        },
        "runtime": {
            "running_agy_processes": running_procs,
            "lsof_open_cids": sorted(lsof_open),
            "active_cids": sorted(active_cids),
            "inside_agent_session": bool(os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()),
        },
        "store_counts": {
            "db_files": len(db_cids),
            "pb_files_nonempty": len(pb_cids),
            "summary_rows": len(summaries),
            "summaries_error": summaries_err,
            "conversations_access_error": conv_access_err,
            "db_read_errors": db_read_errors,
            "multi_workspace_conversations": multi_ws_count,
        },
        "default_project": {
            "exists": dp_exists,
            "polluter_uris": dp_polluters,
            "error": dp_err,
        },
        "polluter_uris": polluter_list,
        "suggested_polluters": suggested_polluters,
        "mode_a": {
            "hidden": mode_a_hidden,
            "secondary_polluted": mode_a_secondary,
            "summary_only_stale": mode_a_summary_only_stale,
            "pb_only_skipped": mode_a_pb_only_skipped,
            "genuine_single_workspace_preserved": len(mode_a_genuine_preserved),
            "jetbox": jetbox_info,
            "metadata_cache_polluted": metadata_polluted,
        },
        "mode_b": {
            "cli_stubs": mode_b_stubs,
            "cli_dual_nonempty_manual_review": mode_b_dual_nonempty,
            "hub": hub_info,
        },
        "mode_c": {
            "anomalies": mode_c_anomalies,
        },
        "mode_d": {
            "archived": mode_d_archived,
            "ls_query": ls_info,
        },
    }


# ---------------------------------------------------------------------------
# Restore & Finalize
# ---------------------------------------------------------------------------


def _default_app_dir() -> str:
    env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "").strip()
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))
    return os.path.abspath(os.path.expanduser("~/.gemini/antigravity-cli"))


def _default_hub_dir() -> str:
    return os.path.abspath(os.path.expanduser("~/.gemini/antigravity"))


def _default_project_json_path() -> str:
    return os.path.abspath(
        os.path.expanduser("~/.gemini/config/projects/default-cli-project.json")
    )


def _is_under_gemini_config(path: str) -> bool:
    cfg_root = os.path.abspath(os.path.expanduser("~/.gemini/config"))
    target = os.path.abspath(os.path.expanduser(path))
    return target == cfg_root or target.startswith(cfg_root + os.sep)


def _format_finalize_command(
    script_path: str,
    app_dir: str,
    default_project_json: str,
    extra_strip_uris: Sequence[str],
) -> str:
    parts = ["python3", shlex.quote(os.path.abspath(script_path)), "--finalize"]
    if os.path.abspath(app_dir) != _default_app_dir():
        parts.extend(["--app-dir", shlex.quote(app_dir)])
    if os.path.abspath(default_project_json) != _default_project_json_path():
        parts.extend(["--default-project-json", shlex.quote(default_project_json)])
    for u in extra_strip_uris:
        parts.extend(["--strip-uri", shlex.quote(u)])
    return " ".join(parts)


def _format_clear_default_project_oneliner(default_project_json: str) -> str:
    quoted_path = json.dumps(os.path.abspath(default_project_json))
    return (
        "python3 -c '"
        "import json, os, shutil, tempfile; "
        f"p = {quoted_path}; "
        'shutil.copy2(p, p + ".bak"); '
        'd = json.load(open(p, "r", encoding="utf-8")); '
        'd.setdefault("projectResources", {})["resources"] = []; '
        'fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp"); '
        'f = os.fdopen(fd, "w", encoding="utf-8"); '
        'json.dump(d, f, indent=2, ensure_ascii=False); f.write("\\n"); f.close(); '
        "os.replace(tmp, p)'"
    )


def _clear_default_project_file(
    path: str,
    backup: BackupManager,
    inside_agent_session: bool,
) -> Dict[str, Any]:
    """Clear ``projectResources.resources`` to ``[]`` while preserving all other fields."""
    if not os.path.exists(path):
        return {"status": "skipped", "reason": "file_not_found"}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    pr = data.get("projectResources")
    current_res = pr.get("resources") if isinstance(pr, dict) else None
    if not current_res:
        return {"status": "already_clean"}

    # Never attempt to bypass ~/.gemini/config/ protection from inside an agent session
    if inside_agent_session and _is_under_gemini_config(path):
        return {
            "status": "deferred_agent_boundary",
            "reason": "Inside an active agy agent session; ~/.gemini/config/ is protected. Run this command in a regular terminal after exiting agy.",
            "user_command": _format_clear_default_project_oneliner(path),
        }

    backup.backup_file(path, os.path.join("config", os.path.basename(path)))
    if not isinstance(pr, dict):
        data["projectResources"] = {"resources": []}
    else:
        pr["resources"] = []
    try:
        atomic_write_json(path, data)
        return {"status": "cleared", "removed_count": len(current_res)}
    except OSError as exc:
        return {
            "status": "permission_denied",
            "error": str(exc),
            "user_command": _format_clear_default_project_oneliner(path),
        }


def run_restore(
    diag: Dict[str, Any],
    *,
    dry_run: bool = False,
    live_inactive_only: bool = False,
    clear_default_project: bool = True,
    rebuild_summaries: bool = True,
    force_rebuild_caches: bool = False,
    backup_dir: Optional[str] = None,
    unarchive: bool = False,
    all_archived: bool = False,
    cid_filter: Optional[Set[str]] = None,
    extra_strip_uris: Sequence[str] = (),
) -> Tuple[int, Dict[str, Any]]:
    """Execute dry-run or real restoration with pre-write validation and backups."""
    app_dir = diag["paths"]["app_dir"]
    default_project_json = diag["paths"]["default_project_json"]
    summaries_db = diag["paths"]["summaries_db"]
    jetbox_pb = diag["paths"]["jetbox_pb"]
    metadata_json = diag["paths"]["metadata_json"]
    last_convs_json = os.path.join(app_dir, "cache", "last_conversations.json")
    conv_dir = os.path.join(app_dir, "conversations")
    hub_info = diag["mode_b"].get("hub")

    polluter_norms: Set[str] = set(diag["polluter_uris"])
    running_procs = diag["runtime"]["running_agy_processes"]
    active_cids: Set[str] = set(diag["runtime"]["active_cids"])
    inside_agent = diag["runtime"]["inside_agent_session"]

    is_default_live_store = (
        os.path.abspath(app_dir) == _default_app_dir()
        or _is_under_gemini_config(default_project_json)
    )
    blocking_procs = running_procs if is_default_live_store else []
    lsof_open = set(diag["runtime"]["lsof_open_cids"])

    if diag["store_counts"].get("conversations_access_error"):
        return 1, {
            "status": "error_access_denied",
            "error": diag["store_counts"]["conversations_access_error"],
        }

    # Safety gate: refuse live write unless --live-inactive-only (or pure dry-run)
    if not dry_run and not live_inactive_only and (blocking_procs or lsof_open):
        pids = [p["pid"] for p in blocking_procs]
        return 2, {
            "status": "refused_agy_running",
            "running_pids": pids,
            "open_cids": sorted(lsof_open),
            "message": (
                "Refusing full offline restore while agy is running. A running agy process "
                "caches jetbox_summaries_proto.pb in RAM and overwrites it on exit, and holds "
                "SQLite locks on active conversations. Exit all agy instances first, then run "
                "this command in a regular terminal — or pass --live-inactive-only for partial "
                "repair of inactive conversations followed by --finalize after exit."
            ),
            "finalize_command": _format_finalize_command(
                __file__, app_dir, default_project_json, extra_strip_uris
            ),
        }

    # Build Mode A repair plan (pre-validate EVERY Protobuf mutation before opening any DB for write)
    mode_a_targets = diag["mode_a"]["hidden"] + diag["mode_a"]["secondary_polluted"]
    mode_a_summary_stale = diag["mode_a"]["summary_only_stale"]
    planned_db_mutations: List[Dict[str, Any]] = []
    skipped_active_cids: List[str] = []
    validation_errors: List[Dict[str, str]] = []

    for item in mode_a_targets:
        cid = item["cid"]
        if live_inactive_only and cid in active_cids:
            skipped_active_cids.append(cid)
            continue
        db_path = os.path.join(conv_dir, f"{cid}.db")
        db_info = inspect_conversation_db(db_path)
        if db_info["error"] or not db_info["blob_main"]:
            validation_errors.append(
                {"cid": cid, "error": db_info["error"] or "empty trajectory_metadata_blob"}
            )
            continue
        try:
            new_blob, changed, surviving = strip_trajectory_metadata_blob(
                db_info["blob_main"], polluter_norms
            )
        except ProtobufError as exc:
            validation_errors.append({"cid": cid, "error": str(exc)})
            continue
        if changed:
            planned_db_mutations.append(
                {
                    "cid": cid,
                    "db_path": db_path,
                    "new_blob": new_blob,
                    "surviving_uris": sorted(dict.fromkeys(normalize_uri(u) for u in surviving)),
                    "was_hidden": item in diag["mode_a"]["hidden"],
                }
            )

    if validation_errors:
        return 3, {
            "status": "aborted_protobuf_validation_failed",
            "errors": validation_errors,
        }

    # Plan Mode B stub isolations
    planned_cli_stubs = [
        s
        for s in diag["mode_b"]["cli_stubs"]
        if not (live_inactive_only and s["cid"] in active_cids)
    ]
    planned_hub_stubs = (
        list(hub_info["mode_b_stubs"])
        if (hub_info and not live_inactive_only)
        else []
    )

    # Plan Mode C zeroed summary cleanups (offline removes stale zeroed rows + summary caches)
    planned_mode_c_zeroed_cids = [
        a["cid"]
        for a in diag["mode_c"]["anomalies"]
        if a["reason"] == "zeroed_summary"
        and not (live_inactive_only and a["cid"] in active_cids)
    ]
    planned_mode_c_missing_cids = [
        a["cid"]
        for a in diag["mode_c"]["anomalies"]
        if a["reason"] == "missing_summary"
        and not (live_inactive_only and a["cid"] in active_cids)
    ]
    caches_exist = os.path.exists(jetbox_pb) or os.path.exists(metadata_json)
    planned_mode_c_cids = planned_mode_c_zeroed_cids + (
        planned_mode_c_missing_cids if caches_exist else []
    )

    # Plan Mode A summary updates (both newly stripped .db CIDs and summary-only stale CIDs)
    summary_update_cids: Set[str] = {m["cid"] for m in planned_db_mutations}
    for item in mode_a_summary_stale:
        cid = item["cid"]
        if not (live_inactive_only and cid in active_cids):
            summary_update_cids.add(cid)

    # Pre-validate summary raw_summary mutations in memory
    planned_summary_mutations: Dict[str, Dict[str, Any]] = {}
    if summary_update_cids and os.path.exists(summaries_db):
        with sqlite_readonly_copy(summaries_db) as conn:
            for cid in sorted(summary_update_cids):
                row = conn.execute(
                    "SELECT workspace_uris, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
                if not row:
                    continue
                uris_raw, raw_sum = row
                try:
                    cur_uris = json.loads(uris_raw) if (uris_raw and uris_raw.startswith("[")) else []
                except ValueError:
                    cur_uris = []
                norm_uris = [normalize_uri(u) for u in cur_uris if u]
                surviving_uris = sorted(dict.fromkeys(u for u in norm_uris if u not in polluter_norms))
                if not surviving_uris:
                    continue
                new_raw_sum = raw_sum
                raw_changed = False
                if raw_sum:
                    try:
                        new_raw_sum, raw_changed = strip_raw_summary_blob(
                            bytes(raw_sum), polluter_norms
                        )
                    except ProtobufError as exc:
                        validation_errors.append({"cid": cid, "error": f"raw_summary: {exc}"})
                        continue
                uris_changed = surviving_uris != sorted(dict.fromkeys(norm_uris))
                if uris_changed or raw_changed:
                    planned_summary_mutations[cid] = {
                        "workspace_uris_json": json.dumps(surviving_uris, separators=(",", ":")),
                        "surviving_uris": surviving_uris,
                        "fts_workspace": uri_basename_encoded(surviving_uris[0]),
                        "raw_summary": new_raw_sum,
                    }

    if validation_errors:
        return 3, {
            "status": "aborted_summary_protobuf_validation_failed",
            "errors": validation_errors,
        }

    # Plan Mode D unarchives
    planned_unarchives: List[str] = []
    if unarchive:
        archived_cids = [a["cid"] for a in diag["mode_d"]["archived"]]
        if cid_filter:
            planned_unarchives = [c for c in archived_cids if c in cid_filter]
        elif all_archived:
            planned_unarchives = archived_cids

    has_repairs = bool(
        planned_db_mutations
        or planned_summary_mutations
        or planned_cli_stubs
        or planned_hub_stubs
        or planned_mode_c_cids
        or (clear_default_project and bool(diag["default_project"]["polluter_uris"]))
        or planned_unarchives
    )
    should_remove_caches_offline = (
        not live_inactive_only
        and rebuild_summaries
        and (
            (force_rebuild_caches and (os.path.exists(jetbox_pb) or os.path.exists(metadata_json)))
            or bool(planned_db_mutations)
            or bool(planned_summary_mutations)
            or bool(planned_cli_stubs)
            or bool(planned_mode_c_cids)
            or diag["mode_a"]["jetbox"]["polluted_entries"] > 0
            or diag["mode_a"]["metadata_cache_polluted"] > 0
        )
    )

    plan_summary: Dict[str, Any] = {
        "mode": "dry_run" if dry_run else ("live_inactive_only" if live_inactive_only else "offline_restore"),
        "clear_default_project": bool(clear_default_project and diag["default_project"]["polluter_uris"]),
        "mode_a_db_repairs": len(planned_db_mutations),
        "mode_a_hidden_restored": sum(1 for m in planned_db_mutations if m["was_hidden"]),
        "mode_a_summary_repairs": len(planned_summary_mutations),
        "mode_a_pb_only_skipped": len(diag["mode_a"]["pb_only_skipped"]),
        "mode_a_genuine_preserved": diag["mode_a"]["genuine_single_workspace_preserved"],
        "mode_b_cli_stubs_to_isolate": len(planned_cli_stubs),
        "mode_b_hub_stubs_to_isolate": len(planned_hub_stubs),
        "mode_c_anomalies_to_reconcile": len(planned_mode_c_cids),
        "mode_d_to_unarchive": len(planned_unarchives),
        "skipped_active_cids": skipped_active_cids,
        "will_remove_summary_caches_for_native_reconcile": should_remove_caches_offline,
        "has_work": bool(has_repairs or should_remove_caches_offline),
    }

    if dry_run:
        plan_summary["status"] = "dry_run_ok"
        plan_summary["mode_a_sample_cids"] = [m["cid"] for m in planned_db_mutations[:15]]
        plan_summary["mode_b_stub_cids"] = [s["cid"] for s in planned_cli_stubs]
        plan_summary["mode_c_cids"] = planned_mode_c_cids[:15]
        return 0, plan_summary

    if not plan_summary["has_work"]:
        plan_summary["status"] = "nothing_to_do"
        return 0, plan_summary

    # Initialize timestamped backup directory
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = backup_dir or os.path.join(tempfile.gettempdir(), f"agy-conv-restore-backup-{ts}")
    backup = BackupManager(bdir)

    results: Dict[str, Any] = {
        **plan_summary,
        "backup_dir": backup.backup_dir,
        "default_project_result": None,
        "mode_b_isolated": [],
        "mode_b_hub_isolated": [],
        "mode_a_dbs_updated": 0,
        "summaries_db_updated": 0,
        "mode_c_zeroed_rows_cleared": 0,
        "jetbox_action": "untouched",
        "metadata_cache_action": "untouched",
        "hub_summaries_pb_action": "untouched",
        "unarchive_results": [],
    }

    # Back up shared index & cache files up front
    if os.path.exists(summaries_db):
        backup.backup_sqlite_triplet(summaries_db, "")
    if os.path.exists(jetbox_pb):
        backup.backup_file(jetbox_pb, "jetbox_summaries_proto.pb")
    if os.path.exists(metadata_json):
        backup.backup_file(metadata_json, os.path.join("cache", "conversation_metadata.json"))
    if os.path.exists(last_convs_json):
        backup.backup_file(last_convs_json, os.path.join("cache", "last_conversations.json"))

    # Step 1: Isolate verified Mode B 0-step stubs (never delete permanently)
    for stub in planned_cli_stubs:
        moved = backup.move_stub_triplet(stub["db_path"], "stub_dbs")
        results["mode_b_isolated"].append({"cid": stub["cid"], "moved_files": moved})

    for stub in planned_hub_stubs:
        moved = backup.move_stub_triplet(stub["db_path"], "hub_stub_dbs")
        results["mode_b_hub_isolated"].append({"cid": stub["cid"], "moved_files": moved})

    # Step 2: Apply Mode A mutations to each .db with per-DB backup & PRAGMA integrity_check
    for mut in planned_db_mutations:
        cid = mut["cid"]
        db_path = mut["db_path"]
        backup.backup_sqlite_triplet(db_path, "conversations")
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            conn.execute(
                "UPDATE trajectory_metadata_blob SET data = ? WHERE id = 'main'",
                (sqlite3.Binary(mut["new_blob"]),),
            )
            conn.commit()
            chk = conn.execute("PRAGMA integrity_check").fetchall()
            if chk != [("ok",)]:
                raise sqlite3.DatabaseError(f"integrity_check failed for {cid}: {chk}")
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            rollback_errors = backup.restore_sqlite_triplet(db_path)
            manifest_path = backup.write_manifest(
                {
                    "status": "aborted_db_error",
                    "failed_cid": cid,
                    "error": str(exc),
                    "rollback_errors": rollback_errors,
                }
            )
            return 4, {
                "status": "aborted_db_integrity_error",
                "failed_cid": cid,
                "error": str(exc),
                "rollback_errors": rollback_errors,
                "backup_dir": backup.backup_dir,
                "manifest_path": manifest_path,
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass
        results["mode_a_dbs_updated"] += 1

    # Step 3: Update conversation_summaries.db + conversations_fts
    need_summaries_update = bool(
        planned_summary_mutations
        or (not live_inactive_only and (planned_mode_c_zeroed_cids or planned_cli_stubs))
    )
    if os.path.exists(summaries_db) and need_summaries_update:
        conn = sqlite3.connect(summaries_db, timeout=10.0)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            has_fts = "conversations_fts" in tables
            for cid, sm in planned_summary_mutations.items():
                conn.execute(
                    """
                    UPDATE conversation_summaries
                    SET workspace_uris = ?, raw_summary = ?
                    WHERE conversation_id = ?
                    """,
                    (
                        sm["workspace_uris_json"],
                        sqlite3.Binary(sm["raw_summary"]) if sm["raw_summary"] is not None else None,
                        cid,
                    ),
                )
                if has_fts:
                    conn.execute(
                        "UPDATE conversations_fts SET workspace = ? WHERE conversation_id = ?",
                        (sm["fts_workspace"], cid),
                    )
                results["summaries_db_updated"] += 1

            # In offline mode, clear stuck zeroed summary rows for healthy Mode C DBs
            # and newly isolated Mode B stubs so native reconcile_summaries.go rebuilds them cleanly
            if not live_inactive_only:
                zeroed_to_clear = list(
                    dict.fromkeys(
                        planned_mode_c_zeroed_cids + [s["cid"] for s in planned_cli_stubs]
                    )
                )
                for zcid in zeroed_to_clear:
                    cur = conn.execute(
                        """
                        DELETE FROM conversation_summaries
                        WHERE conversation_id = ?
                          AND (step_count = 0 OR last_modified_time LIKE '0001-%')
                        """,
                        (zcid,),
                    )
                    if cur.rowcount > 0:
                        results["mode_c_zeroed_rows_cleared"] += cur.rowcount

            conn.commit()
            chk = conn.execute("PRAGMA integrity_check").fetchall()
            if chk != [("ok",)]:
                raise sqlite3.DatabaseError(f"conversation_summaries.db integrity_check failed: {chk}")
            if has_fts:
                conn.execute("INSERT INTO conversations_fts(conversations_fts) VALUES('integrity-check')")
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            rollback_errors = backup.restore_sqlite_triplet(summaries_db)
            manifest_path = backup.write_manifest(
                {
                    "status": "aborted_summaries_error",
                    "error": str(exc),
                    "rollback_errors": rollback_errors,
                }
            )
            return 4, {
                "status": "aborted_summaries_integrity_error",
                "error": str(exc),
                "rollback_errors": rollback_errors,
                "backup_dir": backup.backup_dir,
                "manifest_path": manifest_path,
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Step 4: Clear default-cli-project.json ONLY AFTER all SQLite writes commit & pass integrity checks,
    # so any mid-flight DB error leaves the polluter record intact for safe retries without --strip-uri.
    if clear_default_project and diag["default_project"]["polluter_uris"]:
        results["default_project_result"] = _clear_default_project_file(
            default_project_json, backup, inside_agent
        )

    # Step 5: Reconcile jetbox_summaries_proto.pb and cache/conversation_metadata.json
    if should_remove_caches_offline:
        if os.path.exists(jetbox_pb):
            os.unlink(jetbox_pb)
            results["jetbox_action"] = "backed_up_and_removed_for_native_reconcile"
        if os.path.exists(metadata_json):
            os.unlink(metadata_json)
            results["metadata_cache_action"] = "backed_up_and_removed_for_native_reconcile"
        if hub_info and results["mode_b_hub_isolated"]:
            hub_pb = os.path.join(hub_info["hub_dir"], "agyhub_summaries_proto.pb")
            if os.path.exists(hub_pb):
                backup.backup_file(hub_pb, os.path.join("hub", "agyhub_summaries_proto.pb"))
                os.unlink(hub_pb)
                results["hub_summaries_pb_action"] = "backed_up_and_removed_for_native_reconcile"
    elif live_inactive_only:
        if os.path.exists(jetbox_pb) and planned_summary_mutations:
            updated_map = {
                cid: bytes(sm["raw_summary"])
                for cid, sm in planned_summary_mutations.items()
                if sm["raw_summary"]
            }
            if updated_map:
                with open(jetbox_pb, "rb") as fh:
                    raw_jb = fh.read()
                new_jb, patched_n = patch_jetbox_summaries_proto(raw_jb, updated_map)
                atomic_write_bytes(jetbox_pb, new_jb)
                results["jetbox_action"] = f"patched_{patched_n}_inactive_entries"
        if os.path.exists(metadata_json) and planned_summary_mutations:
            try:
                with open(metadata_json, "r", encoding="utf-8") as fh:
                    cm = json.load(fh)
                convs = cm.get("conversations")
                patched_cm = 0
                if isinstance(convs, dict):
                    for cid, sm in planned_summary_mutations.items():
                        entry = convs.get(cid)
                        if isinstance(entry, dict) and isinstance(entry.get("summary"), dict):
                            entry["summary"]["WorkspaceURIs"] = sm["surviving_uris"]
                            patched_cm += 1
                    if patched_cm:
                        atomic_write_json(metadata_json, cm)
                results["metadata_cache_action"] = f"patched_{patched_cm}_inactive_entries"
            except (OSError, ValueError) as exc:
                results["metadata_cache_action"] = f"error:{exc}"

    # Step 6: Mode D explicit unarchive
    for cid in planned_unarchives:
        ok, msg = call_ls_unarchive(cid)
        results["unarchive_results"].append({"cid": cid, "ok": ok, "message": msg})

    manifest_path = backup.write_manifest(
        {
            "mode": results["mode"],
            "mode_a_dbs_updated": results["mode_a_dbs_updated"],
            "summaries_db_updated": results["summaries_db_updated"],
            "jetbox_action": results["jetbox_action"],
            "metadata_cache_action": results["metadata_cache_action"],
        }
    )
    results["manifest_path"] = manifest_path

    if live_inactive_only:
        results["status"] = "partial_live_restore_complete"
        results["finalize_command"] = _format_finalize_command(
            __file__, app_dir, default_project_json, extra_strip_uris
        )
        results["next_step_note"] = (
            "Inactive .db files are repaired on disk. Because the running agy process keeps "
            "stale summaries in RAM and overwrites jetbox_summaries_proto.pb on exit, exit "
            "all agy sessions and run the finalize_command in a regular terminal before relaunching agy."
        )
    else:
        results["status"] = "offline_restore_complete"

    return 0, results


# ---------------------------------------------------------------------------
# Verification (SQLite integrity, anomaly re-scan, optional /tmp staging hub)
# ---------------------------------------------------------------------------


def verify_store(
    app_dir: str,
    default_project_json: str,
    hub_dir: Optional[str] = None,
    extra_strip_uris: Sequence[str] = (),
    verify_staging: bool = False,
    staging_sample: int = 5,
    staging_timeout: int = 20,
    agy_bin: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Verify store integrity, zero remaining Mode A/B/C issues, and optional staging reconcile."""
    diag = diagnose_store(
        app_dir=app_dir,
        default_project_json=default_project_json,
        hub_dir=hub_dir,
        extra_strip_uris=extra_strip_uris,
        query_ls=False,
    )
    summaries_db = diag["paths"]["summaries_db"]
    conv_dir = os.path.join(diag["paths"]["app_dir"], "conversations")

    sqlite_checks: Dict[str, Any] = {
        "summaries_db_integrity": "not_present",
        "fts5_integrity": "not_present",
        "conversation_dbs_checked": 0,
        "conversation_dbs_failed": [],
    }

    if os.path.exists(summaries_db):
        try:
            with sqlite_readonly_copy(summaries_db, force_copy=True) as conn:
                chk = conn.execute("PRAGMA integrity_check").fetchall()
                sqlite_checks["summaries_db_integrity"] = "ok" if chk == [("ok",)] else str(chk)
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "conversations_fts" in tables:
                    conn.execute(
                        "INSERT INTO conversations_fts(conversations_fts) VALUES('integrity-check')"
                    )
                    sqlite_checks["fts5_integrity"] = "ok"
        except Exception as exc:
            sqlite_checks["summaries_db_integrity"] = f"error: {exc}"

    if os.path.isdir(conv_dir):
        for fn in sorted(os.listdir(conv_dir)):
            if not fn.endswith(".db"):
                continue
            db_path = os.path.join(conv_dir, fn)
            sqlite_checks["conversation_dbs_checked"] += 1
            try:
                with sqlite_readonly_copy(db_path) as conn:
                    chk = conn.execute("PRAGMA integrity_check").fetchall()
                    if chk != [("ok",)]:
                        sqlite_checks["conversation_dbs_failed"].append(
                            {"file": fn, "result": str(chk)}
                        )
            except Exception as exc:
                sqlite_checks["conversation_dbs_failed"].append(
                    {"file": fn, "result": str(exc)}
                )

    remaining = {
        "default_project_polluters": len(diag["default_project"]["polluter_uris"]),
        "mode_a_hidden": len(diag["mode_a"]["hidden"]),
        "mode_a_secondary": len(diag["mode_a"]["secondary_polluted"]),
        "mode_a_summary_only_stale": len(diag["mode_a"]["summary_only_stale"]),
        "mode_a_jetbox_polluted": diag["mode_a"]["jetbox"]["polluted_entries"],
        "mode_b_cli_stubs": len(diag["mode_b"]["cli_stubs"]),
        "mode_b_hub_stubs": len(diag["mode_b"]["hub"]["mode_b_stubs"]) if diag["mode_b"]["hub"] else 0,
        "mode_c_anomalies": len(diag["mode_c"]["anomalies"]),
    }

    # Note: after offline finalize removes jetbox_summaries_proto.pb and deletes Mode C
    # zeroed rows, missing_summary rows await the next agy launch to reconcile. Report them
    # transparently while distinguishing them from hard failures if jetbox_pb is absent.
    jetbox_removed = not diag["mode_a"]["jetbox"]["exists"]
    hard_mode_c = [
        a
        for a in diag["mode_c"]["anomalies"]
        if not (jetbox_removed and a["reason"] == "missing_summary")
    ]
    remaining["mode_c_hard_anomalies"] = len(hard_mode_c)

    staging_res: Optional[Dict[str, Any]] = None
    if verify_staging:
        staging_res = run_staging_verification(
            app_dir=diag["paths"]["app_dir"],
            polluter_norms=set(diag["polluter_uris"]),
            sample_size=staging_sample,
            timeout_sec=staging_timeout,
            agy_bin=agy_bin,
        )

    access_error = diag["store_counts"].get("conversations_access_error")
    passed = (
        not access_error
        and sqlite_checks["summaries_db_integrity"] in ("ok", "not_present")
        and sqlite_checks["fts5_integrity"] in ("ok", "not_present")
        and not sqlite_checks["conversation_dbs_failed"]
        and remaining["default_project_polluters"] == 0
        and remaining["mode_a_hidden"] == 0
        and remaining["mode_a_secondary"] == 0
        and remaining["mode_a_summary_only_stale"] == 0
        and remaining["mode_a_jetbox_polluted"] == 0
        and remaining["mode_b_cli_stubs"] == 0
        and remaining["mode_b_hub_stubs"] == 0
        and remaining["mode_c_hard_anomalies"] == 0
        and (staging_res is None or staging_res.get("passed") is True)
    )

    report = {
        "passed": passed,
        "access_error": access_error,
        "sqlite_checks": sqlite_checks,
        "remaining_anomalies": remaining,
        "jetbox_pending_native_reconcile": jetbox_removed,
        "staging_verification": staging_res,
    }
    return (0 if passed else 1), report


def run_staging_verification(
    app_dir: str,
    polluter_norms: Set[str],
    sample_size: int = 5,
    timeout_sec: int = 20,
    agy_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy sample ``.db`` files into an isolated ``/tmp`` HOME and verify native ``agy --hub`` reconcile."""
    exe = agy_bin or shutil.which("agy")
    if not exe:
        return {"passed": False, "error": "agy binary not found in PATH"}

    conv_dir = os.path.join(app_dir, "conversations")
    ann_dir = os.path.join(app_dir, "annotations")
    if not os.path.isdir(conv_dir):
        return {"passed": False, "error": f"conversations directory not found: {conv_dir}"}

    # Pick up to sample_size non-empty .db files (newest first)
    candidates: List[Tuple[float, str, int]] = []
    for fn in os.listdir(conv_dir):
        if not fn.endswith(".db"):
            continue
        cid = fn[:-3]
        path = os.path.join(conv_dir, fn)
        info = inspect_conversation_db(path)
        if info["error"] or not (info["steps_count"] and info["steps_count"] > 0):
            continue
        candidates.append((os.path.getmtime(path), cid, info["steps_count"]))
    candidates.sort(reverse=True)
    chosen = candidates[: max(1, sample_size)]
    if not chosen:
        return {"passed": False, "error": "no non-empty conversation .db files available to sample"}

    staging_root = tempfile.mkdtemp(prefix="agy-conv-verify-staging-")
    proc: Optional[subprocess.Popen] = None
    try:
        st_app = os.path.join(staging_root, ".gemini", "antigravity-cli")
        st_conv = os.path.join(st_app, "conversations")
        st_ann = os.path.join(st_app, "annotations")
        st_proj = os.path.join(staging_root, ".gemini", "config", "projects")
        os.makedirs(st_conv, exist_ok=True)
        os.makedirs(st_ann, exist_ok=True)
        os.makedirs(st_proj, exist_ok=True)

        atomic_write_json(
            os.path.join(st_proj, "default-cli-project.json"),
            {
                "id": "staging-default-project",
                "name": "Default",
                "projectResources": {"resources": []},
            },
        )

        expected_steps: Dict[str, int] = {}
        for _mtime, cid, steps in chosen:
            expected_steps[cid] = steps
            for suffix in ("", "-wal", "-shm"):
                src = os.path.join(conv_dir, f"{cid}.db{suffix}")
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(st_conv, f"{cid}.db{suffix}"))
            ann_src = os.path.join(ann_dir, f"{cid}.pbtxt")
            if os.path.exists(ann_src):
                shutil.copy2(ann_src, os.path.join(st_ann, f"{cid}.pbtxt"))

        env = os.environ.copy()
        env["HOME"] = staging_root
        for var in (
            "ANTIGRAVITY_LS_ADDRESS",
            "ANTIGRAVITY_CSRF_TOKEN",
            "ANTIGRAVITY_CONVERSATION_ID",
            "ANTIGRAVITY_APP_DATA_DIR",
        ):
            env.pop(var, None)

        log_path = os.path.join(staging_root, "hub.log")
        with open(log_path, "wb") as log_fh:
            proc = subprocess.Popen(
                [exe, "--hub", "--hub-port=0", "--app_data_dir=antigravity-cli"],
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            st_sum_db = os.path.join(st_app, "conversation_summaries.db")
            st_jb_pb = os.path.join(st_app, "jetbox_summaries_proto.pb")
            deadline = time.monotonic() + timeout_sec
            reconciled = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if os.path.exists(st_sum_db) and os.path.exists(st_jb_pb):
                    try:
                        with sqlite_readonly_copy(st_sum_db) as conn:
                            cnt = conn.execute(
                                "SELECT COUNT(*) FROM conversation_summaries WHERE step_count > 0"
                            ).fetchone()[0]
                        if cnt >= len(expected_steps):
                            reconciled = True
                            break
                    except sqlite3.Error:
                        pass
                time.sleep(0.3)

            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)

        reconciled_rows: Dict[str, Any] = {}
        integrity = "not_created"
        if os.path.exists(st_sum_db):
            with sqlite_readonly_copy(st_sum_db) as conn:
                chk = conn.execute("PRAGMA integrity_check").fetchall()
                integrity = "ok" if chk == [("ok",)] else str(chk)
                for cid, sc, uris_raw in conn.execute(
                    "SELECT conversation_id, step_count, workspace_uris FROM conversation_summaries"
                ):
                    try:
                        uris = json.loads(uris_raw) if (uris_raw and uris_raw.startswith("[")) else []
                    except ValueError:
                        uris = []
                    reconciled_rows[cid] = {
                        "step_count": sc,
                        "workspace_uris": uris,
                    }

        jb_check = inspect_jetbox_for_polluters(st_jb_pb, polluter_norms)
        missing_cids = [c for c in expected_steps if c not in reconciled_rows]
        zero_step_cids = [
            c for c in expected_steps if reconciled_rows.get(c, {}).get("step_count", 0) <= 0
        ]
        polluted_cids = [
            c
            for c, row in reconciled_rows.items()
            if any(normalize_uri(u) in polluter_norms for u in row["workspace_uris"])
            and len(row["workspace_uris"]) > 1
        ]
        passed = (
            reconciled
            and integrity == "ok"
            and not missing_cids
            and not zero_step_cids
            and not polluted_cids
            and jb_check["polluted_entries"] == 0
        )
        return {
            "passed": passed,
            "sampled_count": len(expected_steps),
            "reconciled_count": len(reconciled_rows),
            "integrity": integrity,
            "missing_cids": missing_cids,
            "zero_step_cids": zero_step_cids,
            "polluted_cids": polluted_cids,
            "jetbox_polluted_entries": jb_check["polluted_entries"],
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        shutil.rmtree(staging_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Human-readable reporting
# ---------------------------------------------------------------------------


def print_diagnosis(diag: Dict[str, Any], verbose: bool = False) -> None:
    """Print a clean human-readable diagnosis report."""
    paths = diag["paths"]
    rt = diag["runtime"]
    counts = diag["store_counts"]
    dp = diag["default_project"]
    ma = diag["mode_a"]
    mb = diag["mode_b"]
    mc = diag["mode_c"]
    md = diag["mode_d"]

    print(f"agy conv-restore v{VERSION} — Conversation Store Diagnosis")
    print(f"  App dir            : {paths['app_dir']}")
    print(f"  Default project    : {paths['default_project_json']}")
    if counts.get("conversations_access_error"):
        print(f"  ACCESS ERROR       : {counts['conversations_access_error']}")
    print(
        f"  Store totals       : {counts['db_files']} .db, "
        f"{counts['pb_files_nonempty']} non-empty .pb, "
        f"{counts['summary_rows']} summary rows"
    )
    if rt["running_agy_processes"]:
        pids = ", ".join(str(p["pid"]) for p in rt["running_agy_processes"])
        print(f"  Running agy PIDs   : {pids} ({len(rt['active_cids'])} active/open CID(s))")
    else:
        print("  Running agy PIDs   : none (safe for offline restore)")

    print("\n[Mode A] default-cli-project.json Workspace Pollution (#1657)")
    if dp["polluter_uris"]:
        print(f"  Polluters in default-cli-project.json ({len(dp['polluter_uris'])}):")
        for u in dp["polluter_uris"]:
            print(f"    - {uri_display(u)}")
    else:
        print("  default-cli-project.json resources: clean (empty)")
    if diag["polluter_uris"]:
        print(
            f"  Hidden (.db, polluter at URI[0])  : {len(ma['hidden'])} "
            f"(top-level: {sum(1 for x in ma['hidden'] if x['is_top_level'])})"
        )
        print(
            f"  Secondary polluted (.db)          : {len(ma['secondary_polluted'])} "
            f"(top-level: {sum(1 for x in ma['secondary_polluted'] if x['is_top_level'])})"
        )
        print(f"  Summary-only stale                : {len(ma['summary_only_stale'])}")
        print(f"  Legacy .pb-only (skipped safely)  : {len(ma['pb_only_skipped'])}")
        print(f"  Genuine single-workspace kept     : {ma['genuine_single_workspace_preserved']}")
        print(
            f"  jetbox_summaries_proto.pb         : "
            f"{'present' if ma['jetbox']['exists'] else 'absent'} "
            f"(polluted entries: {ma['jetbox']['polluted_entries']})"
        )
        print(f"  conversation_metadata.json        : polluted entries: {ma['metadata_cache_polluted']}")
        if ma["hidden"]:
            limit = len(ma["hidden"]) if verbose else min(10, len(ma["hidden"]))
            print(f"  Hidden conversations (showing {limit} of {len(ma['hidden'])}):")
            for item in ma["hidden"][:limit]:
                kind = "top" if item["is_top_level"] else "child"
                title = item["title"] or "(untitled)"
                print(
                    f"    - {item['cid']} [{kind}, {item['steps_count']} steps] "
                    f"{title} -> {uri_display(item['primary_after'])}"
                )
    elif diag["suggested_polluters"]:
        print(
            f"  No active polluter in default-cli-project.json, but "
            f"{counts['multi_workspace_conversations']} multi-workspace conversations exist."
        )
        print("  Frequent shared URIs (pass --strip-uri <path> if one is a stale polluter):")
        for sp in diag["suggested_polluters"]:
            print(f"    - {sp['path']} (shared by {sp['multi_ws_conversations']} multi-workspace chats)")

    print("\n[Mode B] 0-Step .db Stub Shadowing .pb (#1602)")
    print(f"  CLI store 0-step stubs            : {len(mb['cli_stubs'])}")
    if mb["cli_dual_nonempty_manual_review"]:
        print(
            f"  CLI dual non-empty (manual review): {len(mb['cli_dual_nonempty_manual_review'])}"
        )
    if mb["hub"] and mb["hub"]["exists"]:
        print(f"  Hub store 0-step stubs            : {len(mb['hub']['mode_b_stubs'])}")
    for s in mb["cli_stubs"][: (len(mb["cli_stubs"]) if verbose else 10)]:
        print(f"    - {s['cid']} (.pb {s['pb_size']} bytes, .db 0 steps)")

    print("\n[Mode C] Stale / Missing / Zeroed Summary Rows")
    print(f"  Anomalies detected                : {len(mc['anomalies'])}")
    for a in mc["anomalies"][: (len(mc["anomalies"]) if verbose else 10)]:
        print(
            f"    - {a['cid']} [{a['storage']}] {a['reason']} "
            f"(actual_steps={a['actual_steps']}, summary_steps={a['summary_steps']})"
        )

    print("\n[Mode D] Archived Annotations")
    print(f"  Archived conversations            : {len(md['archived'])}")
    for a in md["archived"][: (len(md["archived"]) if verbose else 10)]:
        title = a["title"] or "(untitled)"
        print(f"    - {a['cid']} ({a['step_count']} steps) {title}")


def print_restore_result(res: Dict[str, Any]) -> None:
    """Print human-readable output for dry-run, restore, or finalize."""
    status = res.get("status", "")
    print(f"Status: {status}")
    if status == "refused_agy_running":
        print(f"  Running agy PIDs : {res.get('running_pids')}")
        print(f"  Open CIDs        : {res.get('open_cids')}")
        print(f"  Reason           : {res.get('message')}")
        print(f"  Offline finalize : {res.get('finalize_command')}")
        return
    if status.startswith("aborted"):
        print(f"  Details: {json.dumps(res, ensure_ascii=False, indent=2)}")
        return
    if res.get("backup_dir"):
        print(f"  Backup directory : {res['backup_dir']}")
    print(f"  Mode A .db fixes : {res.get('mode_a_dbs_updated', res.get('mode_a_db_repairs', 0))} (hidden: {res.get('mode_a_hidden_restored', 0)})")
    print(f"  Mode A preserved : {res.get('mode_a_genuine_preserved', 0)} genuine single-workspace, {res.get('mode_a_pb_only_skipped', 0)} .pb-only skipped")
    print(f"  Mode B CLI stubs : {len(res.get('mode_b_isolated', [])) if 'mode_b_isolated' in res else res.get('mode_b_cli_stubs_to_isolate', 0)}")
    print(f"  Mode B Hub stubs : {len(res.get('mode_b_hub_isolated', [])) if 'mode_b_hub_isolated' in res else res.get('mode_b_hub_stubs_to_isolate', 0)}")
    print(f"  Mode C cleared   : {res.get('mode_c_zeroed_rows_cleared', res.get('mode_c_anomalies_to_reconcile', 0))}")
    if res.get("jetbox_action"):
        print(f"  jetbox_summaries : {res['jetbox_action']}")
    if res.get("metadata_cache_action"):
        print(f"  metadata cache   : {res['metadata_cache_action']}")
    dp_res = res.get("default_project_result")
    if isinstance(dp_res, dict):
        print(f"  default-project  : {dp_res.get('status')}")
        if dp_res.get("user_command"):
            print(f"  Run outside agy  : {dp_res['user_command']}")
    if res.get("skipped_active_cids"):
        print(f"  Skipped active   : {len(res['skipped_active_cids'])} open conversation(s)")
    if res.get("finalize_command"):
        print(f"\nNext step after exiting ALL agy instances:\n  {res['finalize_command']}")


def print_verify_result(report: Dict[str, Any]) -> None:
    """Print human-readable post-restore verification report."""
    verdict = "PASSED" if report["passed"] else "FAILED"
    print(f"Verification: {verdict}")
    sc = report["sqlite_checks"]
    print(f"  conversation_summaries.db : {sc['summaries_db_integrity']}")
    print(f"  conversations_fts         : {sc['fts5_integrity']}")
    print(
        f"  conversation .db files    : {sc['conversation_dbs_checked']} checked, "
        f"{len(sc['conversation_dbs_failed'])} failed"
    )
    rem = report["remaining_anomalies"]
    print(f"  default-project polluters : {rem['default_project_polluters']}")
    print(
        f"  Mode A remaining          : hidden={rem['mode_a_hidden']}, "
        f"secondary={rem['mode_a_secondary']}, "
        f"summary_stale={rem['mode_a_summary_only_stale']}, "
        f"jetbox_polluted={rem['mode_a_jetbox_polluted']}"
    )
    print(f"  Mode B remaining stubs    : cli={rem['mode_b_cli_stubs']}, hub={rem['mode_b_hub_stubs']}")
    print(
        f"  Mode C remaining          : hard={rem['mode_c_hard_anomalies']} "
        f"(total={rem['mode_c_anomalies']}, pending_reconcile={report['jetbox_pending_native_reconcile']})"
    )
    st = report.get("staging_verification")
    if isinstance(st, dict):
        print(f"  Staging agy --hub check   : {'PASSED' if st.get('passed') else 'FAILED'} ({st})")


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conv-restore",
        description=(
            "Diagnose and restore disappeared Antigravity CLI (agy) conversations "
            "across workspace pollution (#1657), 0-step .db stubs (#1602), stale/zeroed "
            "summaries, and archived annotations."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--diagnose",
        "--list",
        dest="action",
        action="store_const",
        const="diagnose",
        help="Read-only WAL-safe diagnosis of all four disappearance modes (default).",
    )
    mode.add_argument(
        "--dry-run",
        dest="action",
        action="store_const",
        const="dry_run",
        help="Preview planned repairs and validate Protobuf transforms without writing.",
    )
    mode.add_argument(
        "--restore",
        dest="action",
        action="store_const",
        const="restore",
        help="Back up and restore affected conversations (refuses while agy runs unless --live-inactive-only).",
    )
    mode.add_argument(
        "--finalize",
        dest="action",
        action="store_const",
        const="finalize",
        help="Offline post-exit finalize: clear default-cli-project.json, clean remaining .db files, remove stale summary caches, and verify.",
    )
    mode.add_argument(
        "--verify",
        dest="action",
        action="store_const",
        const="verify",
        help="Verify SQLite/FTS5 integrity and confirm zero remaining Mode A/B/C anomalies.",
    )

    parser.add_argument("--app-dir", default=None, help="Override Antigravity CLI data directory (default: $ANTIGRAVITY_APP_DATA_DIR or ~/.gemini/antigravity-cli).")
    parser.add_argument("--hub-dir", default=None, help="Override Antigravity Hub data directory (default: ~/.gemini/antigravity).")
    parser.add_argument("--no-hub", action="store_true", help="Skip checking the Hub store (~/.gemini/antigravity).")
    parser.add_argument("--default-project-json", default=None, help="Override path to default-cli-project.json.")
    parser.add_argument("--backup-dir", default=None, help="Override timestamped backup directory path.")
    parser.add_argument(
        "--strip-uri",
        action="append",
        default=[],
        help="Additional polluter workspace path or file:// URI to strip in Mode A (repeatable).",
    )
    parser.add_argument("--project", default=None, help="Filter diagnosis/Mode A display & restoration to conversations matching this project path, URI, or name.")
    parser.add_argument("--cid", action="append", default=[], help="Limit operation to specific conversation ID(s) (repeatable).")
    parser.add_argument("--no-clear-default-project", action="store_true", help="Do not clear projectResources.resources in default-cli-project.json during --restore.")
    parser.add_argument("--live-inactive-only", action="store_true", help="Allow partial --restore while agy runs by skipping all open/active swarm conversations.")
    parser.add_argument("--no-rebuild-summaries", action="store_true", help="Keep jetbox_summaries_proto.pb and conversation_metadata.json during offline --restore.")
    parser.add_argument("--unarchive", action="store_true", help="Unarchive Mode D conversations via ConversationService/UpdateConversation (requires --cid or --all-archived).")
    parser.add_argument("--all-archived", action="store_true", help="With --unarchive, unarchive all detected archived conversations.")
    parser.add_argument("--query-ls", action="store_true", help="Also query local ConversationService/ListConversations during --diagnose when LS env vars are present.")
    parser.add_argument("--verify-staging", action="store_true", help="Run isolated /tmp staging agy --hub reconciliation verification during --verify or after offline --restore.")
    parser.add_argument("--staging-sample", type=int, default=5, help="Number of .db files to copy for --verify-staging (default: 5).")
    parser.add_argument("--staging-timeout", type=int, default=20, help="Seconds to wait for staging agy --hub reconciliation (default: 20).")
    parser.add_argument("--agy-bin", default=None, help="Override path to agy binary for --verify-staging.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print full conversation lists instead of truncated summaries.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    action = args.action or "diagnose"

    app_dir = args.app_dir or _default_app_dir()
    hub_dir = None if args.no_hub else (args.hub_dir or _default_hub_dir())
    default_project_json = args.default_project_json or _default_project_json_path()
    cid_filter = set(args.cid) if args.cid else None

    if action == "verify":
        code, report = verify_store(
            app_dir=app_dir,
            default_project_json=default_project_json,
            hub_dir=hub_dir,
            extra_strip_uris=args.strip_uri,
            verify_staging=args.verify_staging,
            staging_sample=args.staging_sample,
            staging_timeout=args.staging_timeout,
            agy_bin=args.agy_bin,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_verify_result(report)
        return code

    diag = diagnose_store(
        app_dir=app_dir,
        default_project_json=default_project_json,
        hub_dir=hub_dir,
        extra_strip_uris=args.strip_uri,
        project_filter=args.project,
        cid_filter=cid_filter,
        query_ls=args.query_ls,
    )

    if action == "diagnose":
        if args.json:
            print(json.dumps(diag, ensure_ascii=False, indent=2))
        else:
            print_diagnosis(diag, verbose=args.verbose)
        return 1 if diag["store_counts"].get("conversations_access_error") else 0

    if action == "dry_run":
        if diag["store_counts"].get("conversations_access_error"):
            err_res = {
                "status": "error_access_denied",
                "error": diag["store_counts"]["conversations_access_error"],
            }
            if args.json:
                print(json.dumps(err_res, ensure_ascii=False, indent=2))
            else:
                print(f"Error: {err_res['error']}", file=sys.stderr)
            return 1
        code, res = run_restore(
            diag,
            dry_run=True,
            live_inactive_only=args.live_inactive_only,
            clear_default_project=not args.no_clear_default_project,
            rebuild_summaries=not args.no_rebuild_summaries,
            backup_dir=args.backup_dir,
            unarchive=args.unarchive,
            all_archived=args.all_archived,
            cid_filter=cid_filter,
            extra_strip_uris=args.strip_uri,
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print_restore_result(res)
        return code

    if action in ("restore", "finalize"):
        live_mode = False if action == "finalize" else args.live_inactive_only
        clear_dp = True if action == "finalize" else (not args.no_clear_default_project)
        rebuild_sum = True if action == "finalize" else (not args.no_rebuild_summaries)

        if args.unarchive and not (cid_filter or args.all_archived):
            msg = "--unarchive requires at least one --cid <id> or explicit --all-archived"
            if args.json:
                print(json.dumps({"status": "error", "error": msg}))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 2

        code, res = run_restore(
            diag,
            dry_run=False,
            live_inactive_only=live_mode,
            clear_default_project=clear_dp,
            rebuild_summaries=rebuild_sum,
            force_rebuild_caches=(action == "finalize"),
            backup_dir=args.backup_dir,
            unarchive=args.unarchive,
            all_archived=args.all_archived,
            cid_filter=cid_filter,
            extra_strip_uris=args.strip_uri,
        )
        if code == 0 and not live_mode and res.get("status") == "offline_restore_complete":
            verify_strip_uris = list(
                dict.fromkeys(list(args.strip_uri) + list(diag["polluter_uris"]))
            )
            vcode, vreport = verify_store(
                app_dir=app_dir,
                default_project_json=default_project_json,
                hub_dir=hub_dir,
                extra_strip_uris=verify_strip_uris,
                verify_staging=args.verify_staging,
                staging_sample=args.staging_sample,
                staging_timeout=args.staging_timeout,
                agy_bin=args.agy_bin,
            )
            res["verification"] = vreport
            if vcode != 0:
                code = vcode

        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print_restore_result(res)
            if "verification" in res:
                print()
                print_verify_result(res["verification"])
        return code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
