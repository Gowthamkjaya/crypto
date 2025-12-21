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
print(f"🦊 Polymarket shows: {POLYMARKET_ADDRESS}")

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
# 💥 DUMP HEDGE STRATEGY SETTINGS
# ==========================================
DH_WATCH_WINDOW_MINUTES = 2    # Watch first 2 minutes of round
DH_DUMP_THRESHOLD = 0.15       # 15% price drop triggers entry
DH_DUMP_TIMEFRAME = 3          # Check drop over 3 seconds
DH_SUM_TARGET = 0.95           # leg1_price + leg2_price must be < this
DH_WALLET_PERCENTAGE = 0.50    # Use 50% of wallet balance per leg

# Exit settings
DH_MAJORITY_EXIT = 0.99        # Sell majority side at $0.99
DH_MINORITY_EXIT = 0.03        # Sell minority side at $0.03

# Order execution settings
DH_ENTRY_WAIT_TIME = 3         # Wait 3 seconds for leg fills
DH_EXIT_WAIT_TIME = 3          # Wait 3 seconds for exit fills
DH_MAX_SLIPPAGE = 0.02         # Allow 2 cent slippage for dump hedge

# System settings
CHECK_INTERVAL = 1          # Check every 1 second
MIN_ORDER_SIZE = 0.1        # Minimum order size

# Trade Logging
TRADE_LOG_FILE = "hedge_trades.csv"
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

