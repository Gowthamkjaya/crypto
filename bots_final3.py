import os
import time
import requests
import json
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime, timedelta, timezone

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
print(f"🔑 Polymarket shows: {POLYMARKET_ADDRESS}")

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
    SIGNATURE_TYPE = 1  # Use 1 for Magic Link / Email wallets
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# Manual Override (optional - leave empty for auto-detection)
MANUAL_SLUG = ""  # e.g., "btc-updown-15m-1765593000"

# Slug generation for BTC 15min markets
INTERVAL = 900  # 15 minutes in seconds

# ==========================================
# 🎯 MID-GAME LOCK STRATEGY SETTINGS
# ==========================================
# Entry when 5-10 min remaining
LOCK_WINDOW_START = 300  # Start at 5 minutes remaining
LOCK_WINDOW_END = 600    # End at 10 minutes remaining
MIN_ENTRY_PRICE = 0.90    # Buy YES or NO only if price >= 0.90
ORDER_SIZE = 5            # Position size

# Exit Settings - TRAILING STOP LOSS with DYNAMIC activation
TAKE_PROFIT_SPREAD = 0.05   # Take profit at +5 cents from entry
STOP_LOSS_SPREAD = 0.05     # Initial stop loss at -5 cents from entry
MIN_STOP_LOSS_DELAY = 60    # Minimum wait time (1 minute)
STOP_LOSS_BUFFER_TIME = 180 # Must activate SL by 3 minutes before market end (13th minute)
TRAILING_PROFIT_LOCK = 0.5  # Lock in 50% of gains above entry (0.5 = 50%)
CHECK_INTERVAL = 2          # Check every 2 seconds

