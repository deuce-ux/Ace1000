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

STYLE = """
CRITICAL FORMATTING RULES — FOLLOW ALL STRICTLY OR RESPONSE IS INVALID:
1. ZERO markdown symbols — no **, ###, *, __, or ~ anywhere
2. ZERO bullet points with * or -
3. Emojis and plain text ONLY
4. Use ━━━━━━━━━━━━━━━━━━━━ as dividers
5. SAFE PICKS must have odds strictly between 1.20 and 1.75 — NEVER above 1.75
6. VALUE PICKS must have odds strictly between 1.80 and 2.50 — NEVER outside this range
7. COMBO legs must NEVER exceed 2.00 odds each — if a team's win odds exceed 2.00 use BTTS or Over 1.5 instead
8. Always show exact Naira stake amounts
9. Address user by name naturally
10. Base ALL analysis on the real data provided — no made up statistics
"""

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "name": "Champ",
        "bankroll": 0,
        "initial_bankroll": 0,
        "bets": [],
        "bankroll_history": [],
        "chat_id": None
    }

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
        return {
            "strategy": "DEFENSIVE",
            "description": "Bankroll below 50% of start. Flat stake at 1-2% only. Protect capital first.",
            "safe_stake": 0.01,
            "value_stake": 0.02,
            "win_rate": win_rate,
            "bankroll_ratio": bankroll_ratio
        }
    elif bankroll_ratio < 0.75:
        return {
            "strategy": "CONSERVATIVE FLAT",
            "description": "Bankroll recovering. Flat stake at 2-3%. Stay disciplined.",
            "safe_stake": 0.02,
            "value_stake": 0.03,
            "win_rate": win_rate,
            "bankroll_ratio": bankroll_ratio
        }
    elif win_rate >= 0.6 and bankroll_ratio >= 1.0:
        return {
            "strategy": "KELLY CRITERION",
            "description": "Winning consistently and bankroll growing. Quarter Kelly active. Safe 5%, value 3%.",
            "safe_stake": 0.05,
            "value_stake": 0.03,
            "win_rate": win_rate,
            "bankroll_ratio": bankroll_ratio
        }
    elif win_rate >= 0.5 and bankroll_ratio >= 0.9:
        return {
            "strategy": "FLAT STAKING",
            "description": "Solid results. Standard flat staking. Safe 5%, value 3%, combos 2%.",
            "safe_stake": 0.05,
            "value_stake": 0.03,
            "win_rate": win_rate,
            "bankroll_ratio": bankroll_ratio
        }
    else:
        return {
            "strategy": "CONSERVATIVE FLAT",
            "description": "Mixed results. Stay conservative. Safe 3%, value 2%.",
            "safe_stake": 0.03,
            "value_stake": 0.02,
            "win_rate": win_rate,
            "bankroll_ratio": bankroll_ratio
        }

def ask_ai(prompt, image_bytes=None):
    try:
        if image_bytes:
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt
                ]
            )
        else:
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=prompt
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
            raise Exception(f"Both AI models failed. Gemini: {gemini_error}. Groq: {groq_error}")

def fetch_odds():
    all_data = []
    sports = ["soccer_epl", "soccer_uefa_champs_league", "soccer_africa_cup_of_nations"]
    for sport in sports:
        for market in ["h2h", "totals", "btts"]:
            try:
                url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
                params = {
                    "apiKey": ODDS_API_KEY,
                    "regions": "uk",
                    "markets": market,
                    "oddsFormat": "decimal"
                }
                response = requests.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        all_data.extend(data[:1])
            except:
                continue
    return all_data

