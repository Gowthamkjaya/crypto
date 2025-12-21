import os
import time
import requests
import json
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime, timedelta, timezone
import csv
import pandas as pd
from collections import deque

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

# Your Polymarket username/proxy address (the address shown on Polymarket)
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Check what address the private key controls
from eth_account import Account
wallet = Account.from_key(PRIVATE_KEY)
print(f"🔑 Private key controls: {wallet.address}")
print(f"🏦 Polymarket shows: {POLYMARKET_ADDRESS}")

# If they match, we can trade directly (EOA mode)
# If they don't match, Polymarket uses a proxy contract
if wallet.address.lower() == POLYMARKET_ADDRESS.lower():
    print(f"✅ Direct match - using EOA mode")
    USE_PROXY = False
    SIGNATURE_TYPE = 0
    TRADING_ADDRESS = Web3.to_checksum_address(wallet.address)
else:
    print(f"⚠️ Addresses differ - Polymarket uses proxy contract")
    print(f"   We'll try proxy mode with signature_type=1 (Magic Link)")
    USE_PROXY = True
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# Manual Override (optional - leave empty for auto-detection)
MANUAL_SLUG = ""  # e.g., "btc-updown-15m-1765593000"

# Slug generation for BTC 15min markets
INTERVAL = 900  # 15 minutes in seconds

# ==========================================
# 🎯 STRATEGY SELECTION
# ==========================================
ACTIVE_STRATEGY = "BOTH"  # Options: "MID_GAME", "DUMP_HEDGE", "BOTH"

# ==========================================
# 🎯 MID-GAME STRATEGY SETTINGS (NO ONLY)
# ==========================================
# Entry when 5-10 min remaining
MG_LOCK_WINDOW_START = 300  # Start at 5 minutes remaining
MG_LOCK_WINDOW_END = 600    # End at 10 minutes remaining

# Entry criteria
MG_MIN_ENTRY_PRICE = 0.80   # Minimum entry price
MG_MAX_ENTRY_PRICE = 0.84   # Maximum entry price
MG_MIN_BID_SIZE = 300       # Minimum liquidity required

# Position sizing
MG_WALLET_PERCENTAGE = 0.50  # Use 50% of wallet balance for each trade

# Exit Settings
MG_TAKE_PROFIT = 0.95       # Take profit at $0.95
MG_STOP_LOSS = 0.57         # Stop loss at $0.57

# ⭐ NEW: Precise order settings
MG_ENTRY_WAIT_TIME = 5      # Wait 5 seconds for entry fill
MG_EXIT_WAIT_TIME = 3       # Wait 3 seconds for exit fill
MG_MAX_SLIPPAGE = 0.01      # Allow 1 cent slippage if needed

# ==========================================
# 💥 DUMP HEDGE STRATEGY SETTINGS
# ==========================================
DH_WATCH_WINDOW_MINUTES = 2    # Watch first 2 minutes of round
DH_DUMP_THRESHOLD = 0.15       # 15% price drop triggers entry
DH_DUMP_TIMEFRAME = 3          # Check drop over 3 seconds
DH_SUM_TARGET = 0.95           # leg1_price + leg2_price must be < this
DH_SHARES_PER_LEG = 5          # Fixed shares for dump hedge strategy

# ⭐ NEW: Dump hedge precise order settings
DH_ENTRY_WAIT_TIME = 3         # Wait 3 seconds for leg fills
DH_MAX_SLIPPAGE = 0.02         # Allow 2 cent slippage for dump hedge

# System settings
CHECK_INTERVAL = 1          # Check every 1 second for dump detection
MIN_ORDER_SIZE = 0.1        # Minimum order size

