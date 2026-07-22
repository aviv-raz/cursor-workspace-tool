#!/usr/bin/env python3
"""
cursor_workspace_tool.py

Cursor Workspace Tool
Copyright (C) 2026 Aviv Raz
https://github.com/aviv-raz

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

A cross-platform CLI for inspecting and managing Cursor editor "workspaces"
(workspaceStorage entries) and their associated AI Chat / Composer data.

Works from:
  - WSL / native Linux, scanning both the native Linux Cursor config
    (~/.config/Cursor) AND the Windows-side Cursor config reachable through
    /mnt/<drive>/Users/*/AppData/Roaming/Cursor (used when Cursor runs on
    Windows and you connect into WSL via the Remote-WSL extension - in that
    setup ALL workspace/chat storage lives on the Windows side, even for
    WSL-remote workspaces).
  - Native Windows (python.exe), using %APPDATA%\\Cursor.

Commands:
  list                             List every discovered workspace.
  chats [workspace]                Show all workspaces with non-empty AI
                                    Chats and list each chat by name.
  merge <source> <dest>            Move or copy Cursor AI Chats/Composers
                                    from one workspace to another. Nothing
                                    else about either workspace is touched.
  cleanup workspaces [workspace]   Delete stale workspaceStorage entries
                                    that have ZERO non-empty AI Chats.
  cleanup empty-chats [workspace]  Delete empty AI Chat records.
  restore <backup-dir>              Restore database files/folders from a
                                     backup taken by a previous
                                     `merge`/`cleanup ... --apply` run.

Run `cursor_workspace_tool.py <command> --help` for details on each command.

No third-party dependencies are required (Python 3.8+, stdlib only).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import glob
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATE_DIR = Path(os.environ.get("CURSOR_WS_TOOL_STATE", str(Path.home() / ".cache" / "cursor-workspace-tool")))
LAST_LIST_FILE = STATE_DIR / "last_list.json"

# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------


@dataclass
class Root:
    """One Cursor "User" data directory (one Cursor installation)."""

    root_id: str
    label: str
    base: Path  # .../User

    @property
    def workspace_storage(self) -> Path:
        return self.base / "workspaceStorage"

    @property
    def global_storage_db(self) -> Path:
        return self.base / "globalStorage" / "state.vscdb"


def _iter_windows_users_via_wsl_mount() -> List[Tuple[Path, str]]:
    """Find AppData/Roaming/Cursor/User under any /mnt/<drive>/Users/<user>.
    Returns (path, human label) pairs."""
    found = []
    for mnt in sorted(glob.glob("/mnt/*")):
        users_dir = Path(mnt) / "Users"
        if not users_dir.is_dir():
            continue
        try:
            entries = list(users_dir.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            name = entry.name
            if name in ("Default", "Default User", "Public", "All Users", "desktop.ini"):
                continue
            candidate = entry / "AppData" / "Roaming" / "Cursor" / "User"
            try:
                if candidate.is_dir():
                    found.append((candidate, f"Windows (via {mnt}, user {name})"))
            except (PermissionError, OSError):
                continue
    return found


def discover_roots(extra_roots: Optional[List[str]] = None) -> List[Root]:
    roots: List[Root] = []
    seen_resolved = set()

    def add(base: Path, label: str):
        try:
            resolved = base.resolve()
        except OSError:
            resolved = base
        if resolved in seen_resolved:
            return
        try:
            if not base.is_dir():
                return
        except (PermissionError, OSError):
            return
        seen_resolved.add(resolved)
        roots.append(Root(root_id=f"R{len(roots)}", label=label, base=base))

    system = platform.system()

    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            add(Path(appdata) / "Cursor" / "User", "Windows (local)")
    else:
        # Native Linux / WSL install of Cursor (rare when using Remote-WSL,
        # but fully supported in case chats/workspaces live here too).
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        add(Path(xdg) / "Cursor" / "User", "Linux/WSL (local)")

        # Windows install reachable through the WSL /mnt mount.
        for candidate, label in _iter_windows_users_via_wsl_mount():
            add(candidate, label)

    for extra in extra_roots or []:
        p = Path(extra).expanduser()
        # Allow pointing either at ".../Cursor" or ".../Cursor/User"
        if p.name != "User" and (p / "User").is_dir():
            p = p / "User"
        add(p, f"custom ({p})")

    return roots


# ---------------------------------------------------------------------------
# URI decoding helpers
# ---------------------------------------------------------------------------


@dataclass
class DecodedUri:
    scheme: str
    authority: str
    path: str  # url-decoded, posix-style
    raw: str

    @property
    def is_wsl_remote(self) -> bool:
        return self.scheme == "vscode-remote" and self.authority.lower().startswith("wsl")

    @property
    def wsl_distro(self) -> Optional[str]:
        if not self.is_wsl_remote:
            return None
        # authority looks like "wsl+Ubuntu"
        if "+" in self.authority:
            return self.authority.split("+", 1)[1]
        return None

    @property
    def is_wsl_fileshare(self) -> bool:
        """`file://wsl.localhost/<Distro>/...` (or the older `file://wsl$/...`)
        is what Windows Explorer / a plain `file://` URI uses to address the
        WSL filesystem as a network share. It's NOT the same thing as
        `vscode-remote://wsl+<distro>/...` (a real Remote-WSL connection,
        where extensions/terminals actually run inside WSL) - this is just a
        window that was opened by browsing to a WSL path from the Windows
        side, so the distro name ends up as the first path segment rather
        than in the authority.
        """
        return self.scheme == "file" and self.authority.lower() in ("wsl.localhost", "wsl$")

    @property
    def wsl_fileshare_distro(self) -> Optional[str]:
        if not self.is_wsl_fileshare:
            return None
        parts = self.path.lstrip("/").split("/", 1)
        return parts[0] if parts and parts[0] else None

    @property
    def wsl_fileshare_path(self) -> str:
        """The actual posix path inside the distro (i.e. with the leading
        `/<Distro>` segment stripped off)."""
        parts = self.path.lstrip("/").split("/", 1)
        return "/" + parts[1] if len(parts) > 1 else "/"

    @property
    def is_local_file(self) -> bool:
        return self.scheme == "file" and not self.is_wsl_fileshare

    @property
    def is_other_remote(self) -> bool:
        return self.scheme not in ("file",) and not self.is_wsl_remote

    def human_path(self) -> str:
        if self.is_wsl_fileshare:
            return self.wsl_fileshare_path
        if self.is_local_file:
            # /c%3A/Users/... -> C:\Users\...   or   /home/aviv/... stays posix
            p = self.path
            m = re.match(r"^/([a-zA-Z]):(/.*)?$", p)
            if m:
                drive, rest = m.group(1), (m.group(2) or "")
                return f"{drive.upper()}:{rest.replace('/', chr(92))}"
            return p
        if self.is_wsl_remote:
            return self.path
        return f"{self.scheme}://{self.authority}{self.path}"

    def type_label(self) -> str:
        if self.is_wsl_fileshare:
            distro = self.wsl_fileshare_distro or "?"
            return f"Local (Windows with WSL FS: {distro})"
        if self.is_local_file:
            human_path = self.human_path()
            # A Windows drive in the stored URI is authoritative even when
            # this tool itself is running from WSL/Linux. Other local file
            # URIs use the current native platform name generically.
            machine_type = "Windows" if re.match(r"^[A-Za-z]:\\", human_path) else platform.system()
            return f"Local ({machine_type})"
        # Keep remote labels generic: the authority is Cursor/VS Code's
        # original identifier (for example wsl+Ubuntu, ssh-remote+host, or a
        # future authority type this tool does not know about yet).
        return f"Remote ({self.authority or self.scheme})"


def decode_uri(raw: str) -> DecodedUri:
    parsed = urllib.parse.urlsplit(raw)
    path = urllib.parse.unquote(parsed.path)
    authority = urllib.parse.unquote(parsed.netloc)
    return DecodedUri(scheme=parsed.scheme, authority=authority, path=path, raw=raw)


def build_uri_component(raw: str) -> Dict[str, Any]:
    """Rebuild the VS Code URI object shape used inside composerHeaders.value,
    e.g. {"$mid":1,"fsPath":...,"_sep":1,"external":...,"path":...,"scheme":...,"authority":...}
    Best-effort mirror of what Cursor itself writes.
    """
    d = decode_uri(raw)
    comp: Dict[str, Any] = {"$mid": 1}
    if d.is_local_file:
        m = re.match(r"^/([a-zA-Z]):(/.*)?$", d.path)
        if m:
            drive, rest = m.group(1), (m.group(2) or "")
            fspath = f"{drive.lower()}:{rest.replace('/', chr(92))}"
        else:
            fspath = d.path
        comp["fsPath"] = fspath
        comp["_sep"] = 1
    elif d.is_wsl_remote or d.is_wsl_fileshare:
        comp["fsPath"] = d.human_path().replace("/", "\\")
        comp["_sep"] = 1
    comp["external"] = raw
    comp["path"] = d.path
    comp["scheme"] = d.scheme
    if d.authority:
        comp["authority"] = d.authority
    return comp


# ---------------------------------------------------------------------------
# Workspace model
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    root: Root
    ws_id: str  # folder name inside workspaceStorage/
    kind: str  # "folder" | "multi-root" | "empty" | "unknown"
    uri_raw: Optional[str]
    folder_path: Path

    def decoded(self) -> Optional[DecodedUri]:
        if self.uri_raw:
            return decode_uri(self.uri_raw)
        return None

    def display_path(self) -> str:
        if self.kind == "empty":
            return "(empty window - no folder/workspace opened)"
        d = self.decoded()
        if not d:
            return "(unknown - workspace.json missing or unrecognized; possibly a stale/deleted workspace)"
        return d.human_path()

    def opened_as_label(self) -> str:
        """Describe what Cursor opened, independently of where it lives."""
        labels = {
            "folder": "Folder",
            "multi-root": "Workspace file",
            "empty": "Empty window",
            "unknown": "Unknown",
        }
        return labels.get(self.kind, "Unknown")

    def type_label(self) -> str:
        if self.kind == "empty":
            return "Empty window"
        d = self.decoded()
        return d.type_label() if d else "Unknown"

    @property
    def state_db(self) -> Path:
        return self.folder_path / "state.vscdb"

    def exists_on_disk(self) -> Optional[bool]:
        """Best-effort check of whether the underlying project folder/file
        still exists, from the perspective of the machine running this tool.
        Returns None when we cannot determine it (e.g. remote we can't reach).
        """
        if self.kind == "empty" or not self.uri_raw:
            return None
        d = self.decoded()
        if d is None:
            return None
        try:
            if d.is_wsl_fileshare or d.is_wsl_remote:
                if platform.system() != "Windows":
                    # We are inside *some* WSL/Linux box; if it's the same
                    # distro this is directly on our filesystem.
                    return Path(d.human_path()).exists()
                return None
            if d.is_local_file:
                p = d.human_path()
                if platform.system() != "Windows" and re.match(r"^[A-Za-z]:\\", p):
                    # Translate C:\... -> /mnt/c/... to check from WSL/Linux.
                    drive = p[0].lower()
                    rest = p[2:].replace("\\", "/")
                    return Path(f"/mnt/{drive}{rest}").exists()
                return Path(p).exists()
        except OSError:
            return None
        return None


def read_workspace_json(folder: Path) -> Tuple[str, Optional[str]]:
    wj = folder / "workspace.json"
    if folder.name == "empty-window":
        return "empty", None
    if not wj.is_file():
        return "unknown", None
    try:
        data = json.loads(wj.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return "unknown", None
    if "folder" in data:
        return "folder", data["folder"]
    if "workspace" in data:
        return "multi-root", data["workspace"]
    return "unknown", None


def list_workspaces(root: Root) -> List[Workspace]:
    out = []
    ws_storage = root.workspace_storage
    if not ws_storage.is_dir():
        return out
    for entry in sorted(ws_storage.iterdir()):
        if not entry.is_dir():
            continue
        kind, uri_raw = read_workspace_json(entry)
        out.append(Workspace(root=root, ws_id=entry.name, kind=kind, uri_raw=uri_raw, folder_path=entry))
    return out


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


class DatabaseBusyError(RuntimeError):
    pass


@contextlib.contextmanager
def open_db(path: Path, for_write: bool = False):
    if not path.is_file():
        raise FileNotFoundError(f"No such database: {path}")
    con = sqlite3.connect(str(path), timeout=3)
    try:
        if for_write:
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("COMMIT")
            except sqlite3.OperationalError as e:
                con.close()
                raise DatabaseBusyError(
                    f"Database appears to be locked by another process (likely Cursor is running): {path}\n"
                    f"Close Cursor completely (all windows, check it isn't in the tray) and try again.\n({e})"
                )
        yield con
    finally:
        con.close()


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


MANIFEST_NAME = "manifest.json"
OPERATION_REPORT_NAME = "operation-report.md"


def _record_backup_in_manifest(backup_root: Path, backup_name: str, original_path: Path) -> None:
    manifest_path = backup_root / MANIFEST_NAME
    manifest = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    manifest[backup_name] = str(original_path)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def backup_file(path: Path, backup_root: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    rel_marker = path.as_posix().replace(":", "").replace("/", "_").replace("\\", "_")
    dest = backup_root / rel_marker
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    _record_backup_in_manifest(backup_root, dest.name, path)
    return dest


def backup_dir_tree(path: Path, backup_root: Path) -> Optional[Path]:
    """Like `backup_file` but for an entire directory tree (used by `cleanup`
    to snapshot a whole workspaceStorage/<id>/ folder before deleting it)."""
    if not path.is_dir():
        return None
    rel_marker = path.as_posix().replace(":", "").replace("/", "_").replace("\\", "_")
    dest = backup_root / rel_marker
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, dest, dirs_exist_ok=True)
    _record_backup_in_manifest(backup_root, dest.name, path)
    return dest


def workspace_report_lines(ws: "Workspace") -> List[str]:
    return [
        f"- ID: `{ws.ws_id}`",
        f"- Path: `{ws.display_path()}`",
        f"- Cursor root: `{ws.root.base}` ({ws.root.label})",
    ]


def chat_report_lines(chats: List["ChatInfo"], heading: str = "Chats") -> List[str]:
    include_empty = any(chat.type_label == "EMPTY" for chat in chats)
    include_draft = any(chat.type_label == "DRAFT" for chat in chats)
    lines = [
        f"### {heading} ({len(chats)})",
        "",
        chat_type_summary(chats, include_empty=include_empty, include_draft=include_draft),
    ]
    if not chats:
        return lines + ["", "(none)"]
    for chat in chats:
        flags = []
        if chat.is_archived:
            flags.append("archived")
        if chat.is_subagent:
            flags.append("subagent")
        if chat.is_draft:
            flags.append("draft")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        lines.extend(
            [
                "",
                f"- **{chat.name}**{flag_text}",
                f"  - ID: `{chat.composer_id}`",
                f"  - Type: `{chat.type_label}`",
                f"  - Created: {fmt_ts(chat.created_at)}",
                f"  - Last updated: {fmt_ts(chat.last_updated_at) if chat.last_updated_at else '-'}",
                f"  - Messages: {chat.bubble_count}",
            ]
        )
        if chat.type_label == "DRAFT":
            lines.append(f"  - Draft text: {chat.preview or '(none saved)'}")
    return lines


def write_operation_report(backup_root: Path, lines: List[str]) -> Path:
    """Write a human-readable description of the operation beside its backup."""
    backup_root.mkdir(parents=True, exist_ok=True)
    report_path = backup_root / OPERATION_REPORT_NAME
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    try:
        it = path.rglob("*")
    except (PermissionError, OSError):
        return 0
    while True:
        try:
            p = next(it)
        except StopIteration:
            break
        except (PermissionError, OSError):
            continue
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def fmt_ts(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    try:
        return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


def mtime_ts(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Chat (composer) helpers
# ---------------------------------------------------------------------------


def chat_count_for_workspace(root: Root, ws_id: str) -> Tuple[Optional[int], Optional[str]]:
    """Returns (count, error). count is None (not 0!) when the count could
    not be determined, so callers can tell "genuinely zero chats" apart from
    "we couldn't read the database" (e.g. it's locked because Cursor is
    running and actively writing to it)."""
    if not root.global_storage_db.is_file():
        return 0, None
    try:
        with open_db(root.global_storage_db) as con:
            if not table_exists(con, "composerHeaders"):
                return 0, None
            cur = con.execute("SELECT COUNT(*) FROM composerHeaders WHERE workspaceId=?", (ws_id,))
            return cur.fetchone()[0], None
    except (sqlite3.Error, FileNotFoundError, DatabaseBusyError) as e:
        return None, str(e)


def chat_counts_for_workspace(root: Root, ws_id: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Returns (non_empty_count, total_count, error). A chat counts as
    non-empty if it has at least one message (a bubbleId row in cursorDiskKV).
    error is set (and both counts are None) when the counts could not be
    determined at all (e.g. the database is locked because Cursor is running)."""
    if not root.global_storage_db.is_file():
        return 0, 0, None
    try:
        with open_db(root.global_storage_db) as con:
            if not table_exists(con, "composerHeaders"):
                return 0, 0, None
            cur = con.execute("SELECT composerId FROM composerHeaders WHERE workspaceId=?", (ws_id,))
            composer_ids = [row[0] for row in cur.fetchall()]
            total = len(composer_ids)
            if total == 0 or not table_exists(con, "cursorDiskKV"):
                return 0, total, None
            non_empty = 0
            for cid in composer_ids:
                cur2 = con.execute(
                    "SELECT 1 FROM cursorDiskKV WHERE key LIKE ? LIMIT 1", (f"bubbleId:{cid}:%",)
                )
                if cur2.fetchone():
                    non_empty += 1
            return non_empty, total, None
    except (sqlite3.Error, FileNotFoundError, DatabaseBusyError) as e:
        return None, None, str(e)


@dataclass
class ChatBreakdown:
    ui: int = 0
    subagent: int = 0
    archived: int = 0
    empty: int = 0
    draft: int = 0

    @property
    def total(self) -> int:
        return self.ui + self.subagent + self.archived + self.empty + self.draft

    def display(self) -> str:
        return (
            f"UI:{self.ui} SUB-AGENT:{self.subagent} ARCHIVED:{self.archived} "
            f"EMPTY:{self.empty} DRAFT:{self.draft} TOTAL:{self.total}"
        )


def chat_breakdown_for_workspace(
    root: Root,
    ws_id: str,
) -> Tuple[Optional[ChatBreakdown], Optional[str]]:
    """Return disjoint chat categories for the `list` table.

    UI      = non-empty, non-subagent, non-archived chats shown in the main UI
    SUB     = non-empty subagent chats
    ARCH    = non-empty archived regular chats
    EMPTY   = zero-message, non-draft records
    DRAFT   = zero-message records marked as drafts that contain saved text

    Completely empty draft records are ignored and are not included in TOTAL.
    """
    breakdown = ChatBreakdown()
    if not root.global_storage_db.is_file():
        return breakdown, None
    try:
        with open_db(root.global_storage_db) as con:
            if not table_exists(con, "composerHeaders"):
                return breakdown, None
            rows = con.execute(
                "SELECT composerId, isArchived, isSubagent, value "
                "FROM composerHeaders WHERE workspaceId=?",
                (ws_id,),
            ).fetchall()
            has_diskkv = table_exists(con, "cursorDiskKV")
            for composer_id, is_archived, is_subagent, header_value in rows:
                has_messages = False
                if has_diskkv:
                    has_messages = con.execute(
                        "SELECT 1 FROM cursorDiskKV WHERE key LIKE ? LIMIT 1",
                        (f"bubbleId:{composer_id}:%",),
                    ).fetchone() is not None

                is_draft = False
                header_data: Dict[str, Any] = {}
                try:
                    header_data = json.loads(header_value)
                    is_draft = bool(header_data.get("isDraft"))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

                if not has_messages:
                    if is_draft:
                        draft_text = (header_data.get("subtitle") or "").strip()
                        if has_diskkv:
                            row = con.execute(
                                "SELECT value FROM cursorDiskKV WHERE key=?",
                                (f"composerData:{composer_id}",),
                            ).fetchone()
                            if row and row[0]:
                                try:
                                    data = json.loads(row[0])
                                    draft_text = (
                                        (data.get("text") or "").strip()
                                        or _plain_text_from_rich_text(data.get("richText"))
                                        or draft_text
                                    )
                                except (json.JSONDecodeError, TypeError, AttributeError):
                                    pass
                        if draft_text:
                            breakdown.draft += 1
                    else:
                        breakdown.empty += 1
                elif is_subagent:
                    breakdown.subagent += 1
                elif is_archived:
                    breakdown.archived += 1
                else:
                    breakdown.ui += 1
            return breakdown, None
    except (sqlite3.Error, FileNotFoundError, DatabaseBusyError) as e:
        return None, str(e)


@dataclass
class ChatInfo:
    composer_id: str
    name: str
    created_at: Optional[int]
    last_updated_at: Optional[int]
    is_archived: bool
    is_subagent: bool
    is_draft: bool
    bubble_count: int
    preview: str

    @property
    def type_label(self) -> str:
        if self.bubble_count == 0:
            return "DRAFT" if self.is_draft else "EMPTY"
        if self.is_subagent:
            return "SUB-AGENT"
        if self.is_archived:
            return "ARCHIVED"
        return "UI"


def _plain_text_from_rich_text(rich_text: Any) -> str:
    """Extract readable text from a Lexical-editor `richText` payload.

    Draft chats store their unsent input as a Lexical JSON tree in
    `composerData.richText`. We walk the tree and concatenate every node's
    `text` (and `mentionName` for @-mentions / slash commands) so the draft
    preview shows something meaningful even when the flat `text` field is empty.
    """
    if not rich_text:
        return ""
    try:
        data = json.loads(rich_text) if isinstance(rich_text, str) else rich_text
    except (json.JSONDecodeError, TypeError):
        return ""

    parts: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
            elif node.get("type") == "mention" and node.get("mentionName"):
                parts.append(f"@{node['mentionName']}")
            for child in node.get("children", []) or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    root_node = data.get("root") if isinstance(data, dict) else data
    walk(root_node)
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def get_chats_for_workspace(root: Root, ws_id: str) -> List[ChatInfo]:
    out: List[ChatInfo] = []
    if not root.global_storage_db.is_file():
        return out
    with open_db(root.global_storage_db) as con:
        if not table_exists(con, "composerHeaders"):
            return out
        cur = con.execute(
            "SELECT composerId, createdAt, lastUpdatedAt, isArchived, isSubagent, value "
            "FROM composerHeaders WHERE workspaceId=? ORDER BY COALESCE(lastUpdatedAt, createdAt) DESC",
            (ws_id,),
        )
        rows = cur.fetchall()
        has_diskkv = table_exists(con, "cursorDiskKV")
        for composer_id, created_at, last_updated_at, is_archived, is_subagent, header_value in rows:
            bubble_count = 0
            preview = ""
            name = ""
            is_draft = False
            subtitle = ""
            try:
                header_data = json.loads(header_value)
                name = (header_data.get("name") or "").strip()
                is_draft = bool(header_data.get("isDraft"))
                subtitle = (header_data.get("subtitle") or "").strip().replace("\n", " ")
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
            if has_diskkv:
                cur2 = con.execute(
                    "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?",
                    (f"bubbleId:{composer_id}:%",),
                )
                bubble_count = cur2.fetchone()[0]
                cur3 = con.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"composerData:{composer_id}",))
                row = cur3.fetchone()
                if row:
                    try:
                        data = json.loads(row[0])
                        if not name:
                            name = (data.get("name") or "").strip()
                        preview = (data.get("text") or "").strip().replace("\n", " ")
                        if not preview:
                            preview = _plain_text_from_rich_text(data.get("richText"))
                        if not preview:
                            headers = data.get("fullConversationHeadersOnly") or []
                            if headers:
                                preview = f"({len(headers)} conversation header entries, no plain text preview)"
                    except (json.JSONDecodeError, TypeError):
                        pass
            # For drafts the unsent text is sometimes only kept in the header's
            # `subtitle` (e.g. when no composerData row exists yet), so fall back
            # to it before giving up.
            if not preview and subtitle:
                preview = subtitle
            out.append(
                ChatInfo(
                    composer_id=composer_id,
                    name=name or "(untitled chat)",
                    created_at=created_at,
                    last_updated_at=last_updated_at,
                    is_archived=bool(is_archived),
                    is_subagent=bool(is_subagent),
                    is_draft=is_draft,
                    bubble_count=bubble_count,
                    preview=preview[:300] if is_draft else preview[:120],
                )
            )
    return out


