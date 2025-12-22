import os
import time
import requests
import json
import math
import socket
from collections import deque
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime, timedelta, timezone
from eth_account import Account

# ==========================================
# 🔧 MANUAL FIX for OrderOptions
# ==========================================
class OrderOptions:
    def __init__(self, tick_size, neg_risk):
        self.tick_size = str(tick_size)
        self.neg_risk = neg_risk

# ==========================================
# 🛠️ USER CONFIGURATION
# ==========================================

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("❌ PRIVATE_KEY not found in environment variables!")

POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

wallet = Account.from_key(PRIVATE_KEY)
if wallet.address.lower() == POLYMARKET_ADDRESS.lower():
    USE_PROXY = False
    SIGNATURE_TYPE = 0
    TRADING_ADDRESS = Web3.to_checksum_address(wallet.address)
else:
    USE_PROXY = True
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# ==========================================
# 🎯 ARBITRAGE & STOP LOSS SETTINGS
# ==========================================
ENTRY_WINDOW_START = 855 # Start considering markets after 45 seconds of open 
ENTRY_WINDOW_END = 720 # Strictly ignore markets older than this
POSITION_SIZE = 10 

# Set to "LOW" to start with the side <= MAX_FIRST_BID (0.45)
# Set to "HIGH" to start with the side >= MIN_HIGH_BID (0.55) and <= MAX_HIGH_BID (0.66)
ENTRY_SIDE_PREFERENCE = "HIGH"

MAX_FIRST_BID = 0.45   # For "LOW" preference
MIN_HIGH_BID = 0.55    # For "HIGH" preference
MAX_HIGH_BID = 0.69   # Absolute max to avoid overpaying For "HIGH" preference

PAIR_TARGET_COST = 0.95 
STOP_LOSS_DELAY = 120 # Seconds to wait before triggering stop loss for buy leg

# Exit Settings
TRIGGER_ASK_PRICE = 0.08 
OTHER_SIDE_LIMIT_PRICE = 0.97
PANIC_SELL_THRESHOLD = 0.70  # Sell immediately if Leg_2 selling bid drops below $0.70
SELL_PRICE_ADJUSTMENT = 0.01
CHECK_INTERVAL = 5 
PRICE_IMPROVEMENT = 0.01 

MARKET_DURATION = 900
ROUND_HARD_STOP = 855 # cancelling execution of sell orders at the last 45 seconds if leg1 didn't sell yet
ROUND_HARD_STOP_leg2 = 45 # additional time for leg 2 to complete after leg 1 sell

# ==========================================
# 💥 SUDDEN DROP SETTINGS (NEW)
# ==========================================
SUDDEN_DROP_THRESHOLD = 0.15   # 15% drop trigger
SUDDEN_DROP_TIMEFRAME = 4      # Check drop over 4 seconds

HOST = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/Vwy188P6gCu8mAUrbObWH"

