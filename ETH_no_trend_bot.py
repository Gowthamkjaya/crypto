import os
import socket
import csv
import time
import requests
import math
from web3 import Web3
import json
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
PRIVATE_KEY = "0x6cbe6580d99aa3a3bf1d7d93e5df6024d8d1cedb080526f4c834196fa2fe156f"
POLYMARKET_ADDRESS = "0x6C83e9bd90C67fDb623ff6E46f6Ef8C4EC5A1cba"

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
# 🛠️ GLOBAL TRADING VARIABLES
# ==========================================
TRADE_SIDE = "BOTH"         # Options: "YES", "NO", or "BOTH"
ENTRY_PRICE = 0.96        # Target entry price (bid must be >= this)
STOP_LOSS_PRICE = 0.73    # Trigger for the sustained stop loss
SUSTAIN_TIME = 3          # Seconds price must stay below SL to trigger
POSITION_SIZE = 20        # Number of shares per trade
MARKET_WINDOW = 240       # Only trade within the last 180 seconds
POLLING_INTERVAL = 1      # Frequency of price checks (seconds)
ENTRY_TIMEOUT = 210       # Max seconds to wait for entry order to fill
SL_TIMEOUT = 10           # Max seconds for stop loss liquidation

# ==========================================
# 📊 LOGGING CONFIGURATION
# ==========================================
LOG_FILE = "ETH_NO_trading_log.csv"

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            headers = [
                "Market Title", "Market Link", "Status",
                "entry1_Time", "entry_Side", "entry_Price","position_size",
                "sl_Time", "sl_Price", "Final_Status", "Notes", "is_SL_Triggered"
            ]
            writer.writerow(headers)
init_log()

HOST = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/BAkKqoHrx_codLcJVeGmH" # private polygon RPC


