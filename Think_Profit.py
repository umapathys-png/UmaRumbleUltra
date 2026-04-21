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
        
        print(f"🛡️  [SAFETY] Account Equity: ${curr_equity:,.2f} | Daily Change: {drawdown:+.2%}")
        
        if curr_equity < (last_equity * (1 - KILL_SWITCH_LOSS_PCT)):
            print(f"🚨 [KILL SWITCH] Loss of {drawdown:.2%} detected! Liquidating all positions.")
            trading_client.close_all_positions(cancel_orders=True)
            log_to_excel("SYSTEM", "KILL_SWITCH", price=curr_equity)
            raise SystemExit("CRITICAL: Daily Drawdown Hit.")
    except Exception as e:
        print(f"⚠️  [ERROR] Circuit Breaker check failed: {e}")

async def get_secure_metrics(symbol):
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=12)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start)
        df = crypto_data.get_crypto_bars(req).df
        if df.empty: return None

        last_time = df.index[-1][1].to_pydatetime()
        latency = (datetime.now(timezone.utc) - last_time).total_seconds()
        
        if latency > DATA_FRESHNESS_LIMIT:
            print(f"  ⏭️  {symbol}: Data stale ({int(latency)}s delay). Skipping.")
            return None

        df['rsi'] = ta.rsi(df['close'], length=14)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema_200'] = ta.ema(df['close'], length=200)

        curr = df.iloc[-1]
        vol_score = (curr['atr'] / curr['close']) * 100

        return {
            "symbol": symbol,
            "rsi": curr['rsi'],
            "price": curr['close'],
            "vol": vol_score,
            "atr": curr['atr'],
            "is_uptrend": curr['close'] > curr['ema_200']
        }
    except Exception as e:
        print(f"⚠️  [ERROR] Analysis failed for {symbol}: {e}")
        return None

async def manage_portfolio():
    try:
        positions = trading_client.get_all_positions()
        active_symbols = [p.symbol for p in positions]
        print(f"📊 [PORTFOLIO] Active Positions: {active_symbols if active_symbols else 'None'}")
        
        for pos in positions:
            entry = float(pos.avg_entry_price)
            curr = float(pos.current_price)
            gain_pct = ((curr - entry) / entry) * 100
            gain_usd = (curr * float(pos.qty)) - (entry * float(pos.qty))
            
            print(f"   ∟ {pos.symbol}: {gain_pct:+.2f}% (${gain_usd:+.2f})")
            
            if gain_pct >= 0.60 or gain_pct <= -0.40:
                action = "TAKE PROFIT" if gain_pct > 0 else "STOP LOSS"
                print(f"💰 [EXIT] {action} triggered for {pos.symbol} at {gain_pct:.2f}% (${gain_usd:+.2f})")
                trading_client.close_position(pos.symbol)
                log_to_excel(pos.symbol, "EXIT", price=curr, gain=gain_pct)
        return len(positions)
    except Exception as e:
        print(f"⚠️  [ERROR] Portfolio management error: {e}")
        return 0

async def execute_trade(m):
    try:
        qty = BASE_TRADE_RISK / (m['atr'] * 2)
        if (qty * m['price']) > MAX_TRADE_CAP:
            qty = MAX_TRADE_CAP / m['price']
            
        qty = round(qty, 4)
        print(f"🚀 [BUY ORDER] {m['symbol']} | Price: ${m['price']:.4f} | RSI: {m['rsi']:.1f} | Risk-Adj Qty: {qty}")
        
        trading_client.submit_order(LimitOrderRequest(
            symbol=m['symbol'], qty=qty, limit_price=m['price'],
            side=OrderSide.BUY, time_in_force=TimeInForce.GTC
        ))
        log_to_excel(m['symbol'], "ENTRY", rsi=m['rsi'], vol=m['vol'], price=m['price'])
    except Exception as e:
        print(f"❌ [ORDER FAILED] Could not place trade for {m['symbol']}: {e}")

async def main():
    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n{'='*50}\n⚡ Think_Profit APEX | SYSTEM START | {now}\n{'='*50}")
    
    await safety_circuit_breaker()
    active_count = await manage_portfolio()
    slots_needed = MAX_SLOTS - active_count
    
    if slots_needed <= 0:
        print("⏸️  [IDLE] All slots full. Monitoring active trades for exit signals...")
        return

    print(f"📡 [SCANNING] Looking for {slots_needed} new trades to fill slots...")
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=30"
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            raw = await resp.json()
            movers = [
                s['symbol'] for s in raw.get('gainers', []) 
                if '/USD' in s['symbol'] and not any(st in s['symbol'] for st in ['USDT', 'USDC', 'DAI'])
            ]

    print(f"🔎 [FILTER] Analyzing top {len(movers[:15])} movers (excluding stablecoins)...")
    tasks = [get_secure_metrics(sym) for sym in movers[:15]]
    results = await asyncio.gather(*tasks)
    
    for res in results:
        if slots_needed <= 0: break
        if not res: continue
        
        # Verbose rejection logs
        if res['rsi'] >= RSI_THRESHOLD:
            print(f"  ⏭️  {res['symbol']}: RSI {res['rsi']:.1f} too high (Target: <{RSI_THRESHOLD})")
            continue
        if res['vol'] <= MIN_VOLATILITY_SCORE:
            print(f"  ⏭️  {res['symbol']}: Volatility {res['vol']:.2f}% too low")
            continue
        if not res['is_uptrend']:
            print(f"  ⏭️  {res['symbol']}: Macro trend is BEARISH (Below EMA200)")
            continue

        await execute_trade(res)
        slots_needed -= 1
        await asyncio.sleep(1)

    print(f"{'='*50}\n✅ Think_Profit APEX | CYCLE COMPLETE | {datetime.now().strftime('%H:%M:%S')}\n{'='*50}")

if __name__ == "__main__":
    asyncio.run(main())
