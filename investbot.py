import os
import json
import logging
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PACIFIC = timezone(timedelta(hours=-7))

WATCHLIST = {
    # Core ETFs
    "XEQT.TO": "iShares All Equity ETF",
    "VFV.TO": "Vanguard S&P500 CAD",
    "ZSP.TO": "BMO S&P500",
    "XIC.TO": "iShares TSX Composite",
    "ZQQ.TO": "BMO NASDAQ 100",
    # Growth
    "NVDA": "NVIDIA",
    "MSFT": "Microsoft",
    "SHOP.TO": "Shopify",
    "AMZN": "Amazon",
    "META": "Meta",
    # Dividend
    "RY.TO": "Royal Bank",
    "ENB.TO": "Enbridge",
    # Speculative
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "PLTR": "Palantir",
    "ARM": "ARM Holdings"
}


def is_owner(update):
    return update.effective_user.id == OWNER_CHAT_ID

def get_stock_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2d")
        if hist.empty:
            return None
        current = hist['Close'].iloc[-1]
        previous = hist['Close'].iloc[-2] if len(hist) > 1 else current
        change = current - previous
        change_pct = (change / previous) * 100
        return {
            "price": current,
            "change": change,
            "change_pct": change_pct,
            "volume": hist['Volume'].iloc[-1]
        }
    except Exception as e:
        logger.error(f"Error fetching {ticker}: {e}")
        return None

def get_news(query="stock market", count=5):
    try:
        url = f"https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": count,
            "apiKey": NEWS_API_KEY
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("status") == "ok":
            return data.get("articles", [])
        return []
    except Exception as e:
        logger.error(f"News error: {e}")
        return []

def analyze_with_ollama(prompt):
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3.3",
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 300}
            },
            timeout=60
        )
        if response.status_code == 200:
            return response.json().get("response", "Analysis unavailable")
        return "Analysis unavailable"
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return "Local AI unavailable"

def format_price(price, ticker):
    currency = "CAD" if ".TO" in ticker else "USD"
    return f"${price:.2f} {currency}"

def format_change(change, change_pct):
    arrow = "📈" if change >= 0 else "📉"
    sign = "+" if change >= 0 else ""
    return f"{arrow} {sign}{change:.2f} ({sign}{change_pct:.2f}%)"

async def start(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📊 ClawInvestBot Online\n\n"
        "Commands:\n"
        "/brief — Market brief\n"
        "/stock [ticker] — Single stock info\n"
        "/watchlist — All watchlist stocks\n"
        "/news — Latest market news\n"
        "/analyze [ticker] — AI analysis\n"
        "/portfolio — Portfolio summary\n"
        "/crypto — Crypto prices\n"
        "/recommend — What to do today\n"
    )

async def brief(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("📊 Fetching market brief...")

    msg = f"📊 Market Brief — {datetime.now(PACIFIC).strftime('%b %d %Y %I:%M %p PT')}\n\n"

    indices = {"^GSPC": "S&P 500", "^GSPTSE": "TSX", "^VIX": "VIX Fear Index"}
    msg += "🌍 Indices:\n"
    for ticker, name in indices.items():
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name}: {format_price(data['price'], ticker)} {format_change(data['change'], data['change_pct'])}\n"

    msg += "\n📈 Your Watchlist:\n"
    for ticker, name in WATCHLIST.items():
        if "BTC" in ticker or "ETH" in ticker:
            continue
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name}: {format_price(data['price'], ticker)} {format_change(data['change'], data['change_pct'])}\n"

    await update.message.reply_text(msg)

async def crypto(update, context):
    if not is_owner(update):
        return
    msg = "₿ Crypto Prices\n\n"
    for ticker, name in [("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum")]:
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name}: ${data['price']:,.2f} USD {format_change(data['change'], data['change_pct'])}\n"
    await update.message.reply_text(msg)

