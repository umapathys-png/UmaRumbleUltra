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
SLOT_COUNT = 5            
TRADE_AMOUNT = 100        
RSI_THRESHOLD = 35        

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

# Clients (Crypto Only)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

active_symbols = set()
failed_symbols = {} 
lock = asyncio.Lock()

async def get_hot_movers():
    """Fetches top crypto gainers and losers from Alpaca Screener."""
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=50"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Combine gainers and losers to find volatility
                    movers = [item['symbol'] for item in data.get('gainers', []) + data.get('losers', [])]
                    # Ensure format is 'BTC/USD' not 'BTCUSD' if necessary
                    return [m for m in movers if "/" in m] 
                return ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LTC/USD"]
    except Exception as e:
        print(f"Screener Error: {e}")
        return ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LTC/USD"]

async def get_rsi(symbol):
    """Calculates RSI using the last 3 hours of 1-minute crypto bars."""
    try:
        start_time = datetime.now() - timedelta(hours=3)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
            
        if bars.empty: return None
        
        # Calculate RSI on the 'close' column
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        return bars['rsi'].iloc[-1]
    except Exception as e:
        print(f"RSI Error for {symbol}: {e}")
        return None

async def trade_slot(slot_id):
    global active_symbols, failed_symbols
    await asyncio.sleep(slot_id * 2) 
    print(f"💎 Slot {slot_id} Monitoring Crypto...", flush=True)

    while True:
        target = None
        try:
            # 1. BALANCE CHECK
            account = trading_client.get_account()
            if float(account.non_marginable_buying_power) < (TRADE_AMOUNT + 5):
                await asyncio.sleep(30)
                continue

            # 2. TARGET ACQUISITION
            async with lock:
                now = datetime.now()
                failed_symbols = {s: t for s, t in failed_symbols.items() if now < t + timedelta(minutes=5)}
                
                movers = await get_hot_movers()
                for s in movers:
                    if s not in active_symbols and s not in failed_symbols:
                        target = s
                        active_symbols.add(target)
                        break
            
            if not target:
                await asyncio.sleep(15)
                continue

            # 3. BUYING PHASE
            in_position = False
            entry_price = 0
            
            # Check RSI for entry
            rsi = await get_rsi(target)
            if rsi and rsi < RSI_THRESHOLD:
                print(f"Slot {slot_id} 🟢 ENTERING {target} | RSI: {rsi:.2f}", flush=True)
                trading_client.submit_order(MarketOrderRequest(
                    symbol=target, notional=TRADE_AMOUNT, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                ))
                
                await asyncio.sleep(3) # Wait for fill
                pos = trading_client.get_open_position(target.replace("/", ""))
                entry_price = float(pos.avg_entry_price)
                in_position = True
            else:
                # If RSI wasn't right, release symbol and try again
                async with lock:
                    if target in active_symbols: active_symbols.remove(target)
                await asyncio.sleep(5)
                continue

            # 4. EXIT STRATEGY (PROFIT/LOSS)
            while in_position:
                try:
                    pos = trading_client.get_open_position(target.replace("/", ""))
                    current_price = float(pos.current_price)
                    gain = ((current_price - entry_price) / entry_price) * 100
                    
                    if gain >= PROFIT_TARGET_PCT:
                        print(f"Slot {slot_id} 💰 PROFIT SELL {target} | +{gain:.2f}%", flush=True)
                        trading_client.close_position(target.replace("/", ""))
                        in_position = False
                    elif gain <= STOP_LOSS_PCT:
                        print(f"Slot {slot_id} 📉 STOP LOSS {target} | {gain:.2f}%", flush=True)
                        trading_client.close_position(target.replace("/", ""))
                        in_position = False
                    else:
                        await asyncio.sleep(5) # Frequent price checks
                except Exception:
                    # Occasional API hiccups; don't abandon the trade
                    await asyncio.sleep(5)

            # Cleanup
            async with lock:
                if target in active_symbols: active_symbols.remove(target)

        except Exception as e:
            print(f"Slot {slot_id} Error: {e}")
            if target:
                async with lock:
                    if target in active_symbols: active_symbols.remove(target)
            await asyncio.sleep(10)
            
async def main():
    print("🚀 24/7 Crypto HFT Scalper Active.", flush=True)
    # Cancel pending orders on start to clear state
    trading_client.cancel_orders()
    await asyncio.gather(*(trade_slot(i) for i in range(1, SLOT_COUNT + 1)))

if __name__ == "__main__":
    asyncio.run(main())
