def log_placed_order(symbol, entry_price, target, sl, trail_sl, adx):
    with open("/Users/nikti/Desktop/Systems/Algo_Trading/CCXT/demo_algos/results.csv", "a", newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            datetime.utcnow(), symbol, entry_price, target, sl, trail_sl, adx
        ])
import sys
import math

# Liquidity grab algo (Strategy by Shevelev Trade)
# Market Buy Edition
class Logger(object):
    def __init__(self, file_path):
        self.terminal = sys.stdout
        self.log = open(file_path, "a")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        pass  # This is needed for compatibility with some environments

sys.stdout = Logger("/Users/nikti/Desktop/Systems/Algo_Trading/CCXT/demo_algos/output.csv")

'''
1. Sort by top gainers in the last 24 hours on Bybit
2. 4h TF - Select coins that are in a clear uptrend, with up waves on higher volume, than down waves
3. Find a strong resistance level ahead, and wait for the price to approach that level
4. Switch to 5m TF and make sure there is a clear uptrend, with up waves with higher volume than down waves
5. Find a local restance level and enter at the breakout (if the breakout volume is strong)
6. Target the 4 hour resistance level. SL below a swing low. 
'''

# CURRENT ISSUES
'''
If active coin is in a consolidation for while -> exit at nearest profit (Use ADX)

Potential improvements:
- Test VWAP, breakout candle sntrength
- Check the breakout volume

Implement ADX > 27 at both stages of analysys
- Maybe will need an upper ADX limit too

TP & SL arent getting placed sometimes

Run all tests again right before buying

'''

import sys
import os
import traceback
import csv
from datetime import datetime
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bybit_api as key
import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta
import schedule
import talib
import risk as r

active_list = []  # active positions -> waiting to get filled to set the TP; each item: {'symbol', 'target', 'timestamp'}
watchlist = [] # waiting to enter after a breakout; each item: {'symbol', 'local_resistance', 'target', 'timestamp'}
cooldown_list = []
placed_orders = []  # limit buy orders waiting to be filled; each item: {'symbol', 'entry_price', 'target', 'sl', 'timestamp'}

max_positions = 8
max_loss = -6

def format_for_bybit(symbol):
    # Convert BASE/QUOTE to BASE:USDT for Bybit, only if needed
    if not symbol.endswith(':USDT'):
        return symbol + ':USDT'
    return symbol


bybit = ccxt.bybit({
    'enableRateLimit': True,
    'apiKey': key.key,
    'secret': key.secret,
    'options': {
        'defaultType': 'swap',
        'defaultAccountType': 'UNIFIED',
        'recvWindow': 60000
    }
})

# ====================================== SORTING THE SYMBOLS ======================================

# Get coins that have gained between 8% and 50% in the last 24 hours
def get_top_gainers():
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })
    tickers = bybit.fetch_tickers()
    sorted_tickers = sorted(tickers.items(), key=lambda x: x[1]['percentage'], reverse=True)
    top_gainers = [
        symbol for symbol, data in sorted_tickers
        if '/USDT' in symbol and 8 <= data.get('percentage', 0) <= 50
    ]
    return top_gainers[:20]


# Check 4H trend (last 10 candles)
def check_4h_trend(symbol):
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })
    candles = bybit.fetch_ohlcv(symbol, '4h', limit=10)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')    
    df['up_volume'] = df.apply(lambda row: row['volume'] if row['close'] > row['open'] else 0, axis=1)
    df['down_volume'] = df.apply(lambda row: row['volume'] if row['close'] < row['open'] else 0, axis=1)
    return df['up_volume'].sum() > df['down_volume'].sum() * 1.1


# Find resistance within last 30 candles
def find_resistance_level(symbol):
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })

    candles = bybit.fetch_ohlcv(symbol, '4h', limit=30)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

    current_price = df['close'].iloc[-1]

    fractal_highs = []
    for i in range(2, len(df) - 2):
        if df['high'][i] > df['high'][i - 1] and df['high'][i] > df['high'][i - 2] and \
           df['high'][i] > df['high'][i + 1] and df['high'][i] > df['high'][i + 2]:
            fractal_highs.append(df['high'][i])

    valid_resistances = [r for r in fractal_highs if (r - current_price) / current_price >= 0.07]

    if not valid_resistances:
        return None

    resistance = min(valid_resistances)
    resistance_row = df[df['high'] == resistance].iloc[0]
    resistance_time = resistance_row['timestamp']
    return resistance, resistance_time


