# SideQuest Guild bot

The Discord bot ("Game Master") for the SideQuest guild (WoW: Forever). It talks to the
[SideQuest-Guild/website](https://github.com/SideQuest-Guild/website) API over HTTP (`X-API-Key`) and never touches
the database directly; models/schemas/settings shared with the API come from that repo's `sidequest-common` package,
pinned to a commit (see "The `sidequest-common` pin" below).

## Running locally

Requires uv (Python 3.15 is installed from `.python-version`), the bot's token (Developer Portal → Game Master →
Bot), and a running SideQuest API (clone [SideQuest-Guild/website](https://github.com/SideQuest-Guild/website) and
run `make dev` or `make api` there; see that repo's README). The bot needs the **Server Members** and
**Message Content** privileged intents enabled in the Developer Portal, plus the **Manage Events** server permission
(Create Events + Manage Events) for event sync, and, if using the events forum, **Manage Channels** (forum tags),
**Send Messages in Threads**, and **Create Posts**.

```bash
uv sync                           # installs the project, including sidequest-common/sidequest-api from git
export DISCORD_TOKEN=...          # Developer Portal -> Game Master -> Bot
export API_KEY=...                # must match the API's API_KEY
export API_URL=http://localhost:8000
export WEB_BASE_URL=http://localhost:8000
make bot                          # or: uv run sidequest-bot
```

`DISCORD_GUILD_ID` is optional; when set, slash commands sync to that server instantly instead of globally. The
other `WEB_*` settings (OAuth client ids/secrets, role ids, etc.) are optional and only matter if a plugin reads
them (see `WebSettings` in `sidequest_common.settings`).

`make ci` runs what GitHub Actions runs: ruff format and lint, pyrefly, and the test suite (deprecation warnings are
errors). pyrefly fails only on type errors that aren't recorded in `pyrefly-baseline.json`; after fixing some,
`make type-baseline` re-records it. `make fmt` formats the code, and `make help` lists the rest.

## The `sidequest-common` pin

`sidequest-common` (shared models, schemas, settings) isn't on PyPI; `pyproject.toml` pins it as a git dependency at
a specific commit of `SideQuest-Guild/website`:

```toml
[tool.uv.sources]
sidequest-common = { git = "https://github.com/SideQuest-Guild/website", rev = "<sha>", subdirectory = "packages/sidequest-common" }
sidequest-api = { git = "https://github.com/SideQuest-Guild/website", rev = "<sha>", subdirectory = "api" }
```

`sidequest-api` is a dev-only dependency: the test suite boots the real API in-process (see `tests/conftest.py`) so
the bot's tests exercise real request/response shapes instead of hand-rolled fakes.

To bump the pin after a change lands on `SideQuest-Guild/website`'s `main`:

1. Change `rev` in both `[tool.uv.sources]` entries in `pyproject.toml` to the new commit SHA.
2. `uv lock` to re-resolve and update `uv.lock`.
3. `make ci` — a schema or settings change on the other side can break the bot's tests or types.

## Features

Admin command groups are hidden from members without the listed permission (server admins can re-scope them under
Server Settings → Integrations). Every config change is stored by the API and applies immediately.

### Leveling
Every message earns 15–25 XP, at most once per 60 s per member, on MEE6's curve (`5n² + 50n + 100` XP from level
`n` to `n+1`). Level-ups post `GG {mention}, you just advanced to level {level}!` in the same channel unless a
level-up channel is set. Milestone roles (`Lv 5 · Wayfarer` … `Lv 50 · Legend`) are granted automatically; they're
cosmetic and sit below the rank roles, so rank colors and member-list groups stay in charge.

- `/rank [member]`, `/leaderboard [page]`
- `/levels reward set|remove|list` · `/levels set-xp` (Manage Server)
- `/levels config enabled | level-up-channel | level-up-message | xp-range | cooldown | stacking | no-xp-channel-add/remove | no-xp-role-add/remove`

### Logging (`#guild-log`)
Message edits/deletes (with the original text), bulk deletes (with a transcript file), joins (new accounts flagged),
leaves, nickname/role/timeout changes, bans/kicks/unbans by humans (with moderator and reason), channel and role
changes, and voice joins/leaves/moves. The bot's own moderation actions are logged as cases instead.

- `/logging channel <channel|none>`, `/logging status` (Manage Server)

### Welcome
New members get the auto-role (**New Arrival**) and a welcome message in `#general`. If membership screening is on,
both wait until the member accepts the rules. Placeholders: `{mention}` `{user}` `{guild}` `{member_count}`.

- `/welcome channel | message | autorole | test | status` (Manage Server)

### Role menus
Button panels for self-assignable roles. Buttons keep working across bot restarts. `max_roles` limits picks per menu.

- `/rolemenu create | add | remove | edit | delete | list | refresh` (Manage Roles)

### Class halls
A forum post named `<role name> Hall` (e.g. `Warrior Hall` for `@Warrior`) belongs to that role. Gaining the role
adds the member to the post, so it shows in their channel list; losing the role takes them out. Anyone who can see
the forum can still browse and follow any hall. No config: the bot pairs roles and posts by name, indexes halls at
startup and every 6 hours (adding role holders it missed while offline, never removing anyone), and picks up new
hall posts as they're created. Needs Manage Threads to reopen archived halls. In SideQuest, `#choose-your-class` is
a role menu for the nine class roles and `#class-halls` holds their halls.

### Moderation
Each action DMs the member (if possible), records a numbered case, and posts it to the log channel. Moderators can't
act on the owner, the bot, themselves, or anyone whose top role is at or above theirs.

- `/warn`, `/timeout <duration like 10m, 1h30m, 2d>`, `/untimeout` (Timeout Members)
- `/kick` (Kick Members) · `/ban [delete_message_days]`, `/unban <user id>` (Ban Members)
- `/cases [member]`, `/case <number>`, `/reason <number> <text>`

### Automod
Deletes invite links (on by default) and optionally all links; for mention spam (more than 5 mentions) or message
floods (more than 5 messages in 5 s) it also times the member out for 5 minutes. Each hit is recorded as an `automod`
case. Members with Manage Messages, exempt roles, and exempt channels are skipped.

- `/automod status | toggle | mentions | spam | timeout | exempt role|channel add|remove` (Manage Server)

### Status line
The bot's activity rotates every 3 minutes: a countdown to the WoW: Forever launch (Nov 4, counted in US Central;
"Playing World of Warcraft: Forever" afterwards), the next guild event ("Next up: Molten Core, in 3h"), and a few
Azeroth jokes. While an event is running it stays on "Competing in {event}". Bots can't show Rich Presence art,
buttons or timers, only this one line (`src/sidequest_bot/plugins/presence.py`).