# Safety limits
MAX_ACCEPTABLE_SLIPPAGE = 0.05  # Max 5 cent slippage on entry

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class BTCMidGameBot:
    def __init__(self):
        print("🤖 BTC Mid-Game Lock Bot Starting (WITH AUTO-SETTLEMENT)...")
        
        # 1. Setup Web3 (For Balance)
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # 2. Setup Client (For Trading)
        try:
            print(f"🔑 Setting up Polymarket client...")
            
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
            
            # Use official method to create/derive API credentials
            print("🔑 Deriving API credentials...")
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            print(f"✅ Trading as: {self.client.get_address()}\n")
            
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            import traceback
            traceback.print_exc()
            exit()
            
        self.traded_markets = set()  # Track markets we've already traded
        
        # Session tracking
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0

    def get_best_bid(self, token_id):
        """Get best available buying price (price we can sell into)"""
        try:
            book = self.client.get_order_book(token_id)
            if book.bids:
                return max(float(o.price) for o in book.bids)
            return None
        except:
            return None

    def get_balance(self):
        """Get USDC.e balance from the trading address"""
        try:
            raw_bal = self.usdc_contract.functions.balanceOf(TRADING_ADDRESS).call()
            decimals = self.usdc_contract.functions.decimals().call()
            return raw_bal / (10 ** decimals)
        except Exception as e:
            print(f"⚠️ Balance error: {e}")
            return 0.0

    def settle_market(self, condition_id):
        """Claim winnings from a settled market"""
        try:
            print(f"\n💰 Attempting to claim winnings for market...")
            print(f"   Condition ID: {condition_id[:16]}...")
            
            # Use the client's redeem method to claim winnings
            result = self.client.redeem_winnings(condition_id)
            
            if result:
                print(f"   ✅ Successfully claimed winnings!")
                return True
            else:
                print(f"   ⚠️ No winnings to claim or already claimed")
                return False
                
        except Exception as e:
            print(f"   ⚠️ Settlement error: {e}")
            # Not a critical error - market may not be settled yet
            return False

    def get_condition_id_from_token(self, token_id):
        """Extract condition ID from token ID for settlement"""
        try:
            # Query the market details to get condition ID
            # Polymarket API endpoint for token details
            url = f"{HOST}/token/{token_id}"
            resp = requests.get(url, timeout=10)
            
            if resp.status_code == 200:
                data = resp.json()
                return data.get('condition_id')
            
            return None
        except Exception as e:
            print(f"   ⚠️ Could not get condition ID: {e}")
            return None

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
            
            # Get condition ID for settlement
            condition_id = event['markets'][0].get('conditionId')
            
            return {
                'slug': slug,
                'yes_token': clob_ids[0],
                'no_token': clob_ids[1],
                'title': event.get('title', slug),
                'condition_id': condition_id
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

    def get_actual_fill_price(self, order_id, max_retries=10):
        """Get the ACTUAL price the order was filled at"""
        print(f"   🔍 Fetching actual fill price for order {order_id}...")
        
        for attempt in range(max_retries):
            try:
                time.sleep(1)
                order_details = self.client.get_order(order_id)
                
                if isinstance(order_details, dict):
                    status = order_details.get('status', '')
                    
                    if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                        actual_price = None
                        
                        if 'price' in order_details:
                            actual_price = float(order_details['price'])
                        elif 'avgFillPrice' in order_details:
                            actual_price = float(order_details['avgFillPrice'])
                        elif 'trades' in order_details and len(order_details['trades']) > 0:
                            trades = order_details['trades']
                            total_cost = sum(float(t.get('price', 0)) * float(t.get('size', 0)) for t in trades)
                            total_size = sum(float(t.get('size', 0)) for t in trades)
                            if total_size > 0:
                                actual_price = total_cost / total_size
                        
                        if actual_price:
                            print(f"   ✅ Actual fill price: ${actual_price:.4f}")
                            return actual_price
                    
                    print(f"   ⏳ Order status: {status}, retrying... ({attempt+1}/{max_retries})")
                
            except Exception as e:
                print(f"   ⚠️ Error fetching fill price (attempt {attempt+1}): {e}")
        
        print(f"   ❌ Could not determine actual fill price after {max_retries} attempts")
        return None

    def place_order(self, token_id, price, size, side):
        """Place a market order using official Polymarket API"""
        try:
            price = round(price, 2)
            
            print(f"   🔧 Placing order: {size} shares @ ${price}")
            print(f"   🔧 Token: {token_id[:16]}...")
            print(f"   🔧 Side: {side}")
            
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=self.client.create_order(OrderArgs(
                        price=price,
                        size=size,
                        side=side,
                        token_id=token_id,
                    )),
                    orderType=OrderType.FOK,
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                
                if order_result.get('success') or order_result.get('orderID'):
                    order_id = order_result.get('orderID', 'success')
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    print(f"   ⚠️ Order failed: {error_msg}")
                    return None
            else:
                print(f"   ⚠️ Empty or invalid response")
                return None
                
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def calculate_dynamic_sl_delay(self, entry_time, market_end_time):
        """
        Calculate how long to wait before activating stop loss.
        Ensures SL is active by the 13th minute (3 min before market end).
        """
        time_until_market_end = market_end_time - entry_time
        
        # We want SL to activate by 13th minute = 180 seconds before end
        max_delay = time_until_market_end - STOP_LOSS_BUFFER_TIME
        
        # Use the smaller of: calculated delay or minimum delay
        actual_delay = max(MIN_STOP_LOSS_DELAY, min(max_delay, 300))
        
        return actual_delay

    def monitor_with_trailing_stop(self, token_id, entry_price, size, entry_time, market_end_time):
        """Monitor position with take profit and DYNAMIC TRAILING stop loss"""
        tp_price = min(entry_price + TAKE_PROFIT_SPREAD, 0.99)
        initial_sl_price = max(entry_price - STOP_LOSS_SPREAD, 0.01)
        
        # DYNAMIC stop loss activation time
        dynamic_sl_delay = self.calculate_dynamic_sl_delay(entry_time, market_end_time)
        stop_loss_active_time = entry_time + dynamic_sl_delay
        
        # Trailing stop loss starts at initial SL
        trailing_stop = initial_sl_price
        highest_bid = entry_price  # Track the highest price seen
        
        print(f"\n🎯 Exit Targets (DYNAMIC TRAILING STOP):")
        print(f"   Entry: ${entry_price:.4f}")
        print(f"   🚀 Take Profit: ${tp_price:.4f} (+${TAKE_PROFIT_SPREAD:.2f})")
        print(f"   🛡️ Initial Stop Loss: ${initial_sl_price:.4f} (-${STOP_LOSS_SPREAD:.2f})")
        print(f"   📈 Trailing Stop: Locks in {int(TRAILING_PROFIT_LOCK*100)}% of gains above entry")
        print(f"   ⏰ Dynamic SL Activation: {int(dynamic_sl_delay)}s from entry (auto-adjusted)")
        print(f"   ⏰ SL activates at: {datetime.fromtimestamp(stop_loss_active_time, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
        print(f"   ⏰ Market ends at: {datetime.fromtimestamp(market_end_time, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
        
        while True:
            time.sleep(CHECK_INTERVAL)
            current_time = time.time()
            
            # Check if we're past the "no-trade zone" (last 3 minutes)
            time_until_end = market_end_time - current_time
            if time_until_end <= STOP_LOSS_BUFFER_TIME:
                print(f"\n\n⏰ Entering final 3 minutes - forcing exit at market price")
                current_bid = self.get_best_bid(token_id)
                if current_bid:
                    print("   Executing final market exit...")
                    self.place_order(token_id, current_bid - 0.01, size, SELL)
                    
                    pnl = (current_bid - entry_price) * size
                    print(f"   📊 Position closed (time-based exit)")
                    print(f"   Entry: ${entry_price:.4f}")
                    print(f"   Exit: ${current_bid:.2f}")
                    print(f"   P&L: ${pnl:+.2f}")
                    return "time_exit"
            
            # Check current market price
            current_bid = self.get_best_bid(token_id)
            
            if current_bid:
                # Update highest bid and trailing stop
                if current_bid > highest_bid:
                    highest_bid = current_bid
                    
                    # Calculate trailing stop: lock in TRAILING_PROFIT_LOCK % of gains
                    gain_above_entry = highest_bid - entry_price
                    if gain_above_entry > 0:
                        locked_profit = entry_price + (gain_above_entry * TRAILING_PROFIT_LOCK)
                        # Trailing stop is the better of: initial SL or locked profit level
                        trailing_stop = max(initial_sl_price, locked_profit)
                
                time_until_sl_active = max(0, stop_loss_active_time - current_time)
                
                # CHECK TAKE PROFIT FIRST (always active)
                if current_bid >= tp_price:
                    print(f"\n\n💰 TAKE PROFIT HIT at ${current_bid:.2f}!")
                    
                    # Execute market sell at TP
                    print("   Executing Take Profit sell...")
                    self.place_order(token_id, current_bid, size, SELL)
                    
                    profit = (current_bid - entry_price) * size
                    print(f"   📈 Position closed at profit!")
                    print(f"   Entry: ${entry_price:.4f}")
                    print(f"   Exit: ${current_bid:.2f}")
                    print(f"   Highest: ${highest_bid:.2f}")
                    print(f"   Profit: +${profit:.2f}")
                    return "take_profit"
                
                # Check trailing stop loss (only after delay)
                if time_until_sl_active > 0:
                    # Stop loss not active yet
                    print(f"   Current: ${current_bid:.2f} | Entry: ${entry_price:.2f} | High: ${highest_bid:.2f} | TP: ${tp_price:.2f} | SL in: {int(time_until_sl_active)}s | End in: {int(time_until_end)}s", end="\r")
                else:
                    # Stop loss is now active - show trailing stop
                    print(f"   Current: ${current_bid:.2f} | Entry: ${entry_price:.2f} | High: ${highest_bid:.2f} | TP: ${tp_price:.2f} | Trail-SL: ${trailing_stop:.2f} ✓ | End in: {int(time_until_end)}s", end="\r")
                    
                    if current_bid <= trailing_stop:
                        print(f"\n\n🛑 TRAILING STOP TRIGGERED at ${current_bid:.2f}!")
                        
                        # Execute market sell
                        print("   Executing Market Sell...")
                        self.place_order(token_id, current_bid - 0.01, size, SELL)
                        
                        pnl = (current_bid - entry_price) * size
                        pnl_sign = "+" if pnl >= 0 else ""
                        print(f"   📊 Position closed via Trailing Stop")
                        print(f"   Entry: ${entry_price:.4f}")
                        print(f"   Highest: ${highest_bid:.2f} (gained ${highest_bid - entry_price:.2f})")
                        print(f"   Trailing Stop: ${trailing_stop:.2f}")
                        print(f"   Exit: ${current_bid:.2f}")
                        print(f"   P&L: {pnl_sign}${pnl:.2f}")
                        
                        if pnl >= 0:
                            locked_pct = ((current_bid - entry_price) / (highest_bid - entry_price)) * 100 if highest_bid > entry_price else 0
                            print(f"   🎯 Locked in {locked_pct:.0f}% of peak gains")
                        
                        return "trailing_stop"
            
            # Safety check - should never reach here due to time-based exit above
            if current_time > market_end_time:
                print(f"\n\n⏰ Market ended - position will auto-settle")
                return "market_ended"

    def execute_mid_game_lock(self, market, market_start_time):
        """Execute mid-game lock strategy"""
        slug = market['slug']
        market_end_time = market_start_time + 900  # 15 minutes after start
        
        # Check if already traded
        if slug in self.traded_markets:
            return "already_traded"
        
        current_time = time.time()
        time_remaining = market_end_time - current_time
        
        # Check if we're in the mid-game window (5-10 min remaining)
        if time_remaining < LOCK_WINDOW_START or time_remaining > LOCK_WINDOW_END:
            return "outside_window"
        
        # Get current prices
        yes_price = self.get_best_ask(market['yes_token'])
        no_price = self.get_best_ask(market['no_token'])
        
        if not yes_price or not no_price:
            return "no_prices"
        
        # Show current status
        minutes_remaining = int(time_remaining // 60)
        seconds_remaining = int(time_remaining % 60)
        print(f"📊 [{minutes_remaining}m {seconds_remaining}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f}", end="\r")
        
        # Determine which side to enter - ONLY NO (DOWN) side when >= 0.90
        entry_token = None
        entry_side = None
        entry_price = None
        order_size = ORDER_SIZE
        
        # ONLY enter NO (DOWN) if it's >= MIN_ENTRY_PRICE
        if no_price >= MIN_ENTRY_PRICE:
            entry_token = market['no_token']
            entry_side = "NO (DOWN)"
            entry_price = no_price
        else:
            # NO side doesn't qualify
            return "no_opportunity"
        
        print(f"\n\n{'='*60}")
        print(f"🎯 MID-GAME LOCK TRIGGERED - {entry_side}")
        print(f"{'='*60}")
        
        # Check balance
        balance = self.get_balance()
        required = entry_price * order_size
        
        if balance < required:
            print(f"❌ Insufficient funds. Need ${required:.2f}, have ${balance:.2f}")
            print(f"💡 Attempting to settle previous markets...")
            
            # Try to settle previous market if we have condition_id
            if market.get('condition_id'):
                self.settle_market(market['condition_id'])
                time.sleep(2)
                
                # Check balance again
                balance = self.get_balance()
                if balance < required:
                    print(f"   Still insufficient after settlement: ${balance:.2f}")
                    self.traded_markets.add(slug)
                    return "insufficient_funds"
                else:
                    print(f"   ✅ Balance restored: ${balance:.2f}")
        
        print(f"Market: {market['title']}")
        print(f"Time Remaining: {minutes_remaining}m {seconds_remaining}s")
        print(f"📊 YES: ${yes_price:.2f} | NO: ${no_price:.2f}")
        print(f"📈 Entry Side: {entry_side} @ ${entry_price:.2f}")
        
        # Execute entry
        print(f"\n⚡ Placing ENTRY order...")
        entry_id = self.place_order(entry_token, entry_price, order_size, BUY)
        
        if not entry_id:
            print("❌ Entry failed")
            return "entry_failed"
        
        print(f"✅ ENTRY ORDER PLACED! Order ID: {entry_id}")
        
        # Get actual fill price
        print(f"\n🔍 Verifying actual fill price...")
        actual_entry_price = self.get_actual_fill_price(entry_id)
        
        if not actual_entry_price:
            print(f"⚠️ Could not verify fill price, using fallback...")
            time.sleep(2)
            actual_entry_price = self.get_best_bid(entry_token)
            
            if not actual_entry_price:
                print("❌ Critical: Cannot determine entry price. Aborting trade.")
                self.place_order(entry_token, 0.01, order_size, SELL)
                return "entry_failed"
        
        # Calculate slippage
        slippage = abs(actual_entry_price - entry_price)
        print(f"\n📊 ENTRY ANALYSIS:")
        print(f"   Intended: ${entry_price:.4f}")
        print(f"   Actual:   ${actual_entry_price:.4f}")
        print(f"   Slippage: ${slippage:.4f} ({(slippage/entry_price)*100:.2f}%)")
        
        # Check slippage
        if slippage > MAX_ACCEPTABLE_SLIPPAGE:
            print(f"\n🚨 EXCESSIVE SLIPPAGE DETECTED!")
            print(f"   Exiting trade immediately...")
            current_bid = self.get_best_bid(entry_token)
            if current_bid:
                self.place_order(entry_token, current_bid - 0.01, order_size, SELL)
            self.traded_markets.add(slug)
            return "excessive_slippage"
        
        # Monitor with dynamic trailing stop loss
        print(f"\n💎 Active position management: DYNAMIC TRAILING STOP...")
        entry_time = time.time()
        result = self.monitor_with_trailing_stop(entry_token, actual_entry_price, order_size, entry_time, market_end_time)
        
        # Update session stats
        self.session_trades += 1
        if result in ["take_profit", "time_exit"]:
            self.session_wins += 1
        else:
            self.session_losses += 1
        
        # Show session P&L
        current_balance = self.get_balance()
        session_pnl = current_balance - self.starting_balance
        win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
        
        print(f"\n📊 SESSION STATS:")
        print(f"   Starting Balance: ${self.starting_balance:.2f}")
        print(f"   Current Balance: ${current_balance:.2f}")
        print(f"   Session P&L: ${session_pnl:+.2f}")
        print(f"   Trades: {self.session_trades} | Wins: {self.session_wins} | Losses: {self.session_losses}")
        print(f"   Win Rate: {win_rate:.1f}%")
        
        # Try to settle this market immediately after trade
        if market.get('condition_id'):
            print(f"\n💰 Attempting immediate settlement...")
            time.sleep(5)  # Give it a moment to finalize
            self.settle_market(market['condition_id'])
        
        # Mark as traded
        self.traded_markets.add(slug)
        print(f"\n✅ Trade cycle complete!\n")
        
        return "traded"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"📋 Strategy: Mid-Game Lock - NO (DOWN) ONLY with DYNAMIC TRAILING STOP & AUTO-SETTLEMENT")
        print(f"   Entry: Buy NO (DOWN) @ ${MIN_ENTRY_PRICE:.2f}+ when 5-10min remaining")
        print(f"   Position size: {ORDER_SIZE} shares")
        print(f"   Take Profit: +${TAKE_PROFIT_SPREAD:.2f} (always active)")
        print(f"   Stop Loss: -${STOP_LOSS_SPREAD:.2f} initial")
        print(f"   Dynamic SL: Activates between {MIN_STOP_LOSS_DELAY}s-300s (auto-adjusted)")
        print(f"   No-Trade Zone: Last {STOP_LOSS_BUFFER_TIME}s (13th-15th minute)")
        print(f"   Trailing: Locks in {int(TRAILING_PROFIT_LOCK*100)}% of gains above entry")
        print(f"   Max Slippage: ${MAX_ACCEPTABLE_SLIPPAGE:.2f}")
        print(f"   Auto-Settlement: Enabled\n")
        
        current_market = None
        
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                current_timestamp = int(now_utc.timestamp())
                
                # Calculate which 15min window we're in
                market_timestamp = (current_timestamp // 900) * 900
                expected_slug = f"btc-updown-15m-{market_timestamp}"
                
                # Check if we need to find a new market
                if not current_market or current_market['slug'] != expected_slug:
                    print(f"\n🔍 Looking for market: {expected_slug}")
                    
                    if MANUAL_SLUG:
                        current_market = self.get_market_from_slug(MANUAL_SLUG)
                        # For manual slug, extract timestamp
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
                
                # Execute mid-game lock strategy
                status = self.execute_mid_game_lock(current_market, market_timestamp)
                
                if status == "traded":
                    print("✅ Trade executed! Waiting for next market...")
                    time.sleep(10)
                elif status == "already_traded":
                    next_market_time = ((current_timestamp // 900) + 1) * 900
                    wait_time = max(next_market_time - int(time.time()), 5)
                    print(f"\n⏭️ Already traded this market. Next market in {wait_time}s\n")
                    time.sleep(wait_time)
                elif status == "outside_window":
                    # Not in the 5-10 min window yet
                    time.sleep(CHECK_INTERVAL)
                else:
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
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = BTCMidGameBot()
    bot.run()
