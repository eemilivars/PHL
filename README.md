[README.md](https://github.com/user-attachments/files/32809855/README.md)
# PHL Lineup

Daily lineup helper for the Puistola Hockey League (Yahoo league 45520, 10 teams, head-to-head categories).

Every morning a GitHub Action pulls league-wide stats and the schedule from the NHL APIs, writes `site/data.json`, and publishes the site with GitHub Pages. The page does the lineup maths in the browser, so roster and weight changes apply instantly.

## Setup (about 10 minutes, once)

1. Create a **public** repository on GitHub (GitHub Pages is free for public repos) and upload the contents of this folder, keeping the structure:
   ```
   .github/workflows/daily.yml
   scripts/build_data.py
   site/index.html
   site/roster.json
   README.md
   ```
2. Settings > Pages > Build and deployment > Source: **GitHub Actions**.
3. Actions tab > "Daily data refresh" > **Run workflow**. The first run takes 1 to 2 minutes.
4. The site URL is shown on the run summary and in Settings > Pages (`https://<user>.github.io/<repo>/`). Add it to your phone's home screen.

If a run fails, GitHub emails you. The site keeps the last good data until the next successful run.

## Daily use

1. **Roster tab**: keep your 16 players and their status (Healthy, DTD, Out, IR) current. Tick **Active** for the players you have in Yahoo's active slots.
2. **Lineup tab**: pick the day. The top box lists the swaps to make in Yahoo. After making them, press "Save this as my active lineup".
3. **Week plan**: shows the week's games per player, recommended starts, and days with empty or overbooked slots.
4. **Categories**: season totals, strength against an average team, category weights, and pickup candidates per category.

Game days are Eastern Time, which is what Yahoo uses. "Tonight" games start in the Finnish night.

The roster is saved in the browser. To use it on another device, export it on the Roster tab and replace `site/roster.json` in the repository. The push redeploys the site.

## How the recommendations work

**Who starts.** For each position group (F 6, D 4, G 2) the tool starts healthy players who have a game, ranked by projected value for that game. A player without a game never takes a slot from a player with one, because categories are weekly totals and every game played adds to them. Out and IR players are never started. DTD players can start but are ranked 1.5 value points lower.

**Per-game rates (skaters).** For each of G, A, +/-, PPP, GWG, SOG, FW, HIT, BLK:
- This season's totals are blended with a prior of 15 games at last season's rate, so early-season samples don't dominate.
- A player with little or no NHL history gets a prior of 60% of an average fantasy-relevant player at his position.
- If he has played 3 or more games in the last 14 days, that stretch gets 30% weight.

**Opponent adjustment (the edge over Yahoo's default).** Each game's rates are scaled by who the player faces:
- G, A, PPP, GWG: opponent goals against per game relative to league average, to the power 0.7.
- SOG: opponent shots against per game, to the power 0.7.
- BLK: opponent shots for per game (more shots against you means more to block), to the power 0.6.
- +/-: adds 0.25 × (opponent goals against minus league average, minus opponent goals for minus league average).
- Home ice +2% (road −1.5%). Opponent on a back-to-back +4%. Own team on a back-to-back −3%.
- Team rates blend this season with last season until about 10 games.

**Goalies.**
- Start chance comes from the goalie's share of team starts, blended with last season. On the second night of a back-to-back, a starter's chance halves and a backup's rises.
- Win chance comes from the two teams' points percentage, home ice, back-to-backs and the goalie's save percentage.
- Saves are estimated as start chance × opponent shots per game × save percentage.
- Shutout chance rises against low-scoring teams.

**Value.**
- Each category is converted to standard deviations from an average fantasy starter.
- The average starter is measured over about 190 skaters, roughly what a 10-team league rosters plus depth, and the top 40 goalies by start share.
- Values are multiplied by your category weights and summed.
- SV% counts only in proportion to the chance the goalie starts.

**Category strength.** Your best 10 skaters' per-game rates are compared with the league's top 100 skaters by value, which is roughly what an average team's best 10 are. Goalies are compared the same way (your top 2 against the top 20). This uses the whole roster, not the lineup you set.

**Specialists.** Skaters ranked by per-game rate in one category. The 140 highest-value skaters are hidden by default because a 10-team league rosters about that many. The tool does not know other fantasy rosters, so always check availability in Yahoo.

## Data sources

All endpoints are public, unofficial and undocumented. Community reference: https://github.com/Zmalski/NHL-API-Reference

| Data | Endpoint |
|---|---|
| Skater season stats | `api.nhle.com/stats/rest/en/skater/summary`, `/realtime` (hits, blocks), `/faceoffwins` |
| Goalie season stats | `api.nhle.com/stats/rest/en/goalie/summary` |
| Last 14 days | Same reports with `isAggregate=true`, `isGame=true` and a `gameDate` range |
| Team rates | `api.nhle.com/stats/rest/en/team/summary`, team codes from `/en/team` |
| Current NHL team of each player | `api-web.nhle.com/v1/roster/{TEAM}/current` (handles trades and call-ups) |
| Schedule | `api-web.nhle.com/v1/schedule/{date}` (3 weeks from this Monday) |

About 45 requests per run.

## data.json

| Field | Meaning |
|---|---|
| `generatedAt` | UTC time of the build. The page warns if it is more than 36 hours old |
| `todayET` | Build date in Eastern Time |
| `season`, `prevSeason` | Season ids, for example `20262027` |
| `recentWindow` | First and last date of the 14-day form window |
| `skaterFields` | Order of values in skater arrays: gp, g, a, pm, ppp, gwg, sog, fw, hit, blk, toi (minutes per game) |
| `goalieFields` | Order of values in goalie arrays: gp, gs, w, sv, sa, so |
| `teams.{ABBR}` | `name`, and `cur` / `prev` objects with gp, gf, ga, sf, sa (per game) and pts (points %) |
| `games[]` | `id`, `d` (ET date), `h` home, `a` away, `t` start time UTC. Regular season only |
| `skaters[]` | `id` (NHL player id), `n` name, `t` current team, `p` position (C, L, R, D), `cur` / `prev` / `rec` stat arrays or null |
| `goalies[]` | Same shape with goalie arrays |
| `warnings[]` | Non-fatal problems from the build, shown in the page footer |

## Maintenance

| When | What |
|---|---|
| Every add, drop or trade | Roster tab (and export to `roster.json` if you use several devices) |
| Injury news | Status on the Roster tab |
| Each September | Nothing. The season id switches automatically on 1 September |
| If a run fails | Open the failed run's log. `VALIDATION ERROR` lines say what looked wrong. Check the community reference for changed endpoints or field names |

## Known limits

- **The NHL can change these endpoints without notice.** Validation stops a broken build from being published, but a fix needs a code change.
- **The last-14-days query is the least certain part.** If it fails, the build logs a warning and the page uses season rates only.
- **No starting goalie confirmations.** Start chance is an estimate from usage patterns. Check morning skate reports for confirmed starters on close calls.
- **No injury feed.** Status is manual.
- **Yahoo positions.** F slots take any forward. Yahoo's own C/LW/RW eligibility does not matter in this league, so the tool uses the NHL position only to split F from D.
- **The first two weeks lean on last season** by design. Rookies start from a conservative prior.
- **Scheduled runs need repository activity.** GitHub disables scheduled workflows in public repos after 60 days without activity. The workflow commits a small `.heartbeat` file on the 1st and 15th to prevent this. If the schedule ever stops, re-enable it in the Actions tab.
- **Testing.** Open `site/index.html?today=YYYY-MM-DD` to view the page as if it were another date.