# Check 5m trend (last 20 candles)
def check_5m_trend(symbol):
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })
    candles = bybit.fetch_ohlcv(symbol, '5m', limit=20)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['up_volume'] = df.apply(lambda row: row['volume'] if row['close'] > row['open'] else 0, axis=1)
    df['down_volume'] = df.apply(lambda row: row['volume'] if row['close'] < row['open'] else 0, axis=1)
    return df['up_volume'].sum() > df['down_volume'].sum() * 1.1


# find local breakout level
def local_breakout(symbol):
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })
    candles = bybit.fetch_ohlcv(symbol, '5m', limit=20)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    local_resistance = df['high'].max()
    return local_resistance

# ========================================= SL LOGIC ========================================= 

def get_sl(symbol):
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })

    try:
        candles = bybit.fetch_ohlcv(symbol, '15m', limit=30)
        if not candles or len(candles) < 30:
            print(f"❌ Not enough 15m candles to calculate SL for {symbol}")
            return None

        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['sma_14'] = df['close'].rolling(window=14).mean()

        latest_valid_sl = None
        for i in range(2, len(df) - 2):
            if df['low'][i] < df['low'][i - 1] and df['low'][i] < df['low'][i - 2] and \
               df['low'][i] < df['low'][i + 1] and df['low'][i] < df['low'][i + 2]:
                if pd.notna(df['sma_14'][i]) and df['low'][i] < df['sma_14'][i]:
                    latest_valid_sl = df['low'][i] * 0.995  # add buffer

        if latest_valid_sl:
            return latest_valid_sl

        print(f"❌ No valid SL found in the last 30 candles for {symbol}")
    except Exception as e:
        print(f"⚠️ Failed to calculate SL for {symbol}: {e}")

    return None

# for trailing SL
def get_new_sl(symbol):
    bybit = ccxt.bybit({
        'apiKey': key.key,
        'secret': key.secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultAccountType': 'UNIFIED',
            'recvWindow': 60000
        }
    })
    candles = bybit.fetch_ohlcv(symbol, '5m', limit=40)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    fractal_lows = []
    for i in range(2, len(df) - 2):
        if df['low'][i] < df['low'][i - 1] and df['low'][i] < df['low'][i - 2] and \
           df['low'][i] < df['low'][i + 1] and df['low'][i] < df['low'][i + 2]:
            fractal_lows.append(df['low'][i])

    if not fractal_lows:
        return None

    sl = min(fractal_lows)
    return sl

# Return true if R:R >= 1.5
def risk_reward(entry, tp, sl):
    if sl >= entry or tp <= entry:
        return False
    rr = (tp - entry) / (entry - sl)
    return rr >= 1.5


# --- ADX filter function ---
def get_adx(symbol):
    # fetch 1h candles
    candles = bybit.fetch_ohlcv(symbol, '1h', limit=50)
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    # compute ADX
    df['adx'] = talib.ADX(df['high'], df['low'], df['close'], timeperiod=14)
    # return the most recent ADX
    return df['adx'].iloc[-1]

    
# ========================================= MONITORING =========================================

