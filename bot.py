import logging
import os
import requests
import json
import time
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue
from dotenv import load_dotenv
from groq import Groq
from google import genai
from google.genai import types

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
SPORTSDB_KEY = "123"

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(level=logging.INFO)
DATA_FILE = "ace1000_data.json"

_cache = {}
CACHE_DURATION = 1800
WAT_OFFSET = timedelta(hours=1)

def cache_get(key):
    if key in _cache:
        data, timestamp = _cache[key]
        if time.time() - timestamp < CACHE_DURATION:
            return data
    return None

def cache_set(key, data):
    _cache[key] = (data, time.time())

FOOTBALL_DATA_COMPS = [
    ("PL", "EPL"), ("CL", "Champions League"), ("PD", "La Liga"),
    ("SA", "Serie A"), ("BL1", "Bundesliga"), ("FL1", "Ligue 1"), ("ELC", "Championship"),
]

ALL_ODDS_SPORTS = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_turkey_super_league", "soccer_brazil_campeonato",
    "soccer_argentina_primera_division", "soccer_mexico_ligamx",
    "soccer_england_efl_champ", "soccer_conmebol_copa_libertadores",
]

SPORTSDB_LEAGUES = [
    ("4328", "EPL"), ("4335", "Champions League"), ("4329", "Bundesliga"),
    ("4334", "La Liga"), ("4331", "Serie A"), ("4332", "Ligue 1"),
    ("4346", "Brazilian Serie A"), ("4424", "Argentine Primera"),
    ("4480", "MLS"), ("4344", "NPFL Nigeria"),
]

def get_now_utc():
    return datetime.now(timezone.utc)

def get_now_wat():
    return get_now_utc() + WAT_OFFSET

def get_today_str():
    return get_now_wat().strftime("%Y-%m-%d")

def to_wat(utc_str):
    try:
        if not utc_str:
            return "Time TBC"
        if "T" in utc_str:
            dt_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        else:
            clean = utc_str.strip()[:16]
            dt_utc = datetime.strptime(clean, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        dt_wat = dt_utc + WAT_OFFSET
        return dt_wat.strftime("%a %d %b, %I:%M %p")
    except Exception as e:
        logging.error(f"Time conversion error: {e} for {utc_str}")
        return str(utc_str)

def is_future_match(kickoff_str):
    try:
        if not kickoff_str:
            return False
        if "T" in kickoff_str:
            dt_utc = datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))
        else:
            clean = kickoff_str.strip()[:16]
            dt_utc = datetime.strptime(clean, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return dt_utc > get_now_utc() + timedelta(minutes=5)
    except Exception as e:
        logging.error(f"is_future_match error: {e} for {kickoff_str}")
        return False

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"name": "Champ", "bankroll": 0, "initial_bankroll": 0, "bets": [], "bankroll_history": [], "chat_id": None}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def format_naira(amount):
    return f"₦{amount:,.0f}"

def get_name():
    return load_data().get("name", "Champ")

def stake_breakdown(bankroll):
    if bankroll == 0:
        return "⚠️ Set your bankroll first with /bankroll [amount]"
    return (
        f"💰 BANKROLL: {format_naira(bankroll)}\n"
        f"2% = {format_naira(bankroll * 0.02)}\n"
        f"3% = {format_naira(bankroll * 0.03)}\n"
        f"5% = {format_naira(bankroll * 0.05)}\n"
        f"10% = {format_naira(bankroll * 0.10)}"
    )

def get_staking_strategy(data):
    bankroll = data.get("bankroll", 0)
    initial = data.get("initial_bankroll", 0)
    bets = data.get("bets", [])
    won = len([b for b in bets if b.get("result") == "win"])
    lost = len([b for b in bets if b.get("result") == "loss"])
    total_decided = won + lost
    win_rate = (won / total_decided) if total_decided > 0 else 0.5
    bankroll_ratio = (bankroll / initial) if initial > 0 else 1.0
    if bankroll_ratio < 0.5:
        return {"strategy": "DEFENSIVE", "description": "Bankroll below 50%. Stake 1-2% only. Protect capital.", "safe_stake": 0.01, "value_stake": 0.02, "win_rate": win_rate, "bankroll_ratio": bankroll_ratio}
    elif bankroll_ratio < 0.75:
        return {"strategy": "CONSERVATIVE FLAT", "description": "Bankroll recovering. Stake 2-3%. Stay disciplined.", "safe_stake": 0.02, "value_stake": 0.03, "win_rate": win_rate, "bankroll_ratio": bankroll_ratio}
    elif win_rate >= 0.6 and bankroll_ratio >= 1.0:
        return {"strategy": "KELLY CRITERION", "description": "Winning consistently. Quarter Kelly active. Safe 5%, value 3%.", "safe_stake": 0.05, "value_stake": 0.03, "win_rate": win_rate, "bankroll_ratio": bankroll_ratio}
    elif win_rate >= 0.5 and bankroll_ratio >= 0.9:
        return {"strategy": "FLAT STAKING", "description": "Solid results. Flat staking. Safe 5%, value 3%, combos 2%.", "safe_stake": 0.05, "value_stake": 0.03, "win_rate": win_rate, "bankroll_ratio": bankroll_ratio}
    else:
        return {"strategy": "CONSERVATIVE FLAT", "description": "Mixed results. Stay conservative. Safe 3%, value 2%.", "safe_stake": 0.03, "value_stake": 0.02, "win_rate": win_rate, "bankroll_ratio": bankroll_ratio}

def summarize_standings(standings):
    """Convert standings JSON to compact plain text to save tokens."""
    if not standings:
        return "No standings data available"
    lines = []
    for league, table in standings.items():
        lines.append(f"{league} top 5:")
        for t in table[:5]:
            lines.append(f"  {t['pos']}. {t['team']} — {t['pts']}pts ({t['W']}W {t['D']}D {t['L']}L)")
    return "\n".join(lines)

