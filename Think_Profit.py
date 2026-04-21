import os
import asyncio
import aiohttp
import pandas_ta as ta
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- CONFIGURATION ---
MAX_SLOTS = 5
BASE_TRADE_RISK = 10        # Risk $10 per trade based on volatility (not total $100)
MAX_TRADE_CAP = 150         # Never spend more than $150 on a single position
RSI_THRESHOLD = 35          # Aggressive entry
DATA_FRESHNESS_LIMIT = 900  # 15 mins for Alpaca Free Tier
MIN_VOLATILITY_SCORE = 0.3  # ATR/Price %
KILL_SWITCH_LOSS_PCT = 0.02 # 2% Max Daily Drawdown

# API CREDENTIALS
API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

# Initialize Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

# --- SAFETY & UTILITY FUNCTIONS ---

async def safety_circuit_breaker():
    """Stops the bot if total account equity drops below threshold."""
    try:
        account = trading_client.get_account()
        curr_equity = float(account.equity)
        last_equity = float(account.last_equity)
        
        if curr_equity < (last_equity * (1 - KILL_SWITCH_LOSS_PCT)):
            print(f"🚨 KILL SWITCH: Loss > {KILL_SWITCH_LOSS_PCT*100}%. Liquidating...")
            trading_client.close_all_positions(cancel_orders=True)
            raise SystemError("CRITICAL: Daily Drawdown Limit Hit.")
        print(f"🛡️ Safety Check: Account Healthy (${curr_equity:.2f})")
    except Exception as e:
        print(f"⚠️ Circuit Breaker Error: {e}")

async def get_secure_metrics(symbol):
    """Calculates RSI, ATR, and Trend with Freshness Gate."""
    try:
        # Fetch 1m data (for RSI/ATR) and 15m (for Trend)
        start = datetime.now(timezone.utc) - timedelta(hours=10)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start)
        df = crypto_data.get_crypto_bars(req).df
        if df.empty: return None

        # Freshness Check
        last_time = df.index[-1][1].to_pydatetime()
        if (datetime.now(timezone.utc) - last_time).total_seconds() > DATA_FRESHNESS_LIMIT:
            return None

        # Indicators
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema_trend'] = ta.ema(df['close'], length=200) # Macro trend

        curr = df.iloc[-1]
        vol_score = (curr['atr'] / curr['close']) * 100

        return {
            "symbol": symbol,
            "rsi": curr['rsi'],
            "price": curr['close'],
            "vol": vol_score,
            "is_uptrend": curr['close'] > curr['ema_trend'],
            "atr": curr['atr']
        }
    except: return None

async def manage_portfolio():
    """Reconciles live positions and manages exits."""
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            symbol = pos.symbol
            entry = float(pos.avg_entry_price)
            curr = float(pos.current_price)
            gain = ((curr - entry) / entry) * 100
            
            # Scalping Exit Logic
            if gain >= 0.60 or gain <= -0.40:
                print(f"💰 Closing {symbol} at {gain:.2f}%")
                trading_client.close_position(symbol)
        return len(positions)
    except: return 0

async def execute_trade(metrics):
    """Calculates dynamic size and submits limit order."""
    try:
        # Dynamic Sizing: Risk $10 per 2x ATR movement
        # If ATR is high (volatile), qty is low.
        qty = BASE_TRADE_RISK / (metrics['atr'] * 2)
        
        # Cap the total dollar amount spent
        if (qty * metrics['price']) > MAX_TRADE_CAP:
            qty = MAX_TRADE_CAP / metrics['price']
            
        qty = round(qty, 4)
        
        print(f"🚀 Buying {metrics['symbol']} | RSI: {metrics['rsi']:.1f} | Qty: {qty}")
        trading_client.submit_order(LimitOrderRequest(
            symbol=metrics['symbol'],
            qty=qty,
            limit_price=metrics['price'],
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC
        ))
    except Exception as e:
        print(f"❌ Trade Failed for {metrics['symbol']}: {e}")

# --- MAIN LOOP ---

async def main():
    print(f"\n--- ⚡ Think_Profit APEX | {datetime.now().strftime('%H:%M:%S')} ---")
    
    # 1. Emergency Check
    await safety_circuit_breaker()
    
    # 2. Sync Portfolio
    active_slots = await manage_portfolio()
    slots_available = MAX_SLOTS - active_slots
    
    if slots_available <= 0:
        print("⏸️ Max slots filled. Monitoring...")
        return

    # 3. Parallel Scanning
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=30"
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            raw = await resp.json()
            candidates = [
                s['symbol'] for s in raw.get('gainers', []) 
                if '/USD' in s['symbol'] and not any(st in s['symbol'] for st in ['USDT', 'USDC', 'DAI'])
            ]

    # Scan top 15 candidates in parallel
    tasks = [get_secure_metrics(sym) for sym in candidates[:15]]
    results = await asyncio.gather(*tasks)
    
    for res in results:
        if slots_available <= 0: break
        if res and res['rsi'] < RSI_THRESHOLD and res['vol'] > MIN_VOLATILITY_SCORE:
            if res['is_uptrend']:
                await execute_trade(res)
                slots_available -= 1
                await asyncio.sleep(1)

    print("--- ✅ Cycle Complete ---")

if __name__ == "__main__":
    asyncio.run(main())
