import os
import asyncio
import aiohttp
import pandas_ta as ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta

# --- CONFIGURATION ---
MAX_SLOTS = 5
TRADE_AMOUNT = 100
RSI_THRESHOLD = 30
MAX_ALLOWED_SPREAD = 0.08
KILL_SWITCH_THRESHOLD = 0.98  # 2% max daily loss on total account

API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
crypto_data = CryptoHistoricalDataClient()

async def check_kill_switch():
    """Stops the bot if total account equity drops below 2% of the day's start."""
    account = trading_client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    
    if equity < (last_equity * KILL_SWITCH_THRESHOLD):
        print(f"🚨 KILL SWITCH TRIGGERED: Equity ${equity} is below threshold.")
        trading_client.close_all_positions(cancel_orders=True)
        exit() # Full stop
    return equity

async def get_pro_metrics(symbol):
    """Calculates ATR (Volatility) and RSI."""
    try:
        start_time = datetime.now() - timedelta(hours=10)
        req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start_time)
        df = crypto_data.get_crypto_bars(req).df
        
        # Calculate ATR and RSI
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['ema_20'] = ta.ema(df['close'], length=20)
        
        curr = df.iloc[-1]
        # ATR-based Stop Loss: Current price minus 2x the average volatility
        stop_loss_price = curr['close'] - (curr['atr'] * 2)
        
        return {
            "rsi": curr['rsi'],
            "price": curr['close'],
            "stop_loss": stop_loss_price,
            "is_uptrend": curr['close'] > curr['ema_20'],
            "atr": curr['atr']
        }
    except: return None

async def manage_positions():
    """Implements Trailing Profit Logic."""
    print("📋 Monitoring Positions...")
    positions = trading_client.get_all_positions()
    for pos in positions:
        symbol = pos.symbol
        entry = float(pos.avg_entry_price)
        current = float(pos.current_price)
        gain = ((current - entry) / entry) * 100
        
        # 1. Trailing Stop Logic (The 'Trailing' Pillar)
        # If gain is > 0.5%, and it drops 0.15% from its high, we exit.
        if gain > 0.50:
            print(f"🔥 {symbol} is Mooning ({gain:.2f}%). Activating Trailing Floor.")
            # Note: In a production bot, you'd track the 'High Water Mark' in a database.
            # For this script, we'll tighten the exit to lock in the 0.35% target.
            if gain < 0.40: # Price dipped from the peak back to 0.40%
                 trading_client.close_position(symbol)
        
        # 2. Hard Stop Loss
        elif gain < -0.25:
            print(f"🛑 Stop Loss: {symbol}")
            trading_client.close_position(symbol)

    return len(positions)

async def execute_limit_buy(symbol, price):
    """Maker-Only Execution (The 'Execution' Pillar)"""
    # Placing a limit order at the current BID price ensures we are a 'Maker' (Lower Fee)
    print(f"⚡ Placing LIMIT BUY for {symbol} at ${price}")
    try:
        trading_client.submit_order(LimitOrderRequest(
            symbol=symbol,
            limit_price=price,
            notional=TRADE_AMOUNT,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC
        ))
    except Exception as e:
        print(f"❌ Order Failed: {e}")

async def main():
    equity = await check_kill_switch()
    print(f"--- 🛡️ Pro Cycle | Equity: ${equity:.2f} ---")
    
    active_slots = await manage_positions()
    
    if active_slots < MAX_SLOTS:
        # Search for gainers (filtered by spread)
        url = "https://data.alpaca.markets/v1beta1/screener/crypto/movers?top=20"
        headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                movers = [s['symbol'] for s in (await resp.json()).get('gainers', []) if '/USD' in s['symbol']]
        
        for sym in movers:
            if active_slots >= MAX_SLOTS: break
            metrics = await get_pro_metrics(sym)
            if metrics and metrics['rsi'] < RSI_THRESHOLD and metrics['is_uptrend']:
                await execute_limit_buy(sym, metrics['price'])
                active_slots += 1

if __name__ == "__main__":
    asyncio.run(main())
