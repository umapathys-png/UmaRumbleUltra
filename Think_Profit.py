import os
import asyncio
import aiohttp
import csv
import pandas_ta as ta
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- CONFIGURATION ---
MAX_SLOTS = 5
BASE_TRADE_RISK = 10        
MAX_TRADE_CAP = 150         
RSI_THRESHOLD = 35          
DATA_FRESHNESS_LIMIT = 900  
MIN_VOLATILITY_SCORE = 0.3  
KILL_SWITCH_LOSS_PCT = 0.02 
LOG_FILE = 'trade_log.csv'

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

def log_to_excel(symbol, status, rsi=0, vol=0, price=0, gain=0):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['Timestamp', 'Symbol', 'Status', 'RSI', 'Vol %', 'Price', 'P/L %'])
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            symbol, status, round(rsi, 2), round(vol, 2), 
            round(price, 4), f"{gain:.2f}%"
        ])

async def safety_circuit_breaker():
    try:
        account = trading_client.get_account()
        curr_equity = float(account.equity)
        last_equity = float(account.last_equity)
        drawdown = (curr_equity / last_equity) - 1
        print(f"🛡️  [SAFETY] Equity: ${curr_equity:,.2f} | Change: {drawdown:+.2%}")
        if curr_equity < (last_equity * (1 - KILL_SWITCH_LOSS_PCT)):
            trading_client.close_all_positions(cancel_orders=True)
            log_to_excel("SYSTEM", "KILL_SWITCH", price=curr_equity)
            raise SystemExit("CRITICAL: Daily Drawdown Hit.")
    except Exception as e:
        print(f"⚠️  [ERROR] Safety check failed: {e}")

async def get_secure_metrics(symbol):
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=12)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start)
        df = crypto_data.get_crypto_bars(req).df
        if df.empty: return None
        last_time = df.index[-1][1].to_pydatetime()
        if (datetime.now(timezone.utc) - last_time).total_seconds() > DATA_FRESHNESS_LIMIT:
            return None
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema_200'] = ta.ema(df['close'], length=200)
        curr = df.iloc[-1]
        return {
            "symbol": symbol, "rsi": curr['rsi'], "price": curr['close'],
            "vol": (curr['atr'] / curr['close']) * 100, "atr": curr['atr'],
            "is_uptrend": curr['close'] > curr['ema_200']
        }
    except: return None

async def manage_portfolio():
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            entry = float(pos.avg_entry_price)
            curr = float(pos.current_price)
            gain_pct = ((curr - entry) / entry) * 100
            if gain_pct >= 0.60 or gain_pct <= -0.40:
                trading_client.close_position(pos.symbol)
                log_to_excel(pos.symbol, "EXIT", price=curr, gain=gain_pct)
        return len(positions)
    except: return 0

async def execute_trade(m):
    try:
        qty = round(min(MAX_TRADE_CAP / m['price'], BASE_TRADE_RISK / (m['atr'] * 2)), 4)
        trading_client.submit_order(LimitOrderRequest(
            symbol=m['symbol'], qty=qty, limit_price=m['price'],
            side=OrderSide.BUY, time_in_force=TimeInForce.GTC
        ))
        log_to_excel(m['symbol'], "ENTRY", rsi=m['rsi'], vol=m['vol'], price=m['price'])
    except Exception as e:
        print(f"❌ [ORDER FAILED] {m['symbol']}: {e}")

async def main():
    print(f"\n⚡ Think_Profit Start | {datetime.now().strftime('%H:%M:%S')}")
    await safety_circuit_breaker()
    active_count = await manage_portfolio()
    slots_needed = MAX_SLOTS - active_count
    
    if slots_needed > 0:
        url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=30"
        headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                raw = await resp.json()
                movers = [s['symbol'] for s in raw.get('gainers', []) if '/USD' in s['symbol'] and not any(x in s['symbol'] for x in ['USDT', 'USDC'])]
        
        tasks = [get_secure_metrics(sym) for sym in movers[:15]]
        results = await asyncio.gather(*tasks)
        for res in results:
            if slots_needed <= 0: break
            if res and res['rsi'] < RSI_THRESHOLD and res['is_uptrend']:
                await execute_trade(res)
                slots_needed -= 1

    # --- FORCED HEARTBEAT FOR EXCEL ---
    log_to_excel("SYSTEM", "HEARTBEAT", price=0)
    print("✅ Cycle Complete. Log updated.")

if __name__ == "__main__":
    asyncio.run(main())
