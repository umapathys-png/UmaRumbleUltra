import os
import asyncio
import aiohttp
import pandas_ta as ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION ---
MAX_SLOTS = 5
TRADE_AMOUNT = 100
RSI_THRESHOLD = 30
MAX_ALLOWED_SPREAD = 0.08
DATA_FRESHNESS_LIMIT = 60 # Seconds

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

# Initialize Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def api_call_with_retry(func, *args, **kwargs):
    """Reliability Pillar: Exponential Backoff for API Rate Limits."""
    retries = 3
    delay = 2
    for i in range(retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) and i < retries - 1:
                print(f"⚠️ Rate limited. Retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2
            else:
                raise e

async def get_secure_data(symbol):
    """Reliability Pillar: Data Freshness Gate & Precision Analysis."""
    try:
        start_time = datetime.now(timezone.utc) - timedelta(hours=5)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
        
        if bars.empty: return None

        # Check Data Latency
        last_candle_time = bars.index[-1][1].to_pydatetime()
        latency = (datetime.now(timezone.utc) - last_candle_time).total_seconds()
        
        if latency > DATA_FRESHNESS_LIMIT:
            print(f"⏩ Stale Data for {symbol} ({latency:.0f}s old). Skipping.")
            return None

        # Calculate Indicators
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        bars['ema_20'] = ta.ema(bars['close'], length=20)
        curr = bars.iloc[-1]

        return {
            "rsi": curr['rsi'],
            "is_healthy": curr['close'] > curr['ema_20'] and curr['close'] > curr['open'],
            "price": curr['close']
        }
    except Exception as e:
        print(f"❌ Data Error for {symbol}: {e}")
        return None

async def manage_portfolio():
    """Reliability Pillar: State Reconciliation (Live Sync)."""
    print("📋 Reconciling Portfolio State...")
    try:
        # Get actual positions from the exchange (The Source of Truth)
        positions = trading_client.get_all_positions()
        
        total_pnl = 0
        for pos in positions:
            cost = float(pos.cost_basis)
            val = float(pos.qty) * float(pos.current_price)
            pnl = val - cost
            gain_pct = (pnl / cost) * 100
            total_pnl += pnl
            
            print(f"🔎 Holding {pos.symbol}: ${cost:.2f} | P/L: ${pnl:+.2f} ({gain_pct:.2f}%)")

            # Exit Logic (Strict Price Floor)
            if gain_pct >= 0.35 and float(pos.current_price) > float(pos.avg_entry_price):
                print(f"💰 Target Met. Closing {pos.symbol}")
                trading_client.close_position(pos.symbol)
            elif gain_pct <= -0.25:
                print(f"🛑 Stop Loss. Closing {pos.symbol}")
                trading_client.close_position(pos.symbol)
        
        return len(positions), total_pnl
    except Exception as e:
        print(f"❌ Portfolio Sync Error: {e}")
        return 0, 0

async def execute_precise_buy(symbol, price):
    """Reliability Pillar: Precision Rounding & Limit Execution."""
    try:
        # Fetch asset details to check decimal precision (lot_size)
        asset = trading_client.get_asset(symbol.replace("/", ""))
        
        # Calculate quantity based on intended trade amount
        qty = TRADE_AMOUNT / price
        
        # Note: In a full-scale bot, you'd round qty based on asset.min_order_size
        # Here we use a safe 4-decimal round for most crypto pairs
        safe_qty = round(qty, 4)

        print(f"🚀 Placing Precise Limit Buy: {symbol} | Qty: {safe_qty} @ ${price}")
        trading_client.submit_order(LimitOrderRequest(
            symbol=symbol,
            qty=safe_qty,
            limit_price=price,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC
        ))
    except Exception as e:
        print(f"❌ Execution Failure for {symbol}: {e}")

async def main():
    print(f"\n--- ⚡ Think_Profit RELIABLE | {datetime.now().strftime('%H:%M:%S')} ---")
    
    # 1. State Sync
    active_count, current_pnl = await manage_portfolio()
    
    # 2. Open Slot Management
    slots_to_fill = MAX_SLOTS - active_count
    if slots_to_fill > 0:
        # Movers check with USD filter
        url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=20"
        headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                movers = [s['symbol'] for s in (await resp.json()).get('gainers', []) if '/USD' in s['symbol']]

        for sym in movers:
            if slots_to_fill <= 0: break
            
            data = await get_secure_data(sym)
            if data and data['rsi'] < RSI_THRESHOLD and data['is_healthy']:
                await execute_precise_buy(sym, data['price'])
                slots_to_fill -= 1
                await asyncio.sleep(2)
    
    print(f"📊 Cycle Summary: Active Slots: {active_count} | Session P/L: ${current_pnl:+.2f}")
    print("--- ✅ Reliability Check Complete ---\n")

if __name__ == "__main__":
    asyncio.run(main())
