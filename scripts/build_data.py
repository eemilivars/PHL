#!/usr/bin/env python3
"""
Daily data build for the PHL lineup tool.

Pulls league-wide data from the NHL's public (unofficial, undocumented) APIs and
writes one JSON file the static site reads. The site does all the lineup maths
in the browser, so this script only collects and validates data.

Sources
  api.nhle.com/stats/rest/en   skater summary / realtime / faceoffwins, goalie summary,
                               team summary, team list
  api-web.nhle.com/v1          current team rosters (who plays where today), schedule

Output: site/data.json (schema documented in README.md, "data.json" section)

Exit code is non-zero if validation fails. The GitHub Action then stops before
deploying, so the site keeps yesterday's data instead of publishing broken data.
"""
import argparse
import datetime as dt
import json
import sys
import time
from zoneinfo import ZoneInfo

import requests

WEB = "https://api-web.nhle.com/v1"
STATS = "https://api.nhle.com/stats/rest/en"
ET = ZoneInfo("America/New_York")  # NHL and Yahoo game dates are Eastern Time

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "phl-lineup/1.0 (personal fantasy hockey tool)"

# Order of values in the compact per-player arrays written to data.json
SKATER_FIELDS = ["gp", "g", "a", "pm", "ppp", "gwg", "sog", "fw", "hit", "blk", "toi"]
GOALIE_FIELDS = ["gp", "gs", "w", "sv", "sa", "so"]

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print("WARNING:", msg, file=sys.stderr)


def get(url, params=None, tries=4):
    """GET JSON with retry and backoff on network errors, 429 and 5xx."""
    last = None
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
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


def skater_block(cayenne, aggregate=False, is_game=False):
    """Merge summary + realtime + faceoffwins into {playerId: {field: value}}."""
    out = {}
    for row in stats_report("skater", "summary", cayenne, aggregate, is_game):
        out[row["playerId"]] = {
            "name": row.get("skaterFullName"),
            "teams": row.get("teamAbbrevs"),
            "pos": row.get("positionCode"),
            "gp": row.get("gamesPlayed") or 0,
            "g": row.get("goals") or 0,
            "a": row.get("assists") or 0,
            "pm": row.get("plusMinus") or 0,
            "ppp": row.get("ppPoints") or 0,
            "gwg": row.get("gameWinningGoals") or 0,
            "sog": row.get("shots") or 0,
            "toi": round((row.get("timeOnIcePerGame") or 0) / 60, 2),  # seconds -> minutes
            "fw": 0, "hit": 0, "blk": 0,
        }
    for row in stats_report("skater", "realtime", cayenne, aggregate, is_game):
        p = out.get(row["playerId"])
        if p:
            p["hit"] = row.get("hits") or 0
            p["blk"] = row.get("blockedShots") or 0
    for row in stats_report("skater", "faceoffwins", cayenne, aggregate, is_game):
        p = out.get(row["playerId"])
        if p:
            p["fw"] = row.get("totalFaceoffWins") or 0
    return out


def goalie_block(cayenne, aggregate=False, is_game=False):
    out = {}
    for row in stats_report("goalie", "summary", cayenne, aggregate, is_game):
        out[row["playerId"]] = {
            "name": row.get("goalieFullName"),
            "teams": row.get("teamAbbrevs"),
            "gp": row.get("gamesPlayed") or 0,
            "gs": row.get("gamesStarted") or 0,
            "w": row.get("wins") or 0,
            "sv": row.get("saves") or 0,
            "sa": row.get("shotsAgainst") or 0,
            "so": row.get("shutouts") or 0,
        }
    return out


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
    # teams that existed last season but not this one (relocation) drop out once cur has data
    if any("cur" in v for v in out.values()):
        out = {k: v for k, v in out.items() if "cur" in v}
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


def pack(stats, fields):
    return [stats.get(f, 0) for f in fields] if stats else None


def build(now_utc):
    today = now_utc.astimezone(ET).date()
    cur, prev = season_ids(today)
    season_filter = lambda s: f"seasonId={s} and gameTypeId=2"

    teams = teams_block(cur, prev)
    rosters = rosters_block(sorted(teams))

    sk_cur, sk_prev = skater_block(season_filter(cur)), skater_block(season_filter(prev))
    gl_cur, gl_prev = goalie_block(season_filter(cur)), goalie_block(season_filter(prev))

    # Last 14 days. Optional: if the date-range query fails, the site falls back to season rates.
    lo, hi = today - dt.timedelta(days=14), today - dt.timedelta(days=1)
    rec_filter = f'gameDate<="{hi} 23:59:59" and gameDate>="{lo}" and gameTypeId=2'
    try:
        sk_rec = skater_block(rec_filter, aggregate=True, is_game=True)
        gl_rec = goalie_block(rec_filter, aggregate=True, is_game=True)
    except RuntimeError as e:
        warn(f"recent-form query failed, site will use season rates only: {e}")
        sk_rec, gl_rec = {}, {}

    monday = today - dt.timedelta(days=today.weekday())
    schedule = schedule_block(min(monday, today - dt.timedelta(days=1)), days=21)

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
        })

    return {
        "generatedAt": now_utc.isoformat(timespec="seconds"),
        "todayET": today.isoformat(),
        "season": cur,
        "prevSeason": prev,
        "recentWindow": [lo.isoformat(), hi.isoformat()],
        "skaterFields": SKATER_FIELDS,
        "goalieFields": GOALIE_FIELDS,
        "teams": teams,
        "games": schedule,
        "skaters": skaters,
        "goalies": goalies,
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
