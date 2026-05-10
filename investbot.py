import os
import json
import logging
import requests
import yfinance as yf
import csv
import io
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PACIFIC = timezone(timedelta(hours=-7))
NOTES_FILE = "/home/clawadmin/investbot/notes.json"
PORTFOLIO_FILE = "/home/clawadmin/investbot/portfolio.json"
TRADES_FILE = "/home/clawadmin/investbot/trades.json"

MONTHLY_CRYPTO_BUDGET = 200
STOP_LOSS_PCT = 15
TAKE_PROFIT_PCT = 25
MAX_DAILY_TRADES = 2

# Yahoo Finance ticker mapping
YF_TICKER_MAP = {
    "VDY": "VDY.TO",
    "XEQT": "XEQT.TO",
    "TEC": "TEC.TO",
    "TSLA": "TSLA.NE",
    "META": "META.NE",
    "LULU": "LULU",
    "NVDA": "NVDA.NE",
    "SHOP": "SHOP.TO",
    "RY": "RY.TO",
    "ENB": "ENB.TO",
}

# CDR stocks trade in CAD despite being USD companies
CDR_SYMBOLS = {"TSLA", "META", "LULU", "NVDA"}

USD_TO_CAD = 1.38

WATCHLIST = {
    "XEQT.TO": "iShares All Equity ETF",
    "VFV.TO": "Vanguard S&P500 CAD",
    "ZSP.TO": "BMO S&P500",
    "XIC.TO": "iShares TSX Composite",
    "ZQQ.TO": "BMO NASDAQ 100",
    "NVDA": "NVIDIA",
    "MSFT": "Microsoft",
    "SHOP.TO": "Shopify",
    "AMZN": "Amazon",
    "META": "Meta",
    "RY.TO": "Royal Bank",
    "ENB.TO": "Enbridge",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "PLTR": "Palantir",
    "ARM": "ARM Holdings"
}

def is_owner(update):
    return update.effective_user.id == OWNER_CHAT_ID

def load_notes():
    if os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, "r") as f:
            return json.load(f)
    return {"notes": [], "portfolio": [], "rules": []}

def save_notes(data):
    with open(NOTES_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "r") as f:
            return json.load(f)
    return {"positions": {}, "last_updated": None, "source": None}

def save_portfolio(data):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    return {"trades": [], "monthly_spent": 0, "last_reset": None, "pending": None}

def save_trades(data):
    with open(TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_notes_context():
    data = load_notes()
    all_notes = data.get("notes", []) + data.get("portfolio", []) + data.get("rules", [])
    if not all_notes:
        return ""
    return "User personal context:\n" + "\n".join(f"- {n}" for n in all_notes)

def get_portfolio_context():
    data = load_portfolio()
    positions = data.get("positions", {})
    if not positions:
        return ""
    lines = ["Current portfolio positions:"]
    for ticker, pos in positions.items():
        lines.append(f"- {ticker}: {pos['quantity']} shares @ ${pos['avg_price']:.2f} avg")
    return "\n".join(lines)

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
        url = "https://newsapi.org/v2/everything"
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

def analyze_with_ollama(prompt, deep=False):
    model = "llama3.1:70b" if deep else "llama3.2:3b"
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 400}
            },
            timeout=180 if deep else 30
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

