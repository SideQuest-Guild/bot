# Changelog

## v0.1.0 (2026-10-30)

### New

- Initial SideQuest guild platform
- Link Battle.net accounts and give officers a member list
- Two-way sync between site raids and Discord scheduled events
- Guild events with kinds and repeating schedules; rotating bot status line
- Live voice channel counts and online total in the Discord band
- Discord roles for site sign-in and Battle.net link
- Answer Alex's mentions, once every four hours
- Gate role for members who haven't signed in on the site
- Officers confirm members on the guild roster to open the member channels
- Class halls follow role holders into their forum post; role menus in guild gold
- Discord interest on event pages and an events forum post per event
- Event forum sign-up comments @-mention the member and quote their comment
- GM console API — members, activity log, moderation cases and an officer audit trail
- Events carry attendance EP, preset per kind with an officer override, plus a transmog kind
- Discord name, nickname, avatar and role changes reach the site; GM roster shows usernames
- New sign-ups post to officers with Approve and More info; deleting an account erases chat XP
- Deleting a site account strips the member's Discord roles and returns them to the gate
- New guests get a private welcome DM on sign-in, and a pinned Start here button shows it again
- Discord sign-ups, weekly board, calendar feed, away dates, recruiting, Hall of Heroes with badge roles, raid group builder, attendance report, online-now roster
- Role icons for every rank, class, gate and badge role; the bot keeps badge role icons and shows them on announcements
- EPGP loot ledger and guild hub synced from the SideQuest addon: raid sessions, boss kills, loot and EP/GP standings; roster, crafters, bank log, news, announcements to Discord, recruits and in-game sign-ups; Ledger and Guild pages, officers' sync console
- The Familiar role, which bots hold, gets a wind-up clockwork owl icon
- A WoW: Forever news feed posts Blizzard's blue posts and Wowhead's Forever news in a channel officers pick

### Fixed

- Keep link-role sync alive when the bot deploys before the API
- Keep the /linkroles description under Discord's 100-character limit
- An API response in the wrong shape is an APIError, so bot loops survive rolling deploys
- Approve on a signup notice confirms that member, not the clicking GM