# Trade Logging
TRADE_LOG_FILE = "polymarket_trades.csv"
ENABLE_EXCEL = True

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class DualStrategyBot:
    def __init__(self):
        print("🤖 Dual Strategy Trading Bot Starting...")
        print(f"📊 Active Strategy: {ACTIVE_STRATEGY}")
        
        # 1. Setup Web3 (For Balance)
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # 2. Setup Client (For Trading)
        try:
            print(f"🔗 Setting up Polymarket client...")
            
            if USE_PROXY:
                print(f"   Mode: Proxy with Magic Link (signature_type={SIGNATURE_TYPE})")
                print(f"   Funder: {TRADING_ADDRESS}")
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID, 
                    signature_type=SIGNATURE_TYPE,
                    funder=TRADING_ADDRESS
                )
            else:
                print(f"   Mode: EOA (direct trading from {TRADING_ADDRESS})")
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID
                )
            
            print("🔐 Deriving API credentials...")
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            print(f"✅ Trading as: {self.client.get_address()}\n")
            
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            import traceback
            traceback.print_exc()
            exit()
            
        # Track markets
        self.traded_markets_midgame = set()
        self.traded_markets_dumphedge = set()
        
        # Session tracking
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        
        # Dump hedge tracking
        self.dh_leg1_active = False
        self.dh_leg1_side = None
        self.dh_leg1_price = None
        self.dh_leg1_shares = 0
        self.dh_current_market = None
        
        # Price history for dump detection
        self.yes_price_history = deque(maxlen=DH_DUMP_TIMEFRAME + 1)
        self.no_price_history = deque(maxlen=DH_DUMP_TIMEFRAME + 1)
        
        # Trade logging
        self.trade_logs = []
        self.initialize_trade_log()

    def initialize_trade_log(self):
        """Initialize CSV file with headers if it doesn't exist"""
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'strategy', 'market_slug', 'market_title',
                'entry_side', 'entry_time', 'intended_entry_price', 'actual_entry_price',
                'entry_size', 'actual_shares_purchased', 'yes_price_at_entry', 'no_price_at_entry', 
                'time_remaining_at_entry', 'bid_size_at_entry',
                'exit_reason', 'exit_time', 'exit_price', 'time_in_trade_seconds',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'leg2_side', 'leg2_price', 'leg2_shares', 'combined_cost',
                'session_trade_number', 'balance_before', 'balance_after', 'session_pnl_running'
            ]
            
            with open(TRADE_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log initialized: {TRADE_LOG_FILE}")

    def log_trade(self, trade_data):
        """Append trade data to CSV and optionally Excel"""
        try:
            self.trade_logs.append(trade_data)
            
            with open(TRADE_LOG_FILE, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
            
            if ENABLE_EXCEL:
                df = pd.DataFrame(self.trade_logs)
                excel_file = TRADE_LOG_FILE.replace('.csv', '.xlsx')
                df.to_excel(excel_file, index=False, engine='openpyxl')
            
            print(f"✅ Trade logged to {TRADE_LOG_FILE}")
            
        except Exception as e:
            print(f"⚠️ Error logging trade: {e}")

    def get_balance(self):
        """Get USDC.e balance from the trading address"""
        try:
            raw_bal = self.usdc_contract.functions.balanceOf(TRADING_ADDRESS).call()
            decimals = self.usdc_contract.functions.decimals().call()
            return raw_bal / (10 ** decimals)
        except Exception as e:
            print(f"⚠️ Balance error: {e}")
            return 0.0

    def get_market_from_slug(self, slug):
        """Get market details from a specific slug"""
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            resp = requests.get(url, timeout=10).json()
            
            if not resp or len(resp) == 0:
                return None
            
            event = resp[0]
            raw_ids = event['markets'][0].get('clobTokenIds')
            clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            
            return {
                'slug': slug,
                'yes_token': clob_ids[0],
                'no_token': clob_ids[1],
                'title': event.get('title', slug)
            }
        except Exception as e:
            return None

    def get_best_ask(self, token_id):
        """Get cheapest available price"""
        try:
            book = self.client.get_order_book(token_id)
            if book.asks:
                return min(float(o.price) for o in book.asks)
            return None
        except:
            return None

    def get_best_bid(self, token_id):
        """Get best available selling price"""
        try:
            book = self.client.get_order_book(token_id)
            if book.bids:
                return max(float(o.price) for o in book.bids)
            return None
        except:
            return None

    def get_order_book_depth(self, token_id):
        """Get detailed order book information including bid size"""
        try:
            book = self.client.get_order_book(token_id)
            
            best_ask = min(float(o.price) for o in book.asks) if book.asks else None
            best_bid = max(float(o.price) for o in book.bids) if book.bids else None
            
            bid_size = 0
            if book.bids:
                for order in book.bids:
                    bid_size += float(order.size)
            
            return {
                'best_ask': best_ask,
                'best_bid': best_bid,
                'bid_size': bid_size
            }
        except Exception as e:
            print(f"   ⚠️ Error getting order book: {e}")
            return None

    # ==========================================
    # ⭐ NEW PRECISE ORDER FUNCTIONS
    # ==========================================
    
    def place_strict_limit_order(self, token_id, limit_price, size, side, wait_time=5):
        """
        ⭐ NEW FUNCTION: Place a GTC limit order at EXACT price and wait for fill.
        Returns: (success, actual_price, order_id)
        """
        try:
            size = round(size, 1)
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Order size {size} below minimum {MIN_ORDER_SIZE}")
                return False, None, None
            
            # Ensure price is within bounds
            limit_price = max(0.01, min(0.99, round(limit_price, 2)))
            
            print(f"   🎯 Placing STRICT LIMIT {side} | Size: {size} | Price: ${limit_price:.2f}")
            
            # Create GTC (Good-Til-Cancelled) order
            order = self.client.create_order(OrderArgs(
                price=limit_price,
                size=size,
                side=side,
                token_id=token_id,
            ))
            
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=order,
                    orderType=OrderType.GTC,  # Will stay in book until filled or cancelled
                )
            ])
            
            if not resp or len(resp) == 0:
                print(f"   ❌ Empty response from CLOB")
                return False, None, None
            
            order_result = resp[0]
            if not (order_result.get('success') or order_result.get('orderID')):
                error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                print(f"   ❌ Order failed: {error_msg}")
                return False, None, None
            
            order_id = order_result.get('orderID')
            print(f"   📝 Order placed: {order_id} | Waiting {wait_time}s for fill...")
            
            # Wait and check for fill
            start_time = time.time()
            while time.time() - start_time < wait_time:
                time.sleep(0.5)
                
                filled, actual_price = self.check_order_status(order_id)
                if filled:
                    print(f"   ✅ FILLED @ ${actual_price:.2f}")
                    return True, actual_price, order_id
            
            # Not filled - cancel order
            print(f"   ⏰ Not filled in {wait_time}s - cancelling order")
            try:
                self.client.cancel(order_id)
                print(f"   🚫 Order cancelled")
            except:
                pass
            
            return False, None, order_id
            
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            return False, None, None

    def place_market_order_with_validation(self, token_id, max_price, size, side, max_slippage=0.01):
        """
        ⭐ NEW FUNCTION: Place market order but ONLY if current price is within max_slippage of max_price.
        This prevents buying at prices worse than your criteria.
        """
        try:
            size = round(size, 1)
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Order size {size} below minimum {MIN_ORDER_SIZE}")
                return None
            
            # Check current market price
            current_price = self.get_best_ask(token_id) if side == BUY else self.get_best_bid(token_id)
            
            if not current_price:
                print(f"   ⚠️ Cannot get current price")
                return None
            
            # Validate price is acceptable
            if side == BUY:
                if current_price > (max_price + max_slippage):
                    print(f"   🚫 Price moved too high: ${current_price:.2f} > ${max_price + max_slippage:.2f}")
                    return None
                limit_price = min(0.99, round(current_price + max_slippage, 2))
            else:
                if current_price < (max_price - max_slippage):
                    print(f"   🚫 Price moved too low: ${current_price:.2f} < ${max_price - max_slippage:.2f}")
                    return None
                limit_price = max(0.01, round(current_price - max_slippage, 2))
            
            print(f"   ⚡ Market validated: ${current_price:.2f} | Limit: ${limit_price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price,
                size=size,
                side=side,
                token_id=token_id,
            ))
            
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=order,
                    orderType=OrderType.FOK,
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                if order_result.get('success') or order_result.get('orderID'):
                    order_id = order_result.get('orderID', 'success')
                    print(f"   ✅ Order placed: {order_id}")
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    print(f"   ⚠️ Order failed: {error_msg}")
                    return None
            
            return None
            
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            return None

    # ==========================================
    # OLD FUNCTION (KEPT FOR DUMP HEDGE FALLBACK)
    # ==========================================
    
    def place_market_order(self, token_id, current_price, size, side, slippage=0.03):
        """
        OLD FUNCTION: Place a Fill-or-Kill (FOK) order with slippage tolerance.
        Now only used as fallback for dump hedge if precise orders fail.
        """
        try:
            size = round(size, 1)
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Order size {size} below minimum {MIN_ORDER_SIZE}")
                return None

            if side == BUY:
                limit_price = round(current_price + slippage, 2)
                if limit_price > 0.99: limit_price = 0.99
            else:
                limit_price = round(current_price - slippage, 2)
                if limit_price < 0.01: limit_price = 0.01
            
            print(f"   🔧 Placing FOK {side} | Size: {size} | Mkt Price: {current_price:.2f} | Limit Price: {limit_price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price, 
                size=size,
                side=side,
                token_id=token_id,
            ))
            
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=order,
                    orderType=OrderType.FOK,
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                if order_result.get('success') or order_result.get('orderID'):
                    order_id = order_result.get('orderID', 'success')
                    print(f"   ✅ FOK Order placed: {order_id}")
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    print(f"   ⚠️ FOK Order failed: {error_msg}")
                    return None
            else:
                print(f"   ⚠️ Empty response from CLOB")
                return None
                
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            return None 
 
    def check_order_status(self, order_id):
        """Check if order has been filled and return the fill price"""
        try:
            order_details = self.client.get_order(order_id)
            
            if isinstance(order_details, dict):
                status = order_details.get('status', '')
                
                if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                    actual_price = None
                    
                    if 'price' in order_details:
                        actual_price = float(order_details['price'])
                    elif 'avgFillPrice' in order_details:
                        actual_price = float(order_details['avgFillPrice'])
                    
                    if actual_price and actual_price > 0:
                        return True, actual_price
                
                return False, None
            
            return False, None
        except Exception as e:
            return False, None

    def get_actual_position_size(self, token_id):
        """Get the actual number of shares we own for a token"""
        try:
            balance = self.client.get_balance(token_id)
            if balance:
                return float(balance)
            return 0.0
        except Exception as e:
            print(f"   ⚠️ Error getting position size: {e}")
            return 0.0

    # ==========================================
    # DUMP HEDGE STRATEGY (UPDATED WITH PRECISE ORDERS)
    # ==========================================
    
    def detect_dump(self, current_yes, current_no, time_since_start):
        """Detect if either side has dumped significantly"""
        if time_since_start > (DH_WATCH_WINDOW_MINUTES * 60):
            return None, None
        
        self.yes_price_history.append((time.time(), current_yes))
        self.no_price_history.append((time.time(), current_no))
        
        if len(self.yes_price_history) < 2 or len(self.no_price_history) < 2:
            return None, None
        
        # Calculate YES dump
        yes_old_time, yes_old_price = self.yes_price_history[0]
        yes_new_time, yes_new_price = self.yes_price_history[-1]
        yes_time_diff = yes_new_time - yes_old_time
        
        if yes_time_diff >= DH_DUMP_TIMEFRAME and yes_old_price > 0:
            yes_drop_pct = (yes_old_price - yes_new_price) / yes_old_price
            if yes_drop_pct >= DH_DUMP_THRESHOLD:
                return "YES", yes_drop_pct
        
        # Calculate NO dump
        no_old_time, no_old_price = self.no_price_history[0]
        no_new_time, no_new_price = self.no_price_history[-1]
        no_time_diff = no_new_time - no_old_time
        
        if no_time_diff >= DH_DUMP_TIMEFRAME and no_old_price > 0:
            no_drop_pct = (no_old_price - no_new_price) / no_old_price
            if no_drop_pct >= DH_DUMP_THRESHOLD:
                return "NO", no_drop_pct
        
        return None, None

    def execute_dump_hedge_strategy(self, market, market_start_time):
        """Execute dump hedge strategy with PRECISE orders"""
        slug = market['slug']
        
        # Reset for new market
        if self.dh_current_market != slug:
            self.dh_current_market = slug
            self.dh_leg1_active = False
            self.dh_leg1_side = None
            self.dh_leg1_price = None
            self.dh_leg1_shares = 0
            self.yes_price_history.clear()
            self.no_price_history.clear()
        
        if slug in self.traded_markets_dumphedge:
            return "already_traded"
        
        current_time = time.time()
        time_since_start = current_time - market_start_time
        market_end_time = market_start_time + 900
        time_remaining = market_end_time - current_time
        
        yes_price = self.get_best_ask(market['yes_token'])
        no_price = self.get_best_ask(market['no_token'])
        
        if not yes_price or not no_price:
            return "no_prices"
        
        minutes_elapsed = int(time_since_start // 60)
        seconds_elapsed = int(time_since_start % 60)
        
        # LEG 1: Watch for dump
        if not self.dh_leg1_active:
            if time_since_start > (DH_WATCH_WINDOW_MINUTES * 60):
                return "outside_watch_window"
            
            print(f"💥 DH [{minutes_elapsed}m {seconds_elapsed}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Watching for dump...", end="\r")
            
            dump_side, dump_pct = self.detect_dump(yes_price, no_price, time_since_start)
            
            if dump_side:
                print(f"\n\n{'='*60}")
                print(f"💥 DUMP DETECTED - {dump_side} dropped {dump_pct*100:.1f}% in {DH_DUMP_TIMEFRAME}s!")
                print(f"{'='*60}")
                print(f"Market: {market['title']}")
                print(f"Time Since Start: {minutes_elapsed}m {seconds_elapsed}s")
                print(f"YES: ${yes_price:.2f} | NO: ${no_price:.2f}")
                
                # ⭐ UPDATED: Use precise limit order for LEG 1
                entry_token = market['yes_token'] if dump_side == "YES" else market['no_token']
                entry_price = yes_price if dump_side == "YES" else no_price
                
                print(f"\n⚡ Executing LEG 1: BUY {dump_side}")
                
                # Try precise order first
                success, actual_entry_price, entry_id = self.place_strict_limit_order(
                    token_id=entry_token,
                    limit_price=entry_price,
                    size=DH_SHARES_PER_LEG,
                    side=BUY,
                    wait_time=DH_ENTRY_WAIT_TIME
                )
                
                # Fallback to market order if precise fails
                if not success:
                    print(f"   ⚠️ Precise order missed, trying market order with validation...")
                    entry_id = self.place_market_order_with_validation(
                        token_id=entry_token,
                        max_price=entry_price,
                        size=DH_SHARES_PER_LEG,
                        side=BUY,
                        max_slippage=DH_MAX_SLIPPAGE
                    )
                    
                    if not entry_id:
                        print("❌ LEG 1 entry failed")
                        return "leg1_failed"
                    
                    time.sleep(2)
                    filled, actual_entry_price = self.check_order_status(entry_id)
                    
                    if not filled or not actual_entry_price:
                        print("❌ Could not verify LEG 1 fill")
                        return "leg1_failed"
                
                # Get actual position
                self.dh_leg1_shares = self.get_actual_position_size(entry_token)
                if self.dh_leg1_shares <= 0:
                    self.dh_leg1_shares = DH_SHARES_PER_LEG
                
                self.dh_leg1_active = True
                self.dh_leg1_side = dump_side
                self.dh_leg1_price = actual_entry_price
                
                print(f"✅ LEG 1 FILLED @ ${actual_entry_price:.2f}")
                print(f"📦 Shares: {self.dh_leg1_shares:.2f}")
                print(f"\n🔍 Now watching for LEG 2 hedge opportunity...")
                print(f"   Target: leg1_price + opposite_ask < ${DH_SUM_TARGET:.2f}")
                print(f"   Need opposite side < ${DH_SUM_TARGET - actual_entry_price:.2f}")
        
        # LEG 2: Watch for hedge opportunity
        else:
            opposite_side = "NO" if self.dh_leg1_side == "YES" else "YES"
            opposite_price = no_price if opposite_side == "NO" else yes_price
            combined_cost = self.dh_leg1_price + opposite_price
            
            print(f"🔍 DH LEG2 Watch | {opposite_side}: ${opposite_price:.2f} | Combined: ${combined_cost:.2f} | Target: <${DH_SUM_TARGET:.2f}", end="\r")
            
            if combined_cost < DH_SUM_TARGET:
                profit_pct = ((1.0 - combined_cost) / combined_cost) * 100
                
                print(f"\n\n{'='*60}")
                print(f"🎯 HEDGE OPPORTUNITY FOUND!")
                print(f"{'='*60}")
                print(f"LEG 1: {self.dh_leg1_side} @ ${self.dh_leg1_price:.2f}")
                print(f"LEG 2: {opposite_side} @ ${opposite_price:.2f}")
                print(f"Combined Cost: ${combined_cost:.2f}")
                print(f"Guaranteed Profit: ~{profit_pct:.1f}%")
                
                # ⭐ UPDATED: Use precise limit order for LEG 2
                opposite_token = market['no_token'] if opposite_side == "NO" else market['yes_token']
                
                print(f"\n⚡ Executing LEG 2: BUY {opposite_side}")
                
                # Try precise order first
                success, actual_leg2_price, leg2_id = self.place_strict_limit_order(
                    token_id=opposite_token,
                    limit_price=opposite_price,
                    size=DH_SHARES_PER_LEG,
                    side=BUY,
                    wait_time=DH_ENTRY_WAIT_TIME
                )
                
                # Fallback to market order if precise fails
                if not success:
                    print(f"   ⚠️ Precise order missed, trying market order with validation...")
                    leg2_id = self.place_market_order_with_validation(
                        token_id=opposite_token,
                        max_price=opposite_price,
                        size=DH_SHARES_PER_LEG,
                        side=BUY,
                        max_slippage=DH_MAX_SLIPPAGE
                    )
                    
                    if not leg2_id:
                        print("❌ LEG 2 entry failed")
                        return "leg2_failed"
                    
                    time.sleep(2)
                    filled, actual_leg2_price = self.check_order_status(leg2_id)
                    
                    if not filled or not actual_leg2_price:
                        print("❌ Could not verify LEG 2 fill")
                        return "leg2_failed"
                
                # Get actual position
                leg2_shares = self.get_actual_position_size(opposite_token)
                if leg2_shares <= 0:
                    leg2_shares = DH_SHARES_PER_LEG
                
                actual_combined = self.dh_leg1_price + actual_leg2_price
                actual_profit = (1.0 - actual_combined) * min(self.dh_leg1_shares, leg2_shares)
                actual_profit_pct = ((1.0 - actual_combined) / actual_combined) * 100
                
                print(f"✅ LEG 2 FILLED @ ${actual_leg2_price:.2f}")
                print(f"📦 Shares: {leg2_shares:.2f}")
                print(f"\n💰 HEDGE COMPLETE!")
                print(f"   Actual Combined: ${actual_combined:.2f}")
                print(f"   Locked Profit: ${actual_profit:.2f} ({actual_profit_pct:.1f}%)")
                
                # Log trade
                trade_data = {
                    'timestamp': datetime.now().isoformat(),
                    'strategy': 'DUMP_HEDGE',
                    'market_slug': slug,
                    'market_title': market['title'],
                    'entry_side': self.dh_leg1_side,
                    'entry_time': datetime.now().isoformat(),
                    'intended_entry_price': self.dh_leg1_price,
                    'actual_entry_price': self.dh_leg1_price,
                    'entry_size': DH_SHARES_PER_LEG,
                    'actual_shares_purchased': self.dh_leg1_shares,
                    'yes_price_at_entry': yes_price,
                    'no_price_at_entry': no_price,
                    'time_remaining_at_entry': int(time_remaining),
                    'bid_size_at_entry': 0,
                    'exit_reason': 'HEDGE_COMPLETE',
                    'exit_time': datetime.now().isoformat(),
                    'exit_price': actual_leg2_price,
                    'time_in_trade_seconds': int(time.time() - market_start_time),
                    'gross_pnl': actual_profit,
                    'pnl_percent': actual_profit_pct,
                    'win_loss': 'WIN',
                    'leg2_side': opposite_side,
                    'leg2_price': actual_leg2_price,
                    'leg2_shares': leg2_shares,
                    'combined_cost': actual_combined,
                    'session_trade_number': self.session_trades + 1,
                    'balance_before': self.get_balance(),
                    'balance_after': self.get_balance(),
                    'session_pnl_running': 0
                }
                
                self.log_trade(trade_data)
                self.session_wins += 1
                self.session_trades += 1
                self.traded_markets_dumphedge.add(slug)
                
                self.dh_leg1_active = False
                
                return "hedge_complete"
        
        return "watching"

    # ==========================================
    # ⭐ UPDATED MID-GAME STRATEGY WITH PRECISE ORDERS
    # ==========================================
    
    def execute_midgame_strategy(self, market, market_start_time):
        """Execute mid-game strategy with PRECISE order execution - NO (DOWN) ONLY"""
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets_midgame:
            return "already_traded"
        
        current_time = time.time()
        time_remaining = market_end_time - current_time
        
        # Check if we're in the entry window (5-10 minutes remaining)
        if time_remaining < MG_LOCK_WINDOW_START or time_remaining > MG_LOCK_WINDOW_END:
            return "outside_window"
        
        # Get current prices and order book depth
        yes_book = self.get_order_book_depth(market['yes_token'])
        no_book = self.get_order_book_depth(market['no_token'])
        
        if not yes_book or not no_book:
            return "no_orderbook"
        
        yes_price = yes_book['best_ask']
        no_price = no_book['best_ask']
        
        if not yes_price or not no_price:
            return "no_prices"
        
        minutes_remaining = int(time_remaining // 60)
        seconds_remaining = int(time_remaining % 60)
        print(f"📊 MG [{minutes_remaining}m {seconds_remaining}s] YES: ${yes_price:.2f} (Bids: {yes_book['bid_size']:.0f}) | NO: ${no_price:.2f} (Bids: {no_book['bid_size']:.0f})", end="\r")
        
        # ONLY CHECK NO (DOWN) SIDE
        entry_token = market['no_token']
        entry_side = "NO"
        entry_price = no_price
        bid_size = no_book['bid_size']
        
        # Check if NO qualifies based on entry criteria
        if not (MG_MIN_ENTRY_PRICE <= no_price <= MG_MAX_ENTRY_PRICE and 
                no_book['bid_size'] >= MG_MIN_BID_SIZE):
            return "no_opportunity"
        
        # Calculate position size based on wallet balance
        current_balance = self.get_balance()
        available_to_trade = current_balance * MG_WALLET_PERCENTAGE
        order_size = available_to_trade / entry_price
        order_size = round(order_size, 1)
        
        if order_size < MIN_ORDER_SIZE:
            print(f"\n⚠️ Calculated order size {order_size:.2f} is below minimum {MIN_ORDER_SIZE}")
            return "insufficient_balance"
        
        # Ensure we don't exceed available balance
        max_cost = order_size * entry_price
        if max_cost > current_balance:
            print(f"⚠️ Order cost ${max_cost:.2f} exceeds balance ${current_balance:.2f}")
            order_size = (current_balance * 0.99) / entry_price
            order_size = round(order_size, 1)
            print(f"   Adjusted to {order_size} shares (${order_size * entry_price:.2f})")
        
        # Entry criteria met - execute trade
        print(f"\n\n{'='*60}")
        print(f"🎯 MID-GAME ENTRY SIGNAL - {entry_side} (DOWN)")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"Time Remaining: {minutes_remaining}m {seconds_remaining}s")
        print(f"📊 YES: ${yes_price:.2f} | NO: ${no_price:.2f}")
        print(f"📈 Entry Side: {entry_side} @ ${entry_price:.2f}")
        print(f"💰 Available Liquidity (Bid Size): {bid_size:.0f} shares")
        print(f"💵 Wallet Balance: ${current_balance:.2f}")
        print(f"💵 Using {MG_WALLET_PERCENTAGE*100:.0f}% = ${available_to_trade:.2f}")
        print(f"📦 Order Size: {order_size:.2f} shares")
        
        # Initialize trade data
        trade_data = {
            'timestamp': datetime.now().isoformat(),
            'strategy': 'MID_GAME',
            'market_slug': slug,
            'market_title': market['title'],
            'entry_side': entry_side,
            'intended_entry_price': entry_price,
            'entry_size': order_size,
            'yes_price_at_entry': yes_price,
            'no_price_at_entry': no_price,
            'time_remaining_at_entry': int(time_remaining),
            'bid_size_at_entry': bid_size,
            'balance_before': current_balance,
            'session_trade_number': self.session_trades + 1,
            'leg2_side': None,
            'leg2_price': None,
            'leg2_shares': None,
            'combined_cost': None,
        }
        
        # ⭐ UPDATED: Execute entry with PRECISE limit order
        entry_start_time = time.time()
        trade_data['entry_time'] = datetime.fromtimestamp(entry_start_time).isoformat()
        
        print(f"\n⚡ Executing PRECISE ENTRY order...")
        print(f"   Will ONLY fill at ${entry_price:.2f} or better")
        
        success, actual_entry_price, entry_id = self.place_strict_limit_order(
            token_id=entry_token,
            limit_price=entry_price,
            size=order_size,
            side=BUY,
            wait_time=MG_ENTRY_WAIT_TIME
        )
        
        if not success:
            print(f"❌ Entry at ${entry_price:.2f} not available within {MG_ENTRY_WAIT_TIME}s")
            print(f"   Price may have moved above ${MG_MAX_ENTRY_PRICE:.2f} threshold")
            
            # Mark as attempted to prevent retry
            self.traded_markets_midgame.add(slug)
            return "entry_failed"
        
        # Get the ACTUAL number of shares we purchased
        time.sleep(1)
        actual_shares_purchased = self.get_actual_position_size(entry_token)
        
        if actual_shares_purchased <= 0:
            print(f"⚠️ Could not determine actual position size, using order size as fallback")
            actual_shares_purchased = order_size
        
        trade_data['actual_entry_price'] = actual_entry_price
        trade_data['actual_shares_purchased'] = actual_shares_purchased
        
        print(f"✅ ENTRY FILLED @ ${actual_entry_price:.2f}")
        print(f"📦 Actual shares purchased: {actual_shares_purchased:.2f}")
        print(f"\n🎯 Targets:")
        print(f"   Take Profit: ${MG_TAKE_PROFIT:.2f}")
        print(f"   Stop Loss: ${MG_STOP_LOSS:.2f}")
        
        # Monitor position until take profit or stop loss
        print(f"\n💎 Monitoring position...")
        
        while True:
            time.sleep(CHECK_INTERVAL)
            
            current_bid = self.get_best_bid(entry_token)
            
            if not current_bid:
                continue
            
            current_pnl = (current_bid - actual_entry_price) * actual_shares_purchased
            
            print(f"   💹 Current Bid: ${current_bid:.2f} | Est P&L: ${current_pnl:+.2f}", end="\r")
            
            # ⭐ UPDATED: Check Take Profit with PRECISE order
            if current_bid >= MG_TAKE_PROFIT:
                print(f"\n\n🚀 TAKE PROFIT TRIGGERED @ ${current_bid:.2f}!")
                print(f"   Placing sell order at ${MG_TAKE_PROFIT:.2f}")
                
                success, exit_price, exit_id = self.place_strict_limit_order(
                    token_id=entry_token,
                    limit_price=MG_TAKE_PROFIT,
                    size=actual_shares_purchased,
                    side=SELL,
                    wait_time=MG_EXIT_WAIT_TIME
                )
                
                if success:
                    trade_data['exit_reason'] = 'TAKE_PROFIT'
                    trade_data['exit_time'] = datetime.now().isoformat()
                    trade_data['exit_price'] = exit_price
                    trade_data['time_in_trade_seconds'] = time.time() - entry_start_time
                    trade_data['gross_pnl'] = (exit_price - actual_entry_price) * actual_shares_purchased
                    trade_data['pnl_percent'] = ((exit_price - actual_entry_price) / actual_entry_price) * 100
                    trade_data['win_loss'] = 'WIN'
                    trade_data['balance_after'] = self.get_balance()
                    trade_data['session_pnl_running'] = trade_data['balance_after'] - trade_data['balance_before']
                    
                    self.log_trade(trade_data)
                    self.session_wins += 1
                    self.session_trades += 1
                    self.traded_markets_midgame.add(slug)
                    
                    print(f"✅ EXIT FILLED @ ${exit_price:.2f}")
                    print(f"📦 Shares sold: {actual_shares_purchased:.2f}")
                    print(f"💰 P&L: ${trade_data['gross_pnl']:+.2f} ({trade_data['pnl_percent']:+.2f}%)")
                    return "take_profit"
                else:
                    print(f"⚠️ Take profit order not filled, continuing to monitor...")
            
            # ⭐ UPDATED: Check Stop Loss with PRECISE order
            elif current_bid <= MG_STOP_LOSS:
                print(f"\n\n🛑 STOP LOSS TRIGGERED @ ${current_bid:.2f}!")
                print(f"   Placing sell order at ${MG_STOP_LOSS:.2f}")
                
                success, exit_price, exit_id = self.place_strict_limit_order(
                    token_id=entry_token,
                    limit_price=MG_STOP_LOSS,
                    size=actual_shares_purchased,
                    side=SELL,
                    wait_time=MG_EXIT_WAIT_TIME
                )
                
                if success:
                    trade_data['exit_reason'] = 'STOP_LOSS'
                    trade_data['exit_time'] = datetime.now().isoformat()
                    trade_data['exit_price'] = exit_price
                    trade_data['time_in_trade_seconds'] = time.time() - entry_start_time
                    trade_data['gross_pnl'] = (exit_price - actual_entry_price) * actual_shares_purchased
                    trade_data['pnl_percent'] = ((exit_price - actual_entry_price) / actual_entry_price) * 100
                    trade_data['win_loss'] = 'LOSS'
                    trade_data['balance_after'] = self.get_balance()
                    trade_data['session_pnl_running'] = trade_data['balance_after'] - trade_data['balance_before']
                    
                    self.log_trade(trade_data)
                    self.session_losses += 1
                    self.session_trades += 1
                    self.traded_markets_midgame.add(slug)
                    
                    print(f"✅ EXIT FILLED @ ${exit_price:.2f}")
                    print(f"📦 Shares sold: {actual_shares_purchased:.2f}")
                    print(f"💰 P&L: ${trade_data['gross_pnl']:+.2f} ({trade_data['pnl_percent']:+.2f}%)")
                    return "stop_loss"
                else:
                    print(f"⚠️ Stop loss order not filled, continuing to monitor...")

    # ==========================================
    # MAIN BOT LOOP
    # ==========================================

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"\n📋 ACTIVE STRATEGY: {ACTIVE_STRATEGY}")
        
        if ACTIVE_STRATEGY in ["MID_GAME", "BOTH"]:
            print(f"\n📊 MID-GAME STRATEGY (NO ONLY) - ⭐ PRECISE ORDERS ENABLED:")
            print(f"   Entry Window: {MG_LOCK_WINDOW_START}s to {MG_LOCK_WINDOW_END}s remaining")
            print(f"   Entry Price Range: ${MG_MIN_ENTRY_PRICE:.2f} - ${MG_MAX_ENTRY_PRICE:.2f}")
            print(f"   ⭐ Will ONLY fill at exact price or better!")
            print(f"   Entry Wait Time: {MG_ENTRY_WAIT_TIME}s")
            print(f"   Minimum Bid Size: {MG_MIN_BID_SIZE} shares")
            print(f"   Position Size: {MG_WALLET_PERCENTAGE*100:.0f}% of wallet balance")
            print(f"   Take Profit: ${MG_TAKE_PROFIT:.2f}")
            print(f"   Stop Loss: ${MG_STOP_LOSS:.2f}")
            print(f"   Exit Wait Time: {MG_EXIT_WAIT_TIME}s")
        
        if ACTIVE_STRATEGY in ["DUMP_HEDGE", "BOTH"]:
            print(f"\n💥 DUMP HEDGE STRATEGY - ⭐ PRECISE ORDERS ENABLED:")
            print(f"   Watch Window: First {DH_WATCH_WINDOW_MINUTES} minutes of round")
            print(f"   Dump Threshold: {DH_DUMP_THRESHOLD*100:.0f}% drop in {DH_DUMP_TIMEFRAME}s")
            print(f"   Sum Target: <${DH_SUM_TARGET:.2f}")
            print(f"   Shares Per Leg: {DH_SHARES_PER_LEG}")
            print(f"   ⭐ Will attempt precise orders with {DH_MAX_SLIPPAGE:.2f} fallback")
            print(f"   Entry Wait Time: {DH_ENTRY_WAIT_TIME}s")
        
        print(f"\n📊 Trade Logging: {TRADE_LOG_FILE}")
        if ENABLE_EXCEL:
            print(f"   Excel export: ENABLED")
        print(f"\n")
        
        current_market = None
        
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                current_timestamp = int(now_utc.timestamp())
                
                market_timestamp = (current_timestamp // 900) * 900
                expected_slug = f"btc-updown-15m-{market_timestamp}"
                
                if not current_market or current_market['slug'] != expected_slug:
                    print(f"\n🔍 Looking for market: {expected_slug}")
                    
                    if MANUAL_SLUG:
                        current_market = self.get_market_from_slug(MANUAL_SLUG)
                        market_timestamp = int(MANUAL_SLUG.split('-')[-1])
                    else:
                        current_market = self.get_market_from_slug(expected_slug)
                    
                    if current_market:
                        market_end = market_timestamp + 900
                        time_left = market_end - current_timestamp
                        print(f"✅ Active Market Found!")
                        print(f"   {current_market['title']}")
                        print(f"   Time Left: {time_left//60}m {time_left%60}s\n")
                    else:
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        wait_time = next_market_time - current_timestamp
                        print(f"⏳ No active market. Next check in {wait_time}s")
                        time.sleep(min(wait_time, 60))
                        continue
                
                # Execute strategies
                dh_status = None
                mg_status = None
                
                if ACTIVE_STRATEGY in ["DUMP_HEDGE", "BOTH"]:
                    dh_status = self.execute_dump_hedge_strategy(current_market, market_timestamp)
                
                if ACTIVE_STRATEGY in ["MID_GAME", "BOTH"]:
                    mg_status = self.execute_midgame_strategy(current_market, market_timestamp)
                
                # Handle results
                if dh_status == "hedge_complete" or mg_status in ["take_profit", "stop_loss"]:
                    print("\n✅ Trade cycle complete!")
                    
                    # Print session stats
                    current_balance = self.get_balance()
                    session_pnl = current_balance - self.starting_balance
                    win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                    
                    print(f"\n📊 SESSION STATS:")
                    print(f"   Starting Balance: ${self.starting_balance:.2f}")
                    print(f"   Current Balance: ${current_balance:.2f}")
                    print(f"   Session P&L: ${session_pnl:+.2f}")
                    print(f"   Trades: {self.session_trades} | Wins: {self.session_wins} | Losses: {self.session_losses}")
                    print(f"   Win Rate: {win_rate:.1f}%\n")
                    
                    time.sleep(5)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped by user")
                print(f"\n📊 FINAL SESSION STATS:")
                current_balance = self.get_balance()
                session_pnl = current_balance - self.starting_balance
                win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                print(f"   Starting Balance: ${self.starting_balance:.2f}")
                print(f"   Final Balance: ${current_balance:.2f}")
                print(f"   Total P&L: ${session_pnl:+.2f}")
                print(f"   Total Trades: {self.session_trades} | Wins: {self.session_wins} | Losses: {self.session_losses}")
                print(f"   Win Rate: {win_rate:.1f}%")
                print(f"\n📊 Trade log saved: {TRADE_LOG_FILE}")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = DualStrategyBot()
    bot.run()