class EthNoTrendBot:
    def __init__(self):
        print("🤖 ETH No Trend Bot Starting...")
        print(f"📊 Configuration:")
        print(f"   Trade Side: {TRADE_SIDE}")
        print(f"   Entry Price: ${ENTRY_PRICE}")
        print(f"   Stop Loss: ${STOP_LOSS_PRICE}")
        print(f"   Position Size: {POSITION_SIZE} shares")
        print(f"   Trading Window: Last {MARKET_WINDOW}s of market\n")
        
        # Validate TRADE_SIDE input
        if TRADE_SIDE not in ["YES", "NO", "BOTH"]:
            print(f"❌ Invalid TRADE_SIDE: {TRADE_SIDE}. Must be 'YES', 'NO', or 'BOTH'")
            exit()
        
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
        
        self.active_trade = False
        self.traded_markets = set()

    def save_log(self, record): 
        try:
            with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    record.get('title', '-'), record.get('link', '-'), record.get('status', '-'),
                    record.get('entry1_Time', '-'), record.get('entry_Side', '-'), record.get('entry_Price', '-'),
                    record.get('position_size', '-'), record.get('sl_Time', '-'), record.get('sl_Price', '-'), 
                    record.get('Final_Status', '-'), record.get('notes', '-'), record.get('is_SL_Triggered', '-')
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
                
                resp = requests.get(url, timeout=3).json()
                
                for pos in resp:
                    asset = pos.get('asset')
                    size = self.floor_round(float(pos.get('size', 0)), 1)
                    
                    if asset == yes_token: 
                        balances["yes"] = size
                        print(f"    📊 YES Position: {size} shares")
                    elif asset == no_token: 
                        balances["no"] = size
                        print(f"    📊 NO Position: {size} shares")
                
                return balances

            except Exception as e:
                print(f"⚠️ Balance API attempt {attempt+1} failed: {e}")
                if attempt < 4:
                    time.sleep(2)
                else:
                    print(f"❌ Critical: Balance API failed after 5 attempts. Aborting market.")
                    raise Exception("Data API Unreachable")

    def check_order_status(self, order_id):
        """Extracts status and fill price from the CLOB with retry logic."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                order_details = self.client.get_order(order_id)
                if order_details and isinstance(order_details, dict):
                    status = order_details.get('status')
                    if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                        price = float(order_details.get('avgFillPrice') or order_details.get('price') or 0)
                        return True, price
                    return False, status
                return False, "PENDING"
            except socket.gaierror as e:
                if e.errno == 11001:
                    print(f"⚠️ DNS Error (11001). Retrying {attempt+1}/{max_retries}...")
                    time.sleep(1)
                else:
                    return False, "ERROR"
            except Exception as e:
                print(f"⚠️ Order status check error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return False, "ERROR"
        return False, "ERROR"

    def place_order_with_validation(self, token_id, price, size, side, order_type=OrderType.GTC, start_time=None, timeout=None):
        """Single-attempt order placement - caller handles retries with fresh data"""
        if not isinstance(price, (int, float)):
            print(f"❌ Invalid price type: {type(price)}")
            return None, None

        target_price = round(price, 2)
        label = "Market (FOK)" if order_type == OrderType.FOK else "Limit (GTC)"
        
        # Timeout check before placement
        if start_time and timeout:
            if (time.time() - start_time) > timeout:
                print(f"🛑 TIMEOUT: Exceeded before {label} placement.")
                return None, "TIMEOUT"

        # Single placement attempt
        order_id = None
        try:
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=self.client.create_order(
                        OrderArgs(price=target_price, size=size, side=side, token_id=token_id)
                    ), 
                    orderType=order_type 
                )
            ])
            
            if resp and len(resp) > 0:
                response_obj = resp[0]
                
                if response_obj.get('orderID'):
                    order_id = response_obj.get('orderID')
                else:
                    error_msg = response_obj.get('errorMsg', 'Unknown error')
                    print(f"   ⚠️ Order Rejected: {error_msg}")
                    return None, None
            else:
                print(f"   ⚠️ Empty response from API")
                return None, None
                    
        except Exception as e:
            print(f"   ❌ API Exception: {type(e).__name__}: {e}")
            if "404" in str(e) or "Not Found" in str(e):
                print(f"   🔍 Token doesn't exist or market unavailable")
            return None, None
        
        if order_id is None:
            return None, None

        # Allow time for indexing (with timeout respect)
        if order_type == OrderType.FOK:
            if start_time and timeout:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    print(f"🛑 TIMEOUT: Exceeded during FOK indexing wait.")
                    return None, "TIMEOUT"
                sleep_time = min(1.5, timeout - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            else:
                time.sleep(1.5)

        # MONITORING PHASE - Poll until filled or timeout
        while True:
            # Timeout check during monitoring
            if start_time and timeout:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    print(f"🛑 TIMEOUT: {label} not filled within {timeout:.1f}s.")
                    try:
                        self.client.cancel(order_id)
                        print(f"   🚫 Cancelled unfilled order {order_id}")
                    except Exception as e:
                        print(f"   ⚠️ Could not cancel order: {e}")
                    return None, "TIMEOUT"
                
            filled, fill_data = self.check_order_status(order_id)
            
            if filled:
                print(f"🎊 EXECUTED: {side} {label} filled at ${fill_data:.2f}")
                return order_id, fill_data

            # Handle API errors
            if fill_data == "ERROR":
                print(f"⚠️ API ERROR: Lost connection during {label} status check.")
                return None, None

            # FOK-specific handling
            if order_type == OrderType.FOK:
                status_str = str(fill_data).upper()
                if status_str in ["PENDING", "MATCHED", "OPEN"]:
                    # Check timeout before waiting
                    if start_time and timeout:
                        elapsed = time.time() - start_time
                        if elapsed > timeout:
                            print(f"🛑 TIMEOUT: FOK pending but exceeded {timeout:.1f}s.")
                            try:
                                self.client.cancel(order_id)
                                print(f"   🚫 Cancelled pending order {order_id}")
                            except Exception as e:
                                print(f"   ⚠️ Could not cancel order: {e}")
                            return None, "TIMEOUT"
                        
                        wait_time = min(3, timeout - elapsed)
                        print(f"   ⏳ FOK is {status_str}. Re-verifying in {wait_time:.1f}s...")
                        time.sleep(wait_time)
                    else:
                        print(f"   ⏳ FOK is {status_str}. Re-verifying in 3s...")
                        time.sleep(3)
                    
                    filled, fill_data = self.check_order_status(order_id)
                    if filled:
                        print(f"🎊 EXECUTED: {side} {label} filled at ${fill_data:.2f}")
                        return order_id, fill_data
                
                print(f"   ⚠️ FOK Failed. Status: {fill_data}")
                return None, None

            print(f"   ⏳ {side} Limit Order still open (Status: {fill_data})...", end='\r')
            time.sleep(2)

    def persistent_liquidation(self, token_id, side_name, market):
        print(f"⚠️ Initializing Persistent Liquidation for {side_name}...")
        
        while True:
            # 1. Verify current shares
            bal_check = self.get_all_shares_available(market['yes_token'], market['no_token'])
            
            # Get the correct balance based on side
            if side_name == 'YES':
                current_shares = bal_check['yes']
            elif side_name == 'NO':
                current_shares = bal_check['no']
            else:
                current_shares = 0
            
            if current_shares <= 0:
                print(f"✅ Liquidation Complete: No remaining {side_name} shares found.")
                return None
            
            # 2. Fetch latest Best Bid
            bid_data = self.get_order_book_depth(token_id)
            if not bid_data or not bid_data['best_bid']:
                print("   ⏳ No active bids found. Retrying in 1s...")
                time.sleep(0.5)
                continue
            
            # 3. Attempt FOK Sell at current Best Bid
            print(f"   🔄 Attempting liquidation: {current_shares} shares @ ${bid_data['best_bid']}")
            res_id, res_price = self.place_order_with_validation(
                token_id, bid_data['best_bid'], 
                current_shares, SELL, OrderType.FOK
            )
            
            # 4. Success check
            if res_id and isinstance(res_price, (int, float)):
                print(f"✅ Liquidation Successful: {side_name} sold at ${res_price}")
                return res_price
            else:
                print(f"   ⚠️ FOK Failed (Price move or slippage). Retrying liquidation...")
                time.sleep(1)

    def monitor_market(self, market_data, market_start_time):
        """The main loop for monitoring the trading window and executing the strategy."""
        slug = market_data['slug']
        
        # Check if already traded
        if slug in self.traded_markets:
            return
        
        yes_token = market_data['yes_token']
        no_token = market_data['no_token']
        
        # Initialize log record
        log_rec = {
            'title': market_data['title'],
            'link': market_data['link'],
            'status': 'MONITORING',
            'entry1_Time': '-',
            'entry_Side': '-',
            'entry_Price': '-',
            'position_size': '-',
            'sl_Time': '-',
            'sl_Price': '-',
            'Final_Status': 'OPEN',
            'notes': '',
            'is_SL_Triggered': '-'
        }
        # Track when we enter the trading window for timeout enforcement
        entry_window_start = None

        # Display monitoring message based on trade side
        if TRADE_SIDE == "YES":
            print(f"📡 Monitoring {market_data['title']} for YES @ ${ENTRY_PRICE}...")
        elif TRADE_SIDE == "NO":
            print(f"📡 Monitoring {market_data['title']} for NO @ ${ENTRY_PRICE}...")
        else:  # BOTH
            print(f"📡 Monitoring {market_data['title']} for BOTH sides @ ${ENTRY_PRICE}...")

        while True:
            # 1. Check time window
            rem_seconds = (market_start_time + 900) - time.time()
            
            if rem_seconds > MARKET_WINDOW:
                print(f"🕒 Outside window: {rem_seconds-MARKET_WINDOW:.0f}s remaining. Waiting...   ", end='\r')
                entry_window_start = None
                time.sleep(POLLING_INTERVAL)
                continue

            if entry_window_start is None:
                entry_window_start = time.time()
                print(f"\n🔵 Entered trading window. Entry timeout starts now ({ENTRY_TIMEOUT}s)")

            if rem_seconds <= 0:
                print(f"\n⏰ Market expired. Moving to next market.")
                self.traded_markets.add(slug)
                return
            
            # 2. Fetch order books for both sides
            yes_book = self.get_order_book_depth(yes_token)
            no_book = self.get_order_book_depth(no_token)
            
            if not yes_book or not no_book:
                print("⚠️ Unable to fetch order books. Retrying...   ", end='\r')
                time.sleep(POLLING_INTERVAL)
                continue
            
            yes_bid = yes_book['best_bid'] if yes_book['best_bid'] else 0
            no_bid = no_book['best_bid'] if no_book['best_bid'] else 0
            yes_ask = yes_book['best_ask'] if yes_book['best_ask'] else 999
            no_ask = no_book['best_ask'] if no_book['best_ask'] else 999
            yes_ask_size = yes_book['ask_size'] if yes_book['ask_size'] else 0
            no_ask_size = no_book['ask_size'] if no_book['ask_size'] else 0
            
            # Display current prices (single line update)
            if TRADE_SIDE == "YES":
                print(f"Monitoring YES | Bid: ${yes_bid:.2f} | Ask: ${yes_ask:.2f} ({yes_ask_size}) | Target Bid: ${ENTRY_PRICE}   ", end='\r')
            elif TRADE_SIDE == "NO":
                print(f"Monitoring NO | Bid: ${no_bid:.2f} | Ask: ${no_ask:.2f} ({no_ask_size}) | Target Bid: ${ENTRY_PRICE}   ", end='\r')
            else:  # BOTH
                status_msg = f"Monitoring BOTH | YES: ${yes_bid:.2f}/${yes_ask:.2f} ({yes_ask_size}) | NO: ${no_bid:.2f}/${no_ask:.2f} ({no_ask_size}) | Target: ${ENTRY_PRICE}   "
                print(status_msg, end='\r')
            
            # 3. Entry Logic - BID triggers entry, but we buy at ASK (with liquidity check)
            triggered_side = None
            triggered_token = None
            
            if TRADE_SIDE == "YES":
                # Entry condition: BID reaches target AND ask has sufficient liquidity
                if yes_bid >= ENTRY_PRICE and yes_ask_size >= POSITION_SIZE:
                    triggered_side = "YES"
                    triggered_token = yes_token
                    
            elif TRADE_SIDE == "NO":
                # Entry condition: BID reaches target AND ask has sufficient liquidity
                if no_bid >= ENTRY_PRICE and no_ask_size >= POSITION_SIZE:
                    triggered_side = "NO"
                    triggered_token = no_token
                    
            elif TRADE_SIDE == "BOTH":
                yes_valid = yes_bid >= ENTRY_PRICE and yes_ask_size >= POSITION_SIZE                
                no_valid = no_bid >= ENTRY_PRICE and no_ask_size >= POSITION_SIZE
                
                if yes_valid and no_valid:
                    print(f"\n⚡ BOTH sides valid! Choosing based on bid strength...")
                    if yes_bid >= no_bid:
                        print(f"   → Selected YES (Bid ${yes_bid:.2f} >= NO Bid ${no_bid:.2f})")
                        triggered_side = "YES"
                        triggered_token = yes_token
                    else:
                        print(f"   → Selected NO (Bid ${no_bid:.2f} > YES Bid ${yes_bid:.2f})")
                        triggered_side = "NO"
                        triggered_token = no_token
                elif yes_valid:
                    print(f"\n→ Only YES side valid, entering YES")
                    triggered_side = "YES"
                    triggered_token = yes_token
                elif no_valid:
                    print(f"\n→ Only NO side valid, entering NO")
                    triggered_side = "NO"
                    triggered_token = no_token
            
            # Dynamic order placement with re-evaluation
            if not self.active_trade and triggered_side:
                print(f"\n🚀 ENTRY TRIGGERED: {triggered_side} - Starting dynamic order placement...")
                
                entry_attempt = 0
                max_entry_attempts = 20
                
                while entry_attempt < max_entry_attempts:
                    entry_attempt += 1
                    
                    # Check timeout
                    time_in_window = time.time() - entry_window_start
                    if time_in_window > ENTRY_TIMEOUT:
                        print(f"\n❌ Entry window Timeout! Reached last few seconds of the Market.")
                        self.traded_markets.add(slug)
                        return
                    
                    # Re-fetch current order book
                    print(f"🔄 Entry Attempt {entry_attempt}/{max_entry_attempts}: Re-evaluating market...")
                    current_book = self.get_order_book_depth(triggered_token)
                    
                    if not current_book:
                        print(f"   ⚠️ Failed to fetch order book. Retrying in 1s...")
                        time.sleep(1)
                        continue
                    
                    current_bid = current_book['best_bid'] if current_book['best_bid'] else 0
                    current_ask = current_book['best_ask'] if current_book['best_ask'] else 999
                    current_ask_size = current_book['ask_size'] if current_book['ask_size'] else 0
                    
                    # Validate entry criteria still met
                    if current_bid < ENTRY_PRICE-0.02 or current_ask > 0.991:
                        print(f"⚠️ Not tradeable. BID: ${current_bid:.2f}, ASK: ${current_ask:.2f}. Retrying in 1s...")
                        time.sleep(1)
                        continue
                    
                    if current_ask_size < POSITION_SIZE:
                        print(f"⚠️ Insufficient liquidity: {current_ask_size} < {POSITION_SIZE}. Retrying in 1s...")
                        time.sleep(1)
                        continue
                    
                    # Place FOK order at current ask
                    print(f"   📋 Placing FOK: {POSITION_SIZE} shares @ ${current_ask:.2f} (Bid: ${current_bid:.2f})")
                    
                    entry_start = time.time()
                    remaining_timeout = ENTRY_TIMEOUT - time_in_window
                    order_id, fill_price = self.place_order_with_validation(
                        triggered_token, current_ask, POSITION_SIZE, BUY, 
                        OrderType.FOK, start_time=entry_start, timeout=remaining_timeout
                    )
                    
                    # Handle timeout
                    if fill_price == "TIMEOUT":
                        print(f"\n❌ Order placement timed out after {entry_attempt} attempts.")
                        self.traded_markets.add(slug)
                        return
                    
                    # CRITICAL: Wait and validate order completion before proceeding
                    if order_id and not isinstance(fill_price, (int, float)):
                        print(f"   ⏳ Order {order_id} status pending. Waiting 5s for completion...")
                        time.sleep(5)
                        
                        # Re-check order status after wait
                        filled, final_status = self.check_order_status(order_id)
                        
                        if filled and isinstance(final_status, (int, float)):
                            # Order completed successfully
                            fill_price = final_status
                            print(f"   ✅ Order completed at ${fill_price:.2f}")
                        else:
                            # Order still not filled or failed
                            print(f"   ⚠️ Order not completed after wait. Status: {final_status}")
                            order_id = None
                            fill_price = None
                    
                    # Success!
                    if order_id and isinstance(fill_price, (int, float)):
                        log_rec['entry1_Time'] = datetime.now().strftime("%H:%M:%S")
                        log_rec['entry_Side'] = triggered_side
                        log_rec['entry_Price'] = fill_price
                        log_rec['position_size'] = POSITION_SIZE
                        log_rec['status'] = 'SUCCESSFUL_ENTRY'
                        log_rec['notes'] = f'Filled on attempt {entry_attempt}'
                        
                        self.active_trade = True
                        print(f"\n✅ Position Active: {POSITION_SIZE} {triggered_side} shares @ ${fill_price} (Attempt {entry_attempt})")
                        
                        self.manage_position(triggered_token, triggered_side, market_data, market_start_time, log_rec)
                        self.traded_markets.add(slug)
                        return
                    
                    # FOK failed - market moved, re-evaluate
                    print(f"   ⚠️ FOK failed. Market may have moved. Re-evaluating in 0.5s...")
                    time.sleep(0.5)
                
                # Max attempts reached - return to monitoring
                print(f"\n⚠️ Failed to enter after {max_entry_attempts} attempts. Returning to monitoring...")
                self.traded_markets.add(slug)
                return
            
            time.sleep(POLLING_INTERVAL)

    def manage_position(self, token_id, side_name, market_data, market_start_time, log_rec):
        """Handles the 3-second sustained stop loss after entry."""
        print(f"🛡️ Position Active on {side_name}. Monitoring for sustained Stop Loss...")
        breach_start_time = None

        while self.active_trade:
            # Check market expiry
            rem_seconds = (market_start_time + 900) - time.time()
            if rem_seconds <= 1:
                print("🏁 Market Closing. Taking profit.")
                log_rec['status'] = 'SUCCESSFUL_EXIT'
                log_rec['Final_Status'] = 'SUCCESS'
                log_rec['notes'] = f'{side_name} position held until market close'
                self.save_log(log_rec)
                self.active_trade = False
                return

            # Get current bid
            book_data = self.get_order_book_depth(token_id)
            if not book_data or book_data['best_bid'] is None:
                print("⚠️ Unable to fetch order book during monitoring...", end='\r')
                time.sleep(0.5)
                continue
            
            current_bid = book_data['best_bid']
            now = time.time()

            # Stop Loss Condition
            if current_bid <= STOP_LOSS_PRICE + 0.02:
                if breach_start_time is None:
                    breach_start_time = now
                    print(f"\n⚠️ {side_name} price breached ${STOP_LOSS_PRICE}. Starting {SUSTAIN_TIME}s timer...")
                
                elapsed = now - breach_start_time
                print(f"⏱️ Breach sustained for {elapsed:.1f}s / {SUSTAIN_TIME}s...", end='\r')
                
                if elapsed >= SUSTAIN_TIME:
                    print(f"\n🛑 STOP LOSS TRIGGERED: {side_name} price sustained below ${STOP_LOSS_PRICE} for {SUSTAIN_TIME}s")
                    
                    # Execute persistent liquidation
                    sl_price = self.persistent_liquidation(token_id, side_name, market_data)
                    
                    if sl_price:
                        log_rec['sl_Time'] = datetime.now().strftime("%H:%M:%S")
                        log_rec['sl_Price'] = sl_price
                        log_rec['Final_Status'] = 'STOP_LOSS'
                        log_rec['notes'] = f'{side_name} stop loss triggered at ${current_bid}, liquidated at ${sl_price}'
                        log_rec['is_SL_Triggered'] = 'YES'
                    else:
                        log_rec['Final_Status'] = 'STOP_LOSS_FAILED'
                        log_rec['is_SL_Triggered'] = 'YES'
                        log_rec['notes'] = f'{side_name} stop loss triggered but liquidation failed'
                    
                    self.save_log(log_rec)
                    self.active_trade = False
                    print("📉 Position Liquidated.")
                    return
            else:
                if breach_start_time:
                    print(f"\n✅ {side_name} price recovered to ${current_bid}. Resetting timer.")
                    log_rec['is_SL_Triggered'] = 'YES'
                breach_start_time = None

            time.sleep(0.5)  # Fast polling for safety

    def get_order_book_depth(self, token_id):
        """Fetches order book with retry logic for network issues."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                book = self.client.get_order_book(token_id)
                
                # Find best ask
                if book.asks:
                    best_ask_obj = min(book.asks, key=lambda x: float(x.price))
                    best_ask, ask_size = float(best_ask_obj.price), float(best_ask_obj.size)
                else:
                    best_ask, ask_size = None, 0

                # Find best bid
                if book.bids:
                    best_bid_obj = max(book.bids, key=lambda x: float(x.price))
                    best_bid, bid_size = float(best_bid_obj.price), float(best_bid_obj.size)
                else:
                    best_bid, bid_size = None, 0
                
                return {
                    'best_ask': best_ask, 'ask_size': ask_size,
                    'best_bid': best_bid, 'bid_size': bid_size
                }
            except socket.gaierror as e:
                if e.errno == 11001:
                    print(f"⚠️ DNS Error (11001). Retrying {attempt+1}/{max_retries}...")
                    time.sleep(1)
                else:
                    print(f"❌ Socket error: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(1)
            except Exception as e:
                print(f"⚠️ Order book fetch error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
        
        return None

    def get_market_from_slug(self, slug):
        """Fetches market data from Polymarket API with retry logic."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                print(f"   🔍 Fetching: {url} (Attempt {attempt+1}/{max_retries})")
                
                resp = requests.get(url, timeout=10)
                
                # Check HTTP status
                if resp.status_code == 404:
                    print(f"   ⚠️ 404 Error: Market '{slug}' not found on API")
                    print(f"      This usually means:")
                    print(f"      • Market hasn't been created yet (too early)")
                    print(f"      • Wrong slug format")
                    print(f"      • API indexing delay")
                    return None
                
                if resp.status_code != 200:
                    print(f"   ⚠️ HTTP {resp.status_code}: {resp.text[:100]}")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    return None
                
                data = resp.json()
                
                # Validate response structure
                if not data or len(data) == 0:
                    print(f"   ⚠️ Empty response from API")
                    return None
                
                event = data[0]
                
                if 'markets' not in event or len(event['markets']) == 0:
                    print(f"   ⚠️ No markets found in event")
                    return None
                
                market_data = event['markets'][0]
                
                # Parse token IDs
                token_ids = json.loads(market_data['clobTokenIds'])
                yes_token = token_ids[0]
                no_token = token_ids[1]
                
                print(f"   ✅ Market found: {event.get('title', slug)}")
                
                # Verify tokens are valid (basic check)
                if not yes_token or not no_token:
                    print(f"   ❌ Invalid token IDs extracted")
                    return None
                
                return {
                    'slug': slug, 
                    'title': event.get('title', slug),
                    'link': f"https://polymarket.com/event/{slug}",
                    'yes_token': yes_token, 
                    'no_token': no_token
                }
            except requests.exceptions.RequestException as e:
                print(f"   ⚠️ Network error attempt {attempt+1}/{max_retries}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(3)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                print(f"   ⚠️ Data parsing error: {e}")
                print(f"   📄 Raw response: {resp.text[:200] if 'resp' in locals() else 'N/A'}")
                return None
            except Exception as e:
                print(f"⚠️ Market fetch attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(300)
        
        return None

    def run(self):
        """Main execution loop."""
        print("🚀 ETH No Trend Bot Running...\n")
        
        while True:
            try:
                current_time = int(time.time())
                ts = (current_time // 900) * 900
                slug = f"eth-updown-15m-{ts}"
                
                # Calculate market timing
                elapsed_since_open = current_time - ts
                time_until_next = 900 - elapsed_since_open
                
                # Debug: Show current market info
                open_time = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                print(f"\n⏰ Current Market: {slug} | Open Time: {open_time} | Next in: {time_until_next}s", end='\r')
                
                # Skip if already traded
                if slug in self.traded_markets:
                    print(f"   ✓ Already traded this market. Waiting for next...", end='\r')
                    time.sleep(60)
                    continue
                
                # Only attempt to fetch if market should exist (give 5s grace period for creation)
                if elapsed_since_open < 5:
                    print(f"   ⏳ Market just opened. Waiting 5s for API indexing...", end='\r')
                    time.sleep(5)
                    continue
                
                m = self.get_market_from_slug(slug)
                
                if m:
                    self.monitor_market(m, ts)
                else:
                    print(f"⚠️ Unable to fetch market {slug}. Retrying...", end='\r')
                    time.sleep(2)
                    continue
                
                time.sleep(1)
                
            except KeyboardInterrupt:
                print("\n🛑 Bot stopped by user.")
                break
            except Exception as e:
                print(f"\n❌ Unexpected error in main loop: {e}")
                print("Continuing after 3s...")
                time.sleep(3)

if __name__ == "__main__":
    EthNoTrendBot().run()