def parse_wealthsimple_csv(content):
    positions = {}
    try:
        sample = content[:500]
        delimiter = "\t" if "\t" in sample else ","
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        headers = reader.fieldnames or []
        headers_str = " ".join(headers)
        is_holdings = "Market Price" in headers_str or "Book Value" in headers_str

        if is_holdings:
            # Holdings CSV — sum positions across accounts
            for row in reader:
                if not row:
                    continue
                symbol = (row.get("Symbol") or "").strip()
                currency = (row.get("Market Price Currency") or "CAD").strip()
                try:
                    quantity = float(row.get("Quantity") or 0)
                    book_value_cad = float(row.get("Book Value (CAD)") or 0)
                except:
                    continue
                if not symbol or quantity == 0:
                    continue
                # Sum across accounts
                if symbol not in positions:
                    positions[symbol] = {"quantity": 0, "total_cost": 0, "currency": currency}
                positions[symbol]["quantity"] += quantity
                positions[symbol]["total_cost"] += book_value_cad

            # Calculate avg prices
            result = {}
            for symbol, data in positions.items():
                if data["quantity"] > 0.0001:
                    avg = data["total_cost"] / data["quantity"]
                    result[symbol] = {
                        "quantity": round(data["quantity"], 4),
                        "avg_price": round(avg, 4),
                        "total_cost": round(data["total_cost"], 2),
                        "currency": data.get("currency", "CAD")
                    }
            return result

        else:
            # Transaction history CSV
            for row in reader:
                if not row:
                    continue
                activity = (row.get("activity_type") or "").strip()
                sub_type = (row.get("activity_sub_type") or "").strip().upper()
                symbol = (row.get("symbol") or "").strip()
                try:
                    quantity = float(row.get("quantity") or 0)
                    unit_price = float(row.get("unit_price") or 0)
                except:
                    continue
                if not symbol or quantity == 0 or activity != "Trade":
                    continue
                if symbol not in positions:
                    positions[symbol] = {"quantity": 0, "total_cost": 0}
                if sub_type == "BUY":
                    positions[symbol]["quantity"] += quantity
                    positions[symbol]["total_cost"] += quantity * unit_price
                elif sub_type == "SELL":
                    positions[symbol]["quantity"] -= quantity
                    if positions[symbol]["quantity"] < 0:
                        positions[symbol]["quantity"] = 0

            result = {}
            for symbol, data in positions.items():
                if data["quantity"] > 0.0001:
                    avg = data["total_cost"] / data["quantity"]
                    result[symbol] = {
                        "quantity": round(data["quantity"], 4),
                        "avg_price": round(avg, 4),
                        "total_cost": round(data["total_cost"], 2),
                        "currency": "CAD"
                    }
            return result

    except Exception as e:
        logger.error(f"CSV parse error: {e}")
        return {}

def execute_coinbase_trade(side, product_id, amount_usd):
    try:
        import jwt
        import time
        import uuid
        key_name = COINBASE_API_KEY
        key_secret = COINBASE_API_SECRET.replace("\\n", "\n")
        order_id = str(uuid.uuid4())
        path = "/api/v3/brokerage/orders"
        payload = {
            "sub": key_name,
            "iss": "coinbase-cloud",
            "nbf": int(time.time()),
            "exp": int(time.time()) + 120,
            "uri": f"POST api.coinbase.com{path}"
        }
        token = jwt.encode(payload, key_secret, algorithm="ES256")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        order_data = {
            "client_order_id": order_id,
            "product_id": product_id,
            "side": side,
            "order_configuration": {"market_market_ioc": {"quote_size": str(amount_usd)}}
        }
        response = requests.post(f"https://api.coinbase.com{path}", headers=headers, json=order_data, timeout=15)
        return response.json()
    except Exception as e:
        logger.error(f"Trade error: {e}")
        return None

async def start(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📊 ClawInvestBot Online\n\n"
        "Market:\n"
        "/brief — Market brief\n"
        "/stock [ticker] — Stock info\n"
        "/watchlist — Watchlist\n"
        "/news — Market news\n"
        "/analyze [ticker] — Deep AI analysis (2-3 min)\n"
        "/recommend — Quick recommendation\n"
        "/crypto — Crypto prices\n\n"
        "Portfolio:\n"
        "/portfolio — Positions + P&L\n"
        "/position [ticker] [qty] [avg] [CAD/USD] — Add manually\n"
        "/deleteposition [ticker] — Remove\n"
        "/clearportfolio — Clear all\n"
        "Upload CSV — Auto-import from Wealthsimple\n\n"
        "Crypto Trading:\n"
        "/cryptosignal — Trade signals\n"
        "/tradesummary — Trade history\n"
        "/confirm — Execute trade\n"
        "/skip — Skip trade\n\n"
        "Memory:\n"
        "/note [text] — Save note\n"
        "/rule [text] — Save rule\n"
        "/notes — View all\n"
    )

