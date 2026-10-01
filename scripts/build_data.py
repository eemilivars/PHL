#!/usr/bin/env python3
"""
Daily data build for the PHL lineup tool.

Pulls league-wide data from the NHL's public (unofficial, undocumented) APIs and
writes one JSON file the static site reads. The site does all the lineup maths
in the browser, so this script only collects and validates data.

Sources
  api.nhle.com/stats/rest/en   skater summary / realtime / faceoffwins, goalie summary,
                               team summary, team list
  api-web.nhle.com/v1          current team rosters (who plays where today), schedule,
                               player game logs (fallback for history)
  site/roster.json             the players whose game-by-game history is stored

Output: site/data.json (schema documented in README.md, "data.json" section)
Requests per run: about 110 (32 rosters, 3 schedule weeks, ~30 league stats reports,
about 3 per rostered skater and 1 per rostered goalie for history)

Exit code is non-zero if validation fails. The GitHub Action then stops before
deploying, so the site keeps yesterday's data instead of publishing broken data.
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

WEB = "https://api-web.nhle.com/v1"
STATS = "https://api.nhle.com/stats/rest/en"
ET = ZoneInfo("America/New_York")  # NHL and Yahoo game dates are Eastern Time
ROSTER_FILE = Path(__file__).resolve().parent.parent / "site" / "roster.json"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "phl-lineup/1.0 (personal fantasy hockey tool)"

# Order of values in the compact per-player arrays written to data.json
SKATER_FIELDS = ["gp", "g", "a", "pm", "ppp", "gwg", "sog", "fw", "hit", "blk", "toi"]
GOALIE_FIELDS = ["gp", "gs", "w", "sv", "sa", "so"]
# Per-game history rows: [date, *fields]. Skaters drop time on ice.
HIST_SKATER_FIELDS = SKATER_FIELDS[:-1]
HIST_GOALIE_FIELDS = GOALIE_FIELDS

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print("WARNING:", msg, file=sys.stderr, flush=True)


def get(url, params=None, tries=4):
    """GET JSON with retry and backoff on network errors, 429 and 5xx."""
    last = None
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200].strip()}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * 2 ** i)
    raise RuntimeError(f"GET failed after {tries} tries: {url} {params or ''} ({last})")


def season_ids(today):
    """Current season id flips in September, when preseason starts."""
    y = today.year if today.month >= 9 else today.year - 1
    return f"{y}{y + 1}", f"{y - 1}{y}"


def stats_report(kind, report, cayenne, aggregate=False, is_game=False):
    params = {
        "isAggregate": str(aggregate).lower(),
        "isGame": str(is_game).lower(),
        "start": 0,
        "limit": -1,
        "cayenneExp": cayenne,
    }
    return get(f"{STATS}/{kind}/{report}", params).get("data", [])


# ---------------------------------------------------------------------------
# Folding report rows into per-player totals.
#
# Rows are ADDED per player, never overwritten. That makes the result correct whether the
# API returns one row per player (season or date-range totals), one row per team stint for
# a traded player, or one row per game.
# ---------------------------------------------------------------------------
def games_in(row):
    gp = row.get("gamesPlayed")
    if gp is not None:
        return gp
    return 1 if ("gameId" in row or "gameDate" in row) else 0


def row_date(row):
    return str(row.get("gameDate") or "")[:10]


def fetch_skater_rows(cayenne, aggregate=False, is_game=False):
    return {rep: stats_report("skater", rep, cayenne, aggregate, is_game)
            for rep in ("summary", "realtime", "faceoffwins")}


def fold_skaters(rows, keep=None):
    """{playerId: totals} from summary + realtime + faceoffwins rows. keep(row) filters rows."""
    out = {}
    for row in rows["summary"]:
        if keep and not keep(row):
            continue
        a = out.setdefault(row["playerId"], {"name": None, "teams": None, "pos": None, "gp": 0, "g": 0, "a": 0,
                                             "pm": 0, "ppp": 0, "gwg": 0, "sog": 0, "fw": 0, "hit": 0, "blk": 0,
                                             "_toi": 0.0})
        gp = games_in(row)
        a["name"] = row.get("skaterFullName") or a["name"]
        a["teams"] = row.get("teamAbbrevs") or row.get("teamAbbrev") or a["teams"]
        a["pos"] = row.get("positionCode") or a["pos"]
        a["gp"] += gp
        for k, f in (("g", "goals"), ("a", "assists"), ("pm", "plusMinus"), ("ppp", "ppPoints"),
                     ("gwg", "gameWinningGoals"), ("sog", "shots")):
            a[k] += row.get(f) or 0
        a["_toi"] += (row.get("timeOnIcePerGame") or 0) * gp
    for row in rows["realtime"]:
        a = out.get(row["playerId"])
        if a and (not keep or keep(row)):
            a["hit"] += row.get("hits") or 0
            a["blk"] += row.get("blockedShots") or 0
    for row in rows["faceoffwins"]:
        a = out.get(row["playerId"])
        if a and (not keep or keep(row)):
            a["fw"] += row.get("totalFaceoffWins") or 0
    for a in out.values():
        toi = a.pop("_toi")
        a["toi"] = round(toi / a["gp"] / 60, 2) if a["gp"] else 0  # seconds -> minutes per game
    return out


def fold_goalies(rows, keep=None):
    out = {}
    for row in rows:
        if keep and not keep(row):
            continue
        a = out.setdefault(row["playerId"], {"name": None, "teams": None, "gp": 0, "gs": 0, "w": 0,
                                             "sv": 0, "sa": 0, "so": 0})
        a["name"] = row.get("goalieFullName") or a["name"]
        a["teams"] = row.get("teamAbbrevs") or row.get("teamAbbrev") or a["teams"]
        a["gp"] += games_in(row)
        a["gs"] += row.get("gamesStarted") or 0
        a["w"] += row.get("wins") or 0
        a["sv"] += row.get("saves") or 0
        a["sa"] += row.get("shotsAgainst") or 0
        a["so"] += row.get("shutouts") or 0
    return out


def skater_block(cayenne, aggregate=False, is_game=False):
    return fold_skaters(fetch_skater_rows(cayenne, aggregate, is_game))


def goalie_block(cayenne, aggregate=False, is_game=False):
    return fold_goalies(stats_report("goalie", "summary", cayenne, aggregate, is_game))


# ---------------------------------------------------------------------------
def teams_block(cur, prev):
    """32 active teams with per-game rates for the current and previous season."""
    tri = {t["id"]: t for t in get(f"{STATS}/team").get("data", [])}
    out = {}
    for season, key in ((prev, "prev"), (cur, "cur")):
        rows = stats_report("team", "summary", f"seasonId={season} and gameTypeId=2")
        for r in rows:
            t = tri.get(r["teamId"])
            if not t:
                continue
            abbr = t["triCode"]
            rec = out.setdefault(abbr, {"name": t.get("fullName") or r.get("teamFullName")})
            rec[key] = {
                "gp": r.get("gamesPlayed") or 0,
                "gf": r.get("goalsForPerGame"),
                "ga": r.get("goalsAgainstPerGame"),
                "sf": r.get("shotsForPerGame"),
                "sa": r.get("shotsAgainstPerGame"),
                "pts": r.get("pointPct"),
            }
    return out


def rosters_block(abbrevs):
    """Current NHL team for every rostered player (handles trades and call-ups)."""
    out = {}
    for abbr in abbrevs:
        try:
            r = get(f"{WEB}/roster/{abbr}/current")
        except RuntimeError as e:
            warn(f"roster {abbr}: {e}")
            continue
        for group in ("forwards", "defensemen", "goalies"):
            for p in r.get(group, []):
                out[p["id"]] = {
                    "team": abbr,
                    "pos": p.get("positionCode"),
                    "name": f'{p["firstName"]["default"]} {p["lastName"]["default"]}',
                }
    return out


def schedule_block(start, days=21):
    games = {}
    d = start
    while d < start + dt.timedelta(days=days):
        data = get(f"{WEB}/schedule/{d.isoformat()}")
        for day in data.get("gameWeek", []):
            for g in day.get("games", []):
                if g.get("gameType") != 2:
                    continue
                games[g["id"]] = {
                    "id": g["id"],
                    "d": day["date"],
                    "h": g["homeTeam"]["abbrev"],
                    "a": g["awayTeam"]["abbrev"],
                    "t": g.get("startTimeUTC"),
                }
        d += dt.timedelta(days=7)
    return sorted(games.values(), key=lambda g: (g["d"], g["t"] or ""))


# ---------------------------------------------------------------------------
# League-wide windows (last 7, 14, 30 days)
# ---------------------------------------------------------------------------
WINDOWS = {"r7": 7, "rec": 14, "r30": 30}


def window_blocks(today):
    """Per-player totals over the last N days for every window in WINDOWS.

    Plan A: one pull of per-game rows for the longest window, then each window is summed
            locally by game date. Needs rows that carry gameDate.
    Plan B: one aggregated query per window (the API sums the dates). Used only if Plan A
            gets rows without dates or fails. Rejected if any player shows more games than
            days in the window, which means the date filter was ignored.
    """
    hi = today - dt.timedelta(days=1)
    meta = {k: [(today - dt.timedelta(days=n)).isoformat(), hi.isoformat()] for k, n in WINDOWS.items()}
    out = {k: ({}, {}) for k in WINDOWS}
    lo_all = today - dt.timedelta(days=max(WINDOWS.values()))
    cayenne = f'gameDate<="{hi} 23:59:59" and gameDate>="{lo_all}" and gameTypeId=2'

    sk_rows, gl_rows = None, []
    try:
        sk_rows = fetch_skater_rows(cayenne, aggregate=False, is_game=True)
        gl_rows = stats_report("goalie", "summary", cayenne, False, True)
    except RuntimeError as e:
        warn(f"per-game window pull failed, trying aggregated windows: {e}")
        sk_rows = None
    per_game = bool(sk_rows and sk_rows["summary"] and "gameDate" in sk_rows["summary"][0])
    if sk_rows is not None:
        print(f"Window pull (per-game form): {len(sk_rows['summary'])} skater rows, {len(gl_rows)} goalie rows, "
              f"rows carry gameDate: {per_game}", flush=True)

    if per_game:
        for key, (lo_s, hi_s) in meta.items():
            def keep(r, lo_s=lo_s, hi_s=hi_s):
                return lo_s <= row_date(r) <= hi_s
            out[key] = (fold_skaters(sk_rows, keep), fold_goalies(gl_rows, keep))
    else:
        for key, days in WINDOWS.items():
            lo_s, hi_s = meta[key]
            cay = f'gameDate<="{hi_s} 23:59:59" and gameDate>="{lo_s}" and gameTypeId=2'
            for aggregate in (True, False):
                try:
                    sk, gl = skater_block(cay, aggregate, False), goalie_block(cay, aggregate, False)
                except RuntimeError as e:
                    warn(f"{days}-day window (isAggregate={aggregate}) failed: {e}")
                    continue
                most = max([v["gp"] for v in sk.values()] + [v["gp"] for v in gl.values()] + [0])
                if most > days:
                    warn(f"{days}-day window (isAggregate={aggregate}) ignored the date filter "
                         f"(max {most} GP); discarded")
                    continue
                out[key] = (sk, gl)
                break

    for key, days in WINDOWS.items():
        sk, gl = out[key]
        most = max([v["gp"] for v in sk.values()] + [0])
        print(f"Window {days:>2} days {meta[key][0]} to {meta[key][1]}: {len(sk)} skaters, {len(gl)} goalies, "
              f"most games {most}", flush=True)
        if not sk:
            warn(f"{days}-day window has no skater data; the site greys it out")
    return out, meta


# ---------------------------------------------------------------------------
# Game-by-game history for the players in site/roster.json
# ---------------------------------------------------------------------------
def roster_ids():
    try:
        j = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
        return [int(p["id"]) for p in j.get("players", [])]
    except (OSError, ValueError, KeyError, TypeError) as e:
        warn(f"could not read {ROSTER_FILE}: {e}. No game history will be stored")
        return []


def _game_key(row):
    return row.get("gameId") or row_date(row)


def skater_history_stats(pid, season):
    """Per-game rows from the stats API (all 9 categories). None if the API gave no usable rows."""
    cay = f"playerId={pid} and seasonId={season} and gameTypeId=2"
    reps = {rep: stats_report("skater", rep, cay, False, True) for rep in ("summary", "realtime", "faceoffwins")}
    if not reps["summary"] or not row_date(reps["summary"][0]):
        return None
    games = {}
    for r in reps["summary"]:
        g = games.setdefault(_game_key(r), {"d": row_date(r), "g": 0, "a": 0, "pm": 0, "ppp": 0, "gwg": 0,
                                            "sog": 0, "fw": 0, "hit": 0, "blk": 0})
        g["g"] += r.get("goals") or 0
        g["a"] += r.get("assists") or 0
        g["pm"] += r.get("plusMinus") or 0
        g["ppp"] += r.get("ppPoints") or 0
        g["gwg"] += r.get("gameWinningGoals") or 0
        g["sog"] += r.get("shots") or 0
    for r in reps["realtime"]:
        g = games.get(_game_key(r))
        if g:
            g["hit"] += r.get("hits") or 0
            g["blk"] += r.get("blockedShots") or 0
    for r in reps["faceoffwins"]:
        g = games.get(_game_key(r))
        if g:
            g["fw"] += r.get("totalFaceoffWins") or 0
    return [[g["d"], 1, g["g"], g["a"], g["pm"], g["ppp"], g["gwg"], g["sog"], g["fw"], g["hit"], g["blk"]]
            for g in sorted(games.values(), key=lambda g: g["d"])]


def skater_history_gamelog(pid, season):
    """Fallback: NHL web game log. Has no hits, blocks or faceoff wins, so those stay 0."""
    data = get(f"{WEB}/player/{pid}/game-log/{season}/2")
    return [[str(g["gameDate"])[:10], 1, g.get("goals") or 0, g.get("assists") or 0, g.get("plusMinus") or 0,
             g.get("powerPlayPoints") or 0, g.get("gameWinningGoals") or 0, g.get("shots") or 0, 0, 0, 0]
            for g in sorted(data.get("gameLog", []), key=lambda g: g["gameDate"])]


def goalie_history_stats(pid, season):
    cay = f"playerId={pid} and seasonId={season} and gameTypeId=2"
    rows = stats_report("goalie", "summary", cay, False, True)
    if not rows or not row_date(rows[0]):
        return None
    games = {}
    for r in rows:
        g = games.setdefault(_game_key(r), [row_date(r), 1, 0, 0, 0, 0, 0])
        g[2] += r.get("gamesStarted") or 0
        g[3] += r.get("wins") or 0
        g[4] += r.get("saves") or 0
        g[5] += r.get("shotsAgainst") or 0
        g[6] += r.get("shutouts") or 0
    return sorted(games.values(), key=lambda g: g[0])


def goalie_history_gamelog(pid, season):
    data = get(f"{WEB}/player/{pid}/game-log/{season}/2")
    rows = []
    for g in sorted(data.get("gameLog", []), key=lambda g: g["gameDate"]):
        sa, ga = g.get("shotsAgainst") or 0, g.get("goalsAgainst") or 0
        rows.append([str(g["gameDate"])[:10], 1, g.get("gamesStarted") or 0, 1 if g.get("decision") == "W" else 0,
                     max(0, sa - ga), sa, g.get("shutouts") or 0])
    return rows


def history_block(pids, season, today, rosters, sk_cur, gl_cur, gl_prev):
    players = {}
    for pid in pids:
        info = rosters.get(pid)
        is_goalie = (info and info["pos"] == "G") or (not info and (pid in gl_cur or pid in gl_prev))
        if not info and pid not in sk_cur and not is_goalie:
            warn(f"history: player {pid} is not on an NHL roster and has no stats; skipped")
            continue
        played = ((gl_cur if is_goalie else sk_cur).get(pid) or {}).get("gp", 0)
        primary = goalie_history_stats if is_goalie else skater_history_stats
        fallback = goalie_history_gamelog if is_goalie else skater_history_gamelog
        rows, partial = None, False
        try:
            rows = primary(pid, season)
        except RuntimeError as e:
            warn(f"history {pid}: stats API failed ({e})")
        if rows is None or (not rows and played):
            try:
                rows = fallback(pid, season)
                partial = bool(rows) and not is_goalie
                if partial:
                    warn(f"history {pid}: used web game log, so hits, blocks and faceoff wins are missing")
            except RuntimeError as e:
                warn(f"history {pid}: game log failed too ({e})")
                rows = []
        entry = {"k": "G" if is_goalie else "S", "rows": rows or []}
        if partial:
            entry["partial"] = True
        players[str(pid)] = entry
    games = sum(len(p["rows"]) for p in players.values())
    print(f"History: {len(players)} of {len(pids)} roster players, {games} player-games", flush=True)
    return {"asOf": (today - dt.timedelta(days=1)).isoformat(),
            "skaterFields": HIST_SKATER_FIELDS, "goalieFields": HIST_GOALIE_FIELDS, "players": players}


def pack(stats, fields):
    return [stats.get(f, 0) for f in fields] if stats else None


def build(now_utc):
    today = now_utc.astimezone(ET).date()
    cur, prev = season_ids(today)
    season_filter = lambda s: f"seasonId={s} and gameTypeId=2"

    print(f"Build date {today} ET, season {cur}, previous {prev}", flush=True)
    teams = teams_block(cur, prev)
    print(f"Team list: {len(teams)} clubs with stats", flush=True)
    rosters = rosters_block(sorted(teams))
    # Keep only clubs that exist today: a defunct or relocated club has no current roster.
    # (Do not filter on current-season stats: early in the season most teams have none yet.)
    active = {v["team"] for v in rosters.values()}
    teams = {k: v for k, v in teams.items() if k in active}

    sk_cur, sk_prev = skater_block(season_filter(cur)), skater_block(season_filter(prev))
    gl_cur, gl_prev = goalie_block(season_filter(cur)), goalie_block(season_filter(prev))
    print(f"Skaters: {len(sk_cur)} this season, {len(sk_prev)} last. Goalies: {len(gl_cur)} / {len(gl_prev)}. "
          f"Active clubs: {len(teams)}", flush=True)

    # Last 7 / 14 / 30 days, league-wide. A window that fails is left empty and the site greys it
    # out. "rec" (14 days) also feeds the projection model's recent-form weight.
    win, win_meta = window_blocks(today)
    (sk_r7, gl_r7), (sk_rec, gl_rec), (sk_r30, gl_r30) = win["r7"], win["rec"], win["r30"]
    lo, hi = win_meta["rec"]

    monday = today - dt.timedelta(days=today.weekday())
    schedule = schedule_block(min(monday, today - dt.timedelta(days=1)), days=21)

    history = history_block(roster_ids(), cur, today, rosters, sk_cur, gl_cur, gl_prev)

    def team_of(pid, *blocks):
        if pid in rosters:
            return rosters[pid]["team"]
        for b in blocks:
            if pid in b and b[pid].get("teams"):
                return b[pid]["teams"].split(",")[-1].strip()
        return None

    skaters, goalies = [], []
    skater_ids = set(sk_cur) | {p for p, v in sk_prev.items() if v["gp"] >= 5} | \
        {p for p, v in rosters.items() if v["pos"] != "G"}
    for pid in sorted(skater_ids):
        c, pv, rc = sk_cur.get(pid), sk_prev.get(pid), sk_rec.get(pid)
        info = rosters.get(pid, {})
        name = info.get("name") or (c or pv or {}).get("name")
        pos = info.get("pos") or (c or pv or {}).get("pos")
        if not name or pos == "G":
            continue
        skaters.append({
            "id": pid, "n": name, "t": team_of(pid, sk_cur, sk_prev), "p": pos,
            "cur": pack(c, SKATER_FIELDS), "prev": pack(pv, SKATER_FIELDS), "rec": pack(rc, SKATER_FIELDS),
            "r7": pack(sk_r7.get(pid), SKATER_FIELDS), "r30": pack(sk_r30.get(pid), SKATER_FIELDS),
        })
    goalie_ids = set(gl_cur) | {p for p, v in gl_prev.items() if v["gp"] >= 3} | \
        {p for p, v in rosters.items() if v["pos"] == "G"}
    for pid in sorted(goalie_ids):
        c, pv, rc = gl_cur.get(pid), gl_prev.get(pid), gl_rec.get(pid)
        name = rosters.get(pid, {}).get("name") or (c or pv or {}).get("name")
        if not name:
            continue
        goalies.append({
            "id": pid, "n": name, "t": team_of(pid, gl_cur, gl_prev),
            "cur": pack(c, GOALIE_FIELDS), "prev": pack(pv, GOALIE_FIELDS), "rec": pack(rc, GOALIE_FIELDS),
            "r7": pack(gl_r7.get(pid), GOALIE_FIELDS), "r30": pack(gl_r30.get(pid), GOALIE_FIELDS),
        })

    return {
        "generatedAt": now_utc.isoformat(timespec="seconds"),
        "todayET": today.isoformat(),
        "season": cur,
        "prevSeason": prev,
        "recentWindow": [lo, hi],
        "windows": win_meta,          # {"r7": [from, to], "rec": [...], "r30": [...]}
        "skaterFields": SKATER_FIELDS,
        "goalieFields": GOALIE_FIELDS,
        "teams": teams,
        "games": schedule,
        "skaters": skaters,
        "goalies": goalies,
        "history": history,
        "warnings": WARNINGS,
    }


def validate(data):
    """Stop the deploy if the NHL changed something we depend on."""
    errors = []
    today = dt.date.fromisoformat(data["todayET"])
    in_season = (today.month >= 10 or today.month <= 4)
    if not 30 <= len(data["teams"]) <= 34:
        errors.append(f"expected 32 teams, got {len(data['teams'])}")
    prev_sk = sum(1 for s in data["skaters"] if s["prev"])
    if prev_sk < 500:
        errors.append(f"only {prev_sk} skaters with previous-season stats (expected 500+)")
    if in_season and not data["games"]:
        errors.append("no regular-season games in the 3-week schedule window")
    after_opening = today >= dt.date(today.year if today.month >= 10 else today.year - 1, 10, 20)
    if in_season and after_opening and sum(1 for s in data["skaters"] if s["cur"]) < 300:
        errors.append("fewer than 300 skaters with current-season stats after 20 Oct")
    with_team = sum(1 for s in data["skaters"] if s["t"])
    if with_team < 0.8 * len(data["skaters"]):
        errors.append(f"only {with_team}/{len(data['skaters'])} skaters have a team")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data.json")
    args = ap.parse_args()
    data = build(dt.datetime.now(dt.timezone.utc))
    errors = validate(data)
    if errors:
        for e in errors:
            print("VALIDATION ERROR:", e, file=sys.stderr)
        sys.exit(1)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {args.out}: {len(data['skaters'])} skaters, {len(data['goalies'])} goalies, "
          f"{len(data['games'])} games, {len(WARNINGS)} warnings")


if __name__ == "__main__":
    main()
