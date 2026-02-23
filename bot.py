import logging
import os
import requests
import json
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
SPORTSDB_KEY = "123"

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(level=logging.INFO)
DATA_FILE = "ace1000_data.json"

ALL_SPORTS = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_turkey_super_league",
    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
    "soccer_mexico_ligamx", "soccer_england_league1", "soccer_england_league2",
    "soccer_england_efl_champ", "soccer_spain_segunda_division",
    "soccer_italy_serie_b", "soccer_germany_bundesliga2",
    "soccer_africa_cup_of_nations", "soccer_conmebol_copa_libertadores",
]

FOOTBALL_DATA_COMPS = [
    ("PL", "EPL"), ("CL", "Champions League"), ("PD", "La Liga"),
    ("SA", "Serie A"), ("BL1", "Bundesliga"), ("FL1", "Ligue 1"), ("ELC", "Championship"),
]

def get_today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_now_utc():
    return datetime.now(timezone.utc)

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

def ask_ai(prompt, image_bytes=None):
    try:
        if image_bytes:
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt]
            )
        else:
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash-lite", contents=prompt
            )
        logging.info("Using Gemini")
        return response.text
    except Exception as gemini_error:
        logging.info(f"Gemini failed, switching to Groq: {gemini_error}")
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as groq_error:
            raise Exception(f"Both AI failed. Gemini: {gemini_error}. Groq: {groq_error}")