# ---------------------------------------------------------------------------
# Workspace resolution (by ref: hash id / index from last list / path substring)
# ---------------------------------------------------------------------------


def save_last_list(entries: List[Workspace]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {"index": i + 1, "root_id": w.root.root_id, "root_base": str(w.root.base), "ws_id": w.ws_id}
        for i, w in enumerate(entries)
    ]
    LAST_LIST_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_last_list() -> List[Dict[str, Any]]:
    if not LAST_LIST_FILE.is_file():
        return []
    try:
        return json.loads(LAST_LIST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def resolve_workspace(ref: str, roots: List[Root], all_workspaces: List[Workspace]) -> Workspace:
    # 1) numeric index from the most recent `list`
    if ref.isdigit():
        cached = load_last_list()
        for entry in cached:
            if entry["index"] == int(ref):
                for w in all_workspaces:
                    if w.ws_id == entry["ws_id"] and str(w.root.base) == entry["root_base"]:
                        return w
                raise SystemExit(
                    f"Index {ref} was found in the cached list but that workspace no longer exists. "
                    f"Run `list` again."
                )
        # fall through: maybe it's literally a numeric workspace id (legacy ids look like this)

    # 2) exact workspace-id match
    exact = [w for w in all_workspaces if w.ws_id == ref]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SystemExit(
            f"Workspace id '{ref}' exists in multiple Cursor installations: "
            + ", ".join(f"{w.root.label}" for w in exact)
            + ". Disambiguate with --root to limit the search."
        )

    # 3) substring match against the decoded human path
    needle = ref.lower()
    matches = [w for w in all_workspaces if needle in w.display_path().lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        lines = "\n".join(f"  - {w.ws_id} :: {w.display_path()} [{w.root.label}]" for w in matches)
        raise SystemExit(f"'{ref}' matches multiple workspaces, be more specific:\n{lines}")

    raise SystemExit(
        f"Could not find a workspace matching '{ref}'. Run `list` first and pass the index number, "
        f"the workspace id, or a unique substring of its path."
    )


# ---------------------------------------------------------------------------
# `list` command
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    roots = discover_roots(args.root)
    if not roots:
        print("No Cursor data directories were found. Use --root to point at one explicitly.")
        return

    all_workspaces: List[Workspace] = []
    for root in roots:
        all_workspaces.extend(list_workspaces(root))

    if not all_workspaces:
        print("No workspaces found under the discovered Cursor roots:")
        for r in roots:
            print(f"  - [{r.root_id}] {r.label}: {r.base}")
        return

    print(f"Discovered Cursor data roots:")
    for r in roots:
        print(f"  [{r.root_id}] {r.label}\n        {r.base}")
    print()

    rows = []
    chat_errors: List[str] = []
    for w in all_workspaces:
        if args.details or args.chat_counts or args.with_chats:
            breakdown, err = chat_breakdown_for_workspace(w.root, w.ws_id)
            if err:
                chat_errors.append(err)
        else:
            breakdown, err = None, None
        rows.append((w, breakdown, err))

    if args.with_chats:
        rows = [
            (w, breakdown, err)
            for w, breakdown, err in rows
            if err is None and breakdown is not None and breakdown.total > 0
        ]

    if not rows:
        print("No workspaces with chats were found.")
        save_last_list([])
        if chat_errors:
            print(
                f"WARNING: chat counts could not be read for {len(chat_errors)} workspace(s), "
                "so they were excluded from the filter results."
            )
            print(f"  First error: {chat_errors[0]}")
        return

    table_rows: List[List[str]] = []
    for i, (w, breakdown, err) in enumerate(rows, start=1):
        mt = mtime_ts(w.state_db) or mtime_ts(w.folder_path)
        last_used = dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M") if mt else "-"
        size = human_size(dir_size(w.folder_path)) if args.details else "-"
        if err:
            chat_str = "ERR"
        elif breakdown is not None:
            chat_str = breakdown.display()
        else:
            chat_str = "-"
        exists = w.exists_on_disk()
        exists_marker = "" if exists is None else (" (MISSING)" if exists is False else "")
        table_rows.append(
            [
                str(i),
                w.ws_id,
                w.type_label(),
                w.opened_as_label(),
                chat_str,
                size,
                last_used,
                f"{w.display_path()}{exists_marker}",
            ]
        )

    headers = ["#", "ID", "TYPE", "OPENED AS", "CHATS", "SIZE", "LAST USED", "PATH"]
    minimum_widths = [3, 34, 14, 14, 7, 7, 17]
    column_widths = [
        max(minimum_widths[index], len(headers[index]), *(len(row[index]) for row in table_rows))
        for index in range(len(minimum_widths))
    ]
    column_gap = "  "

    def format_list_row(values: List[str]) -> str:
        fixed_columns = [
            values[index].ljust(column_widths[index])
            for index in range(len(column_widths))
        ]
        return column_gap.join(fixed_columns + [values[-1]])

    header = format_list_row(headers)
    formatted_rows = [format_list_row(row) for row in table_rows]
    separator = "-" * max(len(header), *(len(row) for row in formatted_rows))

    print(header)
    print(separator)
    for row in formatted_rows:
        print(row)
        print()

    print(separator)
    save_last_list([w for w, _, _ in rows])
    print()
    if chat_errors:
        if args.with_chats:
            print(
                f"WARNING: chat counts could not be read for {len(chat_errors)} workspace(s), "
                "so they were excluded from the filter results."
            )
        else:
            print(
                f"WARNING: could not read chat counts for {len(chat_errors)} workspace(s) (shown as 'ERR'). "
                f"This almost always means Cursor is currently running and has its database locked/busy - "
                f"close Cursor completely and re-run for accurate counts."
            )
        print(f"  First error: {chat_errors[0]}")
        print()
    print("Tip: pass --details for on-disk size + chat counts (slower), or --chat-counts for chat counts only.")
    print("Use the '#' index, the ID, or a unique path substring with `chats` / `merge`.")


# ---------------------------------------------------------------------------
# `chats` command
# ---------------------------------------------------------------------------


def chat_type_summary(
    chats: List["ChatInfo"],
    *,
    include_empty: bool = False,
    include_draft: bool = False,
) -> str:
    counts = {
        "UI": 0,
        "SUB-AGENT": 0,
        "ARCHIVED": 0,
        "EMPTY": 0,
        "DRAFT": 0,
    }
    for chat in chats:
        counts[chat.type_label] += 1
    parts = [
        f"UI:{counts['UI']}",
        f"SUB-AGENT:{counts['SUB-AGENT']}",
        f"ARCHIVED:{counts['ARCHIVED']}",
    ]
    if include_empty:
        parts.append(f"EMPTY:{counts['EMPTY']}")
    if include_draft:
        parts.append(f"DRAFT:{counts['DRAFT']}")
    parts.append(f"TOTAL:{len(chats)}")
    return "Types: " + " ".join(parts)


def print_chats_section(
    chats: List["ChatInfo"],
    heading: str = "AI Chats / Composers",
    *,
    include_empty: bool = False,
    include_draft: bool = False,
) -> None:
    print(f"{heading} ({len(chats)}):")
    print(
        f"  {chat_type_summary(chats, include_empty=include_empty, include_draft=include_draft)}"
    )
    for c in chats:
        created = fmt_ts(c.created_at)
        updated = fmt_ts(c.last_updated_at) if c.last_updated_at else "-"
        print(f"  - {c.name}")
        print(f"      ID: {c.composer_id}")
        print(f"      type: {c.type_label}")
        print(f"      created: {created}")
        print(f"      last updated: {updated}")
        print(f"      messages: {c.bubble_count}")
        if c.type_label == "DRAFT":
            print(f"      draft text: {c.preview or '(none saved)'}")
    if not chats:
        print("  (none)")


def cmd_chats(args: argparse.Namespace) -> None:
    roots = discover_roots(args.root)
    all_workspaces = [w for r in roots for w in list_workspaces(r)]
    workspaces = (
        [resolve_workspace(args.workspace, roots, all_workspaces)]
        if args.workspace
        else all_workspaces
    )

    sections: List[Tuple[Workspace, List[ChatInfo]]] = []
    for ws in workspaces:
        chats = [
            c
            for c in get_chats_for_workspace(ws.root, ws.ws_id)
            if c.bubble_count > 0 or (c.is_draft and c.preview.strip())
        ]
        if chats:
            sections.append((ws, chats))

    if not sections:
        scope = f" matching '{args.workspace}'" if args.workspace else ""
        print(f"No workspaces{scope} have non-empty AI Chats or drafts.")
        return

    total_chats = sum(len(chats) for _, chats in sections)
    print(f"Workspaces shown: {len(sections)}")
    print(f"Non-empty AI Chats and drafts shown: {total_chats} total")
    print()
    for index, (ws, chats) in enumerate(sections):
        if index:
            print("-" * 100)
            print()
        print(f"Workspace ID : {ws.ws_id}")
        print(f"Path         : {ws.display_path()}")
        print()
        print_chats_section(
            chats,
            "Non-empty AI Chats / Composers and drafts",
            include_draft=True,
        )


# ---------------------------------------------------------------------------
# `merge` command
# ---------------------------------------------------------------------------


@dataclass
class MergePlan:
    chats_to_move: List[str] = field(default_factory=list)
    chats_to_copy: List[str] = field(default_factory=list)
    empty_chats_skipped: List[str] = field(default_factory=list)
    destination_conflicts_skipped: List[str] = field(default_factory=list)
    destination_conflicts_overwritten: List[str] = field(default_factory=list)


def build_merge_plan(
    src: Workspace,
    dest: Workspace,
    chats_mode: str,  # "move" | "copy"
    selected_chat_ids: Optional[List[str]] = None,
    force: bool = False,
) -> MergePlan:
    """Merge only touches Cursor's own AI Chats/Composers (composerHeaders +
    cursorDiskKV in globalStorage/state.vscdb). Nothing else about the
    workspace (state.vscdb ItemTable keys, open tabs, recently-opened files,
    or the GitHub-Copilot-Chat-owned chatSessions/chatEditingSessions
    folders) is touched.
    """
    plan = MergePlan()
    chats = get_chats_for_workspace(src.root, src.ws_id)
    if selected_chat_ids:
        # Preserve the command-line order while ignoring duplicate selectors.
        requested_ids = list(dict.fromkeys(selected_chat_ids))
        chats_by_id = {chat.composer_id: chat for chat in chats}
        missing_ids = [chat_id for chat_id in requested_ids if chat_id not in chats_by_id]
        if missing_ids:
            raise SystemExit(
                "The following --chat-id value(s) do not belong to the source workspace: "
                + ", ".join(missing_ids)
            )
        empty_ids = [chat_id for chat_id in requested_ids if chats_by_id[chat_id].bubble_count == 0]
        if empty_ids:
            raise SystemExit(
                "The following selected chat(s) are empty or drafts and cannot be merged: "
                + ", ".join(empty_ids)
            )
        chats = [chats_by_id[chat_id] for chat_id in requested_ids]

    destination_ids = set()
    if chats_mode == "move" and src.root.base != dest.root.base and dest.root.global_storage_db.is_file():
        with open_db(dest.root.global_storage_db) as con:
            if table_exists(con, "composerHeaders"):
                destination_ids = {
                    row[0]
                    for row in con.execute(
                        "SELECT composerId FROM composerHeaders",
                    ).fetchall()
                }

    for c in chats:
        if c.bubble_count == 0:
            plan.empty_chats_skipped.append(c.composer_id)
            continue
        if chats_mode == "move":
            if c.composer_id in destination_ids:
                if force:
                    plan.destination_conflicts_overwritten.append(c.composer_id)
                else:
                    plan.destination_conflicts_skipped.append(c.composer_id)
                    continue
            plan.chats_to_move.append(c.composer_id)
        else:  # copy
            plan.chats_to_copy.append(c.composer_id)
    return plan


def print_merge_plan(plan: MergePlan, src: Workspace, dest: Workspace, chats_mode: str) -> None:
    print(f"Merge plan: {src.ws_id} ({src.display_path()})")
    print(f"        -> {dest.ws_id} ({dest.display_path()})")
    if src.root.base != dest.root.base:
        print(f"NOTE: source and destination are in DIFFERENT Cursor installations "
              f"({src.root.label} -> {dest.root.label}). A full cross-database copy will be performed.")
    print()
    verb = "MOVED (removed from source)" if chats_mode == "move" else "COPIED (source keeps its own copy)"
    ids = plan.chats_to_move if chats_mode == "move" else plan.chats_to_copy
    print(f"Non-empty AI Chats to be {verb}: {len(ids)}")
    for cid in ids:
        print(f"    * {cid}")
    if plan.destination_conflicts_overwritten:
        print(
            "Destination chats to be overwritten because --force was used: "
            f"{len(plan.destination_conflicts_overwritten)}"
        )
        for cid in plan.destination_conflicts_overwritten:
            print(f"    * {cid}")
    if plan.destination_conflicts_skipped:
        print(
            "Destination ID conflicts skipped (use --force to overwrite): "
            f"{len(plan.destination_conflicts_skipped)}"
        )
        for cid in plan.destination_conflicts_skipped:
            print(f"    * {cid}")
    if plan.empty_chats_skipped:
        print(f"Empty AI Chats skipped (left unchanged): {len(plan.empty_chats_skipped)}")
    print()


def get_reference_workspace_identifier(root: Root, ws: Workspace) -> Dict[str, Any]:
    """Best-fidelity workspaceIdentifier object for `ws`: reuse one already
    written by Cursor itself if any chat exists for it, else synthesize it
    from workspace.json."""
    if root.global_storage_db.is_file():
        with open_db(root.global_storage_db) as con:
            if table_exists(con, "composerHeaders"):
                cur = con.execute(
                    "SELECT value FROM composerHeaders WHERE workspaceId=? LIMIT 1", (ws.ws_id,)
                )
                row = cur.fetchone()
                if row:
                    try:
                        val = json.loads(row[0])
                        if "workspaceIdentifier" in val:
                            return val["workspaceIdentifier"]
                    except (json.JSONDecodeError, TypeError):
                        pass
    # Synthesize from workspace.json
    ident: Dict[str, Any] = {"id": ws.ws_id}
    if ws.kind == "folder" and ws.uri_raw:
        ident["uri"] = build_uri_component(ws.uri_raw)
    elif ws.kind == "multi-root" and ws.uri_raw:
        ident["configPath"] = build_uri_component(ws.uri_raw)
    return ident


COMPOSER_RELATED_KEY_PREFIXES = [
    "checkpointId:{cid}:",
    "composerVirtualRowHeights:{cid}",
    "codeBlockPartialInlineDiffFates:{cid}:",
    "ofsContent:{cid}:",
    "bubbleId:{cid}:",
]


def gather_composer_related_keys(con: sqlite3.Connection, composer_id: str) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    cur = con.execute("SELECT key, value FROM cursorDiskKV WHERE key = ?", (f"composerData:{composer_id}",))
    row = cur.fetchone()
    if row:
        out[row[0]] = row[1]
    for template in COMPOSER_RELATED_KEY_PREFIXES:
        prefix = template.format(cid=composer_id)
        cur = con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?", (prefix + "%",))
        for k, v in cur.fetchall():
            out[k] = v
    # Best-effort: content-addressed blobs referenced from the composer JSON.
    composer_val = out.get(f"composerData:{composer_id}")
    if composer_val:
        try:
            text = composer_val.decode("utf-8", errors="ignore") if isinstance(composer_val, bytes) else composer_val
        except AttributeError:
            text = composer_val
        for h in set(re.findall(r"composer\.content\.[0-9a-fA-F]{16,}", text)):
            cur = con.execute("SELECT key, value FROM cursorDiskKV WHERE key=?", (h,))
            r2 = cur.fetchone()
            if r2:
                out[r2[0]] = r2[1]
    return out


def apply_chat_transfer(
    src: Workspace,
    dest: Workspace,
    composer_ids: List[str],
    mode: str,  # "move" | "copy"
    force: bool = False,
) -> List[Tuple[str, str]]:
    """Reassign (mode=move) or duplicate (mode=copy) the given composer/chat
    ids so that they belong to `dest`. Returns (source_id, destination_id)
    pairs for chats actually transferred.
    """
    same_root = src.root.base == dest.root.base
    dest_identifier = get_reference_workspace_identifier(dest.root, dest)
    resulting_ids: List[Tuple[str, str]] = []

    if same_root:
        with open_db(dest.root.global_storage_db, for_write=True) as con:
            for cid in composer_ids:
                cur = con.execute("SELECT value FROM composerHeaders WHERE composerId=?", (cid,))
                row = cur.fetchone()
                if not row:
                    continue
                header_val = json.loads(row[0])

                if mode == "move":
                    header_val["workspaceIdentifier"] = dest_identifier
                    con.execute(
                        "UPDATE composerHeaders SET workspaceId=?, value=? WHERE composerId=?",
                        (dest.ws_id, json.dumps(header_val), cid),
                    )
                    resulting_ids.append((cid, cid))
                else:  # copy
                    new_cid = str(uuid.uuid4())
                    header_val["workspaceIdentifier"] = dest_identifier
                    header_val["composerId"] = new_cid
                    con.execute(
                        "INSERT INTO composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt, "
                        "isArchived, isSubagent, recency, checkpointAt, value) "
                        "SELECT ?, ?, createdAt, lastUpdatedAt, isArchived, isSubagent, recency, checkpointAt, ? "
                        "FROM composerHeaders WHERE composerId=?",
                        (new_cid, dest.ws_id, json.dumps(header_val), cid),
                    )
                    related = gather_composer_related_keys(con, cid)
                    for key, value in related.items():
                        new_key = key.replace(cid, new_cid, 1) if key.split(":")[0] != "composer.content" else key
                        con.execute(
                            "INSERT OR IGNORE INTO cursorDiskKV (key, value) VALUES (?, ?)", (new_key, value)
                        )
                    resulting_ids.append((cid, new_cid))
            con.commit()
    else:
        # Cross-installation move/copy: read from source root's global db,
        # write into destination root's global db.
        with open_db(src.root.global_storage_db) as src_con, open_db(
            dest.root.global_storage_db, for_write=True
        ) as dest_con:
            for cid in composer_ids:
                cur = src_con.execute("SELECT value, createdAt, lastUpdatedAt, isArchived, isSubagent, recency, "
                                       "checkpointAt FROM composerHeaders WHERE composerId=?", (cid,))
                row = cur.fetchone()
                if not row:
                    continue
                header_val, created_at, last_updated_at, is_archived, is_subagent, recency, checkpoint_at = row
                header_val = json.loads(header_val)
                new_cid = cid if mode == "move" else str(uuid.uuid4())
                header_val["workspaceIdentifier"] = dest_identifier
                header_val["composerId"] = new_cid

                existing = dest_con.execute(
                    "SELECT 1 FROM composerHeaders WHERE composerId=?", (new_cid,)
                ).fetchone()
                if existing:
                    if not force:
                        # Never overwrite unless the caller explicitly opted in.
                        continue
                    existing_related = gather_composer_related_keys(dest_con, new_cid)
                    dest_con.execute("DELETE FROM composerHeaders WHERE composerId=?", (new_cid,))
                    for key in existing_related:
                        if key.split(":")[0] == "composer.content":
                            continue  # content-addressed blobs may be shared
                        dest_con.execute("DELETE FROM cursorDiskKV WHERE key=?", (key,))

                dest_con.execute(
                    "INSERT INTO composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt, "
                    "isArchived, isSubagent, recency, checkpointAt, value) VALUES (?,?,?,?,?,?,?,?,?)",
                    (new_cid, dest.ws_id, created_at, last_updated_at, is_archived, is_subagent, recency,
                     checkpoint_at, json.dumps(header_val)),
                )
                related = gather_composer_related_keys(src_con, cid)
                for key, value in related.items():
                    if mode == "copy" and key.split(":")[0] != "composer.content":
                        new_key = key.replace(cid, new_cid, 1)
                    else:
                        new_key = key
                    dest_con.execute(
                        "INSERT OR IGNORE INTO cursorDiskKV (key, value) VALUES (?, ?)", (new_key, value)
                    )
                resulting_ids.append((cid, new_cid))
            dest_con.commit()

        if mode == "move":
            with open_db(src.root.global_storage_db, for_write=True) as src_con:
                # Delete only chats that were successfully inserted at the
                # destination. A destination ID collision is skipped above and
                # must never cause the source copy to be removed.
                for cid, _ in resulting_ids:
                    src_con.execute("DELETE FROM composerHeaders WHERE composerId=?", (cid,))
                    related = gather_composer_related_keys(src_con, cid)
                    for key in related:
                        if key.split(":")[0] == "composer.content":
                            continue  # content-addressed blob, may be shared; keep it
                        src_con.execute("DELETE FROM cursorDiskKV WHERE key=?", (key,))
                src_con.commit()

    return resulting_ids


def cmd_merge(args: argparse.Namespace) -> None:
    roots = discover_roots(args.root)
    all_workspaces = [w for r in roots for w in list_workspaces(r)]
    src = resolve_workspace(args.source, roots, all_workspaces)
    dest = resolve_workspace(args.dest, roots, all_workspaces)
    selected_chat_ids: List[str] = []
    for value in args.chat_id or []:
        ids = [chat_id.strip() for chat_id in value.split(",")]
        if any(not chat_id for chat_id in ids):
            raise SystemExit(
                "Invalid --chat-id list: IDs must be separated by commas without empty values."
            )
        selected_chat_ids.extend(ids)

    if src.ws_id == dest.ws_id and src.root.base == dest.root.base:
        raise SystemExit("Source and destination are the same workspace.")
    if args.force and args.mode == "copy":
        raise SystemExit("--force only applies to `--mode move`; copy mode always creates new chat IDs.")

    plan = build_merge_plan(src, dest, args.mode, selected_chat_ids, args.force)
    print_merge_plan(plan, src, dest, args.mode)
    source_chats = {chat.composer_id: chat for chat in get_chats_for_workspace(src.root, src.ws_id)}
    ids = plan.chats_to_move if args.mode == "move" else plan.chats_to_copy
    planned_chats = [source_chats[cid] for cid in ids if cid in source_chats]

    if not args.apply:
        print("(dry run - no changes were made. Re-run with --apply to perform this merge.)")
        return

    print("=" * 70)
    print("APPLY MODE - this will modify files on disk.")
    print("Make ABSOLUTELY SURE Cursor is fully closed (all windows, check it")
    print("isn't idling in the system tray) before continuing.")
    print("=" * 70)
    if not args.yes:
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Aborted, nothing was changed.")
            return

    backup_root = Path(args.backup_dir) if args.backup_dir else STATE_DIR / "backups" / dt.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )
    backed_up = []
    dbs_to_backup = {dest.root.global_storage_db}
    if src.root.base != dest.root.base:
        dbs_to_backup.add(src.root.global_storage_db)
    for db in dbs_to_backup:
        b = backup_file(db, backup_root)
        if b:
            backed_up.append(b)
    report_prefix = [
        "# Cursor Workspace Tool Backup Report",
        "",
        f"- Created: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- Operation: `merge`",
        f"- Mode: `--mode {args.mode}`",
        f"- Force destination overwrite: {'yes' if args.force else 'no'}",
        f"- Selection: {'specific chat IDs' if selected_chat_ids else 'all non-empty chats'}",
        f"- Non-empty chats selected: {len(ids)}",
        f"- Empty chats skipped and left unchanged: {len(plan.empty_chats_skipped)}",
        f"- Destination conflicts skipped: {len(plan.destination_conflicts_skipped)}",
        f"- Destination conflicts selected for overwrite: {len(plan.destination_conflicts_overwritten)}",
        "",
        "## Source workspace",
        *workspace_report_lines(src),
        "",
        "## Destination workspace",
        *workspace_report_lines(dest),
        "",
    ]
    report_path = write_operation_report(
        backup_root,
        report_prefix
        + [
            "## Result",
            "",
            "Backup created; merge has not completed yet.",
            "",
            *chat_report_lines(planned_chats, "Chats selected for transfer"),
        ],
    )
    print(f"Backed up {len(backed_up)} database file(s) to: {backup_root}")

    try:
        # Lock check up front, before touching anything.
        with open_db(dest.root.global_storage_db, for_write=True):
            pass
        if src.root.base != dest.root.base and src.root.global_storage_db.is_file():
            with open_db(src.root.global_storage_db, for_write=True):
                pass
    except DatabaseBusyError as e:
        write_operation_report(
            backup_root,
            report_prefix
            + [
                "## Result",
                "",
                f"**Aborted:** database was busy before any merge change was made: `{e}`",
                "",
                *chat_report_lines(planned_chats, "Chats selected for transfer"),
            ],
        )
        print(f"ABORTED before making any change: {e}")
        return

    transferred: List[Tuple[str, str]] = []
    if ids:
        transferred = apply_chat_transfer(src, dest, ids, args.mode, args.force)
        print(f"{'Moved' if args.mode == 'move' else 'Copied'} {len(transferred)}/{len(ids)} chat(s) "
              f"to workspace {dest.ws_id}.")
    else:
        print("No chats to transfer.")

    transferred_source_ids = {source_id for source_id, _ in transferred}
    transferred_chats = [source_chats[cid] for cid in ids if cid in transferred_source_ids and cid in source_chats]
    destination_ids = {source_id: destination_id for source_id, destination_id in transferred}
    report_lines = report_prefix + [
        "## Result",
        "",
        f"- Status: completed",
        f"- Chats {'moved' if args.mode == 'move' else 'copied'}: {len(transferred)}/{len(ids)}",
    ]
    copied_with_new_ids = [
        (chat, destination_ids[chat.composer_id])
        for chat in transferred_chats
        if destination_ids.get(chat.composer_id) != chat.composer_id
    ]
    if copied_with_new_ids:
        report_lines.extend(["", "### Destination IDs created by copy"])
        for chat, destination_id in copied_with_new_ids:
            report_lines.append(f"- {chat.name}: `{chat.composer_id}` -> `{destination_id}`")
    report_lines.extend(["", *chat_report_lines(transferred_chats, "Chats transferred")])
    failed_chats = [source_chats[cid] for cid in ids if cid not in transferred_source_ids and cid in source_chats]
    if failed_chats:
        report_lines.extend(["", *chat_report_lines(failed_chats, "Chats not transferred")])
    write_operation_report(backup_root, report_lines)

    print()
    print("Done. Verifying destination state...")
    new_count, verify_err = chat_count_for_workspace(dest.root, dest.ws_id)
    if verify_err:
        print(f"WARNING: could not verify the new chat count ({verify_err}).")
    else:
        print(f"Destination workspace now has {new_count} chat(s) associated with it.")
    print(f"A backup of the original database files is at: {backup_root}")
    print(f"Operation report: {report_path}")
    print("If anything looks wrong, use the `restore` command with that backup directory.")


# ---------------------------------------------------------------------------
# `cleanup` command
# ---------------------------------------------------------------------------


def workspace_staleness_reason(ws: Workspace, include_empty_windows: bool) -> Optional[str]:
    """Returns a short human-readable reason if `ws` looks like disposable
    clutter, or None if it looks like a workspace someone might still care
    about. Three kinds of clutter accumulate over time in every VS-Code-based
    editor (Cursor never cleans workspaceStorage/ up on its own):
      - "unknown": workspace.json is missing or unreadable - we don't even
        know what project this used to be.
      - "missing": workspace.json is fine, but the folder/.code-workspace
        file it points to no longer exists on disk (from this machine).
      - "empty" (opt-in via include_empty_windows): the shared 'empty-window' entry
        Cursor reuses for every blank window with no folder opened.
    """
    if ws.kind == "unknown":
        return "unknown (workspace.json missing/unreadable)"
    if ws.kind in ("folder", "multi-root") and ws.exists_on_disk() is False:
        return "missing (target folder/file no longer exists on disk)"
    if include_empty_windows and ws.kind == "empty":
        return "empty window (no folder was ever opened in it)"
    return None


def purge_chat_ids_from_global_db(con: sqlite3.Connection, composer_ids: List[str]) -> int:
    """Delete selected composer records and their non-shared cursorDiskKV data."""
    if not table_exists(con, "composerHeaders"):
        return 0
    has_diskkv = table_exists(con, "cursorDiskKV")
    removed = 0
    for cid in composer_ids:
        exists = con.execute(
            "SELECT 1 FROM composerHeaders WHERE composerId=?",
            (cid,),
        ).fetchone()
        if not exists:
            continue
        if has_diskkv:
            for key in gather_composer_related_keys(con, cid):
                if key.split(":")[0] == "composer.content":
                    continue  # content-addressed blob, may be shared; keep it
                con.execute("DELETE FROM cursorDiskKV WHERE key=?", (key,))
        con.execute("DELETE FROM composerHeaders WHERE composerId=?", (cid,))
        removed += 1
    return removed


def purge_workspace_chats_from_global_db(con: sqlite3.Connection, ws_id: str) -> int:
    """Delete every composerHeaders row for `ws_id` and its related cursorDiskKV
    entries from an already-open global-storage connection. Content-addressed
    blobs (composer.content.*) are intentionally kept, because they may be
    shared with other composers. Returns the number of composer records removed.
    """
    if not table_exists(con, "composerHeaders"):
        return 0
    cur = con.execute("SELECT composerId FROM composerHeaders WHERE workspaceId=?", (ws_id,))
    composer_ids = [row[0] for row in cur.fetchall()]
    return purge_chat_ids_from_global_db(con, composer_ids)


def cmd_cleanup_workspaces(args: argparse.Namespace) -> None:
    roots = discover_roots(args.root)
    all_workspaces = [w for r in roots for w in list_workspaces(r)]
    scoped_workspaces = (
        [resolve_workspace(args.workspace, roots, all_workspaces)]
        if args.workspace
        else all_workspaces
    )

    candidates: List[Tuple[Workspace, str, int]] = []
    skipped_has_non_empty_chats: List[Tuple[Workspace, str, int, int]] = []
    skipped_unverifiable: List[Tuple[Workspace, str]] = []

    for w in scoped_workspaces:
        reason = workspace_staleness_reason(w, args.include_empty_windows)
        if reason is None:
            if not args.workspace:
                continue
            reason = "explicitly selected workspace"
        non_empty, total, err = chat_counts_for_workspace(w.root, w.ws_id)
        if err:
            skipped_unverifiable.append((w, reason))
            continue
        if non_empty:
            skipped_has_non_empty_chats.append((w, reason, non_empty, total or 0))
            continue
        candidates.append((w, reason, total or 0))

    print(f"Scanned {len(scoped_workspaces)} workspace(s) across {len(roots)} root(s).")
    print()

    if skipped_has_non_empty_chats:
        print(
            f"Skipped {len(skipped_has_non_empty_chats)} cleanup candidate workspace(s) that have non-empty "
            f"AI Chats (never deleted automatically):"
        )
        for w, reason, non_empty, total in skipped_has_non_empty_chats:
            print(
                f"  - {w.ws_id}  [{reason}]  "
                f"({non_empty}/{total} non-empty/total chat(s)) :: {w.display_path()}"
            )
        print()

    if skipped_unverifiable:
        print(
            f"WARNING: could not verify chat counts for {len(skipped_unverifiable)} stale-looking workspace(s) "
            f"(the database is probably locked because Cursor is running) - skipped for safety:"
        )
        for w, reason in skipped_unverifiable:
            print(f"  - {w.ws_id}  [{reason}]")
        print()

    if not candidates:
        print("Nothing to clean up.")
        return

    total_size = sum(dir_size(w.folder_path) for w, _, _ in candidates)
    print(
        f"Workspaces to DELETE ({len(candidates)}, {human_size(total_size)} total, "
        f"0 non-empty AI Chats each):"
    )
    for w, reason, total in candidates:
        chat_record_note = f"{total} empty chat record(s)" if total else "no chat records"
        print(
            f"  - {w.ws_id}  [{reason}]  ({human_size(dir_size(w.folder_path))}, "
            f"{chat_record_note}) :: {w.display_path()}"
        )
    print()

    if not args.apply:
        print("(dry run - nothing was deleted. Re-run with --apply to actually delete these.)")
        return

    print("=" * 70)
    print("APPLY MODE - this will PERMANENTLY DELETE the workspaceStorage folders listed above.")
    print("Make ABSOLUTELY SURE Cursor is fully closed (all windows, check it")
    print("isn't idling in the system tray) before continuing.")
    print("=" * 70)
    if not args.yes:
        answer = input(f"Type 'yes' to delete {len(candidates)} workspace(s): ").strip().lower()
        if answer != "yes":
            print("Aborted, nothing was deleted.")
            return

    # Lock check up front, before touching anything: a locked state.vscdb
    # would mean Cursor is currently using that workspace, so don't delete it.
    for w, _, _ in candidates:
        if w.state_db.is_file():
            try:
                with open_db(w.state_db, for_write=True):
                    pass
            except DatabaseBusyError as e:
                print(f"ABORTED before deleting anything: {e}")
                return

    # Also lock-check the global DB of every root we'll purge chat records from.
    affected_roots = {w.root.root_id: w.root for w, _, _ in candidates}.values()
    for root in affected_roots:
        if root.global_storage_db.is_file():
            try:
                with open_db(root.global_storage_db, for_write=True):
                    pass
            except DatabaseBusyError as e:
                print(f"ABORTED before deleting anything: {e}")
                return

    backup_root = Path(args.backup_dir) if args.backup_dir else STATE_DIR / "backups" / dt.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    chats_by_workspace = {
        (w.root.root_id, w.ws_id): get_chats_for_workspace(w.root, w.ws_id)
        for w, _, _ in candidates
    }

    # Complete every backup before changing either the global DB or a
    # workspaceStorage directory.
    for root in affected_roots:
        backup_file(root.global_storage_db, backup_root)
    for w, _, _ in candidates:
        backup_dir_tree(w.folder_path, backup_root)

    report_prefix = [
        "# Cursor Workspace Tool Backup Report",
        "",
        f"- Created: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- Operation: `cleanup`",
        "- Cleanup mode: `workspaces`",
        f"- Workspace filter: `{args.workspace}`" if args.workspace else "- Workspace filter: all",
        f"- Included empty windows: {'yes' if args.include_empty_windows else 'no'}",
        f"- Workspaces selected: {len(candidates)}",
        f"- Total workspaceStorage size: {human_size(total_size)}",
        "",
    ]
    for w, reason, _ in candidates:
        report_prefix.extend(
            [
                f"## Workspace `{w.ws_id}`",
                "",
                f"- Cleanup reason: {reason}",
                f"- workspaceStorage size: {human_size(dir_size(w.folder_path))}",
                *workspace_report_lines(w),
                "",
                *chat_report_lines(
                    chats_by_workspace[(w.root.root_id, w.ws_id)],
                    "Chat records removed",
                ),
                "",
            ]
        )
    report_path = write_operation_report(
        backup_root,
        report_prefix
        + [
            "## Result",
            "",
            "Backup created; cleanup has not completed yet.",
        ],
    )

    # Purge leftover chat records (composerHeaders + cursorDiskKV) from the
    # global DB, grouped per root so each DB is opened once.
    purged_records = 0
    ids_by_root: Dict[str, List[str]] = {}
    root_by_id: Dict[str, Root] = {}
    for w, _, _ in candidates:
        ids_by_root.setdefault(w.root.root_id, []).append(w.ws_id)
        root_by_id[w.root.root_id] = w.root
    for root_id, ws_ids in ids_by_root.items():
        root = root_by_id[root_id]
        if not root.global_storage_db.is_file():
            continue
        with open_db(root.global_storage_db, for_write=True) as con:
            for ws_id in ws_ids:
                purged_records += purge_workspace_chats_from_global_db(con, ws_id)
            con.commit()

    deleted = 0
    for w, _, _ in candidates:
        shutil.rmtree(w.folder_path, ignore_errors=True)
        if not w.folder_path.exists():
            deleted += 1
    write_operation_report(
        backup_root,
        report_prefix
        + [
            "## Result",
            "",
            "- Status: completed",
            f"- workspaceStorage folders deleted: {deleted}/{len(candidates)}",
            f"- Global chat records purged: {purged_records}",
        ],
    )
    print(
        f"Deleted {deleted} workspace folder(s) and purged {purged_records} leftover chat record(s) "
        f"from the global database. Backup saved to: {backup_root}"
    )
    print(f"Operation report: {report_path}")
    print("If anything looks wrong, use the `restore` command with that backup directory.")


def find_orphaned_workspaces(
    roots: List[Root],
    all_workspaces: List[Workspace],
) -> List[Workspace]:
    """Return synthetic Workspace objects for workspaceIds that are referenced
    by composerHeaders in a root's global DB but have no workspaceStorage
    folder on disk. These are 'orphaned' chats left behind after their
    workspace folder was removed (or was created by another install)."""
    existing_ids_by_root: Dict[str, set] = {}
    for w in all_workspaces:
        existing_ids_by_root.setdefault(w.root.root_id, set()).add(w.ws_id)

    orphaned: List[Workspace] = []
    for root in roots:
        if not root.global_storage_db.is_file():
            continue
        existing = existing_ids_by_root.get(root.root_id, set())
        try:
            with open_db(root.global_storage_db) as con:
                if not table_exists(con, "composerHeaders"):
                    continue
                ws_ids = [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT workspaceId FROM composerHeaders"
                    ).fetchall()
                ]
        except (sqlite3.Error, FileNotFoundError, DatabaseBusyError):
            continue
        for ws_id in ws_ids:
            if not ws_id or ws_id in existing:
                continue
            orphaned.append(
                Workspace(
                    root=root,
                    ws_id=ws_id,
                    kind="unknown",
                    uri_raw=None,
                    folder_path=root.workspace_storage / ws_id,
                )
            )
    return orphaned


