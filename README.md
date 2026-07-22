# Cursor Workspace Tool

[![PyPI](https://img.shields.io/pypi/v/cursor-workspace-tool.svg)](https://pypi.org/project/cursor-workspace-tool/)
[![Python](https://img.shields.io/pypi/pyversions/cursor-workspace-tool.svg)](https://pypi.org/project/cursor-workspace-tool/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

A single-file, dependency-free command-line tool for inspecting and managing
[Cursor](https://cursor.com) editor **workspaces** and their **AI Chat / Composer**
history — across Windows and WSL/Linux at the same time.

Cursor (like any VS Code–based editor) keeps a growing pile of `workspaceStorage`
entries and chat records that it never cleans up on its own, and it offers no
built-in way to move a conversation from one project to another. This tool lets
you see exactly what is stored, move or copy AI chats between workspaces safely,
and prune the clutter — with an automatic backup before every change.

## Features

- **List** every Cursor workspace found across all installations, with a rich
  per-category breakdown of its AI chats (`list`).
- **Inspect** the non-empty chats and drafts of any workspace, including names,
  IDs, timestamps and message counts (`chats`).
- **Move or copy** AI Chats/Composers between workspaces — all of them or a
  hand-picked selection — without overwriting anything at the destination
  (`merge`).
- **Clean up** stale workspace entries or empty chat records, globally or for a
  single workspace, with drafts always protected (`cleanup`).
- **Restore** any change from an automatic, timestamped backup (`restore`).
- **Cross-platform:** works whether Cursor runs natively on Linux or on Windows
  with the Remote-WSL extension.
- **Safe by default:** write commands are dry-run unless you pass `--apply`,
  always create a backup, and refuse to run against a locked database.

## How it works

The tool automatically discovers Cursor data directories. When run from
**WSL / native Linux** it scans:

- the local Linux Cursor config (`~/.config/Cursor`), and
- every Windows user profile reachable at
  `/mnt/<drive>/Users/*/AppData/Roaming/Cursor`.

The Windows scan matters because when you run Cursor on Windows and connect into
WSL via the Remote-WSL extension, **all** workspace and chat storage — including
data for WSL-remote workspaces — actually lives on the Windows side.

When run natively on **Windows** (`python.exe`), it uses `%APPDATA%\Cursor`.

You can always add extra directories (multiple users, VSCodium, etc.) with
`--root`.

### Where the data lives

| Data | Location |
| --- | --- |
| Per-workspace metadata | `workspaceStorage/<id>/` (`workspace.json`, `state.vscdb`) |
| AI chat headers & content | `globalStorage/state.vscdb` (`composerHeaders`, `cursorDiskKV`) |

Chats are keyed by a globally-unique `composerId` and associated with a
workspace through a `workspaceId`, which is why chats can be reassigned between
workspaces without collisions.

## Requirements

- Python 3.8+ (standard library only — no third-party packages).

## Installation

### Option 1 — install from PyPI (recommended)

```bash
pip install cursor-workspace-tool
# or
uv tool install cursor-workspace-tool
```

After installation, both of these commands are available and do the same thing:

```bash
cursor-workspace-tool --help
cwt --help
```

### Option 2 — install from GitHub

```bash
pip install git+https://github.com/aviv-raz/cursor-workspace-tool.git
# or
uv tool install git+https://github.com/aviv-raz/cursor-workspace-tool.git
```

### Option 3 — install from a local clone

```bash
git clone https://github.com/aviv-raz/cursor-workspace-tool.git
cd cursor-workspace-tool
pip install .
# or: uv tool install .
```

### Option 4 — run the script directly

No install required (Python 3.8+, standard library only):

```bash
git clone https://github.com/aviv-raz/cursor-workspace-tool.git
cd cursor-workspace-tool
python3 cursor_workspace_tool.py --help
```

## Quick start

```bash
# See all workspaces and their chat counts
cwt list --chat-counts

# Inspect the chats of a specific workspace
cwt chats counter-service

# Preview moving those chats into another project (dry-run)
cwt merge counter-service my-other-project

# Actually perform the move
cwt merge counter-service my-other-project --apply
```

The full command name `cursor-workspace-tool` works identically in place of `cwt`.
If you are running the script without installing, replace `cwt` with
`python3 cursor_workspace_tool.py`.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for setup, safety notes, and pull-request guidelines.

## Safety and backups

> **Close Cursor completely before any `--apply` or `restore`.**
> Cursor holds its databases open in the background; writing while it runs can
> cause data loss or lock errors. The tool checks for a locked database and
> aborts before making changes if it detects one — but this is a best-effort
> check, not a substitute for closing Cursor.

Every write creates an **automatic, timestamped backup** first (override the
location with `--backup-dir`) and prints its path when finished. You can always
roll back with `restore <backup-dir>`.

- `merge --apply` backs up the affected `globalStorage/state.vscdb` file(s) — the
  destination's, and the source's too if it belongs to a different installation.
- `cleanup workspaces --apply` backs up **the entire `workspaceStorage/<id>/`
  folder** of each deleted workspace **and** the relevant `globalStorage/state.vscdb`.
- `cleanup empty-chats --apply` backs up `globalStorage/state.vscdb` before
  deleting empty chat records.

Each backup folder also contains a human-readable `operation-report.md`
describing exactly what happened:

- **`merge`** — source and destination workspaces, `--mode move/copy`, how many
  chats were requested vs. actually transferred, and the full chat list (name,
  ID, type, created/last-updated times, message count). In `copy` mode it also
  records the new IDs created at the destination.
- **`cleanup workspaces`** — each deleted workspace, the reason, its path and
  size, the chat records purged from it, and the totals actually removed.
- **`cleanup empty-chats`** — the workspaces scanned, the empty chats deleted,
  and the totals actually removed.

If an operation aborts because the database is locked after the backup was made,
the report is saved with an `Aborted` status. The `manifest.json` in each backup
folder is the exact machine-readable mapping used by `restore`; the
`operation-report.md` is for human review.

## Commands

### `list` — list all workspaces

```bash
python3 cursor_workspace_tool.py list
python3 cursor_workspace_tool.py list --chat-counts   # + per-workspace chat breakdown
python3 cursor_workspace_tool.py list --details       # + on-disk size (slower)
python3 cursor_workspace_tool.py list --with-chats    # only workspaces whose TOTAL > 0
python3 cursor_workspace_tool.py list --root "/mnt/d/Users/other/AppData/Roaming/Cursor/User"
```

Prints a table with: an index (`#`) you can reuse in other commands, the internal
`ID` (the `workspaceStorage` folder name), the environment `TYPE`, what Cursor
`OPENED AS`, a `CHATS` breakdown, on-disk `SIZE`, `LAST USED` time, and the
decoded `PATH`.

With `--chat-counts` or `--details`, the `CHATS` column shows a full,
non-overlapping breakdown that always sums to `TOTAL`, e.g.:

```
UI:4  SUB-AGENT:2  ARCHIVED:0  EMPTY:6  DRAFT:0  TOTAL:12
```

| Category | Meaning |
| --- | --- |
| `UI` | Non-empty regular chats shown in the main chat list (not subagent/archived). |
| `SUB-AGENT` | Non-empty chats created as subagents; usually hidden from the main list. |
| `ARCHIVED` | Non-empty regular chats marked archived. |
| `EMPTY` | Zero-message records that are not drafts. |
| `DRAFT` | Zero-message records flagged `isDraft` **that contain saved unsent text**. |
| `TOTAL` | Sum of the categories above. |

Completely empty drafts (no saved text) are treated as junk: they are **not**
counted in `DRAFT` and **not** included in `TOTAL`.

`--with-chats` filters the table to workspaces whose `TOTAL > 0`. It turns on
chat counting automatically. `EMPTY` records count as existing chats; fully empty
drafts do not. A workspace whose database cannot be read is excluded from the
filter rather than misreported.

**`OPENED AS` values**

- `Folder` — `PATH` is a single folder opened directly.
- `Workspace file` — `PATH` points to a `.code-workspace` file, which may define
  one or many folders.
- `Empty window` — a Cursor window opened with no folder or workspace.
- `Unknown` — no valid `workspace.json`, so what was opened can't be determined.

**`TYPE` values**

- `Local (<OS>)` — a local path opened without a Remote connection. A path
  containing a Windows drive letter is reported as `Local (Windows)` even when
  the tool runs inside WSL; other local URIs use the generic name from
  `platform.system()` (e.g. `Local (Linux)`, `Local (Darwin)`).
- `Remote (<authority>)` — a Remote connection. The value in parentheses is the
  URI's raw `authority`, so current and future remote types display
  automatically — e.g. a Remote-WSL URI `vscode-remote://wsl+Ubuntu/...` shows as
  `Remote (wsl+Ubuntu)`.
- `Local (Windows with WSL FS: <Distro>)` — Cursor runs locally on Windows and
  reaches files inside a WSL distro via `file://wsl.localhost/<Distro>/...`,
  without a Remote-WSL connection.
- `Empty window` / `Unknown` — as above.

Workspaces whose folder/file no longer exists on disk (from the machine running
the tool) are marked `(MISSING)`.

### `chats [workspace]` — non-empty chats and drafts

```bash
python3 cursor_workspace_tool.py chats                                     # all workspaces
python3 cursor_workspace_tool.py chats 4                                   # by list index
python3 cursor_workspace_tool.py chats 2199cf1cd03410b8789ae530f51a71cb    # by exact id
python3 cursor_workspace_tool.py chats counter-service                     # by path substring
```

Without an argument, it scans all workspaces and shows those that have non-empty
chats or drafts **with content**. Under each workspace it lists the non-empty
chats (`UI`, `SUB-AGENT`, `ARCHIVED`) and any `DRAFT` chats that contain typed
text. `EMPTY` chats and completely empty drafts (e.g. the throwaway draft created
when you open a new empty window) are never listed.

The output starts with the number of workspaces and the total number of chats
shown. Each workspace section begins with a `UI / SUB-AGENT / ARCHIVED / DRAFT /
TOTAL` summary, followed by each chat's name, `composerId`, type, creation time,
last-updated time and message count. For `DRAFT` chats the unsent draft text is
shown; the tool extracts it from the chat's saved input (flat text, rich-text
input tree, or header subtitle).

The optional `[workspace]` argument restricts the output to a single workspace.

### `merge <source> <dest>` — move or copy chats between workspaces

This command **only touches Cursor's own non-empty AI Chats/Composers**
(`composerHeaders` / `cursorDiskKV` in `globalStorage/state.vscdb`). A chat is
non-empty when it has at least one message (`bubbleId`) — the same definition
used by `chats` and the `list` counts. Empty records and drafts are skipped and
left untouched in the source, in both `move` and `copy` modes.

It does **not** touch anything else: not per-workspace `state.vscdb` keys (open
tabs, UI state, recent files), and not the `chatSessions/` / `chatEditingSessions/`
folders (those belong to the GitHub Copilot Chat extension, not Cursor's
Composer).

```bash
# Dry-run is the default — always preview first:
python3 cursor_workspace_tool.py merge counter-service my-other-project

# Apply the move:
python3 cursor_workspace_tool.py merge counter-service my-other-project --apply

# Copy instead of move (source keeps its own copy):
python3 cursor_workspace_tool.py merge 3 7 --mode copy --apply

# Transfer only specific chats (comma-separated, or repeat the flag):
python3 cursor_workspace_tool.py merge counter-service my-other-project \
  --chat-id ce5f1fbf-cf28-4fa4-a0c8-ddf616a3aa78,01234567-89ab-cdef-0123-456789abcdef
```

**Mode (`--mode`, default `move`)**

- `--mode move` (default) — the chats are re-associated with the destination and
  removed from the source. This is safe because each chat's ID is globally
  unique, so it can't clobber an existing destination chat.
- `--mode copy` — the chats are duplicated at the destination under new IDs and
  also remain untouched in the source; nothing is deleted or changed on either
  side.

**Selecting specific chats (`--chat-id`)**

By default every non-empty source chat is selected. To pick specific ones, pass
comma-separated IDs (`--chat-id <id-1>,<id-2>`), repeat the flag, or combine both.
Selection is by `composerId` (shown by `chats`) because chat names are not
necessarily unique. If an ID doesn't belong to the source, or refers to an
empty/draft chat, the command stops without making any change.

**Overwriting on conflict (`--force`)**

During a move between two different Cursor installations, the same `composerId`
might already exist at the destination. By default such a conflict is skipped and
nothing is overwritten. `--force` replaces the existing destination chat with the
source version; the backup is made first, and the source copy is deleted only
after the destination write succeeds. `--force` is not valid with `--mode copy`,
since copy always creates a new ID.

When source and destination live in **different Cursor installations**, the tool
performs a full cross-database transfer between the two `globalStorage/state.vscdb`
files, including message content (`bubbleId`), checkpoints, and related content.

### `cleanup workspaces [workspace]` — remove stale workspace entries

```bash
python3 cursor_workspace_tool.py cleanup workspaces                          # dry-run, all
python3 cursor_workspace_tool.py cleanup workspaces --apply                  # delete all candidates
python3 cursor_workspace_tool.py cleanup workspaces 6ef46f8f... --apply      # one workspace
```

Finds workspaces that look like leftovers — `unknown` (missing/unreadable
`workspace.json`) or `missing` (the folder or `.code-workspace` it pointed to no
longer exists) — and deletes them **only if they have zero non-empty AI chats**.
Empty chat records alone do not protect a workspace; a workspace with even one
real chat is never deleted automatically. If chat counts can't be verified (e.g.
Cursor is running and the database is locked), the workspace is **skipped**.

Passing an explicit `[workspace]` makes it a deletion candidate even if it isn't
`unknown`/`missing`, but the zero-real-chats protection still applies.

**What is deleted (no leftovers):**

1. **The whole `workspaceStorage/<id>/` folder** on disk (its per-workspace
   `state.vscdb`, `workspace.json`, and everything else).
2. **The leftover records in the global database** (`globalStorage/state.vscdb`) —
   the `composerHeaders` and `cursorDiskKV` rows for that workspace's (empty)
   chats — so no orphaned rows remain.

> The tool **never touches your actual project files** — only the metadata Cursor
> stores about the workspace. Shared `composer.content.*` blocks are intentionally
> preserved because they may be referenced by other chats.

Options:

- By default `empty-window` (the shared empty-window entry) is **not** touched —
  it's considered a legitimate state, not clutter.
- `--include-empty-windows` also treats `empty-window` entries as deletion
  candidates (still protected if they contain a non-empty chat).

Both the deleted folder and the affected global database are backed up first and
can be recovered with `restore`.

### `cleanup empty-chats [workspace]` — remove empty chat records

```bash
python3 cursor_workspace_tool.py cleanup empty-chats                          # dry-run, all
python3 cursor_workspace_tool.py cleanup empty-chats 6ef46f8f...              # dry-run, one workspace
python3 cursor_workspace_tool.py cleanup empty-chats --apply                  # delete
python3 cursor_workspace_tool.py cleanup empty-chats --include-orphaned       # + orphaned records
```

Deletes only Composer records with zero messages (`bubble_count = 0`), without
deleting any workspace or project files. Drafts (zero-message records flagged
`isDraft`) are **always protected** and listed separately at the end with their
unsent text, so you can see what's in them before deciding. This information is
also saved in `operation-report.md`.

**`--include-orphaned`** — by default only workspaces that still have a
`workspaceStorage` folder are scanned. This flag also scans the global database
directly and finds empty chats whose `workspaceId` **has no `workspaceStorage`
folder** (orphaned records left after a workspace folder was deleted, or created
by a different installation). Orphans are marked `[orphaned - no workspaceStorage
folder]` in the output. Only **empty** chats are removed and drafts stay
protected. The flag is opt-in and is ignored when a specific `[workspace]` is
given. Workspaces that can't be read (usually because Cursor is running) are
skipped with a warning.

Before `--apply`, the tool verifies the database isn't locked, backs up
`globalStorage/state.vscdb`, and writes an `operation-report.md`. Dry-run is
always the default.

### `restore <backup-dir>` — roll back a previous change

```bash
python3 cursor_workspace_tool.py restore ~/.cache/cursor-workspace-tool/backups/20260721_174531
```

Shows which files/folders are in the backup and where they will be restored
(using the exact `manifest.json` saved at backup time — not path guessing), and
asks for confirmation before copying them back over the originals. Works for both
`merge` backups (individual files) and `cleanup` backups (whole folders).

## Identifying a workspace

Anywhere a workspace argument is accepted (`chats`, `merge`, `cleanup`), you can pass:

- the **index** (`#`) from the most recent `list` output (cached between runs),
- the **exact ID** (the folder name under `workspaceStorage/`), or
- a **path substring**, as long as it's unique (if several match, the tool lists
  them and asks you to be more specific).

## FAQ: why are there "weird" values in the `PATH` column?

These are all **normal**. Cursor never removes old `workspaceStorage` entries, so
various leftovers accumulate over time. Importantly, **you don't have to open a
project "on purpose" for an entry to be created** — each of these creates one:

- Opening a single file (e.g. `code file.txt`, or double-clicking a file), not
  just a whole folder.
- Any empty "New Window" (`Ctrl/Cmd+Shift+N`), even if nothing is saved in it.
- Extensions/tools that briefly open a Cursor window (some git tools,
  dev-containers, etc.).
- Moving or renaming a project folder — the **old** entry (with the previous path)
  stays, and a **new** one is created for the new path.
- Deleting a project or `.code-workspace` file from disk without closing it in
  Cursor first.

Specific messages:

- **`(unknown - workspace.json missing or unrecognized; ...)`** — the folder has
  no valid `workspace.json`, so the original project can't be determined; likely
  an orphan from a deleted/renamed workspace.
- **`(MISSING)`** — the tool checked whether the path still exists on disk (from
  the machine running it) and found it gone.
- **`(empty window - no folder/workspace opened)`** — the single shared
  `empty-window` entry Cursor uses whenever a window opens with nothing loaded.
- **`/`** — a valid path: a workspace opened on the WSL root directory (`/`)
  itself rather than a specific project.

If these entries are no longer relevant (and in particular have no AI chats), use
the `cleanup` commands above to prune them.

## Known limitations

- On-disk existence detection is best-effort. For non-WSL remote workspaces
  (SSH, dev-containers) it can't be verified from this machine, so `cleanup` does
  **not** treat them as `missing`; they're deleted only if their `workspace.json`
  is entirely missing/corrupt (`unknown`) **and** they have no non-empty chats.
- When copying (`--mode copy`) a chat between two different Cursor installations,
  large content blocks stored in content-addressable storage
  (`composer.content.*`) are copied best-effort (by scanning the chat JSON for
  references). In very rare cases of missing content, only that specific
  checkpoint/diff would be inaccessible; the chat text itself is unaffected.
- The tool does not connect to WSL over `\\wsl$` from Windows. If you run it on
  Windows, use WSL itself (or `--root` with a UNC path) to reach that side.

## License

Licensed under the **GNU General Public License v3.0** (GPL-3.0). See the
[LICENSE](LICENSE) file for the full text.

This is a copyleft license: you are free to use, modify and redistribute this
tool, but any distributed derivative work must also be released under the GPL and
keep the original copyright and license notices.

Copyright (C) 2026 Aviv Raz —
[https://github.com/aviv-raz](https://github.com/aviv-raz)
