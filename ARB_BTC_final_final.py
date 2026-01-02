import os
import time
import requests
import json
import math
import socket
import csv
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
# 🎯 ARBITRAGE & BUY SETTINGS
# ==========================================
ENTRY_WINDOW_START = 840 # Start considering markets after 45 seconds of open 
ENTRY_WINDOW_END = 420 # Strictly ignore markets older than this
SUDDEN_DROP_THRESHOLD = 0.15   # 15% drop trigger
SUDDEN_DROP_TIMEFRAME = 4      # Check drop over 4 seconds
POSITION_SIZE = 7
MIN_ENTRY_LIQUIDITY = 200
MIN_MOVING_RATIO = 0.5        # ⚖️ Min Lead/Hedge historical ratio
MAX_MOVING_RATIO = 1.5        # ⚖️ Max Lead/Hedge historical ratio
MOVING_RATIO_TIMEFRAME = 60   # Number of data points to consider for moving ratio
PAIR_TARGET_COST = 0.95  # Total cost target for both legs combined
STOP_LOSS_DELAY = 420 # Seconds to wait before triggering stop loss for buy leg
HEDGE_STOP_LOSS_PRICE = 0.32  # If Lead Leg bid drops below this, trigger stop loss
STOP_LOSS_COOLDOWN_MINUTES = 30 # Minutes to avoid re-entering after a stop loss, cool down period

ENTRY_SIDE_PREFERENCE = "HIGH"
MAX_FIRST_BID = 0.45   # For "LOW" preference
MIN_HIGH_BID = 0.54    # For "HIGH" preference
MAX_HIGH_BID = 0.68   # Absolute max to avoid overpaying For "HIGH" preference

# ==========================================
# SELL / EXIT Settings
# ==========================================
TRIGGER_ASK_PRICE = 0.12
OTHER_SIDE_LIMIT_PRICE = 0.92
PANIC_SELL_THRESHOLD = 0.65 # If the bid drops below this during exit, we panic sell immediately  
target_wait_time = 480 # The sell minute check 
MARKET_DURATION = 900
ROUND_HARD_STOP = 810 # cancelling execution of sell orders at the last 90 seconds if leg1 didn't sell yet
ROUND_HARD_STOP_leg2 = 900 - ROUND_HARD_STOP # additional time for leg 2 to complete after leg 1 sell

SELL_PRICE_ADJUSTMENT = 0.01
sustainment_time = 3
PRICE_IMPROVEMENT = 0.005

# ==========================================
# 📊 LOGGING CONFIGURATION
# ==========================================
LOG_FILE = "ARBITRAGE_trading_log.csv"

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            headers = [
                "Market Title", "Market Link", "Status",
                "Stage1_Time", "Lead_Side", "Lead_Price",
                "Stage2_Time", "Hedge_Side", "Hedge_Price",
                "Sell_L1_Side", "Sell_L1_Price", "Sell_L1_Time",
                "Sell_L2_Side", "Sell_L2_Price", "Sell_L2_Time",
                "Final_Status", "Notes", 'Yes_Ask_Size', 'No_Ask_Size',
                "Hedge_Bid_Size","lead_moving_avg","hedge_moving_avg","moving_ratio"
            ]
            writer.writerow(headers)

init_log()