def cmd_cleanup_empty_chats(args: argparse.Namespace) -> None:
    roots = discover_roots(args.root)
    all_workspaces = [w for r in roots for w in list_workspaces(r)]
    scoped_workspaces = (
        [resolve_workspace(args.workspace, roots, all_workspaces)]
        if args.workspace
        else all_workspaces
    )

    sections: List[Tuple[Workspace, List[ChatInfo]]] = []
    protected_drafts: List[Tuple[Workspace, ChatInfo]] = []
    unverifiable: List[Tuple[Workspace, str]] = []

    def collect_empty_chats(workspace: Workspace) -> None:
        try:
            chats = get_chats_for_workspace(workspace.root, workspace.ws_id)
        except (sqlite3.Error, DatabaseBusyError) as e:
            unverifiable.append((workspace, str(e)))
            return
        empty_chats = []
        for chat in chats:
            if chat.bubble_count != 0:
                continue
            if chat.is_draft:
                protected_drafts.append((workspace, chat))
                continue
            empty_chats.append(chat)
        if empty_chats:
            sections.append((workspace, empty_chats))

    for workspace in scoped_workspaces:
        collect_empty_chats(workspace)

    orphaned_count = 0
    if getattr(args, "include_orphaned", False) and not args.workspace:
        for workspace in find_orphaned_workspaces(roots, all_workspaces):
            orphaned_count += 1
            collect_empty_chats(workspace)

    total_empty = sum(len(chats) for _, chats in sections)
    orphaned_note = (
        f", including {orphaned_count} orphaned workspace-id(s) with no workspaceStorage folder"
        if orphaned_count
        else ""
    )
    print(
        f"Scanned {len(scoped_workspaces)} workspace(s) across {len(roots)} root(s){orphaned_note}; "
        f"found {total_empty} empty chat record(s) to delete."
    )
    print()
    for workspace, chats in sections:
        orphan_marker = (
            "  [orphaned - no workspaceStorage folder]"
            if not workspace.folder_path.exists()
            else ""
        )
        print(f"Workspace : {workspace.ws_id}{orphan_marker}")
        print(f"Path      : {workspace.display_path()}")
        print_chats_section(
            chats,
            "Empty AI Chat records",
            include_empty=True,
            include_draft=True,
        )
        print()

    if protected_drafts:
        print(
            f"Protected {len(protected_drafts)} zero-message draft(s) because they may contain unsent text "
            f"(not deleted):"
        )
        for workspace, chat in protected_drafts:
            print(f"  - {chat.name}  ({workspace.ws_id})")
            print(f"      ID: {chat.composer_id}")
            if chat.preview:
                print(f"      unsent text: {chat.preview}")
            else:
                print("      unsent text: (none saved)")
        print()

    if unverifiable:
        print(
            f"WARNING: could not read {len(unverifiable)} workspace(s) (most likely Cursor is running and "
            f"writing to the database) - skipped for safety. Close Cursor and re-run for a complete cleanup."
        )
        print(f"  First error: {unverifiable[0][1]}")
        print()

    if not sections:
        print("Nothing to clean up.")
        return

    if not args.apply:
        print("(dry run - nothing was deleted. Re-run with --apply to delete these empty chat records.)")
        return

    print("=" * 70)
    print("APPLY MODE - this will PERMANENTLY DELETE the empty chat records listed above.")
    print("Make ABSOLUTELY SURE Cursor is fully closed before continuing.")
    print("=" * 70)
    if not args.yes:
        answer = input(f"Type 'yes' to delete {total_empty} empty chat record(s): ").strip().lower()
        if answer != "yes":
            print("Aborted, nothing was deleted.")
            return

    affected_roots = {
        workspace.root.root_id: workspace.root
        for workspace, _ in sections
    }
    for root in affected_roots.values():
        if not root.global_storage_db.is_file():
            continue
        try:
            with open_db(root.global_storage_db, for_write=True):
                pass
        except DatabaseBusyError as e:
            print(f"ABORTED before deleting anything: {e}")
            return

    backup_root = Path(args.backup_dir) if args.backup_dir else STATE_DIR / "backups" / dt.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )
    for root in affected_roots.values():
        backup_file(root.global_storage_db, backup_root)

    report_prefix = [
        "# Cursor Workspace Tool Backup Report",
        "",
        f"- Created: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- Operation: `cleanup`",
        "- Cleanup mode: `empty-chats`",
        f"- Workspace filter: `{args.workspace}`" if args.workspace else "- Workspace filter: all",
        f"- Include orphaned: {'yes' if getattr(args, 'include_orphaned', False) and not args.workspace else 'no'}",
        f"- Orphaned workspace-ids scanned: {orphaned_count}",
        f"- Empty chat records selected: {total_empty}",
        f"- Zero-message drafts protected: {len(protected_drafts)}",
        "",
    ]
    for workspace, chats in sections:
        report_prefix.extend(
            [
                f"## Workspace `{workspace.ws_id}`",
                "",
                *workspace_report_lines(workspace),
                "",
                *chat_report_lines(chats, "Empty chat records removed"),
                "",
            ]
        )
    if protected_drafts:
        report_prefix.append("## Protected drafts (kept, may contain unsent text)")
        report_prefix.append("")
        for workspace, chat in protected_drafts:
            report_prefix.extend(
                [
                    f"- **{chat.name}** ({workspace.ws_id})",
                    f"  - ID: `{chat.composer_id}`",
                    f"  - Unsent text: {chat.preview or '(none saved)'}",
                ]
            )
        report_prefix.append("")
    report_path = write_operation_report(
        backup_root,
        report_prefix
        + [
            "## Result",
            "",
            "Backup created; empty-chat cleanup has not completed yet.",
        ],
    )

    chat_ids_by_root: Dict[str, List[str]] = {}
    for workspace, chats in sections:
        chat_ids_by_root.setdefault(workspace.root.root_id, []).extend(
            chat.composer_id for chat in chats
        )

    removed = 0
    for root_id, composer_ids in chat_ids_by_root.items():
        root = affected_roots[root_id]
        with open_db(root.global_storage_db, for_write=True) as con:
            removed += purge_chat_ids_from_global_db(con, composer_ids)
            con.commit()

    write_operation_report(
        backup_root,
        report_prefix
        + [
            "## Result",
            "",
            "- Status: completed",
            f"- Empty chat records deleted: {removed}/{total_empty}",
        ],
    )
    print(f"Deleted {removed}/{total_empty} empty chat record(s). Backup saved to: {backup_root}")
    print(f"Operation report: {report_path}")
    print("If anything looks wrong, use the `restore` command with that backup directory.")