async def handle_document(update, context):
    if not is_owner(update):
        return
    doc = update.message.document
    if not doc.file_name.endswith(".csv"):
        await update.message.reply_text("Please upload a CSV file from Wealthsimple.")
        return
    await update.message.reply_text("📂 Processing your Wealthsimple export...")
    try:
        file = await context.bot.get_file(doc.file_id)
        content = bytes(await file.download_as_bytearray()).decode("utf-8", errors="ignore")
        positions = parse_wealthsimple_csv(content)
        if not positions:
            await update.message.reply_text(
                "Could not find positions.\n\nFile preview:\n" + content[:300]
            )
            return
        portfolio = load_portfolio()
        portfolio["positions"] = positions
        portfolio["last_updated"] = datetime.now(PACIFIC).strftime("%b %d %Y %I:%M %p PT")
        portfolio["source"] = "Wealthsimple CSV"
        save_portfolio(portfolio)
        msg = f"✅ Portfolio updated\nUpdated: {portfolio['last_updated']}\n\n"
        msg += f"📊 {len(positions)} positions:\n"
        for ticker, pos in positions.items():
            msg += f"• {ticker}: {pos['quantity']} shares @ ${pos['avg_price']:.2f} avg\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

async def portfolio_cmd(update, context):
    if not is_owner(update):
        return
    data = load_portfolio()
    positions = data.get("positions", {})
    if not positions:
        await update.message.reply_text(
            "No positions found.\n\nUpload Wealthsimple holdings CSV or use:\n/position NVDA 10 210.00 CAD"
        )
        return
    msg = f"💼 Portfolio\nSource: {data.get('source', 'Manual')}\nUpdated: {data.get('last_updated', 'Unknown')}\n\n"
    total_cost_cad = 0
    total_value_cad = 0

    for ticker, pos in positions.items():
        # Determine currency — CDRs are always CAD
        currency = "CAD" if ticker in CDR_SYMBOLS else pos.get("currency", "CAD")
        cost_cad = pos.get("total_cost", pos["quantity"] * pos["avg_price"])
        total_cost_cad += cost_cad

        # Get Yahoo Finance ticker
        yf_ticker = YF_TICKER_MAP.get(ticker, ticker)
        market = get_stock_data(yf_ticker)
        if not market:
            market = get_stock_data(ticker)

        if market:
            price = market["price"]
            value = pos["quantity"] * price
            # All .TO and .NE prices are already in CAD
            value_cad = value
            total_value_cad += value_cad
            pnl_cad = value_cad - cost_cad
            pnl_pct = (pnl_cad / cost_cad * 100) if cost_cad > 0 else 0
            arrow = "📈" if pnl_cad >= 0 else "📉"
            msg += f"{arrow} {ticker}\n"
            msg += f"  {pos['quantity']} shares | Avg: ${pos['avg_price']:.2f}\n"
            msg += f"  Now: ${price:.2f} | P&L: ${pnl_cad:+.2f} CAD ({pnl_pct:+.1f}%)\n\n"
        else:
            msg += f"• {ticker}: {pos['quantity']} @ ${pos['avg_price']:.2f}\n\n"

    if total_cost_cad > 0:
        total_pnl = total_value_cad - total_cost_cad
        total_pnl_pct = (total_pnl / total_cost_cad * 100)
        msg += f"──────────────\n"
        msg += f"Total Cost: ${total_cost_cad:,.2f} CAD\n"
        msg += f"Total Value: ${total_value_cad:,.2f} CAD\n"
        msg += f"Total P&L: ${total_pnl:+,.2f} CAD ({total_pnl_pct:+.1f}%)\n"
    await update.message.reply_text(msg)