### Event sync
Guild events created, edited, or cancelled on the site appear as Discord scheduled events, and events created,
edited, or deleted in Discord update the matching guild event, in either direction. Site edits reach Discord within
about 15 seconds (the bot's sync loop interval); Discord edits reach the site immediately via gateway listeners.
Requires the bot's **Manage Events** permission; without it, sync pauses for that server until the permission is
restored.

- Each Discord event's description ends with a `── SideQuest event ──` block: the event's kind, location and size,
  live sign-up counts, and a sign-up link built from `WEB_BASE_URL` (so set it to the public domain in production).
  Text above that block is the event's notes in both directions. Occurrences of a repeating series add the rule
  (e.g. `Every Friday`, `Every other Friday`) to that line.
- Events made in Discord become guild events of kind `other` with no location or size; officers can fill those in on
  the site.
- Deleting an event in Discord cancels its guild event on the site and keeps the sign-ups; deleting a guild event on
  the site deletes its Discord event. Events that already started aren't created in Discord (Discord rejects past
  starts).
- The bot reconciles every event when it starts, so changes made while it was offline are picked up.
- Officers create repeating events (`POST /api/event-series`: weekdays, interval, time, timezone, optional end date)
  and the API keeps the next 14 days of occurrences materialized as ordinary guild events, each with its own
  sign-ups and Discord event, so the bot's sync loop and `GET /api/events` always see real, signable events rather
  than the series rule itself. Editing a single occurrence detaches it from the series; editing the series updates
  its still-attached future occurrences (re-timing or cancelling ones a schedule change no longer matches); ending
  a series cancels its signed-up future occurrences and deletes the signup-less ones, keeping past occurrences.