# ---------------------------------------------------------------------------
# `restore` command
# ---------------------------------------------------------------------------


def cmd_restore(args: argparse.Namespace) -> None:
    backup_dir = Path(args.backup_dir)
    if not backup_dir.is_dir():
        raise SystemExit(f"No such backup directory: {backup_dir}")
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(
            f"No {MANIFEST_NAME} found in {backup_dir} - this doesn't look like a backup created by "
            f"`merge --apply` (or it predates the manifest feature). Nothing can be restored automatically."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"Could not read {manifest_path}: {e}")

    print(f"Backup snapshot: {backup_dir}")
    plan = []
    for backup_name, original_path in manifest.items():
        backup_path = backup_dir / backup_name
        if not backup_path.exists():
            print(f"  ! MISSING backup for {original_path} (expected {backup_path})")
            continue
        plan.append((backup_path, Path(original_path)))
        is_dir = backup_path.is_dir()
        exists_now = Path(original_path).exists()
        size = human_size(dir_size(backup_path)) if is_dir else human_size(backup_path.stat().st_size)
        kind = "directory" if is_dir else "file"
        print(f"  - {backup_path.name} ({kind}, {size})")
        print(f"      -> will restore to: {original_path}{'' if exists_now else '  [does not currently exist]'}")

    if not plan:
        print("Nothing to restore.")
        return

    print()
    print("Make sure Cursor is FULLY closed before restoring, or these changes will be lost/corrupted again.")
    if not args.yes:
        answer = input("Type 'yes' to overwrite the current files with this backup: ").strip().lower()
        if answer != "yes":
            print("Aborted, nothing was restored.")
            return

    for backup_path, original_path in plan:
        if backup_path.is_dir():
            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(backup_path, original_path, dirs_exist_ok=True)
            print(f"Restored directory {original_path}")
            continue
        if original_path.is_file():
            try:
                with open_db(original_path, for_write=True):
                    pass
            except DatabaseBusyError as e:
                raise SystemExit(f"ABORTED restore, a target file is locked (close Cursor first): {e}")
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, original_path)
        print(f"Restored {original_path}")
    print("Done.")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _order_help_groups(parser: argparse.ArgumentParser) -> None:
    """Show argument sections in a readable order: required first, then the
    optional positional group, then the -- flag options."""
    order = {"required arguments": 0, "optional arguments": 1, "options": 3}
    parser._action_groups.sort(key=lambda group: order.get(group.title, 2))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cursor_workspace_tool.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Inspect and manage Cursor workspaces (workspaceStorage entries) and their\n"
            "AI Chat / Composer data, cross-platform (Cursor on Windows + WSL/Linux).\n"
            "\n"
            "The write commands (`merge`, `cleanup`) run as a dry-run unless you pass\n"
            "--apply, and always create a timestamped backup first. Close Cursor fully\n"
            "before applying changes."
        ),
        epilog="Run `cursor_workspace_tool.py <command> -h` for detailed help on a command.",
    )
    p._optionals.title = "options"
    sub = p.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
    )

    p_list = sub.add_parser(
        "list",
        help="List all discovered Cursor workspaces.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "List every Cursor workspace found across all discovered installations.\n"
            "\n"
            "The table shows, per workspace: an index number (#) you can reuse in other\n"
            "commands, the internal id (the workspaceStorage folder name), the TYPE\n"
            "(Local/Remote environment), what Cursor OPENED AS (folder, workspace file,\n"
            "empty window, or unknown), a CHATS breakdown, on-disk SIZE, LAST USED time,\n"
            "and the decoded PATH.\n"
            "\n"
            "With --chat-counts or --details the CHATS column is filled in with a\n"
            "per-category breakdown: UI (regular chats shown in the UI), SUB-AGENT,\n"
            "ARCHIVED, EMPTY\n"
            "(zero-message records), DRAFT (zero-message records with saved unsent text),\n"
            "and TOTAL. Completely empty drafts are ignored and are not included in\n"
            "DRAFT or TOTAL. Reading chat counts requires opening the database, so it is\n"
            "skipped by default for speed.\n"
            "\n"
            "Use --with-chats to show only workspaces whose TOTAL is greater than zero.\n"
            "This option automatically reads and displays chat counts."
        ),
        epilog=(
            "Examples:\n"
            "  cursor_workspace_tool.py list\n"
            "  cursor_workspace_tool.py list --chat-counts  # + per-category chat counts\n"
            "  cursor_workspace_tool.py list --details   # + on-disk size (slower)\n"
            "  cursor_workspace_tool.py list --with-chats  # only TOTAL > 0\n"
        ),
    )
    p_list._optionals.title = "options"
    p_list.add_argument("--root", action="append", help="Add an extra Cursor 'User' (or 'Cursor') directory to scan.")
    p_list.add_argument(
        "--details",
        action="store_true",
        help="Show on-disk size AND the per-category chat breakdown (slower, reads each database).",
    )
    p_list.add_argument(
        "--chat-counts",
        action="store_true",
        help="Show the per-category chat breakdown only, without on-disk size (faster than --details).",
    )
    p_list.add_argument(
        "--with-chats",
        action="store_true",
        help="Show only workspaces with TOTAL > 0 chats. Empty draft shells are ignored, while "
        "EMPTY chat records count as existing chats.",
    )
    p_list.set_defaults(func=cmd_list)

    p_chats = sub.add_parser(
        "chats",
        help="List non-empty AI Chats and drafts of every workspace (or one selected workspace).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Show workspaces that have non-empty AI Chats or drafts, and list them.\n"
            "\n"
            "Non-empty chats (at least one message) and DRAFT chats with saved unsent\n"
            "text are shown. EMPTY chats and completely empty drafts are never listed. For each\n"
            "workspace a type summary is printed (UI / SUB-AGENT / ARCHIVED / DRAFT /\n"
            "TOTAL), followed by each chat with its name, id, type, creation time, last\n"
            "update time, and message count. Drafts also show their unsent draft text.\n"
            "\n"
            "Without an argument, every matching workspace is listed. Pass a workspace\n"
            "to restrict the output to just that one."
        ),
        epilog=(
            "Examples:\n"
            "  cursor_workspace_tool.py chats                      # all workspaces\n"
            "  cursor_workspace_tool.py chats 4                    # by list index\n"
            "  cursor_workspace_tool.py chats counter-service      # by path substring\n"
        ),
    )
    p_chats._optionals.title = "options"
    chats_optional = p_chats.add_argument_group("optional arguments")
    chats_optional.add_argument(
        "workspace",
        nargs="?",
        help="Optional workspace filter: index (#) from `list`, id, or unique path substring.",
    )
    p_chats.add_argument("--root", action="append", help="Add an extra Cursor 'User' directory to scan.")
    p_chats.set_defaults(func=cmd_chats)

    p_merge = sub.add_parser(
        "merge",
        help="Move or copy a workspace's non-empty Cursor AI Chats/Composers to another workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Move or copy Cursor AI Chats/Composers from one workspace to another.\n"
            "\n"
            "Only NON-EMPTY chats are transferred (a chat is non-empty when it has at\n"
            "least one message). Empty chat records and drafts are skipped and left\n"
            "untouched in the source. Nothing else about either workspace is changed:\n"
            "not the per-workspace state.vscdb (open tabs, UI state, recent files) and\n"
            "not the GitHub-Copilot-Chat chatSessions/chatEditingSessions folders.\n"
            "\n"
            "Existing chats at the destination are not overwritten by default. If the\n"
            "source and destination live in different Cursor installations, a full\n"
            "cross-database copy is performed, including messages, checkpoints, and\n"
            "related content.\n"
            "\n"
            "By default, every non-empty source chat is selected. Use\n"
            "--chat-id <id-1>,<id-2> to transfer only specific chats. The option may also\n"
            "be repeated, and both forms can be combined. Obtain IDs from the\n"
            "`chats <source>` command. Selected empty chats and drafts are rejected.\n"
            "\n"
            "During a cross-installation move, an existing destination chat with the same\n"
            "composerId is skipped by default. Pass --force to overwrite that destination\n"
            "chat with the source version. This is not applicable to copy mode, which\n"
            "always creates new IDs.\n"
            "\n"
            "Runs as a dry-run by default; pass --apply to write changes. A timestamped\n"
            "backup of the affected globalStorage/state.vscdb file(s) plus an\n"
            "operation-report.md are created before any change. Close Cursor completely\n"
            "before using --apply."
        ),
        epilog=(
            "Examples:\n"
            "  # Preview moving counter-service's chats into my-other-project (dry-run):\n"
            "  cursor_workspace_tool.py merge counter-service my-other-project\n"
            "\n"
            "  # Actually move them:\n"
            "  cursor_workspace_tool.py merge counter-service my-other-project --apply\n"
            "\n"
            "  # Copy instead of move (source keeps its own copy):\n"
            "  cursor_workspace_tool.py merge 3 7 --mode copy --apply\n"
            "\n"
            "  # Move only two specific non-empty chats:\n"
            "  cursor_workspace_tool.py merge 3 7 --chat-id <id-1>,<id-2> --apply\n"
            "\n"
            "  # Overwrite a same-ID destination chat during a move:\n"
            "  cursor_workspace_tool.py merge 3 7 --chat-id <id> --force --apply\n"
        ),
    )
    p_merge._optionals.title = "options"
    merge_required = p_merge.add_argument_group("required arguments")
    merge_required.add_argument(
        "source",
        help="Source workspace whose non-empty chats are transferred (index # from `list`, id, "
        "or unique path substring).",
    )
    merge_required.add_argument(
        "dest",
        help="Destination workspace that receives the chats (index # from `list`, id, or unique path substring).",
    )
    p_merge.add_argument("--root", action="append", help="Add an extra Cursor 'User' directory to scan.")
    p_merge.add_argument(
        "--mode",
        choices=["move", "copy"],
        default="move",
        help="Choose how selected chats are transferred: 'move' (default) re-associates them with "
        "the destination and removes them from the source; 'copy' duplicates them under new IDs and "
        "leaves the source untouched.",
    )
    p_merge.add_argument(
        "--chat-id",
        action="append",
        metavar="<id>[,<id>...]",
        help="Transfer only the specified non-empty source chat IDs. Separate multiple IDs "
        "with commas, repeat the option, or combine both forms. Without it, all non-empty "
        "source chats are transferred.",
    )
    p_merge.add_argument(
        "--force",
        action="store_true",
        help="For a cross-installation move, overwrite a destination chat that has the same "
        "composerId. Without this option, conflicting chats are skipped. Not valid with copy mode.",
    )
    p_merge.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes. Without this the command only prints what it would do (dry-run).",
    )
    p_merge.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    p_merge.add_argument(
        "--backup-dir",
        help="Directory to store the pre-merge backup and operation report in "
        "(default: a timestamped folder under the tool's state directory).",
    )
    p_merge.set_defaults(func=cmd_merge)

    p_cleanup = sub.add_parser(
        "cleanup",
        help="Delete stale workspaces or empty AI Chat records.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Delete clutter that Cursor never removes on its own. Two modes:\n"
            "\n"
            "  workspaces    delete stale workspaceStorage entries (deleted project,\n"
            "                or unreadable workspace.json) that hold no real chats\n"
            "  empty-chats   delete zero-message chat records left behind when a chat\n"
            "                is opened but nothing is ever sent\n"
            "\n"
            "Both modes run as a dry-run unless you pass --apply, always create a\n"
            "backup first, and can be limited to a single workspace."
        ),
        epilog="Run `cursor_workspace_tool.py cleanup <mode> -h` for details on a mode.",
    )
    p_cleanup._optionals.title = "options"
    cleanup_modes = p_cleanup.add_subparsers(
        dest="cleanup_mode",
        required=True,
        title="required arguments",
        metavar="<mode>",
    )

    p_cleanup_workspaces = cleanup_modes.add_parser(
        "workspaces",
        help="Delete stale workspaceStorage entries that have zero non-empty AI Chats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Delete stale workspaces from workspaceStorage.\n"
            "\n"
            "A workspace is a candidate when it is 'unknown' (workspace.json missing or\n"
            "unreadable) or 'missing' (the folder/.code-workspace it points to no longer\n"
            "exists on disk). A candidate is deleted ONLY if it has zero non-empty AI\n"
            "chats; empty chat records alone do not protect it. A workspace with even a\n"
            "single real chat is always kept. If chat counts cannot be verified (e.g.\n"
            "Cursor is running and the database is locked), the workspace is skipped.\n"
            "\n"
            "When applied, this deletes the whole workspaceStorage/<id>/ folder from disk\n"
            "AND purges that workspace's leftover chat records from globalStorage. It\n"
            "never touches your actual project files. A specific [workspace] can be\n"
            "targeted even if it is not stale, but the zero-real-chats protection still\n"
            "applies. Both the folder and the global database are backed up first."
        ),
        epilog=(
            "Examples:\n"
            "  cursor_workspace_tool.py cleanup workspaces                 # preview all\n"
            "  cursor_workspace_tool.py cleanup workspaces --apply         # delete all\n"
            "  cursor_workspace_tool.py cleanup workspaces <id> --apply    # one workspace\n"
        ),
    )
    p_cleanup_workspaces._optionals.title = "options"
    cleanup_workspaces_optional = p_cleanup_workspaces.add_argument_group("optional arguments")
    cleanup_workspaces_optional.add_argument(
        "workspace",
        nargs="?",
        help="Optional workspace filter: index (#) from `list`, id, or unique path substring.",
    )
    p_cleanup_workspaces.add_argument(
        "--root",
        action="append",
        help="Add an extra Cursor 'User' directory to scan.",
    )
    p_cleanup_workspaces.add_argument(
        "--include-empty-windows",
        action="store_true",
        help="Also treat shared 'empty window' entries as deletion candidates "
        "(still deleted only when they have zero non-empty chats).",
    )
    p_cleanup_workspaces.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this the command only prints what it would do (dry-run).",
    )
    p_cleanup_workspaces.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    p_cleanup_workspaces.add_argument(
        "--backup-dir",
        help="Directory to store the pre-cleanup backup and operation report in "
        "(default: a timestamped folder under the tool's state directory).",
    )
    p_cleanup_workspaces.set_defaults(func=cmd_cleanup_workspaces)

    p_cleanup_empty_chats = cleanup_modes.add_parser(
        "empty-chats",
        help="Delete empty (zero-message) AI Chat records, keeping drafts safe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Delete empty AI Chat records (composers with zero messages) that Cursor\n"
            "leaves behind whenever a chat is opened but nothing is ever sent.\n"
            "\n"
            "Only zero-message records are removed; any chat with at least one message\n"
            "is kept. Drafts (zero-message records flagged isDraft) are always protected\n"
            "and listed separately with their unsent text, because they may contain\n"
            "something you typed but never sent. This only edits globalStorage chat\n"
            "records - it never deletes a workspace or your project files.\n"
            "\n"
            "By default only workspaces that still have a workspaceStorage folder are\n"
            "scanned. Use --include-orphaned to also clean empty chats whose workspaceId\n"
            "has no folder on disk. Workspaces that cannot be read (usually because\n"
            "Cursor is running) are skipped with a warning. A backup of the global\n"
            "database is created before applying."
        ),
        epilog=(
            "Examples:\n"
            "  cursor_workspace_tool.py cleanup empty-chats                      # preview all\n"
            "  cursor_workspace_tool.py cleanup empty-chats --apply              # delete\n"
            "  cursor_workspace_tool.py cleanup empty-chats <id>                 # one workspace\n"
            "  cursor_workspace_tool.py cleanup empty-chats --include-orphaned   # + orphaned\n"
        ),
    )
    p_cleanup_empty_chats._optionals.title = "options"
    cleanup_empty_chats_optional = p_cleanup_empty_chats.add_argument_group("optional arguments")
    cleanup_empty_chats_optional.add_argument(
        "workspace",
        nargs="?",
        help="Optional workspace filter: index (#) from `list`, id, or unique path substring.",
    )
    p_cleanup_empty_chats.add_argument(
        "--root",
        action="append",
        help="Add an extra Cursor 'User' directory to scan.",
    )
    p_cleanup_empty_chats.add_argument(
        "--include-orphaned",
        action="store_true",
        help="Also delete empty chats whose workspaceId has no workspaceStorage folder on disk "
        "(orphaned records). Ignored when a specific workspace is given. Drafts are still protected.",
    )
    p_cleanup_empty_chats.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this the command only prints what it would do (dry-run).",
    )
    p_cleanup_empty_chats.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    p_cleanup_empty_chats.add_argument(
        "--backup-dir",
        help="Directory to store the pre-cleanup backup and operation report in "
        "(default: a timestamped folder under the tool's state directory).",
    )
    p_cleanup_empty_chats.set_defaults(func=cmd_cleanup_empty_chats)

    p_restore = sub.add_parser(
        "restore",
        help="Restore database files/folders from a previous merge/cleanup backup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Restore from a backup created by a previous `merge` or `cleanup --apply`.\n"
            "\n"
            "Reads the manifest.json inside the backup directory to map each saved file\n"
            "or folder back to its exact original location, shows what will be restored,\n"
            "and asks for confirmation before overwriting. Works for both single-file\n"
            "backups (merge) and whole-folder backups (cleanup workspaces). Close Cursor\n"
            "completely first, or the restore may fail on a locked database."
        ),
        epilog=(
            "Example:\n"
            "  cursor_workspace_tool.py restore ~/.cache/cursor-workspace-tool/backups/20260721_174531\n"
        ),
    )
    p_restore._optionals.title = "options"
    restore_required = p_restore.add_argument_group("required arguments")
    restore_required.add_argument(
        "backup_dir",
        metavar="<backup-dir>",
        help="Backup directory printed by a previous `merge`/`cleanup ... --apply` run.",
    )
    p_restore.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    p_restore.set_defaults(func=cmd_restore)

    for sub_parser in (
        p_list,
        p_chats,
        p_merge,
        p_cleanup,
        p_cleanup_workspaces,
        p_cleanup_empty_chats,
        p_restore,
    ):
        _order_help_groups(sub_parser)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except DatabaseBusyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except sqlite3.Error as e:
        message = str(e).lower()
        if "locked" in message or "malformed" in message or "busy" in message:
            print(
                f"ERROR: A Cursor database could not be read reliably (most likely Cursor itself is running "
                f"and writing to it).\nClose Cursor completely and try again.\n({e})",
                file=sys.stderr,
            )
        else:
            print(f"ERROR: database error: {e}", file=sys.stderr)
        return 2
    except SystemExit as e:
        if e.code and isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