HOST = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"

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
        
        self.yes_price_history = deque(maxlen=SUDDEN_DROP_TIMEFRAME)
        self.no_price_history = deque(maxlen=SUDDEN_DROP_TIMEFRAME)
        self.yes_liq_history = deque(maxlen=MOVING_RATIO_TIMEFRAME)
        self.no_liq_history = deque(maxlen=MOVING_RATIO_TIMEFRAME)
        self.traded_markets = set() 

    def save_log(self, record): 
        try:
            with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    record.get('title', '-'), record.get('link', '-'), record.get('status', '-'),
                    record.get('s1_time', '-'), record.get('lead_side', '-'), record.get('lead_price', '-'),
                    record.get('s2_time', '-'), record.get('hedge_side', '-'), record.get('hedge_price', '-'),
                    record.get('l1_side', '-'), record.get('l1_price', '-'), record.get('l1_time', '-'),
                    record.get('l2_side', '-'), record.get('l2_price', '-'), record.get('l2_time', '-'),
                    record.get('final_status', '-'), record.get('notes', '-'),
                    record.get('Yes_Ask_Size', '-'), record.get('No_Ask_Size', '-'),
                    record.get('Hedge_Bid_Size', '-'), record.get('lead_moving_avg', '-'),
                    record.get('hedge_moving_avg', '-'), record.get('moving_ratio', '-')
                ])
        except Exception as e:
            print(f"❌ Logging Error: {e}")
    
    def floor_round(self, n, decimals=1):
        multiplier = 10 ** decimals
        return math.floor(n * multiplier) / multiplier

    def get_all_shares_available(self, yes_token, no_token):
        """
        Fetches positions from the Data API with a 5-attempt retry logic.
        If all attempts fail, raises an Exception to skip the market.
        """
        for attempt in range(5):
            try:
                print(f"🔍 Accessing Data API for position verification (Attempt {attempt+1}/5)...")
                balances = {"yes": 0.0, "no": 0.0}
                url = f"{DATA_API_URL}/positions?user={TRADING_ADDRESS}"
                
                # Increase timeout slightly to handle API lag
                resp = requests.get(url, timeout=12).json()
                
                # Iterate through returned positions
                for pos in resp:
                    asset = pos.get('asset')
                    # Use float(pos.get('size', 0)) to handle string/null outputs
                    size = self.floor_round(float(pos.get('size', 0)), 1)
                    
                    if asset == yes_token: 
                        balances["yes"] = size
                        print(f"    📊 YES Position: {size} shares")
                    elif asset == no_token: 
                        balances["no"] = size
                        print(f"    📊 NO Position: {size} shares")
                
                # Success: Return the actual balances found
                return balances

            except Exception as e:
                print(f"⚠️ Balance API attempt {attempt+1} failed: {e}")
                if attempt < 4:
                    time.sleep(2)
                else:
                    print(f"❌ Critical: Balance API failed after 5 attempts. Aborting market.")
                    raise Exception("Data API Unreachable")

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

    def place_order_with_validation(self, token_id, price, size, side, order_type=OrderType.GTC, start_time=None, timeout=None, lead_token_id=None):
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
                        return "TIMEOUT", "PLACEMENT_FAILURE"

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
            if order_type == OrderType.FOK:
                time.sleep(1.5) # Indexing delay

            while True:
                # ACTIVE STOP LOSS MONITORING (Polling Phase)
                if start_time and timeout:
                    now = time.time()
                    elapsed = now - start_time

                    # 1. TIME-BASED STOP LOSS
                    if elapsed > timeout:
                        print(f"\n🛑 TIMEOUT: {label} active but not filled within {timeout}s.")
                        return order_id, "TIMEOUT"

                    # 2. PRICE-BASED STOP LOSS (Only for HIGH Preference)
                    if lead_token_id and ENTRY_SIDE_PREFERENCE == "HIGH":
                        if elapsed >= 60:
                            lead_book = self.get_order_book_depth(lead_token_id)
                            if lead_book and lead_book['best_bid']:
                                if lead_book['best_bid'] < HEDGE_STOP_LOSS_PRICE:
                                    print(f"\n🛑 MOMENTUM CRASH: Lead Leg bid ${lead_book['best_bid']} dropped below ${HEDGE_STOP_LOSS_PRICE}")
                                    return order_id, "PRICE_STOP_LOSS"
                        else:
                            # Optional: Print status during the grace period
                            print(f"   ⏳ Grace Period: {int(60 - elapsed)}s remaining before Stop Loss active...", end='\r')

                filled, fill_data = self.check_order_status(order_id)
                if filled:
                    print(f"🎊 EXECUTED: {side} {label} filled at ${fill_data:.2f}")
                    return order_id, fill_data

                # NEW: Catch API/Network Errors during polling
                if fill_data == "ERROR":
                    print(f"\n⚠️ API ERROR: Lost connection during {label} status check. Re-attempting...")
                    return order_id, "RETRY"

                if order_type == OrderType.FOK:
                    print(f"   ⚠️ FOK Failed. Status: {fill_data}")
                    return None, None 
                
                print(f"   ⏳ {side} Limit Order still open (Status: {fill_data})...", end='\r')
                time.sleep(2)

    def execute_arbitrage_strategy(self, market, market_start_time):
        slug = market['slug']
        if slug in self.traded_markets: return

        log_rec = {
            "title": market['title'], "link": market['link'], "status": "STARTED",
            "s1_time": "", "lead_side": "", "lead_price": "",
            "s2_time": "", "hedge_side": "", "hedge_price": "",
            "l1_side": "", "l1_price": "", "l1_time": "",
            "l2_side": "", "l2_price": "", "l2_time": "",
            "final_status": "OPEN", "notes": "",
            "Yes_Ask_Size": "", "No_Ask_Size": "" ,"Hedge_Bid_Size": "", 
            "lead_moving_avg": "", "hedge_moving_avg": "", "moving_ratio": ""}
        
        rem = (market_start_time + 900) - time.time()
        
        # A) WATCH ENTRY WINDOW
        if rem > ENTRY_WINDOW_START:
            print(f"🕒 {slug[-4:]} - Window not reached ({int(rem)}s rem). Waiting...", end='\r')
            return 
        elif rem < ENTRY_WINDOW_END:
            print(f"⏩ {slug[-4:]} - Window expired ({int(rem)}s rem). Skipping.")
            self.traded_markets.add(slug)
            return

        # B) TRACK SUDDEN DROP LOGIC + LIQUIDITY FILTER
        books = {side: self.get_order_book_depth(market[f'{side}_token']) for side in ['yes', 'no']}
        if not (books['yes'] and books['no']): return

        # 1. Update History
        self.yes_price_history.append(books['yes']['best_ask'])
        self.no_price_history.append(books['no']['best_ask'])
        self.yes_liq_history.append(books['yes']['ask_size'])
        self.no_liq_history.append(books['no']['ask_size'])

        # Wait until we have enough history to compare
        if len(self.yes_price_history) < SUDDEN_DROP_TIMEFRAME:
            print(f"📡 Building price history... ({len(self.yes_price_history)}/{SUDDEN_DROP_TIMEFRAME})", end='\r')
            return

        # 2. Drop Detection - Calculate percentage changes
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
            return 

        # 1. IDENTIFY NATURAL ROLES (Cheaper side is always Hedge)
        if books['yes']['best_ask'] < books['no']['best_ask']:
            natural_hedge, natural_lead = 'yes', 'no'
        else:
            natural_hedge, natural_lead = 'no', 'yes'

        # 2. APPLY PREFERENCE & PRICE RANGE VALIDATION
        first, second = None, None

        if ENTRY_SIDE_PREFERENCE == "HIGH":
            # Target the expensive side (Winner)
            price = books[natural_lead]['best_ask']
            if MIN_HIGH_BID <= price <= MAX_HIGH_BID:
                first, second = natural_lead, natural_hedge
            else:
                print(f"⚠️ REJECTED - Outside buy range: HIGH preference Lead (${price}) outside range [{MIN_HIGH_BID}-{MAX_HIGH_BID}]")
                return

        elif ENTRY_SIDE_PREFERENCE == "LOW":
            # Target the cheap side (Crashed)
            price = books[natural_hedge]['best_ask']
            if price <= MAX_FIRST_BID:
                first, second = natural_hedge, natural_lead
            else:
                print(f"⚠️ REJECTED: LOW preference Lead (${price}) > MAX_FIRST_BID (${MAX_FIRST_BID})")
                return        

        # 3. 🛡️ DNA FILTERS

        yes_size = books['yes'].get('ask_size', 0)
        no_size = books['no'].get('ask_size', 0)

        # A) Absolute Liquidity check
        if yes_size < MIN_ENTRY_LIQUIDITY or no_size < MIN_ENTRY_LIQUIDITY:
            print(f"❌ REJECTED: Thin books. YES: {books['yes']['ask_size']}, NO: {books['no']['ask_size']}")
            return
        
        # B) Entry Ratio (Hedge Buffer: Hedge MUST be >= Lead)
        if books[second]['ask_size'] < books[first]['ask_size']:
            print(f"❌ REJECTED: Hedge ({books[second]['ask_size']}) < Lead ({books[first]['ask_size']}). No Buffer.")
            return

        # C) Ask Heavy (Hedge Ask > Hedge Bid)
        if books[second]['ask_size'] <= books[second]['bid_size']:
            print(f"❌ REJECTED: Hedge is Bid-Heavy. Skipping to avoid rapid reversal.")
            return
        else:
            hedge_bid_size = books[second]['bid_size']

        # D) Moving Ratio (Lead Avg / Hedge Avg)
        avg_lead_liq = sum(self.yes_liq_history if first == 'yes' else self.no_liq_history) / len(self.yes_liq_history)
        avg_hedge_liq = sum(self.no_liq_history if first == 'yes' else self.yes_liq_history) / len(self.no_liq_history)
        moving_ratio = avg_lead_liq / avg_hedge_liq

        if not (MIN_MOVING_RATIO <= moving_ratio <= MAX_MOVING_RATIO):
            print(f"❌ REJECTED: Moving Ratio {moving_ratio:.2f} imbalanced market.")
            return

        print(f"✅ PASSED Liquidity Filters: Executing {ENTRY_SIDE_PREFERENCE} on {first.upper()} at ${books[first]['best_ask']}")


        # STAGE 1: LEAD LEG (FOK)
        # The rest of the strategy proceeds as normal using the selected 'first' leg
        f_id, actual_f_price = self.place_order_with_validation(
            market[f'{first}_token'], 
            round(books[first]['best_ask'] + PRICE_IMPROVEMENT, 2), 
            POSITION_SIZE, BUY, OrderType.FOK
        )
                
        if not f_id: return
        
        if f_id:
            log_rec['s1_time'] = datetime.now().strftime("%H:%M:%S")
            log_rec['lead_side'] = first.upper()
            log_rec['lead_price'] = actual_f_price
            log_rec['status'] = "LEAD_BOUGHT"
            log_rec['Yes_Ask_Size'] = yes_size
            log_rec['No_Ask_Size'] = no_size
            log_rec['Hedge_Bid_Size'] = hedge_bid_size
            log_rec['lead_moving_avg'] = avg_lead_liq
            log_rec['hedge_moving_avg'] = avg_hedge_liq
            log_rec['moving_ratio'] = moving_ratio

        # STAGE 2: HEDGE LEG
        start_monitor = time.time() 
        s_price_target = round(PAIR_TARGET_COST - actual_f_price, 2)
        print(f"🔗 Hedge: Placing Limit Buy for {second.upper()} at ${s_price_target}")
        
        actual_s_price = "RETRY"
        while actual_s_price == "RETRY":
            print(f"🛡️ Attempting Hedge Leg ({second.upper()}) at ${s_price_target}...")
            s_id, actual_s_price = self.place_order_with_validation(
                market[f'{second}_token'], s_price_target, 
                POSITION_SIZE, BUY, OrderType.GTC,
                start_time=start_monitor, timeout=STOP_LOSS_DELAY,
                lead_token_id=market[f'{first}_token']
            )

            # If the result was a transient error, the loop continues and re-attempts the buy
            if actual_s_price == "RETRY":
                print("   🔄 Re-attempting hedge placement due to API error...")
                self.client.cancel_all() # Clean any partial/ghost orders before retry
                time.sleep(1)

        if s_id and actual_s_price not in ["TIMEOUT", "PRICE_STOP_LOSS", "ERROR"]:
            log_rec['s2_time'] = datetime.now().strftime("%H:%M:%S")
            log_rec['hedge_side'] = second.upper()
            log_rec['hedge_price'] = actual_s_price
            log_rec['status'] = "FULLY_HEDGED"


        # --- STAGE 3: EMERGENCY STOP LOSS ---
        if actual_s_price in ["TIMEOUT", "PRICE_STOP_LOSS"]:
            log_rec['final_status'] = f"STOP_LOSS_{actual_s_price}"
            print(f"\n🛑 STOP LOSS ACTIVATED: Triggered by {actual_s_price}")

            # Step A: Clear any hanging orders to prevent "ghost" fills during audit
            self.client.cancel_all()

            # Step B: Position Verification (The "Double-Fill" Check)
            print("🔍 Auditing wallet")
            time.sleep(5) # Wait for Data API to sync
            bal_check = self.get_all_shares_available(market['yes_token'], market['no_token'])
            
            if bal_check['yes'] >= POSITION_SIZE and bal_check['no'] >= POSITION_SIZE:
                print(f"🔄 RECOVERY: Both YES and NO detected despite {actual_s_price}! This is an Arb, not a Stop Loss.")
                print(f"⏭️ Bypassing liquidation and moving to Stage 4 (Sync & Lock).")
                # Do not liquidate; proceed to Stage 4
            else:
                # Normal Stop Loss: Only one side (or partial) held
                print(f"⚠️ Hedge failed. Initializing Persistent Liquidation for {first.upper()}...")

                while True:
                    # 1. Verify current shares available via Data API
                    bal_check = self.get_all_shares_available(market['yes_token'], market['no_token'])
                    current_shares = bal_check[first] if bal_check[first] >= POSITION_SIZE else bal_check[second]
                    
                    if current_shares <= 0:
                        print(f"✅ Liquidation Complete: No remaining {first.upper()} shares found.")
                        break

                    # 2. Fetch the latest Best Bid for the lead leg
                    bid_data = self.get_order_book_depth(market[f'{first}_token'])
                    if not bid_data or not bid_data['best_bid']:
                        print("   ⏳ No active bids found. Retrying in 1s...")
                        time.sleep(1)
                        continue

                    # 3. Attempt FOK Sell at the current Best Bid
                    print(f"   🔄 Attempting liquidation: {current_shares} shares @ ${bid_data['best_bid']}")
                    res_id, res_price = self.place_order_with_validation(
                        market[f'{first}_token'], bid_data['best_bid'], 
                        current_shares, SELL, OrderType.FOK
                    )

                    # 4. Success check: If order_id is returned, the shares are sold
                    if res_id:
                        print(f"✅ Stop Loss Successful: Lead leg sold at ${res_price}")
                        liquidation_price = res_price # Capture price for logging
                        break
                    else:
                        print(f"   ⚠️ FOK Failed (Price move or slippage). Retrying liquidation...")
                        time.sleep(1) # Short pause before next attempt to avoid rate limits

                log_rec['l1_side'] = first.upper()
                log_rec['l1_price'] = liquidation_price
                log_rec['l1_time'] = datetime.now().strftime("%H:%M:%S")
                log_rec['notes'] = f"Emergency Liquidation due to {actual_s_price}"
                self.save_log(log_rec)

                # 💤 LOCAL COOLDOWN (Only reached if Hedge Buy fails)
                resume_time = datetime.now() + timedelta(minutes=STOP_LOSS_COOLDOWN_MINUTES)
                print(f"\n💤 HEDGE FAILURE COOLDOWN: Sleeping for {STOP_LOSS_COOLDOWN_MINUTES} minutes.")
                print(f"⏰ Resume Time: {resume_time.strftime('%H:%M:%S')}")
                
                time.sleep(STOP_LOSS_COOLDOWN_MINUTES * 60)                 
                print(f"🚀 Cooldown ended. Resuming market monitoring...")

                self.traded_markets.add(slug)
                return

        # STAGE 4: SYNC & LOCK
        print(f"\n✅ HEDGE FILLED! Both sides locked. Safety timer disabled.")
        current_time = time.time()
        elapsed_since_start = current_time - market_start_time
        
        if elapsed_since_start < target_wait_time:
            wait_duration = target_wait_time - elapsed_since_start
            print(f"🕒 Market age: {int(elapsed_since_start)}s. Dynamic Wait: {int(wait_duration)}s. Waiting until 10th minute...")
            time.sleep(wait_duration)
        else:
            print(f"🕒 Market already at {int(elapsed_since_start)}s. Skipping wait and proceeding to Stage 5.")

        # Final check for shares before moving to Stage 5
        distinct_balances = self.get_all_shares_available(market['yes_token'], market['no_token'])

        # STAGE 5: PROFIT EXIT MONITOR
        print(f"📡 EXIT MONITOR: Watching for Ask Price <= ${TRIGGER_ASK_PRICE}")
        trigger_hit_time = None
        trig = None
        target_side_locked = None # NEW: Lock the side being checked

        while True:
            time.sleep(1) 
            now = time.time()
            rem_to_expiry = (market_start_time + ROUND_HARD_STOP) - now

            # Initial Expiry Check for Stage 5 loop
            if rem_to_expiry <= 0:
                print("\n⚠️ ROUND HARD STOP: Leg 1 never hit target. Exiting market.")
                self.client.cancel_all()
                log_rec['final_status'] = "Success - Sell order did not hit target"
                self.save_log(log_rec)
                break

            # If no side is locked, check both
            if not target_side_locked:
                b_yes = self.get_order_book_depth(market['yes_token'])
                b_no = self.get_order_book_depth(market['no_token'])
                
                if b_yes and b_yes['best_ask'] and b_yes['best_ask'] <= TRIGGER_ASK_PRICE:
                    target_side_locked = 'yes'
                elif b_no and b_no['best_ask'] and b_no['best_ask'] <= TRIGGER_ASK_PRICE:
                    target_side_locked = 'no'

            if target_side_locked:
                # Once a side is locked, only monitor that specific side
                book = self.get_order_book_depth(market[f'{target_side_locked}_token'])
                
                if book and book['best_ask'] and book['best_ask'] <= TRIGGER_ASK_PRICE:
                    if trigger_hit_time is None:
                        trigger_hit_time = time.time()
                        print(f"\n🎯 TARGET HIT: {target_side_locked.upper()} <= ${TRIGGER_ASK_PRICE}")
                    
                    elapsed_hit = time.time() - trigger_hit_time
                    if elapsed_hit >= sustainment_time:
                        print(f"✅ SUSTAINED: Price held for {int(elapsed_hit)}s. Proceeding to Sell...")
                        trig = target_side_locked # Set the final trigger side
                        break 
                    else:
                        print(f"   ⏳ Sustainment: {int(sustainment_time - elapsed_hit)}s remaining for {target_side_locked.upper()}...", end='\r')
                else:
                    # Price rebounded above target: Unlock and Reset
                    print(f"\n⚠️ REBOUND: {target_side_locked.upper()} moved to ${book['best_ask'] if book else 'N/A'}. Resetting.")
                    trigger_hit_time = None
                    target_side_locked = None
            
        if trig:
            other = 'no' if trig == 'yes' else 'yes'
            print(f"\n🎯 TRIGGER HIT: {trig.upper()} at ${TRIGGER_ASK_PRICE}!")
            
            try:
                # --- LEG 1: LOSER SIDE SELL ---
                l1_id, l1_price = self.place_order_with_validation(
                    market[f'{trig}_token'], TRIGGER_ASK_PRICE, distinct_balances[trig], 
                    SELL, OrderType.GTC, start_time=time.time(), timeout=rem_to_expiry
                )

                if l1_id and l1_id != "TIMEOUT" and isinstance(l1_price, (int, float)):
                    print(f"✅ Leg 1 Sold successfully at ${l1_price}. Initiating Leg 2 Manager...")

                    log_rec['l1_side'] = trig.upper()
                    log_rec['l1_price'] = l1_price
                    log_rec['l1_time'] = datetime.now().strftime("%H:%M:%S")
                    
                    # Proceed to monitor Leg 2 Winner side
                    l2_order_id = None
                    l2_placed = False  # The flag to prevent re-placement

                    while True:
                        # A) HARD STOP CHECK
                        l2_rem = (market_start_time + ROUND_HARD_STOP + ROUND_HARD_STOP_leg2) - time.time()
                        if l2_rem <= 0:
                            print("⏰ Hard Stop reached before Leg 2 placement. Exiting.")
                            log_rec['final_status'] = "TIMEOUT in last 90s - Before placing L1"
                            self.save_log(log_rec)                            
                            break

                        # B) PLACEMENT PHASE (Only runs until we get an ID)
                        if not l2_placed:
                            print(f"📡 Placing Leg 2 Limit Order at ${OTHER_SIDE_LIMIT_PRICE}...")
                            # Increased to 5s to give the API time to return the ID
                            l2_order_id, _ = self.place_order_with_validation(
                                market[f'{other}_token'], OTHER_SIDE_LIMIT_PRICE, 
                                distinct_balances[other], SELL, OrderType.GTC,
                                start_time=time.time(), timeout=5
                            )
                                                        
                        # FIX: Capture the ID even if the fill-check part timed out
                        if l2_order_id:
                            l2_book = self.get_order_book_depth(market[f'{other}_token'])
                            print(f"✔️ Order {l2_order_id} active. Monitoring Bid: ${l2_book['best_bid'] if l2_book else '??'} | Panic: {PANIC_SELL_THRESHOLD}", end='\r')
                            l2_placed = True
                        else:
                            print("⏳ No ID received. Retrying placement...")
                            self.client.cancel_all()
                            continue

                        # C) MONITORING PHASE (Only runs AFTER l2_placed is True)
                        # 1. Check if Limit Order filled
                        filled, _ = self.check_order_status(l2_order_id)

                        if filled:
                            print(f"🎊 SUCCESS: Leg 2 filled at ${OTHER_SIDE_LIMIT_PRICE}")
                            log_rec['l2_side'] = other.upper()
                            log_rec['l2_price'] = OTHER_SIDE_LIMIT_PRICE
                            log_rec['l2_time'] = datetime.now().strftime("%H:%M:%S")
                            log_rec['final_status'] = "SUCCESS"
                            self.save_log(log_rec)
                            return

                        # 2. Check for Panic Crash
                        l2_book = self.get_order_book_depth(market[f'{other}_token'])
                        if l2_book and l2_book['best_bid']:
                            bid = l2_book['best_bid']
                            if bid <= PANIC_SELL_THRESHOLD:
                                panic_limit_price = round(PANIC_SELL_THRESHOLD - 0.1, 2)
                                print(f"\n🚨 PANIC TRIGGERED! Bid ${bid} <= ${PANIC_SELL_THRESHOLD}")
                                print(f"🛑 Canceling existing orders and placing aggressive persistent Limit Sell at ${panic_limit_price}...")
                                self.client.cancel_all()
                                
                                # Place the aggressive panic limit order
                                # timeout=l2_rem ensures it stays open until the very last second of the market
                                p_id, p_price = self.place_order_with_validation(
                                    market[f'{other}_token'], panic_limit_price, 
                                    distinct_balances[other], SELL, OrderType.GTC,
                                    start_time=time.time(), timeout=l2_rem
                                )
                                
                                if p_id and isinstance(p_price, (int, float)):
                                    print(f"✅ Panic Sell Executed successfully at ${p_price}")
                                else:
                                    print(f"⚠️ Panic Sell remained open until hard stop or failed to fill.")
                                
                                log_rec['l2_side'] = other.upper()
                                log_rec['l2_price'] = p_price if p_id and isinstance(p_price, (int, float)) else "STUCK"
                                log_rec['l2_time'] = datetime.now().strftime("%H:%M:%S")
                                log_rec['final_status'] = "PANIC_EXIT"
                                self.save_log(log_rec)
                                
                                break # Exit after panic liquidation
                        
                        print(f"   ⏳ Watch: 0.97 | Panic: {PANIC_SELL_THRESHOLD} | Bid: {l2_book['best_bid'] if l2_book else '??'}", end='\r')
                        time.sleep(0.5) # Fast cycle for safety
                                        # NEW: Handling for Leg 1 failure

                elif l1_id == "TIMEOUT":
                    print(f"⚠️ Leg 1 Sell timed out without filling. Bypassing Leg 2 to avoid naked position.")
                    self.client.cancel_all()
                    log_rec['final_status'] = "TIMEOUT in last 90s - after placing L1"
                    self.save_log(log_rec)
                else:
                    print(f"❌ Leg 1 Placement failed entirely. Aborting strategy for this market.")
                    self.client.cancel_all()
                    log_rec['final_status'] = "TIMEOUT in last 90s - after placing L1"
                    self.save_log(log_rec)

            except Exception as e:
                print(f"\n❌ Stage 5 Critical Error: {e}")
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
                                recovery_bal[side], SELL, OrderType.FOK,
                                start_time=time.time(), timeout=5
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
            # Find best ask price and its associated size
            if book.asks:
                best_ask_obj = min(book.asks, key=lambda x: float(x.price))
                best_ask, ask_size = float(best_ask_obj.price), float(best_ask_obj.size)
            else:
                best_ask, ask_size = None, 0

            # Fetch Best Bid and its Size
            if book.bids:
                best_bid_obj = max(book.bids, key=lambda x: float(x.price))
                best_bid, bid_size = float(best_bid_obj.price), float(best_bid_obj.size)
            else:
                best_bid, bid_size = None, 0
            
            return {
                'best_ask': best_ask, 'ask_size': ask_size,
                'best_bid': best_bid, 'bid_size': bid_size
            }
        except: return None

    def get_market_from_slug(self, slug):
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            resp = requests.get(url, timeout=10).json()
            event = resp[0]
            market_data = event['markets'][0]
            
            # Construct metadata
            return {
                'slug': slug, 
                'title': event.get('title', slug),
                'link': f"https://polymarket.com/event/{slug}",
                'yes_token': json.loads(market_data['clobTokenIds'])[0], 
                'no_token': json.loads(market_data['clobTokenIds'])[1]
            }
        except: return None

    def run(self):
        while True:
            ts = (int(time.time()) // 900) * 900
            m = self.get_market_from_slug(f"btc-updown-15m-{ts}")
            if m: self.execute_arbitrage_strategy(m, ts)
            time.sleep(1)

if __name__ == "__main__":
    BTCArbitrageBot().run()
