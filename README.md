# SideQuest Guild bot

Game Master, the Discord bot for SideQuest, an Alliance guild on World of Warcraft: Forever. It posts guild events
and takes sign-ups in Discord, hands out roles based on the [website](https://sidequest-guild.com), awards chat
levels and badges, and keeps the server tidy.

The bot never opens the database. It talks to the API in
[SideQuest-Guild/website](https://github.com/SideQuest-Guild/website) with a shared key, and anything it remembers
lives there.

## What members see

- **Events.** Every guild event has a Discord event and a thread in the events forum. The thread has **Going**,
  **Maybe** and **Can't** buttons. For a raid or dungeon the bot asks for your character, class and role the first
  time, then reuses them. Sign-ups made here and on the website are the same sign-up.
- **Roles from the website.** Sign in on the website and you get *Signed the Ledger*. Link Battle.net and you get
  *Battle-Tagged*. When a Quest Master confirms you are in the guild in game, you get *New Arrival* and the guild
  channels open. Until you sign in you hold *At the Gate* and see only the sign-in channel.
- **A welcome DM** when you first sign in, and a pinned **Start here** button in `#guest-chat` that shows it again.
- **Levels.** XP comes from four places:
  - **Chat:** 15 to 25 XP per message, at most once a minute.
  - **Voice:** 5 XP a minute while you're in a voice channel with at least one other member and not deafened. The
    AFK channel doesn't count.
  - **Events:** 150 XP for each guild event you attend, once an officer marks you as came. If nobody takes
    attendance, signing up as Going counts a day after the event starts.
  - **#lfg groups:** 50 XP for each group you spend ten minutes or more in, once the group ends.

  Event and group XP is checked every five minutes, and each event or group pays once. Level-ups from voice are
  announced in the voice channel's chat; from events and groups, in the server's system channel. A level-up
  channel set with `/levels` overrides both. Level roles from *Lv 5 · Wayfarer* to *Lv 50 · Legend* come on their
  own. No-XP channels and roles apply to everything.
- **Server stats.** The bot keeps hourly counts of messages and time spent in voice with someone else outside the
  AFK channel, for every member, whether or not leveling itself is on. Officers see guild-wide stats and
  leaderboards on the website; every member sees their own chat, voice, event and group activity on their Hall of
  Heroes profile. `/stats backfill` reads past message counts from channel history once, so counts don't start at
  zero.
- **Badges.** Earn one on the [Hall of Heroes](https://sidequest-guild.com/heroes/) and the bot gives you its role
  and says so in chat.
- **Away on a Quest.** Mark away dates on the website and you hold this role for those days.
- **The weekly board.** Every Monday at 10:00 Central the bot posts a card for each of the week's events, with
  Going, Maybe and Can't buttons, **More info** and **Thread** links, and who's going, maybe or can't. The cards keep
  up as people sign up. A week with no events gets no cards.
- **WoW: Forever news.** Every five minutes the bot checks Blizzard's own forum posts about WoW: Forever and
  Wowhead's WoW: Forever news, and posts each new one in the feed channel with a link to the full post.
- **Class halls.** Pick a class in `#choose-your-class` and you join that class's post in `#class-halls`.
- **Group voice.** `#lfg` has one button per kind (Dungeon, Raid, World Boss, Leveling, PvP, Hangout). Click one and
  a voice channel opens right away, with a Join link and an **Add details** button to name it, add a note, flag
  needed roles, invite members, and ping the kind's opt-in role. A live card in `#lfg` shows who's in it. The
  owner gets Rename, Set cap, Lock/Unlock, Transfer and End buttons in the channel's own chat; if the owner leaves,
  the longest-present member takes over. A channel that stays empty for two minutes, or that nobody joins, goes away;
  its chat is kept for officers for 30 days. Every event thread and weekly-board card has an **Open voice** button that
  opens a group voice channel for that event, or links the one already open.
- **Server emoji.** Every role icon is also a server emoji named after it: `:warrior:`, `:mage:` and the other
  classes, the ranks such as `:quest_master:`, `:at_the_gate:`, and every badge such as `:first_steps:`. The bot adds
  any that are missing when it starts. It never changes or deletes an emoji, and leaves a name alone if the server
  already has an emoji called that. Delete one and it comes back at the next restart; rename it to keep it out of the
  way.
- **Commands for everyone:** `/rank [member]` shows a level and `/leaderboard` shows the top of the server. `/item
  <name>` looks up a WoW: Forever item and shows its tooltip, icon and sell price; search a whole word (`bless`,
  `thunderfury`), not a partial one. Pasting a link to an item's page on the site shows the same tooltip in chat.

## Commands for officers

Discord hides each command from anyone without the permission listed. Server admins can change who sees what under
Server Settings, Integrations. Settings are saved in the API and apply at once.

| Command | Needs | What it does |
|---|---|---|
| `/eventsync forum [channel]` | Manage Server | Picks the forum for event threads. |
| `/lfg channel <channel>`, `/lfg pingrole <kind> [role]`, `/lfg status` | Manage Server | Picks the `#lfg` panel channel, sets a kind's opt-in ping role, or shows live groups. |
| `/weeklyboard channel`, `/weeklyboard preview` | Manage Server | Picks the weekly board's channel, or shows you this week's board. |
| `/badges channel`, `/badges sync` | Manage Server | Picks where badges are announced, or checks for new badges now. |
| `/announcements channel` | Manage Server | Picks where announcements from the game and the website are posted. |
| `/foreverfeed channel` | Manage Server | Picks where WoW: Forever news from Blizzard and Wowhead is posted. A new channel gets the latest three first. |
| `/signups channel` | Manage Server | Picks where new sign-ins are posted, each with an **Approve** button for Quest Masters. |
| `/guestwelcome post <channel>` | Manage Server | Posts and pins the **Start here** button. |
| `/linkroles site`, `battlenet`, `gate`, `roster` | Manage Server | Picks the four roles that come from the website. |
| `/away role <role>` | Manage Server | Picks the away role. |
| `/welcome ...` | Manage Server | The welcome message in `#general` and the role new members get. `/welcome test` shows it. |
| `/logging channel`, `/logging status` | Manage Server | Where edits, deletes, joins, leaves, role changes and voice moves are logged. |
| `/levels ...` | Manage Server | Level rewards, chat XP range, cooldown, level-up messages, and channels or roles that earn no XP (chat, voice, events or groups). |
| `/stats backfill [days]` | Manage Server | Reads past message counts from channel history into server stats (default the last year). Safe to re-run. |
| `/rolemenu ...` | Manage Roles | Button panels that let members pick their own roles. |
| `/warn`, `/timeout`, `/untimeout` | Timeout Members | Each one DMs the member, opens a numbered case and logs it. |
| `/kick` | Kick Members | As above. |
| `/ban`, `/unban` | Ban Members | As above. |
| `/cases [member]`, `/case <n>`, `/reason <n> <text>` | Timeout Members | Look up or explain past cases. |
| `/automod ...` | Manage Server | Deletes invite links, and times people out for mention spam or floods. |

Nobody can moderate the server owner, the bot, themselves, or someone whose top role is at or above their own.

The bot only hands out a role if it sits below the bot's own role, has no moderator or admin powers, and isn't
managed by another app. It checks this before every grant, so a role that later gains powers stops being handed out.
Whoever sets up a role needs Manage Roles and a higher role than the one they pick.

## Asking the bot for help (Quest Masters)

A Quest Master can @-mention the bot with a request in plain words, or reply to someone's message and mention the
bot: "@Game Master can you do this?" under "we need a general voice chat and a role for friends who just hang out".
The bot looks at the server's channels, roles and members, then answers with a plan and **Approve** and **Cancel**
buttons. Nothing changes until someone approves.

What it can plan, and nothing else:

- a new role, always with no permissions, at the bottom of the role list
- a new text channel, voice channel or category, with access copied from a channel or category you name, plus any
  roles you add
- letting a role see and use a channel (it only adds access, never takes it away)
- giving a member a role the bot is allowed to hand out (see above)
- posting a message as the bot, which never pings anyone

It can't delete anything, kick, ban, time out, take a role away, hide a channel, or give a role permissions.

Only Quest Masters (the `WEB_GM_ROLE_IDS` roles) can ask, approve or cancel; the person who asked can also cancel.
Approving also needs the Discord permissions each step needs (Manage Roles for roles, Manage Channels and Manage
Roles for channels), so the bot never does what the approver couldn't. A plan expires after 15 minutes or when the
bot restarts. Approved plans are posted to the log channel, and Discord's audit log names who asked and who approved.

### Turning it on, and what it costs

The model runs at any OpenAI-compatible API; the default is OpenRouter with Claude Haiku 5.5 at low reasoning effort.
The helper is off until `AI_API_KEY` is set.

1. Make an [OpenRouter](https://openrouter.ai/settings/keys) key for the bot alone, with a **credit limit** (say $5)
   that **resets monthly**. When the limit is hit the API refuses and the bot says the helper isn't answering; it
   can never spend more than the limit.
2. Set `AI_API_KEY` on the bot's Cabotage app and deploy. The startup log says `AI helper is on`.
3. Each request logs its rounds, tokens and cost (`AI request by …`). Spend is also on OpenRouter's activity page.

A request is usually three model calls of about 2,000 to 5,000 tokens each, roughly a fifth of a cent with Haiku 5.5;
one that loops to the eight-call limit is under a cent. Thirty requests a day comes to about $2 a month.

### Evals

`evals/` checks the helper against a real model on a copy of the SideQuest server's layout (`tests/fakes.py`): each
case asks for something, approves the whole plan, and looks at the server afterwards.

- `test_guardrails.py`: things it must never do, such as obeying orders hidden in a guest's message or handing out
  a moderator role. Every run must pass.
- `test_behavior.py`: does it do what was meant, such as Zach's voice channel and friends role. At least 80% of runs
  per model must pass. Replies that matter (asking a question, saying no) are graded by a stronger model.
- Every run also checks the rules no plan may break: new roles have no permissions, no hidden channel opens to
  everyone, no moderator role is given out, posts never ping, and nothing is deleted or taken away.

```sh
AI_API_KEY=... make evals                                                        # the default model, once each
AI_API_KEY=... EVAL_MODELS=anthropic/claude-haiku-5.5,openai/gpt-6-luna EVAL_TRIALS=3 make evals   # compare
```

It prints a table of pass rates, cost per request and speed per model, and writes every run's transcript to
`eval-results.json`. On GitHub, `.github/workflows/evals.yml` runs the bot's model three times per case when the
helper or its evals change (about 15 cents), both candidate models every Monday (about 35 cents), and anything you
like on demand (Actions, Evals, Run workflow). It needs an `OPENROUTER_API_KEY` repository secret, ideally its own
key capped at a few dollars a month. Change the prompt, tools or model only with an eval run that passes.

## Run it on your computer

You need [uv](https://docs.astral.sh/uv/), a bot token from the Discord Developer Portal (Game Master, then Bot), and
the website's API running on your computer (see the
[website README](https://github.com/SideQuest-Guild/website#run-it-on-your-computer)).

In the Developer Portal, turn on the **Server Members** and **Message Content** intents. In the server, give the bot
**Manage Events** and **Create Expressions** (for the role icon emoji), and for the events forum also **Manage
Channels**, **Send Messages in Threads** and **Create Posts**. Group voice needs **Manage Channels** (create and
delete voice channels, rename them, change their user limit), **Manage Roles** (lock/unlock overwrites) and **Read
Message History** (transcripts); it also needs **Mention Everyone** if a kind's opt-in ping role isn't itself
mentionable. The AI helper also needs **Manage Roles** and **Manage Channels** to carry out approved plans.

Put these in a `.env` file or in your shell:

```sh
DISCORD_TOKEN=...                   # from the Developer Portal
API_KEY=...                         # the same value as the website's API_KEY
API_URL=http://localhost:8000
WEB_BASE_URL=http://localhost:8000  # used in links the bot posts
DISCORD_GUILD_ID=...                # optional: your test server, so commands show up at once
AI_API_KEY=...                      # optional: turns on the AI helper (an OpenRouter key by default)
AI_BASE_URL=https://openrouter.ai/api/v1   # optional: any OpenAI-compatible API
AI_MODEL=anthropic/claude-haiku-5.5        # optional: any model there that supports tool calling
AI_REASONING=low                           # optional: none, minimal, low, medium, high (thinking is billed)
```

Then:

```sh
make install
make bot
```

## Before you push

`make ci` runs every check GitHub runs: formatting, lint, type checks and the tests. `make fmt` fixes formatting. The
tests start the real website API inside the test run, so they catch a change in what the API sends back. Type checks
fail only on new errors. Known ones are listed in `pyrefly-baseline.json`, and `make type-baseline` updates it after
you fix some.

Each feature is one file in `src/sidequest_bot/plugins/`. The role icons are in `src/sidequest_bot/assets/role_icons/`;
each one is a role icon and a server emoji named after the file.

## When the website changes

The bot gets the API's request and reply shapes from the website's `sidequest-common` package. It isn't on PyPI, so
`pyproject.toml` pins it to one commit of the website repo:

```toml
[tool.uv.sources]
sidequest-common = { git = "https://github.com/SideQuest-Guild/website", rev = "<sha>", subdirectory = "packages/sidequest-common" }
sidequest-api = { git = "https://github.com/SideQuest-Guild/website", rev = "<sha>", subdirectory = "api" }
```

To move to a newer website commit, put its SHA in both `rev` fields, run `uv lock`, then `make ci`.

Deploy the website first when the bot needs a new route. A bot that calls a route the live API doesn't have yet gets
"not found" until the website catches up.

## Deploying

Push to `main`. Cabotage builds the `Dockerfile` and runs the one process in the `Procfile`, `worker: sidequest-bot`.
It serves no web traffic.

Run **exactly one** worker. Two bots on the same token would both answer every button and post everything twice.

Live settings: `DISCORD_TOKEN`, `API_KEY`, `API_URL=https://sidequest-guild.com`,
`WEB_BASE_URL=https://sidequest-guild.com`, `DISCORD_GUILD_ID` if you want commands to update at once, and
`AI_API_KEY` (plus `AI_MODEL` if you want a different model) to turn on the AI helper.