def trim_prompt_for_groq(prompt, max_chars=25000):
    """Trim prompt to fit Groq token limit."""
    if len(prompt) <= max_chars:
        return prompt
    for cutpoint in ["STANDINGS:", "STANDINGS FOR CONTEXT:", "RECENT RESULTS:", "Odds reference:"]:
        idx = prompt.find(cutpoint)
        if idx > 0:
            trimmed = prompt[:idx] + "\n[Data trimmed to fit model limit]"
            if len(trimmed) <= max_chars:
                logging.info(f"Prompt trimmed at: {cutpoint}")
                return trimmed
    return prompt[:max_chars] + "\n[Truncated]"

def ask_ai(prompt, image_bytes=None):
    """
    AI fallback chain:
    1. Gemini 1.5 Flash (free, higher quota)
    2. Gemini 2.0 Flash
    3. Gemini 2.0 Flash Lite
    4. Groq llama-3.1-8b-instant (trimmed)
    5. Groq llama-3.3-70b-versatile (trimmed)
    6. Groq mixtral-8x7b-32768 (trimmed)
    """
    gemini_models = [
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ]
    for model in gemini_models:
        try:
            if image_bytes:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt]
                )
            else:
                response = gemini_client.models.generate_content(model=model, contents=prompt)
            logging.info(f"Gemini success: {model}")
            return response.text
        except Exception as e:
            err = str(e)
            logging.warning(f"Gemini {model} failed: {err[:120]}")
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                continue
            else:
                break

    logging.info("All Gemini failed — switching to Groq...")
    trimmed = trim_prompt_for_groq(prompt)
    groq_models = [
        ("llama-3.1-8b-instant", 1500),
        ("llama-3.3-70b-versatile", 2000),
        ("mixtral-8x7b-32768", 2000),
    ]
    for groq_model, max_tokens in groq_models:
        try:
            response = groq_client.chat.completions.create(
                model=groq_model,
                messages=[{"role": "user", "content": trimmed}],
                max_tokens=max_tokens
            )
            logging.info(f"Groq success: {groq_model}")
            return response.choices[0].message.content
        except Exception as e:
            err = str(e)
            logging.warning(f"Groq {groq_model} failed: {err[:120]}")
            if "413" in err or "too large" in err.lower() or "tokens" in err.lower():
                trimmed = trim_prompt_for_groq(trimmed, max_chars=18000)
                try:
                    response = groq_client.chat.completions.create(
                        model=groq_model,
                        messages=[{"role": "user", "content": trimmed}],
                        max_tokens=max_tokens
                    )
                    logging.info(f"Groq success after hard trim: {groq_model}")
                    return response.choices[0].message.content
                except:
                    continue
            continue

    raise Exception("All AI models exhausted. Try again in a few minutes.")

# ============================================================
# SOURCE 1: API-Football (PRIMARY)
# ============================================================