- Every event and series carries the Renown attending it is worth (the API field is still `ep`; on the site it's
  labelled Renown so "EP" only ever means SQL, the loot system): `ep_override` if an officer set one
  (0–50), else its kind's preset (`EVENT_KIND_EP`: raid 10, dungeon 5, leveling 3, PvP 3, social 2, transmog 2,
  other 2). Responses return both `ep` (what attending awards) and `ep_override`; a blank Renown box on the officer
  form sends `null`, which follows the kind's preset (including after a kind change or a preset retune). Series copy
  their override onto occurrences.
- Clicking "Interested" on the Discord event reports the full interested-user list to the site (one-way: the API
  can't mark a Discord user interested on the bot's behalf), debounced ~5s per event; `EventDetail.discord_interest`
  shows the total count plus the names/avatars of interested users who also have a site account (others stay
  anonymous), visible only to signed-in site members (or the bot) — anonymous visitors get `discord_interest: null`.
- `/eventsync forum [channel]` (Manage Server) points upcoming events at a forum channel: one thread per guild
  event, named after the event and tagged by kind (`Raid`/`Dungeon`/`Leveling`/`PvP`/`Social`/`Transmog`/`Other`;
  the bot creates missing tags, needs **Manage Channels**). The starter post is edited in place as the event changes;
  cancelling posts a notice and archives+locks the thread, completing just archives+locks it, and deleting the
  guild event on the site posts a removal notice and archives+locks it too. Sign-ups, status changes, new
  comments, and withdrawals get a short forum comment that @-mentions the member without pinging them and quotes
  their sign-up comment ("✅ @Coffee is going — Oakenshield · Warrior · Tank" then "> bring flasks"); needs **Send
  Messages in Threads** and **Create Posts**.