def fetch_todays_odds():
    now = get_now_utc()
    end_of_day = now.replace(hour=23, minute=59, second=59)
    all_data = []
    for sport in ALL_SPORTS:
        for market in ["h2h", "totals", "btts"]:
            try:
                url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
                params = {
                    "apiKey": ODDS_API_KEY, "regions": "uk", "markets": market,
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
    logging.info(f"Fetched {len(all_data)} odds entries")
    return all_data

def fetch_todays_matches():
    today = get_today_str()
    matches = {}
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    for comp_code, comp_name in FOOTBALL_DATA_COMPS:
        try:
            url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches"
            params = {"dateFrom": today, "dateTo": today, "status": "SCHEDULED"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                todays = data.get("matches", [])
                if todays:
                    matches[comp_name] = [
                        {"home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
                         "kickoff": m.get("utcDate", ""), "competition": comp_name}
                        for m in todays
                    ]
        except:
            continue
    logging.info(f"Today's matches: {sum(len(v) for v in matches.values())} games")
    return matches

def fetch_upcoming_matches():
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    upcoming = {}
    today = get_today_str()
    future = (get_now_utc() + timedelta(days=7)).strftime("%Y-%m-%d")
    for comp_code, comp_name in FOOTBALL_DATA_COMPS:
        try:
            url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches"
            params = {"dateFrom": today, "dateTo": future, "status": "SCHEDULED"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                games = data.get("matches", [])[:3]
                if games:
                    upcoming[comp_name] = [
                        {"home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
                         "kickoff": m.get("utcDate", ""), "competition": comp_name}
                        for m in games
                    ]
        except:
            continue
    return upcoming

def fetch_standings():
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    standings = {}
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
    return standings

def fetch_recent_results():
    try:
        url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventspastleague.php?id=4328"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            events = data.get("events", []) or []
            return [
                {"match": f"{e.get('strHomeTeam')} vs {e.get('strAwayTeam')}",
                 "score": f"{e.get('intHomeScore')}-{e.get('intAwayScore')}",
                 "date": e.get("dateEvent")}
                for e in events[-10:]
            ]
    except:
        pass
    return []

def fetch_soon_matches(hours=2):
    soon = []
    now = get_now_utc()
    cutoff = now + timedelta(hours=hours)
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    today = get_today_str()
    for comp_code, comp_name in FOOTBALL_DATA_COMPS:
        try:
            url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches"
            params = {"dateFrom": today, "dateTo": today, "status": "SCHEDULED"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for match in data.get("matches", []):
                    match_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
                    if now <= match_time <= cutoff:
                        soon.append({
                            "home": match["homeTeam"]["name"], "away": match["awayTeam"]["name"],
                            "kickoff": match["utcDate"], "competition": comp_name
                        })
        except:
            continue
    return soon

def fetch_live_context():
    today = get_today_str()
    now = get_now_utc().strftime("%Y-%m-%d %H:%M UTC")
    context = {"today": today, "now": now, "matches": {}, "standings": {}, "recent_results": [], "odds_snapshot": [], "no_matches_today": False}
    try:
        context["matches"] = fetch_todays_matches()
    except:
        pass
    if not context["matches"] or sum(len(v) for v in context["matches"].values()) == 0:
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
    try:
        now_utc = get_now_utc()
        end_of_day = now_utc.replace(hour=23, minute=59, second=59)
        url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
        params = {
            "apiKey": ODDS_API_KEY, "regions": "uk", "markets": "h2h",
            "oddsFormat": "decimal",
            "commenceTimeFrom": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "commenceTimeTo": end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            context["odds_snapshot"] = [
                {"match": f"{g['home_team']} vs {g['away_team']}", "commence": g.get("commence_time", "")}
                for g in data[:5]
            ]
    except:
        pass
    return context

def build_prompt(mode, today_matches, upcoming, standings, recent_results, odds_data, bankroll, staking, soon_matches=None):
    name = get_name()
    today = get_today_str()
    now_utc = get_now_utc().strftime("%Y-%m-%d %H:%M UTC")
    br = bankroll
    strategy = staking["strategy"]
    safe_pct = staking["safe_stake"]
    value_pct = staking["value_stake"]
    s_safe = format_naira(br * safe_pct) if br > 0 else "⚠️ Set bankroll!"
    s_value = format_naira(br * value_pct) if br > 0 else "⚠️ Set bankroll!"
    s_combo = format_naira(br * 0.02) if br > 0 else "⚠️ Set bankroll!"

    no_matches = not today_matches or sum(len(v) for v in today_matches.values()) == 0

    time_context = f"""
CURRENT DATE AND TIME: {now_utc}
TODAY'S DATE: {today}
MATCHES AVAILABLE TODAY: {"YES" if not no_matches else "NO — show upcoming matches instead"}
"""

    matches_context = f"""
TODAY'S MATCHES:
{json.dumps(today_matches, indent=2) if not no_matches else "NONE TODAY"}

UPCOMING MATCHES (use these if no matches today):
{json.dumps(upcoming, indent=2) if no_matches else "NOT NEEDED"}

STANDINGS:
{json.dumps(standings, indent=2)}

RECENT RESULTS:
{json.dumps(recent_results, indent=2)}
"""

    rules = f"""
CRITICAL RULES:
1. ZERO markdown — no **, ###, *, __ anywhere
2. TODAY IS {today}.
3. If matches available today analyze TODAY only.
4. If NO matches today use upcoming fixtures — show their REAL dates clearly.
5. Safe odds: STRICTLY 1.20-1.75 ONLY.
6. Value odds: STRICTLY 1.80-2.50 ONLY.
7. Combo legs: STRICTLY 2.00 or below.
8. ONLY use real teams from data provided.
9. NEVER show "Not available" — if no data skip that pick entirely.
10. NEVER invent fixtures.
11. Cover ALL leagues — not just EPL.
12. Always show exact Naira stakes.
"""

    bankroll_info = f"""
Punter: {name}
Bankroll: {format_naira(br) if br > 0 else "NOT SET — remind user to do /bankroll"}
Strategy: {strategy}
Safe stake: {safe_pct*100:.0f}% = {s_safe}
Value stake: {value_pct*100:.0f}% = {s_value}
Combo stake: 2% = {s_combo}
"""

    if mode == "soon":
        soon_context = f"MATCHES KICKING OFF SOON:\n{json.dumps(soon_matches, indent=2)}" if soon_matches else "NO_SOON_MATCHES"
        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}
{soon_context}

If no soon matches reply exactly: NO_SOON_MATCHES

Format EXACTLY:

⚡ ACE1000 SOON PICKS
{name}, these games kick off very soon! 🇳🇬

━━━━━━━━━━━━━━━━━━━━
For each match:

⚡ [Competition] — [Home vs Away]
Kicks Off: [Time UTC]
Best Bet: [Pick]
Odds: [Odds]
Stake: [% = {s_safe}]

📊 QUICK ANALYSIS:
Form: [Brief from standings]
Verdict: [One sentence]
Confidence: [%]
Risk: [Low/Medium] 🟢🟡
━━━━━━━━━━━━━━━━━━━━

If 2 or more matches:
🔗 QUICK COMBO
Leg 1: [Match - Bet @ Odds]
Leg 2: [Match - Bet @ Odds]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [X]

⚠️ Act fast — these kick off soon!"""

    elif mode == "safe":
        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}
{matches_context}

{"Give 3 SAFE picks from TODAY only." if not no_matches else "No matches today. Give 3 SAFE picks from the upcoming fixtures. Show real match dates."}
Odds STRICTLY 1.20-1.75. Cover different leagues.

{"" if not no_matches else f"NOTE: These are UPCOMING matches, not today. Show the actual date of each match clearly."}

Format EXACTLY:

🛡️ ACE1000 SAFE PICKS — {today}
Hey {name}! {"Low risk picks for today" if not no_matches else "No games today — here are upcoming safe picks"} 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
✅ PICK 1
League: [Competition]
Match: [Real Home vs Away]
{"Kickoff: [Time UTC]" if not no_matches else "Date: [Actual match date and time]"}
Bet: [Pick]
Odds: [1.20-1.75 STRICTLY]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Real stat]
Verdict: [One sentence]
Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 2
League: [Different competition]
Match: [Real Home vs Away]
{"Kickoff: [Time UTC]" if not no_matches else "Date: [Actual match date and time]"}
Bet: [Pick]
Odds: [1.20-1.75 STRICTLY]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 3
League: [Different competition]
Match: [Real Home vs Away]
{"Kickoff: [Time UTC]" if not no_matches else "Date: [Actual match date and time]"}
Bet: [Pick]
Odds: [1.20-1.75 STRICTLY]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [One sentence]
Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
🔗 SAFE COMBO
Combine all 3 on SportyBet:
Combined Odds: [Multiply all 3]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

⚠️ Bet responsibly. Never stake what you cannot afford to lose."""

    elif mode == "combo":
        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}
{matches_context}

{"Build 3 combos from TODAY only." if not no_matches else "No matches today. Build 3 combos from upcoming fixtures. Show real match dates."}
All legs max 2.00 odds. Mix leagues.

Format EXACTLY:

🔗 ACE1000 COMBO BETS — {today}
{name}, {"your data-driven combos for today" if not no_matches else "no games today — here are upcoming combos"} 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
COMBO 1 - BANKER 🏦
Risk: Very Low 🟢

Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]
Win Probability: Very High

━━━━━━━━━━━━━━━━━━━━
COMBO 2 - BALANCED ⚖️
Risk: Medium 🟡

Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]
Win Probability: High

━━━━━━━━━━━━━━━━━━━━
COMBO 3 - JACKPOT 💥
Risk: Higher 🔴

Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 4: [League] [Match] - [Bet] @ [max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]
Win Probability: Medium

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly on SportyBet, {name}!"""

    else:
        if no_matches:
            return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}

NO MATCHES TODAY ({today}).
Use the UPCOMING MATCHES below. Show their REAL dates clearly.
NEVER show "Not available". Only show picks that have real match data.
Cover multiple leagues — not just EPL.

UPCOMING FIXTURES:
{json.dumps(upcoming, indent=2)}

STANDINGS:
{json.dumps(standings, indent=2)}

RECENT RESULTS:
{json.dumps(recent_results, indent=2)}

Format EXACTLY:

📅 ACE1000 UPCOMING PICKS
No matches today ({today}), {name}. Here are the best upcoming games 🇳🇬

📊 Strategy: {strategy}
{staking["description"]}

━━━━━━━━━━━━━━━━━━━━
🛡️ SAFE PICKS (1.20-1.75)

✅ SAFE PICK 1
League: [Real competition]
Match: [Real Home vs Away]
Date: [Actual date e.g. Tuesday 25 Feb]
Kickoff: [Actual time UTC]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Real record]
Form: [Real standings data]
Key Stat: [Real stat]
Verdict: [Confident sentence]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
✅ SAFE PICK 2
League: [Different competition]
Match: [Real Home vs Away]
Date: [Actual date]
Kickoff: [Actual time UTC]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real data]
Key Stat: [Stat]
Verdict: [Conclusion]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
🎯 VALUE PICKS (1.80-2.50)

⭐ VALUE PICK 1
League: [Competition]
Match: [Real Home vs Away]
Date: [Actual date]
Kickoff: [Actual time UTC]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real data]
Key Stat: [Stat]
Verdict: [Conclusion]
Risk: Medium 🟡

⭐ VALUE PICK 2
League: [Different competition]
Match: [Real Home vs Away]
Date: [Actual date]
Kickoff: [Actual time UTC]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real data]
Key Stat: [Stat]
Verdict: [Conclusion]
Risk: Medium 🟡

━━━━━━━━━━━━━━━━━━━━
🔗 COMBO BETS
All legs max 2.00. Mix leagues.

COMBO 1 - BANKER 🏦
Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

COMBO 2 - VALUE ⭐
Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly {name}. Never stake more than you can afford to lose."""

        return f"""You are Ace1000, elite football analyst for {name} on SportyBet Nigeria.
{rules}
{bankroll_info}
{time_context}
{matches_context}

Give a FULL daily betting card for TODAY ONLY.
Include matches from ALL available leagues — not just EPL.
Safe odds 1.20-1.75. Value odds 1.80-2.50. Combo legs max 2.00.
NEVER show "Not available" — skip a pick entirely if no real data exists.

Format EXACTLY:

🎯 ACE1000 DAILY BETTING CARD
{today} — Hey {name}! 🇳🇬

📊 Strategy: {strategy}
{staking["description"]}

━━━━━━━━━━━━━━━━━━━━
🛡️ SAFE PICKS (1.20-1.75 STRICTLY)

✅ SAFE PICK 1
League: [Competition]
Match: [Real Home vs Away — TODAY]
Kickoff: [Time UTC]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Real stat]
Verdict: [Confident sentence]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
✅ SAFE PICK 2
League: [Different competition]
Match: [Real Home vs Away — TODAY]
Kickoff: [Time UTC]
Bet: [Pick]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [Conclusion]
Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
🎯 VALUE PICKS (1.80-2.50 STRICTLY)

⭐ VALUE PICK 1
League: [Competition]
Match: [Real Home vs Away — TODAY]
Kickoff: [Time UTC]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [Conclusion]
Risk: Medium 🟡

⭐ VALUE PICK 2
League: [Different competition]
Match: [Real Home vs Away — TODAY]
Kickoff: [Time UTC]
Bet: [Pick]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Form: [Real standings]
Key Stat: [Stat]
Verdict: [Conclusion]
Risk: Medium 🟡

━━━━━━━━━━━━━━━━━━━━
🔗 COMBO BETS
All legs max 2.00 odds. Mix leagues.

COMBO 1 - BANKER 🏦
Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

COMBO 2 - VALUE ⭐
Leg 1: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 2: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Leg 3: [League] [Match] - [Bet] @ [max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly {name}. Never stake more than you can afford to lose.
Odds reference: {str(odds_data[:5])}"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    await update.message.reply_text(
        f"👋 Welcome to Ace1000, {name}!\n\n"
        f"Your personal SportyBet analyst 🇳🇬\n\n"
        f"💰 Bankroll: {format_naira(bankroll) if bankroll > 0 else 'Not set'}\n\n"
        f"Type /home to see all commands!"
    )

async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    staking = get_staking_strategy(data)
    today = get_today_str()
    await update.message.reply_text(
        f"🏠 ACE1000 HOME\n"
        f"Welcome back, {name}!\n\n"
        f"📅 Today: {today}\n"
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
        f"/soon [hours] — Games kicking off soon\n"
        f"Example: /soon 2\n\n"
        f"💬 CHAT\n"
        f"Just text me anything!\n"
        f"Ask about today's games, teams, or advice.\n"
        f"I have live data.\n\n"
        f"📝 BET TRACKER\n"
        f"/logbet [match] [pick] [odds] [stake]\n"
        f"/result [number] [win/loss]\n"
        f"/mybets — Bet history\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Gemini + Groq\n"
        f"📊 football-data.org + TheSportsDB\n"
        f"🌍 22 leagues covered\n"
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
        f"Martingale — Never recommended. Catastrophic.\n"
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
    await update.message.reply_text(f"✅ Done! I'll call you {name} from now on. 🎯\nUse /home to see all commands.")

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
        data["bankroll_history"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "amount": amount})
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
            "id": len(data["bets"]) + 1, "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
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
        data["bankroll_history"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "amount": data["bankroll"]})
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
    await update.message.reply_text(f"🔍 Fetching matches for {today}, {name}...")
    try:
        odds_data = fetch_todays_odds()
        today_matches = fetch_todays_matches()
        standings = fetch_standings()
        recent = fetch_recent_results()
        upcoming = fetch_upcoming_matches() if not today_matches or sum(len(v) for v in today_matches.values()) == 0 else {}
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
        odds_data = fetch_todays_odds()
        today_matches = fetch_todays_matches()
        standings = fetch_standings()
        recent = fetch_recent_results()
        upcoming = fetch_upcoming_matches() if not today_matches or sum(len(v) for v in today_matches.values()) == 0 else {}
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
        odds_data = fetch_todays_odds()
        today_matches = fetch_todays_matches()
        standings = fetch_standings()
        recent = fetch_recent_results()
        upcoming = fetch_upcoming_matches() if not today_matches or sum(len(v) for v in today_matches.values()) == 0 else {}
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
    await update.message.reply_text(f"⚡ Finding matches in next {hours} hour(s), {name}...")
    try:
        odds_data = fetch_todays_odds()
        today_matches = fetch_todays_matches()
        standings = fetch_standings()
        recent = fetch_recent_results()
        soon_matches = fetch_soon_matches(hours)
        if soon_matches:
            for m in soon_matches:
                m["hours"] = hours
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("soon", today_matches, {}, standings, recent, odds_data, bankroll, staking, soon_matches=soon_matches)
        result = ask_ai(prompt)
        if "NO_SOON_MATCHES" in result:
            await update.message.reply_text(
                f"⚡ No matches in next {hours} hour(s), {name}.\n\n"
                f"Try /soon 6 or /odds for full day card."
            )
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

        await update.message.reply_text(f"🤔 Checking live data, {name}...")

        live = fetch_live_context()

        bets = data.get("bets", [])
        recent_bets = bets[-3:] if bets else []
        won = len([b for b in bets if b.get("result") == "win"])
        lost = len([b for b in bets if b.get("result") == "loss"])

        prompt = f"""You are Ace1000, an elite personal football betting analyst for a Nigerian SportyBet punter named {name}.

You are sharp, direct, and genuinely helpful. You talk like a knowledgeable friend who knows football deeply.
You have access to LIVE data right now — use it to give real, accurate answers.

LIVE DATA:
Current time: {live['now']}
Today: {live['today']}

Today's matches:
{json.dumps(live.get('matches', {}), indent=2)}

{"No matches today. Upcoming: " + json.dumps(live.get('upcoming', {}), indent=2) if live.get('no_matches_today') else ""}

Standings:
{json.dumps(live.get('standings', {}), indent=2)}

Recent results:
{json.dumps(live.get('recent_results', []), indent=2)}

Odds snapshot:
{json.dumps(live.get('odds_snapshot', []), indent=2)}

USER PROFILE:
Name: {name}
Bankroll: {format_naira(bankroll) if bankroll > 0 else "Not set — remind them to use /bankroll"}
Strategy: {staking['strategy']}
Record: {won} wins, {lost} losses
Recent bets: {json.dumps(recent_bets) if recent_bets else "None logged yet"}

RULES:
- ZERO markdown — no **, ###, *, __
- Plain text and emojis only
- Be conversational, sharp, and confident
- Use real data above when answering about specific teams or matches
- If asked for picks reference real fixtures from the data
- Keep responses concise — no walls of text
- Reference SportyBet naturally
- If bankroll not set remind them
- Address them as {name} naturally

User message: {user_message}"""

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
        caption = update.message.caption or f"Analyze these SportyBet odds for {name}. Which have value? Which to avoid? Suggest a combo. Plain text only, no markdown."
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
        odds_data = fetch_todays_odds()
        today_matches = fetch_todays_matches()
        standings = fetch_standings()
        recent = fetch_recent_results()
        upcoming = fetch_upcoming_matches() if not today_matches or sum(len(v) for v in today_matches.values()) == 0 else {}
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_prompt("full", today_matches, upcoming, standings, recent, odds_data, bankroll, staking)
        result = ask_ai(prompt)
        await context.bot.send_message(chat_id=chat_id, text=f"🌅 Good morning {name}! Picks for {today}:\n\n{result}")
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
        odds_data = fetch_todays_odds()
        today_matches = fetch_todays_matches()
        standings = fetch_standings()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        s_value = format_naira(bankroll * staking["value_stake"]) if bankroll > 0 else "Set bankroll first"
        prompt = f"""You are Ace1000. Today is {today}.
Scan TODAY's matches ONLY for ONE outstanding value bet with odds 1.40-2.50.
Only send alert if there is genuine statistical edge from real data.
If nothing stands out reply exactly: NO_ALERT

Format if found:

🚨 VALUE ALERT, {name}! — {today}

League: [Competition]
Match: [Real Home vs Away — TODAY ONLY]
Kickoff: [Time UTC]
Bet: [Pick]
Odds: [1.40-2.50]
Stake: {staking['value_stake']*100:.0f}% = {s_value}

📊 ANALYSIS:
Form: [Brief real data]
Verdict: [One sentence]
Risk: [Low/Medium]

No markdown symbols.

Today's matches: {json.dumps(today_matches)}
Standings: {json.dumps(standings)}
Odds: {str(odds_data[:3])}"""
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

    print(f"🚀 Ace1000Bot LIVE! {get_today_str()}. Smart chat. All leagues. No more Not available.")
    app.run_polling()

if __name__ == "__main__":
    main()