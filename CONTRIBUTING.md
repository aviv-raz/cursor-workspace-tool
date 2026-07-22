# Contributing

Thanks for your interest in improving **Cursor Workspace Tool**.

This project is intentionally small: one Python module, no third-party runtime
dependencies, and a focus on safe defaults (dry-run first, backups before every
write, refuse locked Cursor databases).

## Development setup

```bash
git clone https://github.com/aviv-raz/cursor-workspace-tool.git
cd cursor-workspace-tool

# Editable install (creates both CLI entry points)
pip install -e .

# or with uv
uv pip install -e .
```

After that, both commands should work:

```bash
cursor-workspace-tool --help
cwt --help
```

You can also run the module directly without installing:

```bash
python3 cursor_workspace_tool.py --help
```

## Before you change anything that writes data

Commands that modify Cursor data (`merge --apply`, `cleanup ... --apply`,
`restore`) can affect real chat history.

When testing write paths:

1. Prefer dry-run first (the default without `--apply`).
2. Close Cursor completely before any `--apply` / `restore` test.
3. Use a disposable Cursor profile / test machine when possible.
4. Keep the automatic backups and verify them with `restore`.

## Coding guidelines

- Keep the tool **stdlib-only** for runtime. Do not add PyPI dependencies unless
  there is a very strong reason and it is discussed first.
- Preserve cross-platform behavior: native Linux/WSL **and** Windows Cursor data
  reachable through `/mnt/<drive>/Users/...`.
- Prefer clear CLI wording over cleverness. Help text and README should stay in
  sync with actual flags.
- Do not weaken safety defaults:
  - dry-run unless `--apply`
  - backup before writes
  - never overwrite destination chats unless `--force` was explicitly requested
  - protect drafts during empty-chat cleanup
- Keep changes focused. Prefer small, reviewable pull requests.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make your change.
3. Update docs when behavior or flags change (`README.md`, command `-h` text).
4. Open a pull request that explains:
   - what problem it solves
   - how you tested it
   - any risk to existing Cursor data

## Reporting issues

Please include:

- OS (Windows / WSL distro / Linux)
- How Cursor is set up (native Linux, Windows + Remote-WSL, etc.)
- Exact command you ran
- Expected vs. actual result
- Whether Cursor was closed during write operations

Do **not** paste private chat contents, API keys, or unrelated personal files.

## License

By contributing, you agree that your contributions are licensed under the same
terms as the project: **GNU GPL v3 or later**. See [LICENSE](LICENSE).

Copyright remains with contributors according to the GPL. Please keep existing
copyright and license headers intact when editing files.