### Discord sign-ups
Every forum starter post carries persistent **Going**/**Maybe**/**Can't** buttons, so members can sign up without
leaving Discord, kept in step with the site's own sign-up form. A click with no site account gets an ephemeral
"Sign in at sidequest-guild.com/account/ first" reply with a link button. Non-role-kind events (social, pvp, …)
RSVP immediately with an ephemeral confirmation. Role-kind events (raid, dungeon) for Going/Maybe: a member who's
given a character/class/role on any earlier event signs up with those details at once ("Going as Oakenshield ·
Warrior · Tank", with a "Change character or role" button); otherwise a modal asks for character name, class
(defaulting to a matching Discord class role, e.g. `@Warrior`), and role. Can't never asks for character details.
Cancelled/completed events have their buttons removed, and the live counts line ("Going 12 (2 tanks · 3 healers ·
7 DPS) · Maybe 1 · Can't 2") refreshes within one sync cycle (~15-30s) of any change from either side. Existing
upcoming threads pick up the buttons automatically on the next sync cycle after an upgrade, no manual action
needed. Bot-key routes: `GET/PUT /api/guilds/{guild_id}/events/{event_id}/signups/by-discord/{discord_id}` (404 for
no site account, 409 if the event isn't scheduled).

### Away dates
Members mark date ranges they'll be away from `/account/`'s Away chamber on the site (`GET/POST /api/me/away`,
`DELETE /api/me/away/{id}`; past ranges are pruned on read) so officers and raid leads know not to expect them.
While a range is active the bot also grants a configured Discord role so it's visible outside the site, synced
every 60 seconds against the bot-key `GET /api/guilds/{guild_id}/away-now` route (`src/sidequest_bot/plugins/away.py`).

- `/away role <role>` (Manage Server) — sets or clears the role; in production this is
  `/away role role:@Away on a Quest` (role id `1564677987859963975`).

### Live status
The bot reports live counts, never names: how many people are in each public voice/stage channel, plus the guild's
approximate online/member counts (shown on the website's homepage "In the Discord" line). Every 60 seconds the bot
refreshes `approximate_presence_count`/`approximate_member_count` (`fetch_guild(..., with_counts=True)`, keeping the
last known values if that fetch fails) and pushes a full report; voice/stage channel changes also push immediately,
debounced ~5 seconds per guild so a burst of joins/leaves coalesces into one push (mute/deafen toggles don't count
as a channel change). No privileged presence intent is used (`src/sidequest_bot/plugins/live_status.py`,
`PUT /api/guilds/{guild_id}/live`).

### Attendance voice tracking
While an event is running, the bot tracks per-event voice presence: every 60 seconds it notes which non-bot members
are in one of the guild's voice/stage channels for each event that's started and not yet past its end (or, with no
explicit end, 3 hours after its start) and not cancelled, and pushes each member's cumulative minutes for that event
(`PUT /api/guilds/{guild_id}/attendance/voice`, idempotent replace — not an increment — so a bot restart or a
resent tick never double-counts) (`src/sidequest_bot/plugins/attendance_voice.py`). Officers use these minutes on
the site to help decide who actually showed up to a past event (the Officers page's Attendance tab).

### Link roles
Members who sign in on the site get a role (default name **Signed the Ledger**); members who also link Battle.net
get a second one (**Battle-Tagged**). Unlinking Battle.net drops just that role; unlinking the site account (or no
longer appearing in the sign-in list) drops both. New joiners who haven't signed in get a third role, the gate role
(**At the Gate**), removed the moment they sign in; it's granted immediately on join (or screening-clear) just like
the other two. Once a GM confirms a member on the Officers page's Members tab as actually being on the in-game guild
roster (ahead of the WoW Forever roster API that will later set the same flag automatically), the member gets a
fourth role, the roster role (**New Arrival**) — but never if they already hold a rank above it, so a promoted
member who dropped the role doesn't get it back every minute. Channels other than the sign-in channel are set in
Discord to be visible only to the **Signed the Ledger** role (and `@everyone` loses `View Channel`), so the gate
role's only purpose is to show new members the sign-in channel until they're in; every member channel is further
restricted to the roster role and ranks above it. A sync loop reconciles every member every 60 seconds against
`GET /api/guilds/{guild_id}/member-links`, and joins/screening-clears apply the same plan immediately. Only
configured roles are ever touched, and a role that's missing or unsafe to grant is skipped with a one-time warning.
The same loop also drains the Discord role-reset outbox (`GET /api/guilds/{guild_id}/role-resets`): a member who
deletes their site account keeps nothing but the gate role, stripped of every other role the bot can manage
(skipping `@everyone`, managed/integration roles, and anything at or above the bot's own top role — Discord would
reject those anyway) and immediately re-gated, rather than waiting for the next tick; the bot acks each reset
(`POST .../role-resets/{reset_id}/ack`), acking immediately with no role changes if the member already left
(`src/sidequest_bot/plugins/link_roles.py`).

- `/linkroles site | battlenet | gate | roster` (Manage Server)

### Guest welcome
The moment a member is granted the site link role (the gate -> guest transition, see Link roles above), the bot DMs
them a one-time gold-embed welcome: they're in as a guest, a Quest Master will confirm them soon, and pointers to
`#choose-your-class` and the events page. It's derived from the role grant itself, not a stored flag, so it can't
double-send and needs no migration. A closed DM (or any other HTTP error) is only logged; it never breaks the role
sync. Since Discord won't let the bot send an unprompted ephemeral message, the same welcome is also reachable
on demand: `/guestwelcome post` posts a pinned "Start here" button (a persistent component surviving restarts, like
`role_menus`/`signup_notices`) into a chosen channel (e.g. `#guest-chat`); clicking it replies ephemerally with the
identical embed (`src/sidequest_bot/plugins/guest_welcome.py`).

- `/guestwelcome post <channel>` (Manage Server)

### Signup notices
Every 60 seconds, for each guild with a signup-notice channel configured, the bot posts a gold embed in that channel
for every member who signed in for the first time since the last check (`GET /api/guilds/{guild_id}/signup-notices`):
their name, avatar, BattleTag if linked, and a "Guest until a Quest Master approves" status. The embed carries an
**Approve** button (a persistent component surviving restarts, like `role_menus`) and a **More info** link button to
the member's detail on the Officers page's Members tab (`/officers/#members?member={id}`). Approve re-checks the
clicker holds a `WEB_GM_ROLE_IDS` role, then confirms the member on the roster through the same route the GM console
uses — just with the clicker identified by Discord id instead of a session — and edits the message in place to show
who approved it. If a member is instead confirmed on the site before anyone clicks Approve, the next poll edits that
member's message to say so instead of leaving it stuck on "Guest". The bot acks every post and edit
(`POST /api/guilds/{guild_id}/signup-notices/{member_id}/ack`) so nothing is posted or re-posted twice
(`src/sidequest_bot/plugins/signup_notices.py`).

- `/signups channel` (Manage Server)

### Weekly events board
Every Monday at 10:00 America/Chicago, for each guild with a weekly-board channel configured, the bot posts one gold
embed listing that week's scheduled events (Monday 00:00 to the following Monday 00:00 Central), soonest first: each
line shows the weekday, a Discord timestamp (so it renders in every reader's own timezone), the event kind, a link
to its detail page on the site, and a link to its forum thread if it has one. The events come from the bot-key
`GET /api/guilds/{guild_id}/weekly-board/events?start=...&end=...` route (scoped to the week, unlike the public
`/api/events` list, and the only one that also returns each event's forum thread id). A week with no events posts
nothing. The last posted week is stored on `GuildConfig.weekly_board_last_week`, so a restart or a second check
within the same day never double-posts; because the check only ever fires on Monday, a bot that was down past
10:00 catches up the moment it's back online that day, but never later in the week
(`src/sidequest_bot/plugins/weekly_board.py`).

- `/weeklyboard channel <channel>` (Manage Server)
- `/weeklyboard preview` (Manage Server) — ephemeral preview of the current week's board

### Member profile sync
Member rows (username, global name, nick, avatar, roles, guild membership) stay current with Discord continuously,
not just at sign-in: on `on_ready` and every 10 minutes, the bot pushes a full sweep — the current profile for
every linked member (from `GET /api/guilds/{guild_id}/member-links`) still in the guild, plus the ids of linked
members no longer in the guild. It also pushes immediately, debounced ~5 seconds per guild and coalescing ids, on
nick/role/guild-avatar changes (`on_member_update`), username/global-name/avatar changes
(`on_user_update`, checked against every site guild the user shares with the bot), joins (`on_member_join`), and
departures (`on_member_remove`). Only linked members are ever pushed; the periodic sweep keeps that linked set
current. `PUT /api/guilds/{guild_id}/member-profiles` only updates existing `Member` rows (never creates one) and
never touches `last_login_at`, Battle.net fields, or roster confirmation (`src/sidequest_bot/plugins/member_profiles.py`).

### Announcements
Officers post announcements from the game (`discord: true` queues it for the bot) or from the site's Sync page; both
land in the same feed and also feed the site's news. The bot's `AnnouncementsCog` polls every guild with an
`announcement_channel_id` set every 2 minutes (`GET /api/guilds/{guild_id}/announcements/pending`, bot key), posts
one gold embed per pending announcement (title "Announcement", author = the announcement's author, description = its
text), and acks each one (`POST .../announcements/{id}/posted`) so it's never reposted; a failed ack or a Discord
send failure just logs and leaves the announcement pending for the next poll.
`/announcements channel [channel]` (Manage Server) sets or clears the channel (`src/sidequest_bot/plugins/announcements.py`).

### Hall of Heroes badges
Badges are Discord roles: members earn them on `sidequest-guild.com/heroes/` and the bot grants/announces the
matching role, so earning a badge shows up in Discord without anyone configuring a role manually. Every 5 minutes
(and once on ready), the bot calls `GET /api/guilds/{guild_id}/badges/sync`, which recomputes and awards badges
server-side and reports every linked member's current badge set plus any newly earned badges not yet announced. For
each badge below the bot creates (and, if deleted, recreates) a matching Discord role — not hoisted, not
mentionable, colored per the badge, left wherever Discord places a freshly created role (below the bot's own role,
above `@everyone`) so no other role is ever moved — storing the role id on `GuildConfig.badge_role_ids`. Each
member's badge roles are then reconciled to match exactly what the API reports: permanent badges are only ever
added, never removed, and **Top Helper** is a single moving title the bot adds to the new month's winner and
removes from the last one, both driven by the same reconciliation, with no special-casing in the bot. If a badge
channel is configured, each newly earned badge posts a gold embed (`<@member>` mention, badge name and
description, and a link to the Hall of Heroes) with mentions scoped to just that member, then the bot acks it
(`POST /api/guilds/{guild_id}/badges/ack`) so it isn't announced twice. If no channel is set yet, role sync still
runs every tick, but announcements are left un-acked so they post in full once a channel is configured instead of
being lost for whoever earned them first (`src/sidequest_bot/plugins/badges.py`).

Role icons (the server is boost tier 2, so roles can carry an image): every rank, gate, class and badge role has one,
generated with Codex and kept as 128px PNGs in `src/sidequest_bot/assets/role_icons/`. The bot gives a badge role its
icon when it creates or recreates it, sets it once on a badge role that has none, and attaches the badge's icon to
its announcement as the thumbnail. The other roles' icons were set once by hand from the same folder; level roles
have none.

| Badge | Rule |
|---|---|
| 👣 First Steps | Attended their first guild event. |
| ⚔️ First Raid | Attended their first raid. |
| 🛡️ Raid Regular | Attended 10 raids with the guild. |
| 🏅 Raid Veteran | Attended 25 raids with the guild. |
| 🌙 Perfect Month | Attended every raid the guild ran in a calendar month (at least 4 raids that month). |
| 🏰 Founding Member | Confirmed on the guild roster before launch. |
| 💬 Chatterbox | Reached chat level 10. |
| 🌟 Top Helper | Attended the most dungeon and leveling events last month. A moving monthly title. |

- `/badges channel [channel]` (Manage Server) — set or clear the announcement channel
- `/badges sync` (Manage Server) — run a sync now, with an ephemeral summary

## Role safety
Anything the bot hands out on its own (level rewards, the auto-role, role-menu buttons, link roles) goes through
`src/sidequest_bot/lib/roles.py`:

- **The role itself:** it must be below the bot's role, not managed by an integration, and free of moderator/admin
  permissions. This is re-checked right before every grant, so a role that later gains permissions stops being
  handed out.
- **Who configures it:** they need Manage Roles, and the role must sit below their own highest role.

## Deploying

A single `Dockerfile` (Python 3.15 slim + uv) builds the bot into an image whose default command is `sidequest-bot`;
it serves no HTTP port, so there's no `HEALTHCHECK`. `Procfile` declares one Cabotage process:

```
worker: sidequest-bot
```

Cabotage builds the image from the `Dockerfile` and runs the `Procfile`'s processes. Run **exactly one** `worker`
replica: two bots would double every Discord event push, badge announcement, and signup notice.

Environment (see the Contract above for the full variable list): `DISCORD_TOKEN`, `DISCORD_GUILD_ID` (optional),
`API_KEY` (must match the API's `API_KEY`), `API_URL` (reach the API over the public site, e.g.
`https://sidequest-guild.com`), `WEB_BASE_URL` (same).

**Deploy order:** deploy the API (from `SideQuest-Guild/website`) before a bot release that needs new API endpoints
or schema fields — the bot is a plain HTTP client with no migrations of its own, so it's safe to deploy standalone,
but a bot that calls a route the live API doesn't have yet will just get 404s until the API catches up.

```bash
docker build -t sidequest-bot .
docker run --rm -e DISCORD_TOKEN=... -e API_KEY=... -e API_URL=... -e WEB_BASE_URL=... sidequest-bot
```