class BTCArbitrageBot:
    def __init__(self):
        print("🤖 BTC Arbitrage Bot Starting...")
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        try:
            if USE_PROXY:
                self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=SIGNATURE_TYPE, funder=TRADING_ADDRESS)
            else:
                self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
            
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            print(f"✅ Client Ready. Trading as: {self.client.get_address()}\n")
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            exit()
        
        self.yes_price_history = deque(maxlen=SUDDEN_DROP_TIMEFRAME + 1)
        self.no_price_history = deque(maxlen=SUDDEN_DROP_TIMEFRAME + 1)
        self.traded_markets = set() 

    def floor_round(self, n, decimals=1):
        multiplier = 10 ** decimals
        return math.floor(n * multiplier) / multiplier

    def get_all_shares_available(self, yes_token, no_token):
        print(f"🔍 Accessing Data API for position verification...")
        balances = {"yes": 0.0, "no": 0.0}
        try:
            url = f"{DATA_API_URL}/positions?user={TRADING_ADDRESS}"
            resp = requests.get(url, timeout=10).json()
            for pos in resp:
                asset = pos.get('asset')
                size = self.floor_round(float(pos.get('size', 0)), 1)
                if asset == yes_token: 
                    balances["yes"] = size
                    print(f"   📊 YES Position: {size} shares")
                elif asset == no_token: 
                    balances["no"] = size
                    print(f"   📊 NO Position: {size} shares")
            return balances
        except Exception as e:
            print(f"⚠️ Balance API error: {e}. Fallback used.")
            return {"yes": POSITION_SIZE, "no": POSITION_SIZE}

    def check_order_status(self, order_id):
        try:
            order_details = self.client.get_order(order_id)
            if order_details and isinstance(order_details, dict):
                status = order_details.get('status')
                if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                    price = float(order_details.get('avgFillPrice') or order_details.get('price') or 0)
                    return True, price
                return False, status
            return False, "PENDING"
        except:
            return False, "ERROR"

    def place_order_with_validation(self, token_id, price, size, side, order_type=OrderType.GTC, start_time=None, timeout=None):
        if not isinstance(price, (int, float)):
            return "ERROR", None

        target_price = round(price, 2)
        label = "Market (FOK)" if order_type == OrderType.FOK else "Limit (GTC)"

        while True: 
            order_id = None
            while order_id is None:
                # NEW: Stop loss check during the placement attempt phase
                if start_time and timeout:
                    if (time.time() - start_time) > timeout:
                        print(f"\n🛑 TIMEOUT: Reached during {label} placement attempt.")
                        return "TIMEOUT", None

                try:
                    print(f"📋 Placing {label} {side}: {size} shares @ ${target_price}")
                    resp = self.client.post_orders([
                        PostOrdersArgs(
                            order=self.client.create_order(
                                OrderArgs(price=target_price, size=size, side=side, token_id=token_id)
                            ), 
                            orderType=order_type 
                        )
                    ])
                    if resp and len(resp) > 0 and (resp[0].get('success') or resp[0].get('orderID')):
                        order_id = resp[0].get('orderID')
                        print(f"   🆔 Order Accepted! ID: {order_id}")
                    else:
                        error_msg = resp[0].get('errorMsg') if resp else 'No response'
                        print(f"   ⏳ API Error: {error_msg}. Retrying placement...")
                        time.sleep(1)
                except Exception as e:
                    print(f"   ❌ API Exception: {e}. Retrying in 1s...")
                    time.sleep(1)

            time.sleep(1.5) # Indexing delay

            while True:
                # ACTIVE STOP LOSS MONITORING (Polling Phase)
                if start_time and timeout:
                    if (time.time() - start_time) > timeout:
                        print(f"\n🛑 TIMEOUT: {label} failed to fill within {timeout}s.")
                        return "TIMEOUT", None

                filled, fill_data = self.check_order_status(order_id)
                if filled:
                    print(f"🎊 EXECUTED: {side} {label} filled at ${fill_data:.2f}")
                    return order_id, fill_data
                
                if order_type == OrderType.FOK:
                    print(f"   ⚠️ FOK Failed. Status: {fill_data}")
                    return None, None 
                
                print(f"   ⏳ {side} Limit Order still open (Status: {fill_data})...", end='\r')
                time.sleep(2)

    def execute_arbitrage_strategy(self, market, market_start_time):
        slug = market['slug']
        if slug in self.traded_markets: return
        
        rem = (market_start_time + 900) - time.time()
        
        # A) WATCH ENTRY WINDOW
        if rem > ENTRY_WINDOW_START:
            print(f"🕒 {slug[-4:]} - Window not reached ({int(rem)}s rem). Waiting...", end='\r')
            return 
        elif rem < ENTRY_WINDOW_END:
            print(f"⏩ {slug[-4:]} - Window expired ({int(rem)}s rem). Skipping.")
            self.traded_markets.add(slug)
            return

        # B) TRACK SUDDEN DROP LOGIC
        books = {side: self.get_order_book_depth(market[f'{side}_token']) for side in ['yes', 'no']}
        if not (books['yes'] and books['no']): return

        # Record current prices to history
        self.yes_price_history.append(books['yes']['best_ask'])
        self.no_price_history.append(books['no']['best_ask'])

        # Wait until we have enough history to compare
        if len(self.yes_price_history) <= SUDDEN_DROP_TIMEFRAME:
            print(f"📡 Building price history... ({len(self.yes_price_history)}/{SUDDEN_DROP_TIMEFRAME})", end='\r')
            return

        # Calculate percentage changes
        yes_change = (books['yes']['best_ask'] - self.yes_price_history[0]) / self.yes_price_history[0]
        no_change = (books['no']['best_ask'] - self.no_price_history[0]) / self.no_price_history[0]

        # Detect drop on YES
        yes_dropped = False
        if yes_change <= -SUDDEN_DROP_THRESHOLD:
            yes_dropped = True
            print(f"💥 SUDDEN DROP ON YES: {yes_change*100:.1f}% (${self.yes_price_history[0]} -> ${books['yes']['best_ask']})")

        # Detect drop on NO
        no_dropped = False
        if no_change <= -SUDDEN_DROP_THRESHOLD:
            no_dropped = True
            print(f"💥 SUDDEN DROP ON NO: {no_change*100:.1f}% (${self.no_price_history[0]} -> ${books['no']['best_ask']})")

        # Regular status print if no drop triggered (optional, helps with monitoring)
        if not yes_dropped and not no_dropped:
            print(f"🔍 Monitoring: YES {yes_change*100:+.1f}% (${books['yes']['best_ask']}) | NO {no_change*100:+.1f}% (${books['no']['best_ask']})", end='\r')

        # C) APPLY PREFERENCE IF DROP OCCURRED
        first, second = None, None
        
        if ENTRY_SIDE_PREFERENCE == "LOW":
            # Must have dropped AND be under MAX_FIRST_BID
            if yes_dropped and books['yes']['best_ask'] <= MAX_FIRST_BID:
                first, second = 'yes', 'no'
            elif no_dropped and books['no']['best_ask'] <= MAX_FIRST_BID:
                first, second = 'no', 'yes'
        
        elif ENTRY_SIDE_PREFERENCE == "HIGH":
            # Must have dropped AND be between MIN and MAX
            if yes_dropped and MIN_HIGH_BID <= books['yes']['best_ask'] <= MAX_HIGH_BID:
                first, second = 'yes', 'no'
            elif no_dropped and MIN_HIGH_BID <= books['no']['best_ask'] <= MAX_HIGH_BID:
                first, second = 'no', 'yes'

        if not first:
            # If a drop occurred but didn't meet price preferences, we wait
            if yes_dropped or no_dropped:
                print(f"⚠️ Drop occurred but price did not match {ENTRY_SIDE_PREFERENCE} range.")
            return

        # STAGE 1: LEAD LEG (FOK)
        # The rest of the strategy proceeds as normal using the selected 'first' leg
        f_id, actual_f_price = self.place_order_with_validation(
            market[f'{first}_token'], 
            round(books[first]['best_ask'] + PRICE_IMPROVEMENT, 2), 
            POSITION_SIZE, BUY, OrderType.FOK
        )
        if not f_id: return
        
        # STAGE 2: HEDGE LEG WITH SAFETY TIMER
        start_monitor = time.time() 
        s_price_target = round(PAIR_TARGET_COST - actual_f_price, 2)
        print(f"🔗 Hedge: Placing Limit Buy for {second.upper()} at ${s_price_target}")
        
        s_id, actual_s_price = self.place_order_with_validation(
            market[f'{second}_token'], s_price_target, POSITION_SIZE, BUY, 
            OrderType.GTC, start_time=start_monitor, timeout=STOP_LOSS_DELAY
        )

        # --- STAGE 3: EMERGENCY STOP LOSS (UPDATED WITH RETRY LOOP) ---
        if s_id == "TIMEOUT":
            print(f"\n🛑 STOP LOSS ACTIVATED: Safety window of {STOP_LOSS_DELAY}s exceeded.")
            self.client.cancel_all() # Clear the hanging hedge order
            
            print(f"⚠️ Initializing Persistent Liquidation for Lead Leg ({first.upper()})...")
            
            while True:
                # 1. Verify current shares available via Data API
                bal_check = self.get_all_shares_available(market['yes_token'], market['no_token'])
                current_shares = bal_check[first]
                
                if current_shares <= 0:
                    print(f"✅ Liquidation Complete: No remaining {first.upper()} shares found.")
                    break

                # 2. Fetch the latest Best Bid for the lead leg
                bid_data = self.get_order_book_depth(market[f'{first}_token'])
                if not bid_data or not bid_data['best_bid']:
                    print("   ⏳ No active bids found. Retrying in 2s...")
                    time.sleep(2)
                    continue

                # NEW LOGIC: Conditional Validation for HIGH preference
                if ENTRY_SIDE_PREFERENCE == "HIGH":
                    if bid_data['best_bid'] >= 0.47:
                        print(f"   ⏳ Preference HIGH: Current bid ${bid_data['best_bid']} >= 0.47. Waiting for lower bid...", end='\r')
                        time.sleep(1)
                        continue

                # 3. Attempt FOK Sell at the current Best Bid
                print(f"   🔄 Attempting liquidation: {current_shares} shares @ ${bid_data['best_bid']}")
                res_id, res_price = self.place_order_with_validation(
                    market[f'{first}_token'], 
                    bid_data['best_bid'], 
                    current_shares, 
                    SELL, 
                    OrderType.FOK
                )

                # 4. Success check: If order_id is returned, the shares are sold
                if res_id:
                    print(f"✅ Stop Loss Successful: Lead leg sold at ${res_price}")
                    break
                else:
                    print(f"   ⚠️ FOK Failed (Price move or slippage). Retrying liquidation...")
                    time.sleep(1) # Short pause before next attempt to avoid rate limits

            self.traded_markets.add(slug)
            return

        # STAGE 4: SYNC & LOCK
        print(f"\n✅ HEDGE FILLED! Both sides locked. Safety timer disabled.")
        print("⏳ Syncing Data API (30s delay)...")
        time.sleep(30) 
        distinct_balances = self.get_all_shares_available(market['yes_token'], market['no_token'])

        # STAGE 5: PROFIT EXIT MONITOR
        print(f"📡 EXIT MONITOR: Watching for Ask Price <= ${TRIGGER_ASK_PRICE}")
        while True:
            time.sleep(1) 
            now = time.time()
            rem_to_expiry = (market_start_time + ROUND_HARD_STOP) - now

            # Initial Expiry Check for Stage 5 loop
            if rem_to_expiry <= 0:
                print("\n⏰ Market Hard Stop (850s) reached. Exiting market.")
                break

            b_yes = self.get_order_book_depth(market['yes_token'])
            b_no = self.get_order_book_depth(market['no_token'])
            
            trig = None
            if b_yes and b_yes['best_ask'] and b_yes['best_ask'] <= TRIGGER_ASK_PRICE: trig = 'yes'
            elif b_no and b_no['best_ask'] and b_no['best_ask'] <= TRIGGER_ASK_PRICE: trig = 'no'
            
            if trig:
                other = 'no' if trig == 'yes' else 'yes'
                print(f"\n🎯 TRIGGER HIT: {trig.upper()} at ${TRIGGER_ASK_PRICE}!")
                
                try:
                    # Leg 1: Loser Side Sell
                    # Leg 1: Loser Side Sell (Now with Safety Timeout)
                    l1_id, l1_price = self.place_order_with_validation(
                        market[f'{trig}_token'], TRIGGER_ASK_PRICE, distinct_balances[trig], SELL, OrderType.GTC,
                        start_time=time.time(), timeout=rem_to_expiry  # <--- ADDED
                    )

                    # NEW: Handle the timeout to exit the strategy and move to next market
                    if l1_id == "TIMEOUT":
                        print("⏰ Leg 1 placement/fill timed out at Hard Stop. Exiting market.")
                        self.client.cancel_all()
                        self.traded_markets.add(slug)
                        return
                    
                    if l1_id:
                        print(f"✅ Leg 1 Sold. initiating Leg 2 sell order ({other.upper()})...")
                        l2_order_id = None
                        
                        while True:
                            # 1. Check Panic Condition First
                            l2_rem = (market_start_time + ROUND_HARD_STOP + ROUND_HARD_STOP_leg2) - time.time()
                            if l2_rem <= 0:
                                print("⏰ Hard Stop reached during Leg 2. Moving to next market.")
                                break # This breaks the Leg 2 loop and reaches the 'return' below

                            l2_book = self.get_order_book_depth(market[f'{other}_token'])
                            if l2_book and l2_book['best_bid'] and l2_book['best_bid'] < PANIC_SELL_THRESHOLD:
                                print(f"🚨 PANIC SELL TRIGGERED: Bid at ${l2_book['best_bid']}. Liquidating!")
                                self.client.cancel_all()
                                self.place_order_with_validation(
                                    market[f'{other}_token'], l2_book['best_bid'], 
                                    distinct_balances[other], SELL, OrderType.FOK
                                )
                                break # Exit Leg 2 Loop after Panic Sell

                            # 2. Place/Check Limit Order
                            if not l2_order_id:
                                # Use short timeout (1s) to keep this monitor loop active
                                l2_order_id, _ = self.place_order_with_validation(
                                    market[f'{other}_token'], OTHER_SIDE_LIMIT_PRICE, 
                                    distinct_balances[other], SELL, OrderType.GTC,
                                    start_time=time.time(), timeout=1 
                                )

                            if l2_order_id and l2_order_id != "TIMEOUT":
                                filled, _ = self.check_order_status(l2_order_id)
                                if filled:
                                    print(f"🎊 SUCCESS: Leg 2 filled at ${OTHER_SIDE_LIMIT_PRICE}")
                                    break # Exit Leg 2 Loop after Fill
                            
                            print(f"   ⏳ Leg 2: Waiting for $0.97 | Panic: ${PANIC_SELL_THRESHOLD} | Bid: ${l2_book['best_bid'] if l2_book else '??'}", end='\r')
                            time.sleep(1)

                except Exception as e:
                    print(f"\n❌ CRITICAL ERROR in Stage 5: {e}. Starting Emergency Recovery...")
                    self.client.cancel_all()
                    
                    # Verify if shares are still held
                    recovery_bal = self.get_all_shares_available(market['yes_token'], market['no_token'])
                    for side in ['yes', 'no']:
                        if recovery_bal[side] > 0:
                            print(f"⚠️ Residual {side.upper()} position found ({recovery_bal[side]} shares). Closing...")
                            r_book = self.get_order_book_depth(market[f'{side}_token'])
                            if r_book and r_book['best_bid']:
                                self.place_order_with_validation(
                                    market[f'{side}_token'], r_book['best_bid'], 
                                    recovery_bal[side], SELL, OrderType.FOK
                                )
                
                self.traded_markets.add(slug)
                return

    def get_order_book_depth_safe(self, token_id):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                book = self.client.get_order_book(token_id)
                # ... existing logic ...
                return book
            except socket.gaierror as e:
                if e.errno == 11001:
                    print(f"⚠️ DNS Error (11001). Internet blip? Retrying {attempt+1}/{max_retries}...")
                    time.sleep(2)
                else:
                    raise e
        return None

    def get_order_book_depth(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            best_ask = min(float(o.price) for o in book.asks) if book.asks else None
            best_bid = max(float(o.price) for o in book.bids) if book.bids else None
            return {'best_ask': best_ask, 'best_bid': best_bid}
        except: return None

    def get_market_from_slug(self, slug):
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            resp = requests.get(url, timeout=10).json()
            event = resp[0]
            raw_ids = event['markets'][0].get('clobTokenIds')
            clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            return {'slug': slug, 'yes_token': clob_ids[0], 'no_token': clob_ids[1]}
        except: return None

    def run(self):
        while True:
            ts = (int(time.time()) // 900) * 900
            m = self.get_market_from_slug(f"btc-updown-15m-{ts}")
            if m: self.execute_arbitrage_strategy(m, ts)
            time.sleep(1)

if __name__ == "__main__":

    BTCArbitrageBot().run()
