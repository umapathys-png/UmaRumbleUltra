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
PROFIT_TARGET_PCT = 0.25  
STOP_LOSS_PCT = -0.15     
MAX_SLOTS = 5            
TRADE_AMOUNT = 100        
RSI_THRESHOLD = 35        

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

# Initialize Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def get_rsi(symbol):
    """Fetches data and calculates RSI."""
    try:
        start_time = datetime.now() - timedelta(hours=3)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
        if bars.empty: 
            print(f"⚠️ No data found for {symbol}")
            return None
        
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        current_rsi = bars['rsi'].iloc[-1]
        return current_rsi
    except Exception as e:
        print(f"❌ Error calculating RSI for {symbol}: {e}")
        return None

async def manage_existing_positions():
    """Checks current holdings and decides to SELL or HOLD."""
    print("📋 Checking existing portfolio...")
    positions = trading_client.get_all_positions()
    
    if not positions:
        print("ℹ️ No active positions to manage.")
        return 0

    for pos in positions:
        symbol = pos.symbol
        entry_price = float(pos.avg_entry_price)
        current_price = float(pos.current_price)
        gain = ((current_price - entry_price) / entry_price) * 100
        
        print(f"🔎 Monitoring {symbol}: Entry: ${entry_price:.4f} | Current: ${current_price:.4f} | Gain: {gain:.2f}%")
        
        if gain >= PROFIT_TARGET_PCT:
            print(f"💰 PROFIT TARGET MET: Selling {symbol} (+{gain:.2f}%)")
            trading_client.close_position(symbol)
        elif gain <= STOP_LOSS_PCT:
            print(f"📉 STOP LOSS HIT: Selling {symbol} ({gain:.2f}%)")
            trading_client.close_position(symbol)
        else:
            print(f"⏳ Holding {symbol}: Waiting for target...")
            
    return len(positions)

async def seek_new_trades(open_slots):
    """Scans for new RSI dip opportunities using ONLY USD pairs."""
    print(f"🔍 Searching for {open_slots} new opportunities...")
    
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=50"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                # FILTER: Only include symbols ending in /USD to match your cash balance
                movers = [
                    item['symbol'] for item in data.get('gainers', []) + data.get('losers', [])
                    if item['symbol'].endswith('/USD')
                ]
                
        for symbol in movers:
            if open_slots <= 0: 
                print("✅ All slots filled for this cycle.")
                break
            
            # (Rest of the loop remains the same...)
            
            # Check if we already have this symbol to avoid duplicates
            try:
                trading_client.get_open_position(symbol.replace("/", ""))
                continue 
            except:
                pass

            rsi = await get_rsi(symbol)
            if rsi is not None:
                if rsi < RSI_THRESHOLD:
                    print(f"🚀 RSI TRIGGER: Buying ${TRADE_AMOUNT} of {symbol} (RSI: {rsi:.2f})")
                    trading_client.submit_order(MarketOrderRequest(
                        symbol=symbol, notional=TRADE_AMOUNT, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                    ))
                    open_slots -= 1
                    await asyncio.sleep(2) 
                else:
                    print(f"⏭️ Skipping {symbol}: RSI is {rsi:.2f} (Not oversold)")
                    
    except Exception as e:
        print(f"❌ Critical Search Error: {e}")

async def main():
    print(f"--- ⚡ Think_Profit Cycle Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    try:
        # Step 1: Manage current portfolio
        current_pos_count = await manage_existing_positions()
        
        # Step 2: Look for new entries if slots are available
        open_slots = MAX_SLOTS - current_pos_count
        if open_slots > 0:
            await seek_new_trades(open_slots)
        else:
            print("🚫 Maximum slot capacity reached (5/5). No new trades this cycle.")
            
    except Exception as e:
        print(f"❌ Main Loop Error: {e}")

    print(f"--- ✅ Cycle Complete ---")

if __name__ == "__main__":
    asyncio.run(main())
