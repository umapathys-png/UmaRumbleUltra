import os
import asyncio
import aiohttp
import pandas_ta as ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta

# --- CONFIGURATION ---
PROFIT_TARGET_PCT = 0.35  # Target 0.35% (higher to cover spread)
STOP_LOSS_PCT = -0.25     
MAX_SLOTS = 5             
TRADE_AMOUNT = 100        
RSI_THRESHOLD = 30        
MAX_ALLOWED_SPREAD = 0.08 # Max 0.08% spread allowed for entry

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def manage_existing_positions():
    """Manages exits and logs the exact dollar investment and P/L."""
    print("📋 --- Portfolio Status ---")
    try:
        positions = trading_client.get_all_positions()
    except Exception as e:
        print(f"❌ Error fetching positions: {e}")
        return 0
    
    if not positions:
        print("ℹ️ No active investments.")
        return 0

    total_invested = 0
    for pos in positions:
        symbol = pos.symbol
        qty = float(pos.qty)
        entry_price = float(pos.avg_entry_price)
        current_price = float(pos.current_price)
        
        invested_amount = float(pos.cost_basis) 
        current_value = qty * current_price
        pl_dollars = current_value - invested_amount
        gain_pct = (pl_dollars / invested_amount) * 100
        
        total_invested += invested_amount
        
        print(f"🔎 {symbol}: Invested: ${invested_amount:.2f} | P/L: ${pl_dollars:+.2f} ({gain_pct:.2f}%)")

        if gain_pct >= PROFIT_TARGET_PCT and current_price > entry_price:
            print(f"✅ PROFIT EXIT: Closing {symbol} | Net: ${pl_dollars:+.2f}")
            trading_client.close_position(symbol)
        elif gain_pct <= STOP_LOSS_PCT:
            print(f"🛑 STOP LOSS: Closing {symbol} | Net: ${pl_dollars:+.2f}")
            trading_client.close_position(symbol)
            
    print(f"💰 Total Capital At Risk: ${total_invested:.2f}")
    return len(positions)

async def get_market_health(symbol):
    """Hummingbot style: Checks Spread and Trend Quality."""
    try:
        # 1. Spread Check
        headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
        async with aiohttp.ClientSession() as session:
            url = f"https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols={symbol.replace('/', '')}"
            async with session.get(url, headers=headers) as resp:
                q = (await resp.json())['quotes'][symbol.replace('/', '')]
                spread = ((float(q['ap']) - float(q['bp'])) / float(q['bp'])) * 100

        # 2. Trend & RSI Check
        start_time = datetime.now() - timedelta(hours=5)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
        bars['ema_20'] = ta.ema(bars['close'], length=20)
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        
        curr = bars.iloc[-1]
        # Only healthy if price > EMA 20 and RSI is low (Mean Reversion)
        is_healthy = curr['close'] > curr['ema_20'] and curr['close'] > curr['open']
        
        return {"spread": spread, "rsi": curr['rsi'], "is_healthy": is_healthy}
    except:
        return None

async def seek_new_trades(open_slots):
    """Finds new trades using liquidity and trend filters."""
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=50"
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            movers = [s['symbol'] for s in (await resp.json()).get('gainers', []) if s['symbol'].endswith('/USD')]

    for symbol in movers:
        if open_slots <= 0: break
        
        try:
            trading_client.get_open_position(symbol.replace("/", ""))
            continue 
        except: pass

        h = await get_market_health(symbol)
        if h and h['spread'] <= MAX_ALLOWED_SPREAD:
            if h['rsi'] < RSI_THRESHOLD and h['is_healthy']:
                print(f"🚀 BUY: {symbol} | Invest: ${TRADE_AMOUNT} | Spread: {h['spread']:.3f}%")
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, notional=TRADE_AMOUNT, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                ))
                open_slots -= 1
                await asyncio.sleep(2)

async def main():
    print(f"\n--- ⚡ Think_Profit Cycle: {datetime.now().strftime('%H:%M:%S')} ---")
    active_count = await manage_existing_positions()
    await seek_new_trades(MAX_SLOTS - active_count)
    print(f"--- ✅ Cycle Complete ---\n")

if __name__ == "__main__":
    asyncio.run(main())