def fetch_real_stats():
    stats = {}
    try:
        headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}

        for comp_code, comp_key in [("PL", "epl"), ("CL", "cl")]:
            try:
                url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches?status=SCHEDULED"
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    matches = data.get("matches", [])[:5]
                    stats[f"{comp_key}_matches"] = [
                        {
                            "home": m["homeTeam"]["name"],
                            "away": m["awayTeam"]["name"],
                            "date": m.get("utcDate", "")
                        } for m in matches
                    ]
            except:
                continue

        try:
            url = "https://api.football-data.org/v4/competitions/PL/standings"
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                table = data.get("standings", [{}])[0].get("table", [])[:12]
                stats["epl_standings"] = [
                    {
                        "position": t["position"],
                        "team": t["team"]["name"],
                        "played": t["playedGames"],
                        "won": t["won"],
                        "drawn": t["draw"],
                        "lost": t["lost"],
                        "goals_for": t["goalsFor"],
                        "goals_against": t["goalsAgainst"],
                        "points": t["points"]
                    } for t in table
                ]
        except:
            pass

        logging.info("Real stats fetched")
    except Exception as e:
        logging.error(f"Stats error: {e}")
    return stats

def fetch_team_news():
    news = {}
    try:
        leagues = [("4328", "EPL"), ("4335", "Champions_League")]
        for league_id, league_name in leagues:
            try:
                url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventsnextleague.php?id={league_id}"
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    events = data.get("events", []) or []
                    news[league_name] = [
                        {
                            "match": f"{e.get('strHomeTeam')} vs {e.get('strAwayTeam')}",
                            "date": e.get("dateEvent"),
                            "time": e.get("strTime"),
                            "venue": e.get("strVenue", "")
                        } for e in events[:5]
                    ]
            except:
                continue

            try:
                url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventspastleague.php?id={league_id}"
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    events = data.get("events", []) or []
                    news[f"{league_name}_results"] = [
                        {
                            "match": f"{e.get('strHomeTeam')} vs {e.get('strAwayTeam')}",
                            "score": f"{e.get('intHomeScore')}-{e.get('intAwayScore')}",
                            "date": e.get("dateEvent")
                        } for e in events[-8:]
                    ]
            except:
                continue

        logging.info("Team news fetched")
    except Exception as e:
        logging.error(f"TheSportsDB error: {e}")
    return news

def fetch_soon_matches(hours=2):
    soon = []
    try:
        headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        for comp in ["PL", "CL"]:
            try:
                url = f"https://api.football-data.org/v4/competitions/{comp}/matches?status=SCHEDULED"
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    for match in data.get("matches", []):
                        match_time = datetime.fromisoformat(
                            match["utcDate"].replace("Z", "+00:00")
                        )
                        if now <= match_time <= cutoff:
                            soon.append({
                                "home": match["homeTeam"]["name"],
                                "away": match["awayTeam"]["name"],
                                "kickoff": match["utcDate"],
                                "competition": comp
                            })
            except:
                continue

        logging.info(f"Found {len(soon)} matches in next {hours} hours")
    except Exception as e:
        logging.error(f"Soon matches error: {e}")
    return soon

