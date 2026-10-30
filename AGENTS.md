# SideQuest Guild bot

A discord.py bot. Talks to the [SideQuest-Guild/website](https://github.com/SideQuest-Guild/website) API only over
HTTP (`X-API-Key`), never the database. `README.md` explains the bot to people; keep it current and in plain words.

## Layout
- `src/sidequest_bot/` — one cog per file in `plugins/`. `api_client.py` is the only way the bot talks to the API.
  `lib/roles.py` guards every role grant/revoke. `assets/role_icons/` are the badge/rank/gate role PNGs.
- `tests/` — `conftest.py` boots the real API in-process on a per-test SQLite file (via the `sidequest-api` dev
  dependency) and wires the bot's own `SideQuestAPI` client to it, so tests exercise real request/response shapes.

## Working
- `make install`, then `make fmt`, then `make ci` (ruff format, ruff check, pyrefly, pytest). Commit and push only
  when `make ci` exits 0.
- Commits are SSH-signed via 1Password. If signing fails, stop and report it; never disable signing.
- Conventional-commit subjects, written as plain-English sentences.

## The `sidequest-common` pin
`sidequest-common` (and, for tests, `sidequest-api`) come from `SideQuest-Guild/website` as git dependencies pinned
to a commit SHA in `[tool.uv.sources]` in `pyproject.toml`. Bump the SHA, `uv lock`, then `make ci` — a schema or
settings change on the other side can break this repo without warning.

## Rolling deploys
Run exactly one `worker` replica (see `Procfile`/README). The bot and the API deploy independently: a new bot
talking to an old API, or an old bot talking to a new API, must both tolerate the other side returning 404 for a
route it doesn't know yet — never assume the other side has already deployed.

## Rules that bite
- Bot code catches `APIError`, never `ValidationError` (`api_client._parse` wraps bad shapes).
- Slash command names ≤32 chars, descriptions 1-100 (a bad one crash-loops the prod bot; `test_app_command_limits.py`
  enforces it).
- No function-level imports (ruff PLC0415, except `__main__.py`). Pyrefly known errors live in
  `pyrefly-baseline.json`. Tests fail on warnings.
