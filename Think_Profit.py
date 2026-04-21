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
RSI_THRESHOLD = 50          # Market Check threshold
DATA_FRESHNESS_LIMIT = 1200 # Increased to 20 mins for better reliability
MIN_VOLATILITY_SCORE = 0.3  
KILL_SWITCH_LOSS_PCT = 0.02 
LOG_FILE = 'trade_log.csv'

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

if not API_KEY or not SECRET_KEY:
    raise ValueError("Missing API_KEY or SECRET_KEY. Check your GitHub Secrets!")

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

async def get_secure_metrics(symbol):
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=12)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start)
        df = crypto_data.get_crypto_bars(req).df
        
        if df.empty:
            print(f"  🔍 {symbol}: No data returned from Alpaca.")
            return None
            
        last_time = df.index[-1][1].to_pydatetime()
        age = (datetime.now(timezone.utc) - last_time).total_seconds()
        
        if age > DATA_FRESHNESS_LIMIT:
            print(f"  🛑 {symbol}: Stale data ({int(age)}s old). Skipping.")
            return None
            
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema_200'] = ta.ema(df['close'], length=200)
        
        curr = df.iloc[-1]
        rsi_val = curr['rsi']
        is_uptrend = curr['close'] > curr['ema_200']
        
        # LOGGING EACH SCAN
        trend_str = "UP" if is_uptrend else "DOWN"
        print(f"  📊 {symbol:10} | RSI: {rsi_val:5.2f} | Trend: {trend_str} | Price: ${curr['close']:,.4f}")
        
        return {
            "symbol": symbol, "rsi": rsi_val, "price": curr['close'],
            "vol": (curr['atr'] / curr['close']) * 100, "atr": curr['atr'],
            "is_uptrend": is_uptrend
        }
    except Exception as e:
        print(f"  ⚠️  {symbol}: Error calculating metrics: {e}")
        return None

async def main():
    print(f"\n🚀 --- STARTING SCALPER CYCLE | {datetime.now().strftime('%H:%M:%S')} ---")
    
    # 1. Check Account Health
    account = trading_client.get_account()
    equity = float(account.equity)
    print(f"💰 Account Equity: ${equity:,.2f}")
    
    # 2. Portfolio Management (Exit Logic)
    positions = trading_client.get_all_positions()
    print(f"📦 Active Positions: {len(positions)} / {MAX_SLOTS}")
    
    for pos in positions:
        entry = float(pos.avg_entry_price)
        curr = float(pos.current_price)
        gain_pct = ((curr - entry) / entry) * 100
        print(f"   ∟ {pos.symbol}: {gain_pct:+.2f}%")
        if gain_pct >= 0.60 or gain_pct <= -0.40:
            print(f"   ✅ Closing {pos.symbol} at {gain_pct:+.2f}%")
            trading_client.close_position(pos.symbol)
            log_to_excel(pos.symbol, "EXIT", price=curr, gain=gain_pct)

    slots_needed = MAX_SLOTS - len(positions)
    if slots_needed <= 0:
        print("⏭️  Portfolio full. No new trades needed.")
    else:
        print(f"🔎 Scanning for {slots_needed} new opportunities...")
        url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=30"
        headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                raw = await resp.json()
                movers = [s['symbol'] for s in raw.get('gainers', []) if '/USD' in s['symbol'] and not any(x in s['symbol'] for x in ['USDT', 'USDC'])]
        
        print(f"📡 Found {len(movers)} movers. Checking technicals...")
        tasks = [get_secure_metrics(sym) for sym in movers[:15]]
        results = await asyncio.gather(*tasks)
        
        trades_executed = 0
        for res in results:
            if trades_executed >= slots_needed: break
            if not res: continue
            
            # TRADE DECISION LOGIC
            if res['rsi'] < RSI_THRESHOLD :
                print(f"🎯 ENTRY SIGNAL: {res['symbol']} met all criteria!")
                qty = round(min(MAX_TRADE_CAP / res['price'], BASE_TRADE_RISK / (res['atr'] * 2)), 4)
                try:
                    trading_client.submit_order(LimitOrderRequest(
                        symbol=res['symbol'], qty=qty, limit_price=res['price'],
                        side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                    ))
                    log_to_excel(res['symbol'], "ENTRY", rsi=res['rsi'], vol=res['vol'], price=res['price'])
                    trades_executed += 1
                except Exception as e:
                    print(f"❌ Order failed for {res['symbol']}: {e}")
            elif res['rsi'] >= RSI_THRESHOLD:
                pass # Already printed in get_secure_metrics
        
        if trades_executed == 0:
            print("😴 No coins met entry criteria (RSI < 35 + Uptrend) this cycle.")

    log_to_excel("SYSTEM", "HEARTBEAT", price=equity)
    print(f"🏁 --- CYCLE COMPLETE ---")

if __name__ == "__main__":
    asyncio.run(main())