def build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="full", soon_matches=None):
    name = get_name()
    br = bankroll
    strategy = staking["strategy"]
    safe_pct = staking["safe_stake"]
    value_pct = staking["value_stake"]
    s_safe = format_naira(br * safe_pct) if br > 0 else "⚠️ Set bankroll first!"
    s_value = format_naira(br * value_pct) if br > 0 else "⚠️ Set bankroll first!"
    s_combo = format_naira(br * 0.02) if br > 0 else "⚠️ Set bankroll first!"

    bankroll_info = (
        f"Punter: {name}\n"
        f"Bankroll: {format_naira(br) if br > 0 else 'NOT SET'}\n"
        f"Active Strategy: {strategy}\n"
        f"Safe stake: {safe_pct*100:.0f}% = {s_safe}\n"
        f"Value stake: {value_pct*100:.0f}% = {s_value}\n"
        f"Combo stake: 2% = {s_combo}"
    )

    stats_summary = f"REAL STANDINGS AND FIXTURES:\n{json.dumps(real_stats, indent=2)}" if real_stats else ""
    news_summary = f"UPCOMING MATCHES AND RECENT RESULTS:\n{json.dumps(team_news, indent=2)}" if team_news else ""
    soon_summary = f"MATCHES KICKING OFF SOON:\n{json.dumps(soon_matches, indent=2)}" if soon_matches else ""

    base_rules = f"""{STYLE}
You are Ace1000, an elite football betting analyst for a Nigerian SportyBet punter named {name}.
{bankroll_info}

{stats_summary}
{news_summary}
{soon_summary}

STRICT ODDS RULES — VIOLATION = INVALID RESPONSE:
- SAFE picks: odds MUST be 1.20 to 1.75 ONLY. Not 1.76, not 1.80. Maximum 1.75.
- VALUE picks: odds MUST be 1.80 to 2.50 ONLY.
- COMBO legs: odds MUST be 2.00 or below ONLY. If team win odds exceed 2.00 use Over 1.5 or BTTS instead.
- If you cannot find a bet within the range DO NOT invent one. Skip that pick.
- Use ONLY real teams from the data provided above.
- Base ALL statistics on real data. Do not invent statistics.
"""

    if mode == "safe":
        return f"""{base_rules}

Give 3 SAFE picks only. Odds strictly 1.20-1.75.
Best options: Over 1.5 Goals, BTTS Yes, Double Chance, heavy favorites.

Format EXACTLY:

🛡️ ACE1000 SAFE PICKS
Hey {name}! High confidence. Low risk. SportyBet ready 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
✅ PICK 1
Match: [Real match from data]
Bet: [Pick — must give odds 1.20-1.75]
Odds: [1.20-1.75 ONLY]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Real or knowledge-based record]
Recent Form: [Based on real results data]
Key Stat: [Real stat from data]
Verdict: [One confident sentence]

Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 2
Match: [Real match]
Bet: [Pick — odds 1.20-1.75]
Odds: [1.20-1.75 ONLY]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Recent Form: [Real results]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 3
Match: [Real match]
Bet: [Pick — odds 1.20-1.75]
Odds: [1.20-1.75 ONLY]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Recent Form: [Real results]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
🔗 SAFE COMBO
All 3 picks combined on SportyBet:
Combined Odds: [Multiply all 3 — must be realistic]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

⚠️ Bet responsibly. Never stake what you cannot afford to lose.

Odds data: {str(odds_data[:3])}"""

    elif mode == "combo":
        return f"""{base_rules}

Build 3 combo bets. ALL legs MUST be 2.00 odds or below. No exceptions.

Format EXACTLY:

🔗 ACE1000 COMBO BETS
{name}, your data-driven combos for SportyBet 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
COMBO 1 - BANKER 🏦
Risk: Very Low 🟢
Every leg max 2.00 odds

Leg 1: [Real Match - Bet @ max 2.00] — [One sentence from real data]
Leg 2: [Real Match - Bet @ max 2.00] — [One sentence]
Leg 3: [Real Match - Bet @ max 2.00] — [One sentence]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]
Win Probability: Very High

━━━━━━━━━━━━━━━━━━━━
COMBO 2 - BALANCED ⚖️
Risk: Medium 🟡
Every leg max 2.00 odds

Leg 1: [Real Match - Bet @ max 2.00] — [Why]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]
Win Probability: High

━━━━━━━━━━━━━━━━━━━━
COMBO 3 - JACKPOT 💥
Risk: Higher 🔴
Every leg max 2.00 odds

Leg 1: [Real Match - Bet @ max 2.00] — [Why]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]
Leg 4: [Real Match - Bet @ max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]
Win Probability: Medium

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly on SportyBet, {name}!

Odds data: {str(odds_data[:3])}"""

    elif mode == "soon":
        return f"""{base_rules}

IMPORTANT: Only analyze the MATCHES KICKING OFF SOON listed above.
If no matches are kicking off soon say exactly: NO_SOON_MATCHES
Give fast analysis for immediate betting decisions.

Format EXACTLY:

⚡ ACE1000 LIVE SOON PICKS
{name}, these games kick off very soon! 🇳🇬

📊 Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
For each match kicking off soon:

⚡ MATCH [number]
Match: [Home vs Away]
Kicks Off: [Time]
Best Bet: [Pick — specify odds range]
Odds: [Estimated odds]
Stake: [Appropriate % = Naira amount]

📊 QUICK ANALYSIS:
Form: [Brief real form summary]
Key Reason: [One sentence why this bet]
Confidence: [%]

Risk: [Low/Medium] 🟢🟡
━━━━━━━━━━━━━━━━━━━━

If 2 or more matches found, add:
🔗 QUICK COMBO
[Combine the best 2 picks]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [X]

⚠️ Act fast — these kick off soon!

Odds data: {str(odds_data[:3])}"""

    else:
        return f"""{base_rules}

Give a FULL daily betting card using real data only.
STRICT: Safe odds 1.20-1.75. Value odds 1.80-2.50. Combo legs max 2.00.

Format EXACTLY:

🎯 ACE1000 DAILY BETTING CARD
Hey {name}! Here are today's best picks 🇳🇬

📊 Active Strategy: {strategy}
{staking["description"]}

━━━━━━━━━━━━━━━━━━━━
🛡️ SAFE PICKS (Odds 1.20 - 1.75 STRICTLY)

✅ SAFE PICK 1
Match: [Real match from data]
Bet: [Pick giving odds 1.20-1.75]
Odds: [1.20-1.75 ONLY — reject anything above 1.75]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Real record]
Recent Form: [From real results data]
Key Stat: [Real stat]
Verdict: [Confident one sentence]

Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
✅ SAFE PICK 2
Match: [Real match]
Bet: [Pick giving odds 1.20-1.75]
Odds: [1.20-1.75 ONLY]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ANALYSIS:
H2H: [Record]
Recent Form: [Real data]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
🎯 VALUE PICK (Odds 1.80 - 2.50 STRICTLY)

⭐ VALUE PICK 1
Match: [Real match]
Bet: [Pick giving odds 1.80-2.50]
Odds: [1.80-2.50 ONLY]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Record]
Recent Form: [Real data]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Medium 🟡

━━━━━━━━━━━━━━━━━━━━
🔗 COMBO BETS
RULE: Every leg MUST be 2.00 odds or below. Use Over 1.5 or BTTS if team win odds exceed 2.00.

COMBO 1 - BANKER 🏦
Leg 1: [Real Match - Bet @ max 2.00] — [Why from real data]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

COMBO 2 - VALUE ⭐
Leg 1: [Real Match - Bet @ max 2.00] — [Why]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [Combined odds x {s_combo}]

━━━━━━━━━━━━━━━━━━━━
⚠️ Bet responsibly {name}. Never stake more than you can afford to lose.

Odds data: {str(odds_data[:3])}"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    bankroll_status = format_naira(bankroll) if bankroll > 0 else "Not set"
    await update.message.reply_text(
        f"👋 Welcome to Ace1000, {name}!\n\n"
        f"Your personal SportyBet analyst 🇳🇬\n\n"
        f"💰 Bankroll: {bankroll_status}\n\n"
        f"Type /home to see all commands!"
    )

async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bankroll = data.get("bankroll", 0)
    staking = get_staking_strategy(data)
    bankroll_status = format_naira(bankroll) if bankroll > 0 else "Not set"
    await update.message.reply_text(
        f"🏠 ACE1000 HOME\n"
        f"Welcome back, {name}!\n\n"
        f"💰 Bankroll: {bankroll_status}\n"
        f"📊 Active Strategy: {staking['strategy']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 ALL COMMANDS\n\n"
        f"👤 PROFILE\n"
        f"/setname [name] — Set your name\n"
        f"/bankroll [amount] — Set your bankroll\n"
        f"/mystats — Full performance stats\n"
        f"/strategy — View active staking strategy\n"
        f"/history — Bankroll history\n\n"
        f"⚽ BETTING PICKS\n"
        f"/odds — Full daily betting card\n"
        f"/safe — Safe picks only (1.20-1.75)\n"
        f"/combo — Combo bets only\n"
        f"/soon [hours] — Picks for games kicking off soon\n"
        f"Example: /soon 2 (games in next 2 hours)\n\n"
        f"📝 BET TRACKER\n"
        f"/logbet [match] [pick] [odds] [stake]\n"
        f"Example: /logbet ArsenalvsChelsea Over2.5 1.65 2000\n"
        f"/result [number] [win/loss] — Update result\n"
        f"/mybets — See all your bets\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Gemini + Groq powered\n"
        f"📊 football-data.org + TheSportsDB\n"
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
        f"💡 STRATEGY GUIDE:\n"
        f"Martingale — Never recommended. Catastrophic long term.\n"
        f"Flat Staking — Safe and sustainable for consistent bettors.\n"
        f"Kelly Criterion — Optimal when winning consistently.\n"
        f"Defensive — Protects capital during losing streaks.\n\n"
        f"Ace1000 adjusts automatically as your results change. 🎯"
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
    await update.message.reply_text(
        f"✅ Done! I'll call you {name} from now on.\n\n"
        f"Welcome to Ace1000, {name}! 🎯\n"
        f"Use /home to see all commands."
    )

async def set_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Example: /bankroll 50000")
        return
    try:
        amount = float(context.args[0].replace(",", ""))
        if amount <= 0:
            await update.message.reply_text("❌ Bankroll must be greater than 0.")
            return
        data = load_data()
        data["bankroll"] = amount
        data["chat_id"] = update.effective_chat.id
        if data["initial_bankroll"] == 0:
            data["initial_bankroll"] = amount
        data["bankroll_history"].append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "amount": amount
        })
        save_data(data)
        name = data.get("name", "Champ")
        staking = get_staking_strategy(data)
        await update.message.reply_text(
            f"✅ Bankroll set to {format_naira(amount)}, {name}!\n\n"
            f"{stake_breakdown(amount)}\n\n"
            f"📊 Active Strategy: {staking['strategy']}\n"
            f"{staking['description']}\n\n"
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
    total_bets = len(bets)
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
        f"💰 Current Bankroll: {format_naira(bankroll)}\n"
        f"🏦 Starting Bankroll: {format_naira(initial)}\n"
        f"📈 Bankroll Growth: {growth:+.1f}%\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 BETTING RECORD\n"
        f"Total Bets: {total_bets}\n"
        f"✅ Won: {won}\n"
        f"❌ Lost: {lost}\n"
        f"⏳ Pending: {pending}\n"
        f"Win Rate: {(won/max(won+lost,1)*100):.1f}%\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 FINANCIALS\n"
        f"Total Staked: {format_naira(total_staked)}\n"
        f"Total Returned: {format_naira(total_returned)}\n"
        f"Profit/Loss: {format_naira(profit)}\n"
        f"ROI: {roi:+.1f}%\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Active Strategy: {staking['strategy']}\n\n"
        f"{stake_breakdown(bankroll)}"
    )

async def bankroll_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    history = data.get("bankroll_history", [])
    if not history:
        await update.message.reply_text(f"No bankroll history yet, {name}.")
        return
    lines = [f"📅 {name}'s BANKROLL HISTORY\n━━━━━━━━━━━━━━━━━━━━"]
    for entry in history[-10:]:
        lines.append(f"{entry['date']}: {format_naira(entry['amount'])}")
    await update.message.reply_text("\n".join(lines))

async def log_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "❌ Format: /logbet [match] [pick] [odds] [stake]\n"
            "Example: /logbet ArsenalvsChelsea Over2.5 1.65 2000"
        )
        return
    try:
        match = context.args[0]
        pick = context.args[1]
        odds = float(context.args[2])
        stake = float(context.args[3])
        potential = odds * stake
        data = load_data()
        bet = {
            "id": len(data["bets"]) + 1,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "match": match,
            "pick": pick,
            "odds": odds,
            "stake": stake,
            "potential": potential,
            "result": "pending"
        }
        data["bets"].append(bet)
        save_data(data)
        name = data.get("name", "Champ")
        await update.message.reply_text(
            f"✅ Bet #{bet['id']} logged, {name}!\n\n"
            f"Match: {match}\n"
            f"Pick: {pick}\n"
            f"Odds: {odds}\n"
            f"Stake: {format_naira(stake)}\n"
            f"Potential Win: {format_naira(potential)}\n\n"
            f"Use /result {bet['id']} win or /result {bet['id']} loss to update!"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid odds or stake.")

async def update_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Format: /result [bet number] [win/loss]")
        return
    try:
        bet_id = int(context.args[0])
        result = context.args[1].lower()
        if result not in ["win", "loss"]:
            await update.message.reply_text("❌ Result must be win or loss")
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
            data["bankroll_history"].append({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "amount": data["bankroll"]
            })
            save_data(data)
            staking = get_staking_strategy(data)
            await update.message.reply_text(
                f"🎉 YES {name.upper()}! BET #{bet_id} WON!\n\n"
                f"Match: {bet['match']}\n"
                f"Pick: {bet['pick']}\n"
                f"Profit: +{format_naira(profit)}\n"
                f"New Bankroll: {format_naira(data['bankroll'])}\n\n"
                f"📊 Strategy: {staking['strategy']}\n"
                f"Ace1000 delivering! 🔥"
            )
        else:
            data["bankroll"] = max(0, bankroll - bet["stake"])
            data["bankroll_history"].append({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "amount": data["bankroll"]
            })
            save_data(data)
            staking = get_staking_strategy(data)
            await update.message.reply_text(
                f"❌ Bet #{bet_id} lost, {name}.\n\n"
                f"Match: {bet['match']}\n"
                f"Pick: {bet['pick']}\n"
                f"Lost: -{format_naira(bet['stake'])}\n"
                f"New Bankroll: {format_naira(data['bankroll'])}\n\n"
                f"📊 Strategy: {staking['strategy']}\n"
                f"{staking['description']}\n\n"
                f"Stay disciplined 💪"
            )
    except ValueError:
        await update.message.reply_text("❌ Invalid bet number.")

async def my_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    bets = data.get("bets", [])
    if not bets:
        await update.message.reply_text(f"No bets logged yet, {name}. Use /logbet to record your bets!")
        return
    lines = [f"📝 {name}'s BET HISTORY\n━━━━━━━━━━━━━━━━━━━━"]
    for bet in bets[-10:]:
        emoji = "✅" if bet["result"] == "win" else "❌" if bet["result"] == "loss" else "⏳"
        lines.append(
            f"{emoji} #{bet['id']} {bet['match']}\n"
            f"   {bet['pick']} @ {bet['odds']} | Stake: {format_naira(bet['stake'])}"
        )
    await update.message.reply_text("\n".join(lines))

async def get_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    await update.message.reply_text(f"🔍 Fetching real stats for you, {name}...")
    try:
        odds_data = fetch_odds()
        real_stats = fetch_real_stats()
        team_news = fetch_team_news()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="full")
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def safe_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    await update.message.reply_text(f"🛡️ Fetching safe picks for you, {name}...")
    try:
        odds_data = fetch_odds()
        real_stats = fetch_real_stats()
        team_news = fetch_team_news()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="safe")
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def combo_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    await update.message.reply_text(f"🔗 Building combos for you, {name}...")
    try:
        odds_data = fetch_odds()
        real_stats = fetch_real_stats()
        team_news = fetch_team_news()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="combo")
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
            hours = int(context.args[0])
            if hours < 1 or hours > 24:
                hours = 2
        except:
            hours = 2
    await update.message.reply_text(f"⚡ Finding matches kicking off in the next {hours} hour(s), {name}...")
    try:
        odds_data = fetch_odds()
        real_stats = fetch_real_stats()
        team_news = fetch_team_news()
        soon_matches = fetch_soon_matches(hours)
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="soon", soon_matches=soon_matches)
        result = ask_ai(prompt)
        if "NO_SOON_MATCHES" in result:
            await update.message.reply_text(
                f"⚡ No matches found kicking off in the next {hours} hour(s), {name}.\n\n"
                f"Try /soon 6 for the next 6 hours or /odds for today's full card."
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
        bankroll_info = (
            f"User: {name}. Bankroll: {format_naira(bankroll)}. Strategy: {staking['strategy']}."
            if bankroll > 0 else f"User: {name}. No bankroll set yet."
        )
        user_message = update.message.text
        prompt = (
            f"{STYLE}\nYou are Ace1000, a sharp football betting analyst for a Nigerian SportyBet punter. "
            f"{bankroll_info}. The user says: {user_message}\n\n"
            f"Address them as {name}. Be helpful, friendly, and reference SportyBet. No markdown symbols."
        )
        result = ask_ai(prompt)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    name = data.get("name", "Champ")
    await update.message.reply_text(f"📸 Analyzing your odds image, {name}...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        caption = (
            update.message.caption or
            f"Analyze these SportyBet odds for {name}. Which have value? Which to avoid? Suggest a combo. No markdown."
        )
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
    try:
        odds_data = fetch_odds()
        real_stats = fetch_real_stats()
        team_news = fetch_team_news()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        prompt = build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="full")
        result = ask_ai(prompt)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🌅 Good morning {name}! Ace1000 has your picks ready:\n\n{result}"
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
            f"📊 WEEKLY SUMMARY\n"
            f"Hey {name}! Here is how your week went:\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Won: {won}\n"
            f"❌ Lost: {lost}\n"
            f"Win Rate: {(won/max(won+lost,1)*100):.1f}%\n"
            f"Profit/Loss: {format_naira(profit)}\n"
            f"Bankroll Growth: {growth:+.1f}%\n"
            f"Current Bankroll: {format_naira(bankroll)}\n\n"
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
    try:
        odds_data = fetch_odds()
        real_stats = fetch_real_stats()
        team_news = fetch_team_news()
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        s_value = format_naira(bankroll * staking["value_stake"]) if bankroll > 0 else "Set bankroll first"
        prompt = f"""{STYLE}
You are Ace1000. Scan for ONE outstanding value bet using real data below.
Only alert if odds are 1.40-2.50 AND there is clear statistical edge from real data.
If nothing genuinely stands out reply with exactly: NO_ALERT

Format if found:

🚨 VALUE ALERT, {name}!

Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.40-2.50]
Stake: {staking['value_stake']*100:.0f}% = {s_value}

📊 ANALYSIS:
H2H: [Real record]
Recent Form: [From actual results]
Key Stat: [Real stat]
Verdict: [One sentence]

Risk: [Low/Medium]
Strategy: {staking['strategy']}

Real stats: {json.dumps(real_stats)}
Team news: {json.dumps(team_news)}
Odds data: {str(odds_data[:2])}"""
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
    job_queue.run_daily(
        morning_briefing,
        time=datetime.strptime("07:00", "%H:%M").time()
    )
    job_queue.run_daily(
        weekly_summary,
        time=datetime.strptime("20:00", "%H:%M").time(),
        days=(6,)
    )
    job_queue.run_repeating(value_alert, interval=21600, first=60)

    print("🚀 Ace1000Bot is LIVE! Real stats. Smart staking. Gemini + Groq.")
    app.run_polling()

if __name__ == "__main__":
    main()