def fetch_api_football_fixtures(date_str):
    cached = cache_get(f"api_football_{date_str}")
    if cached is not None:
        return cached
    if not API_FOOTBALL_KEY:
        logging.warning("No API_FOOTBALL_KEY")
        return {}
    matches = {}
    headers = {"x-apisports-key": API_FOOTBALL_KEY, "x-rapidapi-host": "v3.football.api-sports.io"}
    try:
        url = "https://v3.football.api-sports.io/fixtures"
        params = {"date": date_str, "timezone": "UTC"}
        response = requests.get(url, headers=headers, params=params, timeout=20)
        logging.info(f"API-Football status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            errors = data.get("errors", {})
            if errors:
                logging.error(f"API-Football errors: {errors}")
                return {}
            fixtures = data.get("response", [])
            logging.info(f"API-Football: {len(fixtures)} raw fixtures")
            for fixture in fixtures:
                try:
                    league_name = fixture["league"]["name"]
                    country = fixture["league"]["country"]
                    home = fixture["teams"]["home"]["name"]
                    away = fixture["teams"]["away"]["name"]
                    kickoff_utc = fixture["fixture"]["date"]
                    status = fixture["fixture"]["status"]["short"]
                    if not is_future_match(kickoff_utc):
                        continue
                    kickoff_wat = to_wat(kickoff_utc)
                    key = f"{league_name} ({country})"
                    if key not in matches:
                        matches[key] = []
                    matches[key].append({
                        "home": home, "away": away,
                        "kickoff": kickoff_wat, "kickoff_utc": kickoff_utc, "status": status
                    })
                except Exception as e:
                    logging.error(f"Fixture parse error: {e}")
                    continue
            total = sum(len(v) for v in matches.values())
            logging.info(f"API-Football future matches: {total}")
            cache_set(f"api_football_{date_str}", matches)
        else:
            logging.error(f"API-Football failed: {response.status_code}")
    except Exception as e:
        logging.error(f"API-Football exception: {e}")
    return matches

# ============================================================
# SOURCE 2: football-data.org (BACKUP)
# ============================================================

def fetch_football_data_fixtures(date_str):
    cached = cache_get(f"fd_{date_str}")
    if cached is not None:
        return cached
    matches = {}
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    for comp_code, comp_name in FOOTBALL_DATA_COMPS:
        try:
            url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches"
            params = {"dateFrom": date_str, "dateTo": date_str, "status": "SCHEDULED"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                future = []
                for m in data.get("matches", []):
                    kickoff_utc = m.get("utcDate", "")
                    if is_future_match(kickoff_utc):
                        future.append({
                            "home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
                            "kickoff": to_wat(kickoff_utc), "kickoff_utc": kickoff_utc, "status": "NS"
                        })
                if future:
                    matches[comp_name] = future
        except:
            continue
    logging.info(f"football-data.org future: {sum(len(v) for v in matches.values())}")
    cache_set(f"fd_{date_str}", matches)
    return matches

# ============================================================
# SOURCE 3: TheSportsDB (LAST RESORT)
# ============================================================

def fetch_sportsdb_fixtures(date_str):
    cached = cache_get(f"sdb_{date_str}")
    if cached is not None:
        return cached
    matches = {}
    for league_id, league_name in SPORTSDB_LEAGUES:
        try:
            url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventsday.php?d={date_str}&l={league_id}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                events = data.get("events", []) or []
                future = []
                for e in events:
                    kickoff_utc = f"{e.get('dateEvent')} {e.get('strTime', '00:00')}".strip()
                    if is_future_match(kickoff_utc):
                        future.append({
                            "home": e.get("strHomeTeam"), "away": e.get("strAwayTeam"),
                            "kickoff": to_wat(kickoff_utc), "kickoff_utc": kickoff_utc, "status": "NS"
                        })
                if future:
                    matches[league_name] = future
        except:
            continue
    logging.info(f"TheSportsDB future: {sum(len(v) for v in matches.values())}")
    cache_set(f"sdb_{date_str}", matches)
    return matches

# ============================================================
# MASTER FETCH
# ============================================================

def fetch_all_matches(date_str=None):
    if not date_str:
        date_str = get_today_str()
    all_matches = {}
    api_matches = fetch_api_football_fixtures(date_str)
    if api_matches:
        all_matches.update(api_matches)
    fd_matches = fetch_football_data_fixtures(date_str)
    for league, games in fd_matches.items():
        if league not in all_matches:
            all_matches[league] = games
    if not all_matches:
        logging.info("Falling back to TheSportsDB...")
        all_matches.update(fetch_sportsdb_fixtures(date_str))
    total = sum(len(v) for v in all_matches.values())
    logging.info(f"TOTAL FUTURE MATCHES for {date_str}: {total} across {len(all_matches)} leagues")
    return all_matches

def fetch_upcoming_matches():
    upcoming = {}
    now_wat = get_now_wat()
    for days_ahead in range(1, 8):
        date_str = (now_wat + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        matches = fetch_all_matches(date_str)
        if matches:
            for league, games in matches.items():
                if league not in upcoming:
                    upcoming[league] = games[:2]
            if sum(len(v) for v in upcoming.values()) >= 10:
                break
    return upcoming

def fetch_standings():
    cached = cache_get("standings_all")
    if cached:
        return cached
    standings = {}
    if API_FOOTBALL_KEY:
        key_leagues = [
            (39, "EPL"), (140, "La Liga"), (135, "Serie A"),
            (78, "Bundesliga"), (61, "Ligue 1"), (71, "Brazil Serie A"),
            (332, "NPFL Nigeria"), (253, "MLS"),
        ]
        headers = {"x-apisports-key": API_FOOTBALL_KEY, "x-rapidapi-host": "v3.football.api-sports.io"}
        for league_id, league_name in key_leagues:
            try:
                url = "https://v3.football.api-sports.io/standings"
                params = {"league": league_id, "season": 2024}
                response = requests.get(url, headers=headers, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    standings_data = data.get("response", [])
                    if standings_data:
                        table = standings_data[0].get("league", {}).get("standings", [[]])[0][:8]
                        standings[league_name] = [
                            {"pos": t["rank"], "team": t["team"]["name"],
                             "W": t["all"]["win"], "D": t["all"]["draw"], "L": t["all"]["lose"],
                             "GF": t["all"]["goals"]["for"], "GA": t["all"]["goals"]["against"], "pts": t["points"]}
                            for t in table
                        ]
            except:
                continue
    if not standings:
        headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
        for comp_code, comp_name in [("PL", "EPL"), ("PD", "La Liga"), ("SA", "Serie A"), ("BL1", "Bundesliga")]:
            try:
                url = f"https://api.football-data.org/v4/competitions/{comp_code}/standings"
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    table = data.get("standings", [{}])[0].get("table", [])[:8]
                    standings[comp_name] = [
                        {"pos": t["position"], "team": t["team"]["name"],
                         "W": t["won"], "D": t["draw"], "L": t["lost"],
                         "GF": t["goalsFor"], "GA": t["goalsAgainst"], "pts": t["points"]}
                        for t in table
                    ]
            except:
                continue
    cache_set("standings_all", standings)
    return standings

def fetch_recent_results():
    cached = cache_get("recent_results")
    if cached:
        return cached
    try:
        url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventspastleague.php?id=4328"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            events = data.get("events", []) or []
            result = [
                {"match": f"{e.get('strHomeTeam')} vs {e.get('strAwayTeam')}",
                 "score": f"{e.get('intHomeScore')}-{e.get('intAwayScore')}",
                 "date": e.get("dateEvent")}
                for e in events[-10:]
            ]
            cache_set("recent_results", result)
            return result
    except:
        pass
    return []

def fetch_todays_odds():
    cached = cache_get("todays_odds")
    if cached:
        return cached
    now = get_now_utc()
    end_of_day = now.replace(hour=23, minute=59, second=59)
    all_data = []
    for sport in ALL_ODDS_SPORTS:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
            params = {
                "apiKey": ODDS_API_KEY, "regions": "uk", "markets": "h2h",
                "oddsFormat": "decimal",
                "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commenceTimeTo": end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data:
                    all_data.extend(data[:2])
        except:
            continue
    cache_set("todays_odds", all_data)
    return all_data

def fetch_soon_matches(hours=2):
    now_utc = get_now_utc()
    cutoff = now_utc + timedelta(hours=hours)
    all_matches = fetch_all_matches(get_today_str())
    soon = []
    for league, games in all_matches.items():
        for game in games:
            try:
                kickoff_utc_str = game.get("kickoff_utc", "")
                if not kickoff_utc_str:
                    continue
                if "T" in kickoff_utc_str:
                    match_time = datetime.fromisoformat(kickoff_utc_str.replace("Z", "+00:00"))
                else:
                    match_time = datetime.strptime(kickoff_utc_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                if now_utc + timedelta(minutes=5) < match_time <= cutoff:
                    soon.append({
                        "home": game["home"], "away": game["away"],
                        "kickoff": game["kickoff"], "kickoff_utc": kickoff_utc_str, "competition": league
                    })
            except:
                continue
    return soon

def fetch_live_context():
    today = get_today_str()
    now_wat = get_now_wat().strftime("%Y-%m-%d %I:%M %p WAT")
    context = {
        "today": today, "now": now_wat,
        "matches": {}, "standings": {},
        "recent_results": [], "no_matches_today": False, "total_matches": 0
    }
    try:
        context["matches"] = fetch_all_matches(today)
        context["total_matches"] = sum(len(v) for v in context["matches"].values())
    except:
        pass
    if context["total_matches"] == 0:
        try:
            context["upcoming"] = fetch_upcoming_matches()
            context["no_matches_today"] = True
        except:
            pass
    try:
        context["standings"] = fetch_standings()
    except:
        pass
    try:
        context["recent_results"] = fetch_recent_results()
    except:
        pass
    return context

def build_match_list(matches):
    match_list = ""
    for league, games in matches.items():
        match_list += f"\n{league}:\n"
        for g in games:
            match_list += f"  - {g['home']} vs {g['away']} | {g['kickoff']} (WAT)\n"
    return match_list if match_list else "No matches found"

def build_prompt(mode, today_matches, upcoming, standings, recent_results, odds_data, bankroll, staking, soon_matches=None):
    name = get_name()
    today = get_today_str()
    now_wat = get_now_wat().strftime("%A %d %B %Y, %I:%M %p WAT")
    br = bankroll
    strategy = staking["strategy"]
    safe_pct = staking["safe_stake"]
    value_pct = staking["value_stake"]
    s_safe = format_naira(br * safe_pct) if br > 0 else "⚠️ Set bankroll!"
    s_value = format_naira(br * value_pct) if br > 0 else "⚠️ Set bankroll!"
    s_combo = format_naira(br * 0.02) if br > 0 else "⚠️ Set bankroll!"

    no_matches = not today_matches or sum(len(v) for v in today_matches.values()) == 0
    total_matches = sum(len(v) for v in today_matches.values()) if today_matches else 0

    match_list = build_match_list(today_matches) if not no_matches else "NO MATCHES TODAY"
    upcoming_list = build_match_list(upcoming) if no_matches and upcoming else ""

    # Compact standings to save tokens
    standings_text = summarize_standings(standings)

    time_context = f"""
NIGERIA TIME NOW: {now_wat}
TODAY: {today}
FUTURE MATCHES AVAILABLE: {total_matches}
"""

    rules = f"""
ABSOLUTE RULES:
1. ZERO markdown — no **, ###, *, __
2. Only use matches from the REAL MATCH LIST below
3. All times are Nigeria time (WAT) — show exactly as given e.g. Tue 24 Feb, 9:00 PM
4. NEVER use UTC or 24-hour times
5. Safe odds: STRICTLY 1.20-1.75 ONLY
6. Value odds: STRICTLY 1.80-2.50 ONLY
7. Combo legs: STRICTLY 2.00 or below
8. NEVER show "Not available" — skip pick if no real data
9. NEVER invent fixtures, teams, dates or scores
10. Always show exact Naira stake amounts
11. Cover multiple leagues
"""

    bankroll_info = f"""
Punter: {name}
Bankroll: {format_naira(br) if br > 0 else "NOT SET — remind user to do /bankroll [amount]"}
Strategy: {strategy}
Safe stake: {safe_pct*100:.0f}% = {s_safe}
Value stake: {value_pct*100:.0f}% = {s_value}
Combo stake: 2% = {s_combo}
"""

    if mode == "soon":
        soon_list = ""
        if soon_matches:
            for m in soon_matches:
                soon_list += f"  - {m['home']} vs {m['away']} | {m['competition']} | {m['kickoff']} (WAT)\n"
        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}

MATCHES KICKING OFF SOON:
{soon_list if soon_list else "NONE — reply exactly: NO_SOON_MATCHES"}

Format EXACTLY:

⚡ ACE1000 SOON PICKS
{name}, these kick off very soon! 🇳🇬

━━━━━━━━━━━━━━━━━━━━
For each match:

⚡ [Competition] — [Home vs Away]
Kicks Off: [Nigeria time from list]
Best Bet: [Pick]
Odds: [Estimated odds]
Stake: [% = Naira]

📊 QUICK ANALYSIS:
Form: [Brief]
Verdict: [One sentence]
Confidence: [%]
Risk: [Low/Medium] 🟢🟡
━━━━━━━━━━━━━━━━━━━━

If 2+ matches:
🔗 QUICK COMBO
Leg 1: [Match - Bet @ max 2.00]
Leg 2: [Match - Bet @ max 2.00]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [X]

⚠️ Act fast — these kick off soon!"""

    elif mode == "safe":
        fixture_source = (
            f"UPCOMING FIXTURES (Nigeria time):\n{upcoming_list}"
            if no_matches and upcoming_list
            else f"TODAY'S FUTURE MATCHES (Nigeria time):\n{match_list}"
        )
        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}

{fixture_source}

STANDINGS:
{standings_text}

Give 3 SAFE picks from the list above only. Odds STRICTLY 1.20-1.75.

Format EXACTLY:

🛡️ ACE1000 SAFE PICKS — {today}
Hey {name}! {"Safe picks for today" if not no_matches else "No games today — upcoming picks"} 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
✅ PICK 1
League: [From list]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time from list]
Bet: [Pick]
Odds: [1.20-1.75 STRICTLY]
Confidence: [%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [From standings]
Key Stat: [Real stat]
Verdict: [One sentence]
Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 2
League: [Different league]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time from list]
Bet: [Pick]
Odds: [1.20-1.75 STRICTLY]
Confidence: [%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [From standings]
Key Stat: [Real stat]
Verdict: [One sentence]
Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 3
League: [Different league]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time from list]
Bet: [Pick]
Odds: [1.20-1.75 STRICTLY]
Confidence: [%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [From standings]
Key Stat: [Real stat]
Verdict: [One sentence]
Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
🔗 SAFE COMBO
Combine all 3:
Combined Odds: [Multiply all 3]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

⚠️ Bet responsibly. Never stake what you cannot afford to lose."""

    elif mode == "combo":
        fixture_source = (
            f"UPCOMING FIXTURES:\n{upcoming_list}"
            if no_matches and upcoming_list
            else f"TODAY'S FUTURE MATCHES (Nigeria time):\n{match_list}"
        )
        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}

{fixture_source}

STANDINGS:
{standings_text}

Build 3 combos from the list above only. All legs max 2.00. Mix leagues.

Format EXACTLY:

🔗 ACE1000 COMBO BETS — {today}
{name}, {"today's combos" if not no_matches else "upcoming combos"} 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
COMBO 1 - BANKER 🏦
Risk: Very Low 🟢

Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
COMBO 2 - BALANCED ⚖️
Risk: Medium 🟡

Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
COMBO 3 - JACKPOT 💥
Risk: Higher 🔴

Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 4: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly on SportyBet, {name}!"""

    else:
        if no_matches:
            return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}

NO REMAINING MATCHES TODAY ({today}).

UPCOMING FIXTURES — ONLY USE THESE:
{upcoming_list if upcoming_list else "NO UPCOMING FIXTURES FOUND"}

STANDINGS:
{standings_text}

Show real upcoming fixtures only. Never invent matches. Show Nigeria time exactly as given.

Format EXACTLY:

📅 ACE1000 UPCOMING PICKS
No more matches today, {name}. Best upcoming games 🇳🇬

📊 Strategy: {strategy}
{staking["description"]}

━━━━━━━━━━━━━━━━━━━━
🛡️ SAFE PICKS (1.20-1.75)

✅ SAFE PICK 1
League: [From list]
Match: [Exact teams]
Kickoff: [Exact Nigeria time from list]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
✅ SAFE PICK 2
League: [Different league]
Match: [Exact teams]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
🎯 VALUE PICKS (1.80-2.50)

⭐ VALUE PICK 1
League: [From list]
Match: [Exact teams]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Medium 🟡

⭐ VALUE PICK 2
League: [Different league]
Match: [Exact teams]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Medium 🟡

━━━━━━━━━━━━━━━━━━━━
🔗 COMBO BETS

COMBO 1 - BANKER 🏦
Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

COMBO 2 - VALUE ⭐
Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly {name}. Never stake more than you can afford to lose."""

        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}

TODAY'S REMAINING FUTURE MATCHES (Nigeria time):
{match_list}

STANDINGS:
{standings_text}

{total_matches} future matches today across {len(today_matches)} leagues.
Best value picks. Safe 1.20-1.75. Value 1.80-2.50. Combo legs max 2.00.
Only use exact matches from the list. Show Nigeria time as given.

Format EXACTLY:

🎯 ACE1000 DAILY BETTING CARD
{today} — Hey {name}! 🇳🇬
{total_matches} matches remaining today

📊 Strategy: {strategy}
{staking["description"]}

━━━━━━━━━━━━━━━━━━━━
🛡️ SAFE PICKS (1.20-1.75 STRICTLY)

✅ SAFE PICK 1
League: [From list]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Real stat]
Verdict: [One sentence]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
✅ SAFE PICK 2
League: [Different league]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
🎯 VALUE PICKS (1.80-2.50 STRICTLY)

⭐ VALUE PICK 1
League: [From list]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Medium 🟡

⭐ VALUE PICK 2
League: [Different league]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Medium 🟡

━━━━━━━━━━━━━━━━━━━━
🔗 COMBO BETS

COMBO 1 - BANKER 🏦
Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

COMBO 2 - VALUE ⭐
Leg 1: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 2: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Leg 3: [League] [Exact match] - [Bet] @ [max 2.00] | [Nigeria time]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly {name}. Never stake more than you can afford to lose."""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    now_wat = get_now_wat().strftime("%I:%M %p WAT")
    await update.message.reply_text(
        f"👋 Welcome to Ace1000, {name}!\n\n"
        f"Your personal SportyBet analyst 🇳🇬\n\n"
        f"🕐 Nigeria Time: {now_wat}\n"
        f"💰 Bankroll: {format_naira(bankroll) if bankroll > 0 else 'Not set'}\n\n"
        f"Type /home to see all commands!"
    )

async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    staking = get_staking_strategy(data)
    today = get_today_str()
    now_wat = get_now_wat().strftime("%I:%M %p WAT")
    await update.message.reply_text(
        f"🏠 ACE1000 HOME\n"
        f"Welcome back, {name}!\n\n"
        f"📅 Today: {today}\n"
        f"🕐 Nigeria Time: {now_wat}\n"
        f"💰 Bankroll: {format_naira(bankroll) if bankroll > 0 else 'Not set'}\n"
        f"📊 Strategy: {staking['strategy']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 COMMANDS\n\n"
        f"👤 PROFILE\n"
        f"/setname [name] — Set your name\n"
        f"/bankroll [amount] — Set bankroll\n"
        f"/mystats — Full stats\n"
        f"/strategy — Active staking strategy\n"
        f"/history — Bankroll history\n\n"
        f"⚽ PICKS\n"
        f"/odds — Full daily card\n"
        f"/safe — Safe picks only\n"
        f"/combo — Combo bets only\n"
        f"/soon [hours] — Games kicking off soon\n\n"
        f"💬 CHAT\n"
        f"Just text me anything!\n"
        f"Live data from 1000+ leagues.\n\n"
        f"📝 BET TRACKER\n"
        f"/logbet [match] [pick] [odds] [stake]\n"
        f"/result [number] [win/loss]\n"
        f"/mybets — Bet history\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Gemini + Groq AI\n"
        f"📊 API-Football + football-data.org + TheSportsDB\n"
        f"🌍 1000+ leagues worldwide\n"
        f"⏰ All times in Nigeria time (WAT)\n"
        f"🇳🇬 Built for SportyBet"
    )

async def view_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    staking = get_staking_strategy(data)
    bets = data.get("bets", [])
    won = len([b for b in bets if b.get("result") == "win"])
    lost = len([b for b in bets if b.get("result") == "loss"])
    await update.message.reply_text(
        f"📊 {name}'s ACTIVE STRATEGY\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Strategy: {staking['strategy']}\n\n"
        f"{staking['description']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Win Rate: {staking['win_rate']*100:.1f}%\n"
        f"Bankroll Health: {staking['bankroll_ratio']*100:.1f}% of starting\n"
        f"Won: {won} | Lost: {lost}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Safe Stake: {staking['safe_stake']*100:.0f}% = {format_naira(bankroll * staking['safe_stake'])}\n"
        f"Value Stake: {staking['value_stake']*100:.0f}% = {format_naira(bankroll * staking['value_stake'])}\n"
        f"Combo Stake: 2% = {format_naira(bankroll * 0.02)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"STRATEGY GUIDE:\n"
        f"Martingale — Never recommended.\n"
        f"Flat Staking — Safe and sustainable.\n"
        f"Kelly Criterion — Best when winning consistently.\n"
        f"Defensive — Protects capital in losing streaks.\n\n"
        f"Ace1000 adjusts automatically. 🎯"
    )

async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Example: /setname John")
        return
    name = " ".join(context.args)
    data = load_data()
    data["name"] = name
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    await update.message.reply_text(f"✅ Done! I'll call you {name} from now on. 🎯")

async def set_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Example: /bankroll 50000")
        return
    try:
        amount = float(context.args[0].replace(",", ""))
        if amount <= 0:
            await update.message.reply_text("❌ Amount must be greater than 0.")
            return
        data = load_data()
        data["bankroll"] = amount
        data["chat_id"] = update.effective_chat.id
        if data["initial_bankroll"] == 0:
            data["initial_bankroll"] = amount
        data["bankroll_history"].append({
            "date": get_now_wat().strftime("%Y-%m-%d %I:%M %p"), "amount": amount
        })
        save_data(data)
        name = data.get("name", "Champ")
        staking = get_staking_strategy(data)
        await update.message.reply_text(
            f"✅ Bankroll set to {format_naira(amount)}, {name}!\n\n"
            f"{stake_breakdown(amount)}\n\n"
            f"📊 Strategy: {staking['strategy']}\n{staking['description']}\n\n"
            f"Use /odds to get today's picks!"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Example: /bankroll 50000")

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    initial = data.get("initial_bankroll", 0)
    bets = data.get("bets", [])
    staking = get_staking_strategy(data)
    won = len([b for b in bets if b.get("result") == "win"])
    lost = len([b for b in bets if b.get("result") == "loss"])
    pending = len([b for b in bets if b.get("result") == "pending"])
    total_staked = sum(b.get("stake", 0) for b in bets if b.get("result") != "pending")
    total_returned = sum(b.get("stake", 0) * b.get("odds", 1) for b in bets if b.get("result") == "win")
    profit = total_returned - total_staked
    roi = (profit / total_staked * 100) if total_staked > 0 else 0
    growth = ((bankroll - initial) / initial * 100) if initial > 0 else 0
    await update.message.reply_text(
        f"📊 {name}'s ACE1000 STATS\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Current: {format_naira(bankroll)}\n"
        f"🏦 Starting: {format_naira(initial)}\n"
        f"📈 Growth: {growth:+.1f}%\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 RECORD\n"
        f"Total: {len(bets)} | ✅ {won} | ❌ {lost} | ⏳ {pending}\n"
        f"Win Rate: {(won/max(won+lost,1)*100):.1f}%\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 FINANCIALS\n"
        f"Staked: {format_naira(total_staked)}\n"
        f"Returned: {format_naira(total_returned)}\n"
        f"P&L: {format_naira(profit)}\n"
        f"ROI: {roi:+.1f}%\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Strategy: {staking['strategy']}\n\n"
        f"{stake_breakdown(bankroll)}"
    )

async def bankroll_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    history = data.get("bankroll_history", [])
    if not history:
        await update.message.reply_text(f"No history yet, {name}.")
        return
    lines = [f"📅 {name}'s BANKROLL HISTORY\n━━━━━━━━━━━━━━━━━━━━"]
    for entry in history[-10:]:
        lines.append(f"{entry['date']}: {format_naira(entry['amount'])}")
    await update.message.reply_text("\n".join(lines))

async def log_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text("❌ Format: /logbet [match] [pick] [odds] [stake]\nExample: /logbet ArsenalvsChelsea Over2.5 1.65 2000")
        return
    try:
        match, pick = context.args[0], context.args[1]
        odds, stake = float(context.args[2]), float(context.args[3])
        data = load_data()
        bet = {
            "id": len(data["bets"]) + 1,
            "date": get_now_wat().strftime("%Y-%m-%d %I:%M %p"),
            "match": match, "pick": pick, "odds": odds, "stake": stake,
            "potential": odds * stake, "result": "pending"
        }
        data["bets"].append(bet)
        save_data(data)
        name = data.get("name", "Champ")
        await update.message.reply_text(
            f"✅ Bet #{bet['id']} logged, {name}!\n\n"
            f"Match: {match}\nPick: {pick}\nOdds: {odds}\n"
            f"Stake: {format_naira(stake)}\nPotential Win: {format_naira(odds * stake)}\n\n"
            f"Use /result {bet['id']} win or /result {bet['id']} loss to update!"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid odds or stake.")

async def update_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Format: /result [bet number] [win/loss]")
        return
    try:
        bet_id, result = int(context.args[0]), context.args[1].lower()
        if result not in ["win", "loss"]:
            await update.message.reply_text("❌ Must be win or loss")
            return
        data = load_data()
        name = data.get("name", "Champ")
        bet = next((b for b in data["bets"] if b["id"] == bet_id), None)
        if not bet:
            await update.message.reply_text(f"❌ Bet #{bet_id} not found.")
            return
        bet["result"] = result
        bankroll = data.get("bankroll", 0)
        if result == "win":
            profit = (bet["odds"] - 1) * bet["stake"]
            data["bankroll"] = bankroll + profit
        else:
            data["bankroll"] = max(0, bankroll - bet["stake"])
        data["bankroll_history"].append({
            "date": get_now_wat().strftime("%Y-%m-%d %I:%M %p"), "amount": data["bankroll"]
        })
        save_data(data)
        staking = get_staking_strategy(data)
        if result == "win":
            await update.message.reply_text(
                f"🎉 YES {name.upper()}! BET #{bet_id} WON!\n\n"
                f"Match: {bet['match']}\nProfit: +{format_naira((bet['odds']-1)*bet['stake'])}\n"
                f"New Bankroll: {format_naira(data['bankroll'])}\n\n"
                f"📊 Strategy: {staking['strategy']}\nAce1000 delivering! 🔥"
            )
        else:
            await update.message.reply_text(
                f"❌ Bet #{bet_id} lost, {name}.\n\n"
                f"Match: {bet['match']}\nLost: -{format_naira(bet['stake'])}\n"
                f"New Bankroll: {format_naira(data['bankroll'])}\n\n"
                f"📊 Strategy: {staking['strategy']}\n{staking['description']}\nStay disciplined 💪"
            )
    except ValueError:
        await update.message.reply_text("❌ Invalid bet number.")

async def my_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bets = data.get("bets", [])
    if not bets:
        await update.message.reply_text(f"No bets yet, {name}. Use /logbet!")
        return
    lines = [f"📝 {name}'s BET HISTORY\n━━━━━━━━━━━━━━━━━━━━"]
    for bet in bets[-10:]:
        emoji = "✅" if bet["result"] == "win" else "❌" if bet["result"] == "loss" else "⏳"
        lines.append(f"{emoji} #{bet['id']} {bet['match']}\n   {bet['pick']} @ {bet['odds']} | {format_naira(bet['stake'])}")
    await update.message.reply_text("\n".join(lines))

async def get_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    today = get_today_str()
    now_wat = get_now_wat().strftime("%I:%M %p WAT")
    await update.message.reply_text(f"🔍 Fetching upcoming matches for {today} ({now_wat}), {name}...")
    try:
        today_matches = fetch_all_matches(today)
        standings = fetch_standings()
        recent = fetch_recent_results()
        odds_data = fetch_todays_odds()
        total = sum(len(v) for v in today_matches.values()) if today_matches else 0
        upcoming = {}
        if total == 0:
            await update.message.reply_text("No remaining matches today. Fetching upcoming fixtures...")
            upcoming = fetch_upcoming_matches()
        else:
            await update.message.reply_text(f"Found {total} upcoming matches! Analyzing...")
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("full", today_matches, upcoming, standings, recent, odds_data, bankroll, staking)
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def safe_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    today = get_today_str()
    await update.message.reply_text(f"🛡️ Fetching safe picks for {today}, {name}...")
    try:
        today_matches = fetch_all_matches(today)
        standings = fetch_standings()
        recent = fetch_recent_results()
        odds_data = fetch_todays_odds()
        total = sum(len(v) for v in today_matches.values()) if today_matches else 0
        upcoming = fetch_upcoming_matches() if total == 0 else {}
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("safe", today_matches, upcoming, standings, recent, odds_data, bankroll, staking)
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def combo_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    today = get_today_str()
    await update.message.reply_text(f"🔗 Building combos for {today}, {name}...")
    try:
        today_matches = fetch_all_matches(today)
        standings = fetch_standings()
        recent = fetch_recent_results()
        odds_data = fetch_todays_odds()
        total = sum(len(v) for v in today_matches.values()) if today_matches else 0
        upcoming = fetch_upcoming_matches() if total == 0 else {}
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("combo", today_matches, upcoming, standings, recent, odds_data, bankroll, staking)
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def soon_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    hours = 2
    if context.args:
        try:
            hours = max(1, min(24, int(context.args[0])))
        except:
            hours = 2
    now_wat = get_now_wat().strftime("%I:%M %p WAT")
    await update.message.reply_text(f"⚡ Finding matches in next {hours} hour(s) — it's {now_wat}, {name}...")
    try:
        soon_matches = fetch_soon_matches(hours)
        today_matches = fetch_all_matches(get_today_str())
        standings = fetch_standings()
        recent = fetch_recent_results()
        odds_data = fetch_todays_odds()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("soon", today_matches, {}, standings, recent, odds_data, bankroll, staking, soon_matches=soon_matches)
        result = ask_ai(prompt)
        if "NO_SOON_MATCHES" in result:
            await update.message.reply_text(f"⚡ No matches in next {hours} hour(s), {name}.\nTry /soon 6 or /odds for full day card.")
        else:
            await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = load_data()
        name = data.get("name", "Champ")
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        user_message = update.message.text
        now_wat = get_now_wat().strftime("%I:%M %p WAT")
        await update.message.reply_text(f"🤔 Checking live data ({now_wat}), {name}...")
        live = fetch_live_context()
        bets = data.get("bets", [])
        recent_bets = bets[-3:] if bets else []
        won = len([b for b in bets if b.get("result") == "win"])
        lost = len([b for b in bets if b.get("result") == "loss"])
        match_summary = ""
        if live.get("matches") and live["total_matches"] > 0:
            match_summary = build_match_list(live["matches"])
        elif live.get("upcoming"):
            match_summary = "No matches today. Upcoming:\n" + build_match_list(live["upcoming"])
        standings_text = summarize_standings(live.get("standings", {}))
        prompt = f"""You are Ace1000, elite personal football betting analyst for {name} on SportyBet Nigeria.
Sharp, direct, knowledgeable. All times are Nigeria time (WAT).

LIVE DATA:
Nigeria time now: {live['now']}
Today: {live['today']}
Total upcoming matches today: {live.get('total_matches', 0)}

REAL UPCOMING MATCHES (Nigeria time):
{match_summary if match_summary else "No upcoming matches found"}

STANDINGS:
{standings_text}

USER:
Name: {name}
Bankroll: {format_naira(bankroll) if bankroll > 0 else "Not set — remind to use /bankroll"}
Strategy: {staking['strategy']}
Record: {won} wins, {lost} losses
Recent bets: {json.dumps(recent_bets) if recent_bets else "None yet"}

RULES:
- ZERO markdown — no **, ###, *, __
- Plain text and emojis only
- Be conversational and sharp
- Only reference real matches from data above
- All times in Nigeria time 12-hour format e.g. 9:00 PM
- Never invent fixtures
- Keep responses concise
- Address them as {name}

User says: {user_message}"""
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    await update.message.reply_text(f"📸 Analyzing your image, {name}...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        caption = update.message.caption or f"Analyze these SportyBet odds for {name}. Which have value? Which to avoid? Suggest a combo. Plain text only."
        result = ask_ai(caption, image_bytes=file_bytes)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    name = data.get("name", "Champ")
    today = get_today_str()
    try:
        today_matches = fetch_all_matches(today)
        standings = fetch_standings()
        recent = fetch_recent_results()
        odds_data = fetch_todays_odds()
        total = sum(len(v) for v in today_matches.values()) if today_matches else 0
        upcoming = fetch_upcoming_matches() if total == 0 else {}
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("full", today_matches, upcoming, standings, recent, odds_data, bankroll, staking)
        result = ask_ai(prompt)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🌅 Good morning {name}! {total} matches today 🇳🇬\n\n{result}"
        )
    except Exception as e:
        logging.error(f"Morning briefing error: {e}")

async def weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    name = data.get("name", "Champ")
    bets = data.get("bets", [])
    bankroll = data.get("bankroll", 0)
    initial = data.get("initial_bankroll", 0)
    staking = get_staking_strategy(data)
    won = len([b for b in bets if b.get("result") == "win"])
    lost = len([b for b in bets if b.get("result") == "loss"])
    total_staked = sum(b.get("stake", 0) for b in bets if b.get("result") != "pending")
    total_returned = sum(b.get("stake", 0) * b.get("odds", 1) for b in bets if b.get("result") == "win")
    profit = total_returned - total_staked
    growth = ((bankroll - initial) / initial * 100) if initial > 0 else 0
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📊 WEEKLY SUMMARY — {name}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Won: {won} | ❌ Lost: {lost}\n"
            f"Win Rate: {(won/max(won+lost,1)*100):.1f}%\n"
            f"P&L: {format_naira(profit)}\n"
            f"Growth: {growth:+.1f}%\n"
            f"Bankroll: {format_naira(bankroll)}\n\n"
            f"📊 Next Week Strategy: {staking['strategy']}\n"
            f"{staking['description']}\n\n"
            f"Keep it disciplined {name}! 💪🇳🇬"
        )
    )

async def value_alert(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    name = data.get("name", "Champ")
    today = get_today_str()
    try:
        today_matches = fetch_all_matches(today)
        standings = fetch_standings()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        s_value = format_naira(bankroll * staking["value_stake"]) if bankroll > 0 else "Set bankroll first"
        match_list = build_match_list(today_matches) if today_matches else "NO MATCHES"
        total = sum(len(v) for v in today_matches.values()) if today_matches else 0
        standings_text = summarize_standings(standings)
        prompt = f"""You are Ace1000. Nigeria time: {get_now_wat().strftime("%I:%M %p WAT")}. Today: {today}.
Scan real upcoming matches for ONE outstanding value bet with odds 1.40-2.50.
Only alert if genuine edge exists. Only use matches from the list.
If nothing stands out reply exactly: NO_ALERT

REAL UPCOMING MATCHES ({total} total — Nigeria time):
{match_list}

STANDINGS:
{standings_text}

Format if found:
🚨 VALUE ALERT, {name}! — {today}

League: [Real competition]
Match: [Exact teams from list]
Kickoff: [Exact Nigeria time]
Bet: [Pick]
Odds: [1.40-2.50]
Stake: {staking['value_stake']*100:.0f}% = {s_value}

📊 ANALYSIS:
Form: [Real data]
Verdict: [One sentence]
Risk: [Low/Medium]

No markdown."""
        result = ask_ai(prompt)
        if "NO_ALERT" not in result:
            await context.bot.send_message(chat_id=chat_id, text=result)
    except Exception:
        pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("home", home))
    app.add_handler(CommandHandler("setname", set_name))
    app.add_handler(CommandHandler("bankroll", set_bankroll))
    app.add_handler(CommandHandler("mystats", my_stats))
    app.add_handler(CommandHandler("strategy", view_strategy))
    app.add_handler(CommandHandler("history", bankroll_history))
    app.add_handler(CommandHandler("odds", get_odds))
    app.add_handler(CommandHandler("safe", safe_picks))
    app.add_handler(CommandHandler("combo", combo_picks))
    app.add_handler(CommandHandler("soon", soon_picks))
    app.add_handler(CommandHandler("logbet", log_bet))
    app.add_handler(CommandHandler("result", update_result))
    app.add_handler(CommandHandler("mybets", my_bets))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    job_queue = app.job_queue
    job_queue.run_daily(morning_briefing, time=datetime.strptime("07:00", "%H:%M").time())
    job_queue.run_daily(weekly_summary, time=datetime.strptime("20:00", "%H:%M").time(), days=(6,))
    job_queue.run_repeating(value_alert, interval=21600, first=60)
    print(f"🚀 Ace1000Bot LIVE! {get_today_str()} | {get_now_wat().strftime('%I:%M %p WAT')} | Free AI only | Nigeria time")
    app.run_polling()

if __name__ == "__main__":
    main()