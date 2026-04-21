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
PROFIT_TARGET_PCT = 0.30  # Slightly higher to ensure profit after spread
STOP_LOSS_PCT = -0.20     
MAX_SLOTS = 5             
TRADE_AMOUNT = 100        
RSI_THRESHOLD = 30        
MAX_ALLOWED_SPREAD = 0.08 # Do not buy if spread is wider than 0.08%

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def get_market_health(symbol):
    """Checks Spread and 5-minute Trend to ensure high-quality entries."""
    try:
        # 1. Check Spread (Hummingbot Logic)
        headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
        async with aiohttp.ClientSession() as session:
            url = f"https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols={symbol.replace('/', '')}"
            async with session.get(url, headers=headers) as resp:
                q_data = await resp.json()
                quote = q_data['quotes'][symbol.replace('/', '')]
                spread = ((float(quote['ap']) - float(quote['bp'])) / float(quote['bp'])) * 100

        # 2. Check 5m Trend (EMA 20)
        start_time = datetime.now() - timedelta(hours=5)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
        bars['ema_20'] = ta.ema(bars['close'], length=20)
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        
        curr = bars.iloc[-1]
        is_uptrend = curr['close'] > curr['ema_20']
        is_green_candle = curr['close'] > curr['open']

        return {
            "spread": spread,
            "rsi": curr['rsi'],
            "is_healthy": is_uptrend and is_green_candle,
            "price": curr['close']
        }
    except:
        return None

async def manage_existing_positions():
    """Manages exits with strict price-floor protection."""
    print("📋 Checking portfolio...")
    positions = trading_client.get_all_positions()
    for pos in positions:
        entry = float(pos.avg_entry_price)
        current = float(pos.current_price)
        gain = ((current - entry) / entry) * 100
        
        # Protect against selling at a loss due to bad data/spread
        if gain >= PROFIT_TARGET_PCT and current > entry:
            print(f"💰 Selling {pos.symbol} at profit.")
            trading_client.close_position(pos.symbol)
        elif gain <= STOP_LOSS_PCT:
            print(f"📉 Stop Loss triggered for {pos.symbol}.")
            trading_client.close_position(pos.symbol)
    return len(positions)

async def seek_new_trades(open_slots):
    """Finds new trades with Spread and Trend filters."""
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=50"
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            movers = [s['symbol'] for s in data.get('gainers', []) if s['symbol'].endswith('/USD')]

    for symbol in movers:
        if open_slots <= 0: break
        
        # Check if already owned
        try:
            trading_client.get_open_position(symbol.replace("/", ""))
            continue 
        except: pass

        health = await get_market_health(symbol)
        if health and health['spread'] <= MAX_ALLOWED_SPREAD:
            if health['rsi'] < RSI_THRESHOLD and health['is_healthy']:
                print(f"🚀 BUY SIGNAL: {symbol} | Spread: {health['spread']:.3f}%")
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, notional=TRADE_AMOUNT, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                ))
                open_slots -= 1
                await asyncio.sleep(2)

async def main():
    print(f"--- ⚡ Cycle Started: {datetime.now().strftime('%H:%M:%S')} ---")
    active_count = await manage_existing_positions()
    await seek_new_trades(MAX_SLOTS - active_count)
    print("--- ✅ Cycle Complete ---")

if __name__ == "__main__":
    asyncio.run(main())
