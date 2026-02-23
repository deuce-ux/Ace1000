import logging
import os
import requests
import json
from datetime import datetime
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
IMPORTANT FORMATTING RULES — FOLLOW STRICTLY:
- Do NOT use markdown symbols like **, ###, *, or __ anywhere
- Do NOT use bullet points with * or -
- Use emojis and plain text only
- Use clean dividers like ━━━━━━━━━━━━━━━━━━━━
- Every pick MUST include detailed reasoning with real H2H stats, form, and verdict
- Be confident and direct like a professional analyst
- Address the user by name naturally
- COMBO LEGS must have maximum odds of 2.00 each — no higher
- Only recommend bets with genuine statistical edge
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
        strategy = "DEFENSIVE"
        description = "Your bankroll is below 50% of starting amount. Flat staking at 1-2% per bet. Protect what you have first."
        safe_stake = 0.01
        value_stake = 0.02
    elif bankroll_ratio < 0.75:
        strategy = "CONSERVATIVE FLAT"
        description = "Bankroll is recovering. Flat staking at 2-3% per bet. Stay disciplined and let it rebuild."
        safe_stake = 0.02
        value_stake = 0.03
    elif win_rate >= 0.6 and bankroll_ratio >= 1.0:
        strategy = "KELLY CRITERION"
        description = "You are winning consistently and bankroll is growing. Quarter Kelly — safe picks 5%, value picks 3%."
        safe_stake = 0.05
        value_stake = 0.03
    elif win_rate >= 0.5 and bankroll_ratio >= 0.9:
        strategy = "FLAT STAKING"
        description = "Solid performance. Standard flat staking. Safe picks 5%, value picks 3%, combos 2%."
        safe_stake = 0.05
        value_stake = 0.03
    else:
        strategy = "CONSERVATIVE FLAT"
        description = "Mixed results. Stay conservative. Safe picks 3%, value picks 2%."
        safe_stake = 0.03
        value_stake = 0.02

    return {
        "strategy": strategy,
        "description": description,
        "safe_stake": safe_stake,
        "value_stake": value_stake,
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
        logging.info(f"Gemini failed ({gemini_error}), switching to Groq...")
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

        epl_url = "https://api.football-data.org/v4/competitions/PL/matches?status=SCHEDULED"
        epl_response = requests.get(epl_url, headers=headers, timeout=10)
        if epl_response.status_code == 200:
            epl_data = epl_response.json()
            matches = epl_data.get("matches", [])[:5]
            stats["epl_matches"] = []
            for match in matches:
                stats["epl_matches"].append({
                    "home": match["homeTeam"]["name"],
                    "away": match["awayTeam"]["name"],
                    "date": match.get("utcDate", "")
                })

        cl_url = "https://api.football-data.org/v4/competitions/CL/matches?status=SCHEDULED"
        cl_response = requests.get(cl_url, headers=headers, timeout=10)
        if cl_response.status_code == 200:
            cl_data = cl_response.json()
            matches = cl_data.get("matches", [])[:3]
            stats["cl_matches"] = []
            for match in matches:
                stats["cl_matches"].append({
                    "home": match["homeTeam"]["name"],
                    "away": match["awayTeam"]["name"],
                    "date": match.get("utcDate", "")
                })

        standings_url = "https://api.football-data.org/v4/competitions/PL/standings"
        standings_response = requests.get(standings_url, headers=headers, timeout=10)
        if standings_response.status_code == 200:
            standings_data = standings_response.json()
            table = standings_data.get("standings", [{}])[0].get("table", [])[:10]
            stats["epl_standings"] = []
            for team in table:
                stats["epl_standings"].append({
                    "position": team["position"],
                    "team": team["team"]["name"],
                    "played": team["playedGames"],
                    "won": team["won"],
                    "drawn": team["draw"],
                    "lost": team["lost"],
                    "goals_for": team["goalsFor"],
                    "goals_against": team["goalsAgainst"],
                    "points": team["points"]
                })

        logging.info("Real stats fetched successfully")
    except Exception as e:
        logging.error(f"Stats fetch error: {e}")

    return stats

def fetch_team_news():
    news = {}
    try:
        leagues = [
            {"id": "4328", "name": "EPL"},
            {"id": "4335", "name": "Champions League"},
        ]

        for league in leagues:
            url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventsnextleague.php?id={league['id']}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                events = data.get("events", []) or []
                news[league["name"]] = []
                for event in events[:5]:
                    news[league["name"]].append({
                        "match": f"{event.get('strHomeTeam')} vs {event.get('strAwayTeam')}",
                        "date": event.get("dateEvent"),
                        "time": event.get("strTime"),
                        "venue": event.get("strVenue"),
                        "home_form": event.get("strHomeTeamBadge", ""),
                        "status": event.get("strStatus", "")
                    })

        # Fetch last results for form
        for league in leagues:
            url = f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventspastleague.php?id={league['id']}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                events = data.get("events", []) or []
                key = f"{league['name']}_results"
                news[key] = []
                for event in events[-8:]:
                    news[key].append({
                        "match": f"{event.get('strHomeTeam')} vs {event.get('strAwayTeam')}",
                        "score": f"{event.get('intHomeScore')}-{event.get('intAwayScore')}",
                        "date": event.get("dateEvent")
                    })

        logging.info("Team news fetched from TheSportsDB")
    except Exception as e:
        logging.error(f"TheSportsDB fetch error: {e}")

    return news

def build_odds_prompt(odds_data, real_stats, team_news, bankroll, staking, mode="full"):
    name = get_name()
    br = bankroll
    strategy = staking["strategy"]
    safe_pct = staking["safe_stake"]
    value_pct = staking["value_stake"]
    s_safe = format_naira(br * safe_pct) if br > 0 else "Set bankroll first"
    s_value = format_naira(br * value_pct) if br > 0 else "Set bankroll first"
    s_combo = format_naira(br * 0.02) if br > 0 else "Set bankroll first"

    bankroll_info = (
        f"Punter: {name}\n"
        f"Bankroll: {format_naira(br)}\n"
        f"Active Strategy: {strategy}\n"
        f"Safe stake: {safe_pct*100:.0f}% = {s_safe}\n"
        f"Value stake: {value_pct*100:.0f}% = {s_value}\n"
        f"Combo stake: 2% = {s_combo}"
    )

    stats_summary = f"REAL MATCH DATA:\n{json.dumps(real_stats, indent=2)}" if real_stats else ""
    news_summary = f"UPCOMING MATCHES AND RECENT RESULTS:\n{json.dumps(team_news, indent=2)}" if team_news else ""

    if mode == "safe":
        return f"""{STYLE}
You are Ace1000, an elite football betting analyst for a Nigerian SportyBet punter named {name}.
{bankroll_info}

{stats_summary}

{news_summary}

Using ALL the real data above including recent results and upcoming fixtures, give 3 SAFE picks.
Odds must be 1.20-1.75 only. Focus on Over 1.5, BTTS, Double Chance, strong favorites.
Base reasoning on actual recent form from results data.

Format EXACTLY:

🛡️ ACE1000 SAFE PICKS
Hey {name}! High confidence. Low risk. SportyBet ready 🇳🇬

📊 Active Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
✅ PICK 1
Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.20-1.75]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ACE1000 ANALYSIS:
H2H: [Record from data]
Recent Form: [Based on actual recent results]
Key Stat: [Real stat from data]
Verdict: [Confident conclusion]

Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 2
Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.20-1.75]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ACE1000 ANALYSIS:
H2H: [Record]
Recent Form: [Actual recent results]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
✅ PICK 3
Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.20-1.75]
Confidence: [85-95%]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ACE1000 ANALYSIS:
H2H: [Record]
Recent Form: [Actual recent results]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Low 🟢
━━━━━━━━━━━━━━━━━━━━
🔗 SAFE COMBO
Combine all 3 on SportyBet:
Combined Odds: [multiply all 3]
Stake: 2% = {s_combo}
Potential Return: [combined odds x {s_combo}]

⚠️ Bet responsibly. Never stake what you cannot afford to lose.

Odds data: {str(odds_data[:3])}"""

    elif mode == "combo":
        return f"""{STYLE}
You are Ace1000, an elite football betting analyst for a Nigerian SportyBet punter named {name}.
{bankroll_info}

{stats_summary}

{news_summary}

STRICT RULE: Every combo leg must have maximum odds of 2.00. No leg above 2.00.
Use real upcoming fixtures and recent form from the data above.

Format EXACTLY:

🔗 ACE1000 COMBO BETS
{name}, here are your data-driven combos 🇳🇬

📊 Active Strategy: {strategy}

━━━━━━━━━━━━━━━━━━━━
COMBO 1 - BANKER 🏦
Risk: Very Low 🟢

Leg 1: [Real Match - Bet @ max 2.00] — [Why based on recent form]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [combined odds x {s_combo}]
Win Probability: Very High

━━━━━━━━━━━━━━━━━━━━
COMBO 2 - BALANCED ⚖️
Risk: Medium 🟡

Leg 1: [Real Match - Bet @ max 2.00] — [Why]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [combined odds x {s_combo}]
Win Probability: High

━━━━━━━━━━━━━━━━━━━━
COMBO 3 - JACKPOT 💥
Risk: Higher 🔴

Leg 1: [Real Match - Bet @ max 2.00] — [Why]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]
Leg 4: [Real Match - Bet @ max 2.00] — [Why]

Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [combined odds x {s_combo}]
Win Probability: Medium

━━━━━━━━━━━━━━━━━━━━
⚠️ Always bet responsibly on SportyBet, {name}!

Odds data: {str(odds_data[:3])}"""

    else:
        return f"""{STYLE}
You are Ace1000, an elite football betting analyst for a Nigerian SportyBet punter named {name}.
{bankroll_info}

{stats_summary}

{news_summary}

Using ALL real data above including upcoming fixtures and recent results, give a FULL daily betting card.
COMBO LEGS: Maximum 2.00 odds per leg. No exceptions.
Base all analysis on actual data provided.

Format EXACTLY:

🎯 ACE1000 DAILY BETTING CARD
Hey {name}! Here are today's best picks 🇳🇬

📊 Active Strategy: {strategy}
{staking["description"]}

━━━━━━━━━━━━━━━━━━━━
🛡️ SAFE PICKS (Odds 1.20 - 1.75)

✅ SAFE PICK 1
Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.20-1.75]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ACE1000 ANALYSIS:
H2H: [Record]
Recent Form: [From actual results data]
Key Stat: [Real stat]
Verdict: [Confident conclusion]

Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
✅ SAFE PICK 2
Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.20-1.75]
Stake: {safe_pct*100:.0f}% = {s_safe}

📊 ACE1000 ANALYSIS:
H2H: [Record]
Recent Form: [Actual results]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Low 🟢

━━━━━━━━━━━━━━━━━━━━
🎯 VALUE PICK (Odds 1.80 - 2.50)

⭐ VALUE PICK 1
Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.80-2.50]
Stake: {value_pct*100:.0f}% = {s_value}

📊 ACE1000 ANALYSIS:
H2H: [Record]
Recent Form: [Actual results]
Key Stat: [Stat]
Verdict: [Conclusion]

Risk: Medium 🟡

━━━━━━━━━━━━━━━━━━━━
🔗 COMBO BETS
All legs max 2.00 odds only

COMBO 1 - BANKER 🏦
Leg 1: [Real Match - Bet @ max 2.00] — [Why from real data]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Leg 3: [Real Match - Bet @ max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [combined odds x {s_combo}]

COMBO 2 - VALUE ⭐
Leg 1: [Real Match - Bet @ max 2.00] — [Why]
Leg 2: [Real Match - Bet @ max 2.00] — [Why]
Combined Odds: [X.XX]
Stake: 2% = {s_combo}
Potential Return: [combined odds x {s_combo}]

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
        f"/combo — Combo bets only\n\n"
        f"📝 BET TRACKER\n"
        f"/logbet [match] [pick] [odds] [stake]\n"
        f"Example: /logbet ArsenalvsChelsea Over2.5 1.65 2000\n"
        f"/result [number] [win/loss] — Update result\n"
        f"/mybets — See all your bets\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Gemini + Groq powered\n"
        f"📊 Real stats: football-data.org + TheSportsDB\n"
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
        f"💡 STRATEGY GUIDE:\n"
        f"Martingale — Never recommended. Catastrophic long term.\n"
        f"Flat Staking — Safe, sustainable, consistent bettors.\n"
        f"Kelly Criterion — Optimal when winning consistently.\n"
        f"Defensive — Protects capital during losing streaks.\n\n"
        f"Ace1000 automatically adjusts as your results change. 🎯"
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
                f"📊 Strategy Update: {staking['strategy']}\n"
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
                f"📊 Strategy Update: {staking['strategy']}\n"
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
    await update.message.reply_text(f"🔍 Fetching real stats and team news for you, {name}...")
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
    await update.message.reply_text(f"🔗 Building data-driven combos for you, {name}...")
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = load_data()
        name = data.get("name", "Champ")
        bankroll = data.get("bankroll", 0)
        staking = get_staking_strategy(data)
        bankroll_info = f"User: {name}. Bankroll: {format_naira(bankroll)}. Strategy: {staking['strategy']}." if bankroll > 0 else f"User: {name}. No bankroll set."
        user_message = update.message.text
        prompt = f"{STYLE}\nYou are Ace1000, a smart football betting analyst for a Nigerian SportyBet punter. {bankroll_info}. The user says: {user_message}\n\nAddress them as {name}. Respond helpfully. Reference SportyBet. Be conversational and friendly. No markdown symbols."
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
        caption = update.message.caption or f"Analyze these SportyBet odds for {name}. Tell me which are good value, which to avoid, and suggest a combo. No markdown symbols."
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
You are Ace1000. Using ALL the real data below, scan for ONE outstanding value bet.
Only alert if genuinely good with odds between 1.40-2.50 and clear statistical edge from real data.
If nothing stands out reply with exactly: NO_ALERT

If you find something use this format:

🚨 VALUE ALERT, {name}!

Match: [Real upcoming match]
Bet: [Pick]
Odds: [1.40-2.50]
Stake: {staking['value_stake']*100:.0f}% = {s_value}

📊 ACE1000 ANALYSIS:
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
    app.add_handler(CommandHandler("logbet", log_bet))
    app.add_handler(CommandHandler("result", update_result))
    app.add_handler(CommandHandler("mybets", my_bets))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    job_queue = app.job_queue
    job_queue.run_daily(morning_briefing, time=datetime.strptime("07:00", "%H:%M").time())
    job_queue.run_daily(weekly_summary, time=datetime.strptime("20:00", "%H:%M").time(), days=(6,))
    job_queue.run_repeating(value_alert, interval=21600, first=60)

    print("🚀 Ace1000Bot is running! Real stats + Team news. Gemini + Groq.")
    app.run_polling()

if __name__ == "__main__":
    main()