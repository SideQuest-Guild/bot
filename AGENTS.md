# SideQuest Guild bot

A discord.py bot. It talks to the [SideQuest-Guild/website](https://github.com/SideQuest-Guild/website) API only over
HTTP (`X-API-Key`), never to the database. `README.md` explains the bot to people; keep it current and in plain words.

## Layout
- `src/sidequest_bot/plugins/`: one cog per file. `api_client.py` is the only way the bot talks to the API.
  `lib/roles.py` checks every role it gives or takes. `assets/role_icons/` holds the role icon PNGs.
- `tests/`: `conftest.py` starts the real API in the test process on a fresh SQLite file (from the `sidequest-api`
  dev dependency) and points the bot's own `SideQuestAPI` client at it, so tests send real requests.

## Working
- `make install`, `make fmt`, then `make ci` (ruff, pyrefly, pytest). Commit and push only when `make ci` exits 0.
- Commits are SSH-signed via 1Password. If signing fails, ask the user to unlock it; never turn signing off.
- Commit subjects are Conventional Commits written as plain sentences. They become the changelog, so say what
  changed for guild members.
- Other agents work in this tree at the same time: never `git stash`, `checkout`, `reset`, or commit files you
  didn't change.

## Releases
Each release adds a section to the changelog on the docs site and on GitHub. Without a tag, every change sits
under "Unreleased".
- Cut one after a batch of `feat` or `fix` commits is live and checked, and at least at the end of any day that
  shipped one.
- Version `v0.MINOR.PATCH`: any `feat` since the last tag bumps MINOR (`v0.1.0` to `v0.2.0`); only fixes bump PATCH.

```sh
git cliff --tag v0.2.0 -o CHANGELOG.md
git add CHANGELOG.md && git commit -m "docs: changelog for v0.2.0"
git tag -s v0.2.0 -m "Bot v0.2.0"
git push origin main v0.2.0
gh workflow run build.yml -R SideQuest-Guild/docs   # show it on the docs site now, not at tomorrow's build
```

The tag starts `.github/workflows/release.yml`, which publishes the GitHub release. `v*` tags can't be moved or
deleted; fix a bad release with a new version.

## The `sidequest-common` pin
`sidequest-common` (and, for tests, `sidequest-api`) come from `SideQuest-Guild/website`, pinned to a commit in
`[tool.uv.sources]` in `pyproject.toml`. To pick up a change there: bump the commit, `uv lock`, `make ci`. A schema or
settings change on the website side can break this repo without warning.

## Deploys
- Run exactly one `worker`. Two bots on one token answer everything twice.
- The bot and the API deploy separately. Either one may be newer, so treat a 404 from a route the other side
  doesn't have yet as "try again later", never as fatal.

## Rules that bite
- Catch `APIError`, never `ValidationError` (`api_client._parse` wraps bad responses).
- Slash command names are at most 32 characters and descriptions 1 to 100. A bad one crash-loops the prod bot;
  `test_app_command_limits.py` checks it.
- No imports inside functions (ruff PLC0415, except `__main__.py`). Known pyrefly errors live in
  `pyrefly-baseline.json`. Tests fail on warnings.
- `AGENTS.md` is in the global gitignore; add it with `git add -f AGENTS.md`.
