import os
import asyncio
import aiohttp
import pandas_ta as ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta, timezone

# --- UPDATED AGGRESSIVE CONFIG ---
MAX_SLOTS = 5
TRADE_AMOUNT = 100
RSI_THRESHOLD = 38           # More frequent entries
MAX_ALLOWED_SPREAD = 0.12    # Slightly more tolerant of spreads
DATA_FRESHNESS_LIMIT = 900   
MIN_VOLATILITY_SCORE = 0.25  # Lowered to capture more movers

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def get_secure_data(symbol):
    """Reliability Pillar: Checks Data Freshness, Volatility, and RSI."""
    try:
        start_time = datetime.now(timezone.utc) - timedelta(hours=6)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        bars = crypto_data.get_crypto_bars(req).df
        if bars.empty: return None

        # 1. Freshness Check (Fix for image_82e17c.png)
        last_candle_time = bars.index[-1][1].to_pydatetime()
        latency = (datetime.now(timezone.utc) - last_candle_time).total_seconds()
        if latency > DATA_FRESHNESS_LIMIT:
            return None

        # 2. Indicators
        bars['rsi'] = ta.rsi(bars['close'], length=14)
        bars['ema_20'] = ta.ema(bars['close'], length=20)
        
        # 3. Volatility Check (ATR as % of price)
        bars['atr'] = ta.atr(bars['high'], bars['low'], bars['close'], length=14)
        curr = bars.iloc[-1]
        volatility_score = (curr['atr'] / curr['close']) * 100

        if volatility_score < MIN_VOLATILITY_SCORE:
            # Coin is moving too slow to be profitable
            return None

        return {
            "rsi": curr['rsi'],
            "is_healthy": curr['close'] > curr['ema_20'] and curr['close'] > curr['open'],
            "price": curr['close'],
            "vol": volatility_score
        }
    except:
        return None

async def manage_portfolio():
    """State Reconciliation: Syncs with Alpaca's live reality."""
    try:
        positions = trading_client.get_all_positions()
        total_pnl = 0
        for pos in positions:
            cost = float(pos.cost_basis)
            val = float(pos.qty) * float(pos.current_price)
            pnl = val - cost
            gain_pct = (pnl / cost) * 100
            total_pnl += pnl
            
            # Profit/Loss Exit Logic
            if gain_pct >= 0.50 and float(pos.current_price) > float(pos.avg_entry_price):
                trading_client.close_position(pos.symbol)
            elif gain_pct <= -0.30:
                trading_client.close_position(pos.symbol)
        
        return len(positions), total_pnl
    except:
        return 0, 0

async def seek_new_trades(open_slots):
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=25"
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            raw_data = await resp.json()
            movers = [s['symbol'] for s in raw_data.get('gainers', []) if '/USD' in s['symbol']]

    print(f"📡 Scanning {len(movers)} potential movers...")

    for sym in movers:
        if open_slots <= 0: break
        if any(x in sym for x in ['USDT', 'USDC', 'DAI']): continue # Skip stables

        data = await get_secure_data(sym)
        
        if not data:
            print(f"  ⏭️ {sym}: Skipping (Data stale or too slow)")
            continue

        # DEBUG LOGGING: This tells you exactly what the bot is seeing
        if data['rsi'] >= RSI_THRESHOLD:
            print(f"  ⏭️ {sym}: RSI too high ({data['rsi']:.1f} > {RSI_THRESHOLD})")
            continue
        
        if not data['is_healthy']:
            print(f"  ⏭️ {sym}: Not in an uptrend.")
            continue

        print(f"🚀 SIGNAL FOUND: {sym} | RSI: {data['rsi']:.1f} | Vol: {data['vol']:.2f}%")
        qty = round(TRADE_AMOUNT / data['price'], 4)
        trading_client.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, limit_price=data['price'],
            side=OrderSide.BUY, time_in_force=TimeInForce.GTC
        ))
        open_slots -= 1

async def main():
    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n--- ⚡ Think_Profit PRO | {now} ---")
    
    active_count, current_pnl = await manage_portfolio()
    print(f"📊 Active Slots: {active_count} | Session P/L: ${current_pnl:+.2f}")
    
    if active_count < MAX_SLOTS:
        await seek_new_trades(MAX_SLOTS - active_count)
    
    print("--- ✅ Cycle Complete ---\n")

if __name__ == "__main__":
    asyncio.run(main())