async def add_position(update, context):
    if not is_owner(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /position NVDA 10 210.00 CAD")
        return
    try:
        ticker = context.args[0].upper()
        quantity = float(context.args[1])
        avg_price = float(context.args[2])
        currency = context.args[3].upper() if len(context.args) > 3 else "CAD"
        total_cost = quantity * avg_price
        data = load_portfolio()
        data["positions"][ticker] = {
            "quantity": quantity,
            "avg_price": avg_price,
            "total_cost": total_cost,
            "currency": currency
        }
        data["last_updated"] = datetime.now(PACIFIC).strftime("%b %d %Y %I:%M %p PT")
        data["source"] = "Manual"
        save_portfolio(data)
        await update.message.reply_text(f"✅ {ticker}: {quantity} shares @ ${avg_price:.2f} {currency}")
    except ValueError:
        await update.message.reply_text("Usage: /position NVDA 10 210.00 CAD")

async def delete_position(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /deleteposition NVDA")
        return
    ticker = context.args[0].upper()
    data = load_portfolio()
    if ticker in data["positions"]:
        del data["positions"][ticker]
        save_portfolio(data)
        await update.message.reply_text(f"✅ {ticker} removed.")
    else:
        await update.message.reply_text(f"❌ {ticker} not found.")

async def clear_portfolio(update, context):
    if not is_owner(update):
        return
    save_portfolio({"positions": {}, "last_updated": None, "source": None})
    await update.message.reply_text("✅ Portfolio cleared.")

async def crypto_signal(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("🔍 Analyzing crypto signals...")
    trades = load_trades()
    today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
    today_trades = [t for t in trades["trades"] if t.get("date") == today]
    if len(today_trades) >= MAX_DAILY_TRADES:
        await update.message.reply_text(f"Max daily trades ({MAX_DAILY_TRADES}) reached.")
        return
    articles = get_news("bitcoin ethereum crypto", 10)
    news_summary = "\n".join([a["title"] for a in articles[:5]])
    btc_data = get_stock_data("BTC-USD")
    eth_data = get_stock_data("ETH-USD")
    prompt = f"""Crypto trading analyst. Give ONE signal.
News: {news_summary}
BTC: ${btc_data['price']:,.2f} ({btc_data['change_pct']:+.2f}%)
ETH: ${eth_data['price']:,.2f} ({eth_data['change_pct']:+.2f}%)
Budget: $200/month. Stop loss 15%. Take profit 25%.
Format exactly:
SIGNAL: BUY or SELL or HOLD
COIN: BTC or ETH
REASON: one sentence
CONFIDENCE: 0-100%"""
    analysis = analyze_with_ollama(prompt, deep=False)
    lines = analysis.strip().split("\n")
    signal = "HOLD"
    coin = "BTC"
    for line in lines:
        if line.startswith("SIGNAL:"):
            signal = line.replace("SIGNAL:", "").strip()
        if line.startswith("COIN:"):
            coin = line.replace("COIN:", "").strip()
    if "HOLD" in signal.upper():
        await update.message.reply_text(f"📊 Signal\n\n{analysis}\n\nNo trade needed.")
        return
    price = btc_data["price"] if "BTC" in coin else eth_data["price"]
    trades["pending"] = {"signal": signal.upper(), "coin": coin, "product_id": f"{coin}-USD",
                         "amount_usd": MONTHLY_CRYPTO_BUDGET, "price": price, "analysis": analysis}
    save_trades(trades)
    await update.message.reply_text(
        f"🚨 Trade Signal\n\n{analysis}\n\n"
        f"Action: {signal} {coin} | Amount: ${MONTHLY_CRYPTO_BUDGET}\n"
        f"Price: ${price:,.2f} | SL: -{STOP_LOSS_PCT}% | TP: +{TAKE_PROFIT_PCT}%\n\n"
        f"/confirm or /skip"
    )

async def confirm_trade(update, context):
    if not is_owner(update):
        return
    trades = load_trades()
    pending = trades.get("pending")
    if not pending:
        await update.message.reply_text("No pending trade.")
        return
    await update.message.reply_text(f"⚡ Executing {pending['signal']} {pending['coin']}...")
    result = execute_coinbase_trade(pending["signal"], pending["product_id"], pending["amount_usd"])
    if result and result.get("success"):
        trades["trades"].append({"date": datetime.now(PACIFIC).strftime("%Y-%m-%d"),
                                  "signal": pending["signal"], "coin": pending["coin"],
                                  "amount_usd": pending["amount_usd"], "price": pending["price"]})
        trades["monthly_spent"] = trades.get("monthly_spent", 0) + pending["amount_usd"]
        trades["pending"] = None
        save_trades(trades)
        await update.message.reply_text(f"✅ Done! {pending['signal']} {pending['coin']} ${pending['amount_usd']}")
    else:
        trades["pending"] = None
        save_trades(trades)
        await update.message.reply_text(f"❌ Failed.\n{json.dumps(result, indent=2)[:300]}")

async def skip_trade(update, context):
    if not is_owner(update):
        return
    trades = load_trades()
    trades["pending"] = None
    save_trades(trades)
    await update.message.reply_text("⏭ Skipped.")

async def trade_summary(update, context):
    if not is_owner(update):
        return
    trades = load_trades()
    today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
    today_trades = [t for t in trades["trades"] if t.get("date") == today]
    msg = f"📊 Trading\nBudget: ${MONTHLY_CRYPTO_BUDGET}/month\nSpent: ${trades.get('monthly_spent',0)}\nToday: {len(today_trades)}/{MAX_DAILY_TRADES}\n\n"
    if trades["trades"]:
        for t in trades["trades"][-5:]:
            msg += f"• {t['date']} — {t['signal']} {t['coin']} ${t['amount_usd']}\n"
    else:
        msg += "No trades yet."
    await update.message.reply_text(msg)

async def brief(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("📊 Fetching...")
    msg = f"📊 Market Brief — {datetime.now(PACIFIC).strftime('%b %d %Y %I:%M %p PT')}\n\n🌍 Indices:\n"
    for ticker, name in {"^GSPC": "S&P 500", "^GSPTSE": "TSX", "^VIX": "VIX"}.items():
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name}: {format_price(data['price'], ticker)} {format_change(data['change'], data['change_pct'])}\n"
    msg += "\n📈 Watchlist:\n"
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
    msg = "₿ Crypto\n\n"
    for ticker, name in [("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum")]:
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name}: ${data['price']:,.2f} {format_change(data['change'], data['change_pct'])}\n"
    await update.message.reply_text(msg)

async def stock(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /stock NVDA")
        return
    ticker = context.args[0].upper()
    data = get_stock_data(ticker)
    if not data:
        await update.message.reply_text(f"Could not fetch {ticker}")
        return
    await update.message.reply_text(
        f"📊 {ticker}\nPrice: {format_price(data['price'], ticker)}\n"
        f"Change: {format_change(data['change'], data['change_pct'])}\nVolume: {data['volume']:,.0f}"
    )

async def watchlist_cmd(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("📋 Fetching...")
    msg = "📋 Watchlist\n\n"
    for ticker, name in WATCHLIST.items():
        data = get_stock_data(ticker)
        if data:
            msg += f"• {name} ({ticker})\n  {format_price(data['price'], ticker)} {format_change(data['change'], data['change_pct'])}\n\n"
    await update.message.reply_text(msg)

async def news_cmd(update, context):
    if not is_owner(update):
        return
    articles = get_news("stock market OR TSX OR S&P500 OR crypto", 5)
    if not articles:
        await update.message.reply_text("Could not fetch news")
        return
    msg = "📰 News\n\n"
    for i, a in enumerate(articles[:5], 1):
        msg += f"{i}. {a['title']}\n   {a['source']['name']}\n\n"
    await update.message.reply_text(msg)

async def analyze(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /analyze NVDA")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"🤖 Deep analyzing {ticker}...\n⏳ 2-3 minutes.")
    data = get_stock_data(ticker)
    if not data:
        await update.message.reply_text(f"Could not fetch {ticker}")
        return
    prompt = f"""{get_notes_context()}\n{get_portfolio_context()}
Analyze {ticker}: Price ${data['price']:.2f}, Change {data['change_pct']:.2f}%, Volume {data['volume']:,.0f}
4-5 sentences: price action, short term, long term, specific recommendation. Direct."""
    analysis = analyze_with_ollama(prompt, deep=True)
    await update.message.reply_text(f"🤖 {ticker}\n\n{analysis}")

async def recommend(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("🤖 Generating...")
    market_data = {}
    for ticker in ["NVDA", "XEQT.TO", "BTC-USD", "^GSPC"]:
        data = get_stock_data(ticker)
        if data:
            market_data[ticker] = data
    prompt = f"""Canadian investor, 30yo, $1800 CAD/month. FHSA+TFSA.
{get_notes_context()}
{get_portfolio_context()}
Market: {json.dumps({k: {{'price': round(v['price'],2), 'change_pct': round(v['change_pct'],2)}} for k,v in market_data.items()})}
3-4 sentences: market today, buy/hold/wait, one action. Direct. Respect rules."""
    rec = analyze_with_ollama(prompt, deep=False)
    await update.message.reply_text(f"💡 Recommendation\n\n{rec}")

async def note(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /note text")
        return
    text = " ".join(context.args)
    data = load_notes()
    data["notes"].append(f"{text} ({datetime.now(PACIFIC).strftime('%b %d')})")
    save_notes(data)
    await update.message.reply_text(f"✅ Note: {text}")

async def rule(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /rule Never sell NVDA below $300")
        return
    text = " ".join(context.args)
    data = load_notes()
    data["rules"].append(text)
    save_notes(data)
    await update.message.reply_text(f"✅ Rule: {text}")

async def show_notes(update, context):
    if not is_owner(update):
        return
    data = load_notes()
    msg = "📝 Context\n\n"
    if data.get("rules"):
        msg += "Rules:\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(data["rules"])) + "\n\n"
    if data.get("notes"):
        msg += "Notes:\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(data["notes"]))
    if not any([data.get("rules"), data.get("notes")]):
        msg += "Nothing saved."
    await update.message.reply_text(msg)

async def morning_brief(context):
    if datetime.now(PACIFIC).weekday() >= 5:
        return
    await context.bot.send_message(chat_id=OWNER_CHAT_ID, text="☀️ Market opens in 30 mins.\n/brief or /recommend")

async def midday_update(context):
    if datetime.now(PACIFIC).weekday() >= 5:
        return
    await context.bot.send_message(chat_id=OWNER_CHAT_ID, text="📊 Midday. /cryptosignal for opportunities.")

async def close_summary(context):
    if datetime.now(PACIFIC).weekday() >= 5:
        return
    await context.bot.send_message(chat_id=OWNER_CHAT_ID, text="🔔 Markets closed. /portfolio for P&L.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("brief", brief))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("recommend", recommend))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("note", note))
    app.add_handler(CommandHandler("rule", rule))
    app.add_handler(CommandHandler("notes", show_notes))
    app.add_handler(CommandHandler("position", add_position))
    app.add_handler(CommandHandler("deleteposition", delete_position))
    app.add_handler(CommandHandler("clearportfolio", clear_portfolio))
    app.add_handler(CommandHandler("cryptosignal", crypto_signal))
    app.add_handler(CommandHandler("confirm", confirm_trade))
    app.add_handler(CommandHandler("skip", skip_trade))
    app.add_handler(CommandHandler("tradesummary", trade_summary))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    jq = app.job_queue
    jq.run_daily(morning_brief, time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=PACIFIC))
    jq.run_daily(midday_update, time=datetime.strptime("12:00", "%H:%M").time().replace(tzinfo=PACIFIC))
    jq.run_daily(close_summary, time=datetime.strptime("16:30", "%H:%M").time().replace(tzinfo=PACIFIC))
    print("ClawInvestBot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
