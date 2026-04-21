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
PROFIT_TARGET_PCT = 0.25  # Target 0.25% gain
STOP_LOSS_PCT = -0.15     # Exit if price drops 0.15%
MAX_SLOTS = 5             # Maximum concurrent trades
TRADE_AMOUNT = 100        # USD amount per trade
RSI_THRESHOLD = 35        # Oversold entry point

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

# Initialize Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def manage_existing_positions():
    """Checks holdings and executes Take-Profit or Stop-Loss with Price Protection."""
    print("📋 Checking existing portfolio...")
    try:
        positions = trading_client.get_all_positions()
    except Exception as e:
        print(f"❌ Error fetching positions: {e}")
        return 0
    
    if not positions:
        print("ℹ️ No active positions.")
        return 0

    for pos in positions:
        symbol = pos.symbol
        entry_price = float(pos.avg_entry_price)
        current_price = float(pos.current_price)
        gain = ((current_price - entry_price) / entry_price) * 100
        
        print(f"🔎 {symbol} | Entry: ${entry_price:.4f} | Current: ${current_price:.4f} | Gain: {gain:.2f}%")
        
        # LOGIC: Only sell for profit if gain target is met AND price is physically higher than entry
        if gain >= PROFIT_TARGET_PCT and current_price > entry_price:
            print(f"💰 PROFIT TARGET MET: Selling {symbol} (+{gain:.2f}%)")
            trading_client.close_position(symbol)
        elif gain <= STOP_LOSS_PCT:
            print(f"📉 STOP LOSS HIT: Selling {symbol} ({gain:.2f}%)")
            trading_client.close_position(symbol)
        else:
            print(f"⏳ {symbol}: Holding for target.")
            
    return len(positions)

async def get_candle_analysis(symbol):
    """OHLC Candle Logic: Analyzes trend and RSI for better entry timing."""
    try:
        start_time = datetime.now() - timedelta(hours=3)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
        if len(bars) < 2: return None
        
        # RSI Calculation
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        current_rsi = bars['rsi'].iloc[-1]
        
        # Candle Data
        curr = bars.iloc[-1]
        prev = bars.iloc[-2]
        
        # BULLISH CANDLE LOGIC
        is_green = curr['close'] > curr['open']
        is_engulfing = (curr['close'] - curr['open']) > (prev['open'] - prev['close'])
        
        # Bounce Logic
        recent_low = bars['low'].rolling(window=15).min().iloc[-1]
        has_bounced = curr['close'] > (recent_low * 1.001)

        return {
            "rsi": current_rsi,
            "is_bullish": is_green and (is_engulfing or has_bounced),
            "price": curr['close'],
            "dip": recent_low
        }
    except Exception as e:
        print(f"❌ Analysis Error for {symbol}: {e}")
        return None

async def seek_new_trades(open_slots):
    """Scans movers and buys only when Candle Logic confirms the bounce."""
    print(f"🔍 Searching for {open_slots} opportunities with Candle Logic...")
    
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=50"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                # Filter for USD pairs to avoid balance errors
                movers = [s['symbol'] for s in data.get('gainers', []) + data.get('losers', []) 
                          if s['symbol'].endswith('/USD')]

        for symbol in movers:
            if open_slots <= 0: break
            
            clean_symbol = symbol.replace("/", "")
            try:
                trading_client.get_open_position(clean_symbol)
                continue 
            except: pass

            analysis = await get_candle_analysis(symbol)
            
            if analysis and analysis['rsi'] < RSI_THRESHOLD:
                if analysis['is_bullish']:
                    print(f"🔥 ENTRY SIGNAL: {symbol} | RSI: {analysis['rsi']:.2f} | Price: {analysis['price']}")
                    trading_client.submit_order(MarketOrderRequest(
                        symbol=symbol, notional=TRADE_AMOUNT, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                    ))
                    open_slots -= 1
                    await asyncio.sleep(2) # Prevent API hammering
                else:
                    print(f"⏳ Watching {symbol}: RSI {analysis['rsi']:.2f} is low, but candle is weak.")
    except Exception as e:
        print(f"❌ Search Error: {e}")

async def main():
    print(f"--- ⚡ Think_Profit Cycle Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    try:
        # Step 1: Manage current holdings
        active_count = await manage_existing_positions()
        
        # Step 2: Fill empty slots
        open_slots = MAX_SLOTS - active_count
        if open_slots > 0:
            await seek_new_trades(open_slots)
        else:
            print("🚫 Maximum slot capacity (5/5) reached.")
            
    except Exception as e:
        print(f"❌ System Error: {e}")
    print(f"--- ✅ Cycle Complete ---")

if __name__ == "__main__":
    asyncio.run(main())
