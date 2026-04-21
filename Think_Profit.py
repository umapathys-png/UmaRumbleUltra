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

# --- CONFIGURATION ---
MAX_SLOTS = 5
TRADE_AMOUNT = 100
RSI_THRESHOLD = 30
MAX_ALLOWED_SPREAD = 0.10   # 0.10% max spread
DATA_FRESHNESS_LIMIT = 900  # 15 mins (Required for Alpaca Free Tier)
MIN_VOLATILITY_SCORE = 0.5  # Coin must have an ATR/Price ratio of at least 0.5%

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
    """Finds non-stablecoin movers with high volatility."""
    url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=25"
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            raw_data = await resp.json()
            # BLACKLIST: USDT, USDC, DAI (Fix for automatic stablecoin buys)
            movers = [
                s['symbol'] for s in raw_data.get('gainers', []) 
                if '/USD' in s['symbol'] 
                and not any(x in s['symbol'] for x in ['USDT', 'USDC', 'DAI'])
            ]

    for sym in movers:
        if open_slots <= 0: break
        
        # Avoid double-buying
        try:
            trading_client.get_open_position(sym.replace("/", ""))
            continue
        except: pass

        data = await get_secure_data(sym)
        if data and data['rsi'] < RSI_THRESHOLD and data['is_healthy']:
            print(f"🚀 BUY: {sym} | RSI: {data['rsi']:.2f} | Volatility: {data['vol']:.2f}%")
            qty = round(TRADE_AMOUNT / data['price'], 4)
            trading_client.submit_order(LimitOrderRequest(
                symbol=sym, qty=qty, limit_price=data['price'],
                side=OrderSide.BUY, time_in_force=TimeInForce.GTC
            ))
            open_slots -= 1
            await asyncio.sleep(2)

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