def check_watchlist(watchlist):
    global placed_orders
    try:
        print("🔍 Checking watchlist for breakout confirmations...\n")
        bybit = ccxt.bybit({
            'apiKey': key.key,
            'secret': key.secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
                'defaultAccountType': 'UNIFIED',
                'recvWindow': 60000
            }
        })

        for item in watchlist[:]:  # loop through a copy of the list to allow removing items
            symbol = item['symbol']
            local_resistance = item['local_resistance']
            target = item['target']
            print(f"Checking {symbol} for breakout...")

            # Skip if this symbol is already in active_list or placed_orders
            if any(active.get('symbol', '') == symbol for active in active_list):
                continue
            if any(order.get('symbol', '') == symbol for order in placed_orders):
                continue

            # fetch last 10 candles on 5m timeframe using formatted symbol
            candles = bybit.fetch_ohlcv(format_for_bybit(symbol), '5m', limit=10)
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            last_close = df['close'].iloc[-2]  # second last candle close price

            print(f'Breakout level: {local_resistance}; Last close: {last_close}')

            # If breakout detected 
            if last_close > local_resistance:
                # Fetch 20 latest 5m candles for volume analysis
                vol_candles = bybit.fetch_ohlcv(format_for_bybit(symbol), '5m', limit=20)
                vol_df = pd.DataFrame(vol_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                up_volume = vol_df.apply(lambda row: row['volume'] if row['close'] > row['open'] else 0, axis=1).sum()
                down_volume = vol_df.apply(lambda row: row['volume'] if row['close'] < row['open'] else 0, axis=1).sum()
                print(f"Breakout detected for {symbol}. Up volume: {up_volume}, Down volume: {down_volume}")

                print('============ Analyzing ==============')
                if up_volume > down_volume * 1.2:
                    print(f"✅ Up volume exceeds down volume")

                    # ADX filter on 1h (pre-entry scan)
                    adx_val = get_adx(format_for_bybit(symbol_formatted := format_for_bybit(symbol)))
                    if adx_val <= 27:
                        print(f"❌ {symbol}: ADX below threshold (27) at entry time. Waiting for better conditions.\n")
                        continue

                    # Re-check 4H trend before entry
                    if not check_4h_trend(format_for_bybit(symbol)):
                        print(f"❌ {symbol}: 4H trend no longer valid at entry time. Skipping trade.\n")
                        continue

                    # Re-check 5M trend before entry
                    if not check_5m_trend(format_for_bybit(symbol)):
                        print(f"❌ {symbol}: 5M trend no longer valid at entry time. Skipping trade.\n")
                        continue

                    # Calculate entry price as local_resistance * 1.002 (buffer)
                    buffer = 1.003
                    entry_price = local_resistance * buffer
                    stop_loss = get_sl(format_for_bybit(symbol))
                    if not stop_loss:
                        print(f"❌ No valid SL price found for {symbol}. Skipping trade.\n")
                        continue

                    # --- R:R check ---
                    if not risk_reward(entry_price, target, stop_loss):
                        print(f"❌ {symbol}: R:R is below 1.5. Skipping for now\n")
                        continue
                    # --- Resistance too close to current price check ---
                    if (target - entry_price) / entry_price < 0.05:
                        print(f"❌ {symbol}: Resistance too close to entry price. Setup Invalidated\n")
                        watchlist.remove(item)
                        continue

                    # Place limit buy order with updated precision logic
                    test_risk_usd = 6
                    amount = test_risk_usd / entry_price
                    print(f"Placing limit buy for {symbol} at {entry_price}")

                    try:
                        bybit.set_leverage(1, format_for_bybit(symbol))
                    except Exception as e:
                        if "leverage not modified" in str(e):
                            print(f"Leverage already set for {symbol}. Continuing...\n")
                        else:
                            print(f"Error setting leverage: {e}")

                    def get_symbol_precision(symbol):
                        markets = bybit.load_markets()
                        tick_size = markets[symbol]['precision']['price']
                        # convert tick_size (e.g. 0.0001) to number of decimal places
                        return int(round(-math.log10(tick_size)))

                    formatted_symbol = format_for_bybit(symbol)
                    precision = get_symbol_precision(formatted_symbol)
                    entry_price_rounded = round(entry_price, precision)
                    tp_rounded = round(target, precision)
                    sl_rounded = round(stop_loss, precision)
                    try:
                        tp_sl_params = {
                            'takeProfit': tp_rounded,  
                            'stopLoss':   sl_rounded,    
                        }
                        order = bybit.create_order(
                            symbol=formatted_symbol,
                            type='limit',
                            side='buy',
                            amount=amount,
                            price=entry_price_rounded,
                            params={
                                'timeInForce': "GTC",
                                **tp_sl_params,
                            }
                        )
                        print(f"✅ Limit buy order placed for {symbol} at {entry_price}")
                        print('=======================================')

                        placed_orders.append({
                            'symbol': symbol,
                            'entry_price': entry_price,
                            'target': target,
                            'sl': stop_loss,
                            'timestamp': datetime.utcnow()
                        })
                        log_placed_order(symbol, entry_price, target, stop_loss, False, adx_val)
                        watchlist.remove(item)
                        print(f"Order response: {order}")  # Log the full response
                    except Exception as e:
                        print(f"❌ Failed to place limit buy for {symbol}: {e}")
                        print('=======================================')
                        continue
                else:
                    print(f"❌ {symbol}: Buyers are not in control. No entry.\n")
                    print('=======================================')
            else:
                print(f"{symbol}: No breakout yet. Moving on...\n")
    except Exception as e:
        print(f"[{datetime.utcnow()}] check_watchlist ERROR: {e}")
        traceback.print_exc()


# --- New function: monitor_placed_orders ---
def monitor_placed_orders():
    global placed_orders
    print("⏲️ Monitoring placed limit orders (using open positions)...")
    try:
        positions = r.get_all_open_positions()
        open_symbols = [pos.get('Symbol', '').split(':')[0] for pos in positions]
        for item in placed_orders[:]:
            symbol = item['symbol']
            base_symbol = symbol.split('/')[0]
            entry_price = item['entry_price']
            target = item['target']
            sl = item['sl']
            ts = item['timestamp']
            print(f"Checking placed order for {symbol} (entry: {entry_price}, tp: {target}, sl: {sl})")

            if base_symbol in open_symbols:
                print(f"Order for {symbol} filled. Moving to active_list.")
                trail_sl = (target - entry_price) / entry_price >= 0.10
                active_list.append({
                    'symbol': symbol,
                    'target': target,
                    'timestamp': datetime.utcnow(),
                    'sl': sl,
                    'trail_sl': trail_sl
                })
                placed_orders.remove(item)
            elif datetime.utcnow() - ts > timedelta(hours=4):
                print(f"Order for {symbol} expired (>4h). Cancelling...")
                try:
                    bybit.cancel_all_orders(format_for_bybit(symbol))
                except Exception as e:
                    print(f"Error cancelling orders for {symbol}: {e}")
                placed_orders.remove(item)
    except Exception as e:
        print(f"Error monitoring placed orders: {e}")


def monitor_trailing_sl():
    try:
        print("\n📡 Checking for trailing SL updates...\n")
        for item in active_list[:]:
            symbol = item['symbol']
            current_sl = item.get('sl')
            trail_sl_flag = item.get('trail_sl')
            if trail_sl_flag:
                positions = r.get_all_open_positions()
                entry_price = None
                for pos in positions:
                    pos_symbol = pos.get('Symbol', '').split(':')[0]
                    if pos_symbol == symbol.split('/')[0]:
                        entry_price = float(pos.get('Entry Price', 0))
                        break
                new_sl = get_new_sl(format_for_bybit(symbol))
                if new_sl and new_sl > current_sl:
                    print(f"🔄 Trailing SL update for {symbol}: {current_sl} → {new_sl}")
                    item['sl'] = new_sl
    except Exception as e:
        print(f"[{datetime.utcnow()}] monitor_trailing_sl ERROR: {e}")
        traceback.print_exc()

# Expire watchlist entries after 2 hours
def expire_watchlist_entries():
    print("⏳ Checking for expired watchlist entries...\n")
    now = datetime.utcnow()
    for item in watchlist[:]:
        symbol = item['symbol']
        if now - item['timestamp'] > timedelta(hours=1):
            print(f"🗑️ Removing {symbol} from watchlist (expired 1h)\n")
            watchlist.remove(item)

 # Clean up closed positions from active_list
def clean_exited_positions():
    try:
        print("🧹 Checking for exited positions to clean from active list...\n")
        current_positions = r.get_all_open_positions()
        current_symbols = [pos.get('Symbol', '').split(':')[0] for pos in current_positions]

        for item in active_list[:]:
            active_symbol = item.get('symbol', '').split(':')[0]
            print(f"🔍 Checking if {active_symbol} is still open (comparing against: {current_symbols})...")
            if active_symbol not in current_symbols:
                print(f"✅ {active_symbol} position closed. Removing from active list.\n")
                active_list.remove(item)
            else:
                print(f"⏸️ {active_symbol} is still active.\n")
    except Exception as e:
        print(f"[{datetime.utcnow()}] clean_exited_positions ERROR: {e}")
        traceback.print_exc()


def check_pnl():
    try:
        max_loss = -7
        max_profit = 17
        positions = r.get_all_open_positions()  # returns dicts with 'Symbol' and 'PnL (%)'
        for pos in positions:
            # Extract base symbol and PnL percent
            api_symbol = pos.get('Symbol', '')
            symbol = api_symbol.split(':')[0]  # e.g., 'BTC/USDT'
            pnl_percent = float(pos.get('PnL (%)', 0))

            if pnl_percent <= max_loss:
                print(f'⚠️ {symbol} hit max loss of {max_loss}%. Exiting trade.\n')
                r.kill_switch(format_for_bybit(symbol))
            elif pnl_percent >= max_profit:
                print(f'🚀 {symbol} hit max profit of {max_profit}%. Exiting trade.\n')
                r.kill_switch(format_for_bybit(symbol))
    except Exception as e:
        print(f"[{datetime.utcnow()}] check_pnl ERROR: {e}")
        traceback.print_exc()
    
# Main logic loop
def main():
    try:
        print('\nStarting the bot...\n')
        print('🔍 Checking active PNL & symbol list size...\n')
        # check_pnl()

        top_gainers = get_top_gainers()
        batch_size = 5
        for i in range(0, len(top_gainers), batch_size):
            batch = top_gainers[i:i + batch_size]
            for symbol in batch:
                symbol_base_quote = symbol.split(':')[0]
                print(f"\n🔎 Checking if {symbol_base_quote} already being traded...")
                print(f"Active list symbols: {[item.get('symbol', '') for item in active_list]}")
                print(f"Watchlist symbols: {[item.get('symbol', '') for item in watchlist]}")
                print(f"Placed limit orders: {[item.get('symbol', '') for item in placed_orders]}")

                if any(item.get('symbol', '') == symbol_base_quote for item in active_list) or \
                   any(item.get('symbol', '') == symbol_base_quote for item in watchlist) or \
                   any(item.get('symbol', '') == symbol_base_quote for item in placed_orders):
                    continue

                print(f"\nChecking {symbol_base_quote}...")

                if not check_4h_trend(format_for_bybit(symbol_base_quote)):
                    print(f"❌ {symbol_base_quote} is not in 4H uptrend")
                    continue
                else:
                    print('✅ 4H trend is up')

                res_result = find_resistance_level(format_for_bybit(symbol_base_quote))
                if not res_result:
                    print(f"❌ No valid resistance found for {symbol_base_quote}")
                    continue
                else:
                    resistance, resistance_time = res_result
                    print('✅ Clear resistance found')
                    print(f"-> Selected resistance: {resistance}")

                if not check_5m_trend(format_for_bybit(symbol_base_quote)):
                    print(f"❌ {symbol_base_quote} is not in 5m uptrend")
                    continue
                else:
                    print('✅ 5M trend is up')

                # ADX filter on 1h (initial scan)
                adx_val = get_adx(format_for_bybit(symbol_base_quote))
                if adx_val <= 27:
                    print(f"❌ {symbol_base_quote}: ADX below threshold (27). Skipping for now.\n")
                    continue
                else:
                    print(f'✅ ADX = {adx_val}')

                local_resistance = local_breakout(format_for_bybit(symbol_base_quote))
                if not local_resistance:
                    print(f"❌ No valid breakout found for {symbol_base_quote}")
                    continue
                else:
                    print('✅ Breakout level set')
                watchlist.append({
                    'symbol': symbol_base_quote,
                    'local_resistance': local_resistance,
                    'target': resistance * 0.992,
                    'timestamp': datetime.utcnow(),
                    'resistance_time': resistance_time
                })
                print(f'🕑 Added {symbol_base_quote} to the watchlist for breakout monitoring.\n')

            # Allow scheduled tasks to run between batches
            check_watchlist(watchlist)
            monitor_trailing_sl()
    except Exception as e:
        print(f"[{datetime.utcnow()}] main ERROR: {e}")
        traceback.print_exc()


# Schedule the bot every 10 minutes
schedule.every(20).minutes.do(main)

# Check PNL close for all positions
schedule.every(10).minutes.do(check_pnl)

schedule.every(5).minutes.do(lambda: check_watchlist(watchlist))

# Schedule monitor_placed_orders every 10 minutes
schedule.every(10).minutes.do(monitor_placed_orders)

# Monitor trailing SL updates
schedule.every(10).minutes.do(monitor_trailing_sl)

# Check and remove exited positions from active list
schedule.every(10).minutes.do(clean_exited_positions)

# Schedule expiry checks for the watchlist
schedule.every(30).minutes.do(expire_watchlist_entries)


main()

while True:
    try:
        schedule.run_pending()
        time.sleep(5)
    except Exception as e:
        print(f"Error: {e}\n")
        time.sleep(30)




#====================================================================================

# def check_pnl():
#     try:
#         max_loss = -10
#         positions = r.get_all_open_positions()  # returns dicts with 'Symbol' and 'PnL (%)'
#         for pos in positions:
#             # Extract base symbol and PnL percent
#             api_symbol = pos.get('Symbol', '')
#             symbol = api_symbol.split(':')[0]  # e.g., 'BTC/USDT'
#             pnl_percent = float(pos.get('PnL (%)', 0))

#             if pnl_percent <= max_loss:
#                 print(f'⚠️ {symbol} hit max loss of {max_loss}%. Exiting trade.\n')
#                 r.kill_switch(format_for_bybit(symbol))

#     except Exception as e:
#         print(f"[{datetime.utcnow()}] check_pnl ERROR: {e}")
#         traceback.print_exc()



# Volume delta since resistance 
# def volume_delta_since_resistance(symbol, resistance_time):
    
#     candles = bybit.fetch_ohlcv(symbol, '4h', limit=50)

#     df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
#     df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
#     df = df[df['timestamp'] > resistance_time]

#     if df.empty:
#         return False
#     up_volume = df.apply(lambda row: row['volume'] if row['close'] > row['open'] else 0, axis=1).sum()
#     down_volume = df.apply(lambda row: row['volume'] if row['close'] < row['open'] else 0, axis=1).sum()

#     return up_volume > down_volume