async def stock(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /stock NVDA or /stock SHOP.TO")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"🔍 Looking up {ticker}...")

    data = get_stock_data(ticker)
    if not data:
        await update.message.reply_text(f"❌ Could not fetch data for {ticker}")
        return

    msg = (
        f"📊 {ticker}\n\n"
        f"Price: {format_price(data['price'], ticker)}\n"
        f"Change: {format_change(data['change'], data['change_pct'])}\n"
        f"Volume: {data['volume']:,.0f}\n"
    )
    await update.message.reply_text(msg)

async def watchlist(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("📋 Fetching watchlist...")
    msg = "📋 Watchlist\n\n"
    for ticker, name in WATCHLIST.items():
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name} ({ticker})\n"
            msg += f"  {format_price(data['price'], ticker)} {format_change(data['change'], data['change_pct'])}\n\n"
    await update.message.reply_text(msg)

async def news(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("📰 Fetching market news...")
    articles = get_news("stock market OR TSX OR S&P500 OR crypto", 5)
    if not articles:
        await update.message.reply_text("❌ Could not fetch news")
        return
    msg = "📰 Market News\n\n"
    for i, article in enumerate(articles[:5], 1):
        msg += f"{i}. {article['title']}\n"
        msg += f"   {article['source']['name']}\n\n"
    await update.message.reply_text(msg)

async def analyze(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /analyze NVDA")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"🤖 Analyzing {ticker} with local AI...")

    data = get_stock_data(ticker)
    if not data:
        await update.message.reply_text(f"❌ Could not fetch data for {ticker}")
        return

    prompt = f"""You are a financial analyst. Analyze this stock briefly:
Ticker: {ticker}
Current Price: ${data['price']:.2f}
Change today: {data['change_pct']:.2f}%
Volume: {data['volume']:,.0f}

Give a 3-4 sentence analysis covering:
1. What this price action means
2. Short term outlook
3. One actionable recommendation (buy/hold/sell/watch)
Be direct and concise."""

    analysis = analyze_with_ollama(prompt)
    msg = f"🤖 AI Analysis — {ticker}\n\n{analysis}"
    await update.message.reply_text(msg)

async def portfolio(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("💼 Portfolio Summary\n\n"
        "Monthly allocation ($1800 CAD):\n\n"
        "Core ETFs — $900 (50%)\n"
        "• XEQT: $500/month\n"
        "• VFV: $400/month\n\n"
        "Growth Stocks — $540 (30%)\n"
        "• NVDA: $200/month\n"
        "• MSFT: $200/month\n"
        "• SHOP: $140/month\n\n"
        "Speculative — $360 (20%)\n"
        "• BTC: $200/month\n"
        "• High risk pick: $160/month\n\n"
        "Use /watchlist for current prices\n"
        "Use /analyze [ticker] for AI analysis"
    )

async def recommend(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("🤖 Generating recommendation...")

    market_data = {}
    for ticker in ["NVDA", "XEQT.TO", "BTC-USD"]:
        data = get_stock_data(ticker)
        if data:
            market_data[ticker] = data

    prompt = f"""You are a financial advisor for a Canadian investor with $1800 CAD/month.
Portfolio split: 50% ETFs, 30% growth stocks, 20% crypto.
Holdings: XEQT, VFV, NVDA, MSFT, SHOP, BTC.

Current data:
{json.dumps({k: {'price': v['price'], 'change_pct': v['change_pct']} for k, v in market_data.items()}, indent=2)}

Give 3-4 sentences on:
1. What the market is doing today
2. Whether to buy/hold/wait this week
3. One specific action to take
Be direct. No disclaimers."""

    recommendation = analyze_with_ollama(prompt)
    await update.message.reply_text(f"💡 Today's Recommendation\n\n{recommendation}")

# Scheduled jobs
async def morning_brief(context):
    now = datetime.now(PACIFIC)
    if now.weekday() >= 5:
        return
    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text="☀️ Market opening in 30 mins. Use /brief for full update."
    )

async def midday_update(context):
    now = datetime.now(PACIFIC)
    if now.weekday() >= 5:
        return
    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text="📊 Midday check. Use /brief or /recommend for current analysis."
    )

async def close_summary(context):
    now = datetime.now(PACIFIC)
    if now.weekday() >= 5:
        return
    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text="🔔 Markets closed. Use /watchlist for end of day summary."
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("brief", brief))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("portfolio", portfolio))
    app.add_handler(CommandHandler("recommend", recommend))
    app.add_handler(CommandHandler("crypto", crypto))

    jq = app.job_queue
    jq.run_daily(morning_brief, time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=PACIFIC))
    jq.run_daily(midday_update, time=datetime.strptime("12:00", "%H:%M").time().replace(tzinfo=PACIFIC))
    jq.run_daily(close_summary, time=datetime.strptime("16:30", "%H:%M").time().replace(tzinfo=PACIFIC))

    print("ClawInvestBot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