class HedgeStrategyBot:
    def __init__(self):
        print("🤖 Dump Hedge Strategy Bot Starting...")
        
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
        self.traded_markets = set()
        
        # Session tracking
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        
        # Hedge tracking
        self.leg1_active = False
        self.leg1_side = None
        self.leg1_price = None
        self.leg1_shares = 0
        self.leg1_token = None
        
        self.leg2_side = None
        self.leg2_price = None
        self.leg2_shares = 0
        self.leg2_token = None
        
        self.hedge_complete = False
        self.current_market = None
        
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
                'timestamp', 'market_slug', 'market_title',
                'leg1_side', 'leg1_entry_price', 'leg1_shares',
                'leg2_side', 'leg2_entry_price', 'leg2_shares',
                'combined_entry_cost', 'hedge_profit_locked',
                'majority_side', 'majority_exit_price', 'majority_shares',
                'minority_side', 'minority_exit_price', 'minority_shares',
                'final_pnl', 'pnl_percent',
                'session_trade_number', 'balance_before', 'balance_after'
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

    def place_strict_limit_order(self, token_id, limit_price, size, side, wait_time=5):
        """
        Place a GTC limit order at EXACT price and wait for fill.
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
                    orderType=OrderType.GTC,
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

    def place_market_order_with_validation(self, token_id, max_price, size, side, max_slippage=0.02):
        """
        Place market order but ONLY if current price is within max_slippage of max_price.
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

    def execute_hedge_strategy(self, market, market_start_time):
        """Execute dump hedge strategy with exit logic"""
        slug = market['slug']
        
        # Reset for new market
        if self.current_market != slug:
            self.current_market = slug
            self.leg1_active = False
            self.leg1_side = None
            self.leg1_price = None
            self.leg1_shares = 0
            self.leg1_token = None
            self.leg2_side = None
            self.leg2_price = None
            self.leg2_shares = 0
            self.leg2_token = None
            self.hedge_complete = False
            self.yes_price_history.clear()
            self.no_price_history.clear()
        
        if slug in self.traded_markets:
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
        
        # PHASE 1: Watch for dump
        if not self.leg1_active:
            if time_since_start > (DH_WATCH_WINDOW_MINUTES * 60):
                return "outside_watch_window"
            
            print(f"💥 [{minutes_elapsed}m {seconds_elapsed}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Watching...", end="\r")
            
            dump_side, dump_pct = self.detect_dump(yes_price, no_price, time_since_start)
            
            if dump_side:
                print(f"\n\n{'='*60}")
                print(f"💥 DUMP DETECTED - {dump_side} dropped {dump_pct*100:.1f}% in {DH_DUMP_TIMEFRAME}s!")
                print(f"{'='*60}")
                
                # Calculate position size based on 50% of wallet
                current_balance = self.get_balance()
                available_per_leg = current_balance * DH_WALLET_PERCENTAGE
                
                entry_token = market['yes_token'] if dump_side == "YES" else market['no_token']
                entry_price = yes_price if dump_side == "YES" else no_price
                
                # Calculate shares (round to nearest whole number)
                leg1_size = round(available_per_leg / entry_price)
                
                if leg1_size < MIN_ORDER_SIZE:
                    print(f"❌ Insufficient balance for trade")
                    return "insufficient_balance"
                
                print(f"\n⚡ LEG 1: BUY {dump_side}")
                print(f"   Wallet: ${current_balance:.2f}")
                print(f"   Using 50%: ${available_per_leg:.2f}")
                print(f"   Shares: {leg1_size}")
                
                success, actual_entry_price, entry_id = self.place_strict_limit_order(
                    token_id=entry_token,
                    limit_price=entry_price,
                    size=leg1_size,
                    side=BUY,
                    wait_time=DH_ENTRY_WAIT_TIME
                )
                
                if not success:
                    print(f"   ⚠️ Precise order missed, trying market order...")
                    entry_id = self.place_market_order_with_validation(
                        token_id=entry_token,
                        max_price=entry_price,
                        size=leg1_size,
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
                
                self.leg1_shares = self.get_actual_position_size(entry_token)
                if self.leg1_shares <= 0:
                    self.leg1_shares = leg1_size
                
                self.leg1_active = True
                self.leg1_side = dump_side
                self.leg1_price = actual_entry_price
                self.leg1_token = entry_token
                
                print(f"✅ LEG 1 FILLED @ ${actual_entry_price:.2f}")
                print(f"📦 Shares: {self.leg1_shares:.2f}")
                print(f"\n👀 Watching for LEG 2 opportunity...")
        
        # PHASE 2: Complete the hedge
        elif not self.hedge_complete:
            opposite_side = "NO" if self.leg1_side == "YES" else "YES"
            opposite_price = no_price if opposite_side == "NO" else yes_price
            combined_cost = self.leg1_price + opposite_price
            
            print(f"👀 LEG2 Watch | {opposite_side}: ${opposite_price:.2f} | Combined: ${combined_cost:.2f} | Target: <${DH_SUM_TARGET:.2f}", end="\r")
            
            if combined_cost < DH_SUM_TARGET:
                profit_pct = ((1.0 - combined_cost) / combined_cost) * 100
                
                print(f"\n\n{'='*60}")
                print(f"🎯 HEDGE OPPORTUNITY!")
                print(f"{'='*60}")
                print(f"Combined Cost: ${combined_cost:.2f}")
                print(f"Locked Profit: ~{profit_pct:.1f}%")
                
                opposite_token = market['no_token'] if opposite_side == "NO" else market['yes_token']
                
                # Use same number of shares as leg 1
                leg2_size = self.leg1_shares
                
                print(f"\n⚡ LEG 2: BUY {opposite_side}")
                print(f"   Shares: {leg2_size}")
                
                success, actual_leg2_price, leg2_id = self.place_strict_limit_order(
                    token_id=opposite_token,
                    limit_price=opposite_price,
                    size=leg2_size,
                    side=BUY,
                    wait_time=DH_ENTRY_WAIT_TIME
                )
                
                if not success:
                    print(f"   ⚠️ Precise order missed, trying market order...")
                    leg2_id = self.place_market_order_with_validation(
                        token_id=opposite_token,
                        max_price=opposite_price,
                        size=leg2_size,
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
                
                self.leg2_shares = self.get_actual_position_size(opposite_token)
                if self.leg2_shares <= 0:
                    self.leg2_shares = leg2_size
                
                self.leg2_side = opposite_side
                self.leg2_price = actual_leg2_price
                self.leg2_token = opposite_token
                self.hedge_complete = True
                
                actual_combined = self.leg1_price + actual_leg2_price
                locked_profit = (1.0 - actual_combined) * min(self.leg1_shares, self.leg2_shares)
                
                print(f"✅ LEG 2 FILLED @ ${actual_leg2_price:.2f}")
                print(f"📦 Shares: {self.leg2_shares:.2f}")
                print(f"\n💰 HEDGE COMPLETE!")
                print(f"   Combined: ${actual_combined:.2f}")
                print(f"   Locked Profit: ${locked_profit:.2f}")
                print(f"\n👉 Now monitoring for exit opportunities...")
        
        # PHASE 3: Exit the hedge
        else:
            # Determine which side is majority based on current prices
            yes_bid = self.get_best_bid(market['yes_token'])
            no_bid = self.get_best_bid(market['no_token'])
            
            if not yes_bid or not no_bid:
                return "watching"
            
            # Majority side is the one currently worth more
            if yes_bid > no_bid:
                majority_side = "YES"
                majority_token = market['yes_token']
                majority_shares = self.leg1_shares if self.leg1_side == "YES" else self.leg2_shares
                majority_target = DH_MAJORITY_EXIT
                
                minority_side = "NO"
                minority_token = market['no_token']
                minority_shares = self.leg1_shares if self.leg1_side == "NO" else self.leg2_shares
                minority_target = DH_MINORITY_EXIT
            else:
                majority_side = "NO"
                majority_token = market['no_token']
                majority_shares = self.leg1_shares if self.leg1_side == "NO" else self.leg2_shares
                majority_target = DH_MAJORITY_EXIT
                
                minority_side = "YES"
                minority_token = market['yes_token']
                minority_shares = self.leg1_shares if self.leg1_side == "YES" else self.leg2_shares
                minority_target = DH_MINORITY_EXIT
            
            majority_bid = yes_bid if majority_side == "YES" else no_bid
            minority_bid = yes_bid if minority_side == "YES" else no_bid
            
            print(f"🎯 Exit Watch | Majority {majority_side}: ${majority_bid:.2f} (Target ${majority_target:.2f}) | Minority {minority_side}: ${minority_bid:.2f} (Target ${minority_target:.2f})", end="\r")
            
            # Try to sell majority side first
            if majority_bid >= majority_target:
                print(f"\n\n{'='*60}")
                print(f"💎 MAJORITY EXIT TRIGGERED - {majority_side} @ ${majority_bid:.2f}")
                print(f"{'='*60}")
                
                success, exit_price, exit_id = self.place_strict_limit_order(
                    token_id=majority_token,
                    limit_price=majority_target,
                    size=majority_shares,
                    side=SELL,
                    wait_time=DH_EXIT_WAIT_TIME
                )
                
                if success:
                    print(f"✅ MAJORITY SOLD @ ${exit_price:.2f}")
                    print(f"📦 Shares: {majority_shares:.2f}")
                    
                    # Now wait for minority exit
                    print(f"\n👉 Waiting for minority {minority_side} to reach ${minority_target:.2f}...")
                    
                    while True:
                        time.sleep(CHECK_INTERVAL)
                        
                        minority_bid = self.get_best_bid(minority_token)
                        if not minority_bid:
                            continue
                        
                        print(f"   Minority {minority_side}: ${minority_bid:.2f} (Target ${minority_target:.2f})", end="\r")
                        
                        if minority_bid <= minority_target:
                            print(f"\n\n💎 MINORITY EXIT TRIGGERED @ ${minority_bid:.2f}")
                            
                            success2, exit_price2, exit_id2 = self.place_strict_limit_order(
                                token_id=minority_token,
                                limit_price=minority_target,
                                size=minority_shares,
                                side=SELL,
                                wait_time=DH_EXIT_WAIT_TIME
                            )
                            
                            if success2:
                                print(f"✅ MINORITY SOLD @ ${exit_price2:.2f}")
                                print(f"📦 Shares: {minority_shares:.2f}")
                                
                                # Calculate final P&L
                                final_balance = self.get_balance()
                                balance_before = self.starting_balance if self.session_trades == 0 else final_balance - (final_balance - self.starting_balance) / (self.session_trades + 1)
                                
                                combined_entry = self.leg1_price + self.leg2_price
                                combined_exit = exit_price + exit_price2
                                final_pnl = (combined_exit - combined_entry) * min(self.leg1_shares, self.leg2_shares)
                                pnl_percent = ((combined_exit - combined_entry) / combined_entry) * 100
                                
                                print(f"\n💰 TRADE COMPLETE!")
                                print(f"   Entry: ${combined_entry:.2f}")
                                print(f"   Exit: ${combined_exit:.2f}")
                                print(f"   P&L: ${final_pnl:+.2f} ({pnl_percent:+.2f}%)")
                                
                                # Log trade
                                trade_data = {
                                    'timestamp': datetime.now().isoformat(),
                                    'market_slug': slug,
                                    'market_title': market['title'],
                                    'leg1_side': self.leg1_side,
                                    'leg1_entry_price': self.leg1_price,
                                    'leg1_shares': self.leg1_shares,
                                    'leg2_side': self.leg2_side,
                                    'leg2_entry_price': self.leg2_price,
                                    'leg2_shares': self.leg2_shares,
                                    'combined_entry_cost': combined_entry,
                                    'hedge_profit_locked': (1.0 - combined_entry) * min(self.leg1_shares, self.leg2_shares),
                                    'majority_side': majority_side,
                                    'majority_exit_price': exit_price,
                                    'majority_shares': majority_shares,
                                    'minority_side': minority_side,
                                    'minority_exit_price': exit_price2,
                                    'minority_shares': minority_shares,
                                    'final_pnl': final_pnl,
                                    'pnl_percent': pnl_percent,
                                    'session_trade_number': self.session_trades + 1,
                                    'balance_before': balance_before,
                                    'balance_after': final_balance
                                }
                                
                                self.log_trade(trade_data)
                                self.session_wins += 1
                                self.session_trades += 1
                                self.traded_markets.add(slug)
                                
                                return "trade_complete"
                            else:
                                print(f"⚠️ Minority exit order not filled, will retry...")
                        
                        # Check if market is about to end
                        if time.time() > (market_end_time - 30):
                            print(f"\n⚠️ Market ending soon, forcing minority exit...")
                            break
                else:
                    print(f"⚠️ Majority exit order not filled, continuing to monitor...")
        
        return "watching"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Hedge Strategy Bot Running...")
        print(f"\n💥 DUMP HEDGE STRATEGY:")
        print(f"   Watch Window: First {DH_WATCH_WINDOW_MINUTES} minutes")
        print(f"   Dump Threshold: {DH_DUMP_THRESHOLD*100:.0f}% drop in {DH_DUMP_TIMEFRAME}s")
        print(f"   Sum Target: <${DH_SUM_TARGET:.2f}")
        print(f"   Position Size: {DH_WALLET_PERCENTAGE*100:.0f}% of wallet per leg")
        print(f"   Majority Exit: ${DH_MAJORITY_EXIT:.2f}")
        print(f"   Minority Exit: ${DH_MINORITY_EXIT:.2f}")
        print(f"\n📊 Trade Log: {TRADE_LOG_FILE}\n")
        
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
                
                # Execute hedge strategy
                status = self.execute_hedge_strategy(current_market, market_timestamp)
                
                if status == "trade_complete":
                    # Print session stats
                    current_balance = self.get_balance()
                    session_pnl = current_balance - self.starting_balance
                    
                    print(f"\n📊 SESSION STATS:")
                    print(f"   Starting Balance: ${self.starting_balance:.2f}")
                    print(f"   Current Balance: ${current_balance:.2f}")
                    print(f"   Session P&L: ${session_pnl:+.2f}")
                    print(f"   Trades: {self.session_trades} | Wins: {self.session_wins}\n")
                    
                    time.sleep(5)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped by user")
                current_balance = self.get_balance()
                session_pnl = current_balance - self.starting_balance
                print(f"\n📊 FINAL SESSION STATS:")
                print(f"   Starting Balance: ${self.starting_balance:.2f}")
                print(f"   Final Balance: ${current_balance:.2f}")
                print(f"   Total P&L: ${session_pnl:+.2f}")
                print(f"   Total Trades: {self.session_trades} | Wins: {self.session_wins}")
                print(f"\n📊 Trade log: {TRADE_LOG_FILE}")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = HedgeStrategyBot()
    bot.run()
