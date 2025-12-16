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
ORDER_SIZE = 7            # Position size

# Exit Settings
TAKE_PROFIT_SPREAD = 0.05  # Take profit at +5 cents from entry
STOP_LOSS_SPREAD = 0.05     # Stop loss at -5 cents from entry
STOP_LOSS_DELAY = 300       # Wait 5 minutes before activating stop loss
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
        print("🤖 BTC Mid-Game Lock Bot Starting...")
        
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

    def monitor_with_tp_and_sl(self, token_id, entry_price, size, entry_time):
        """Monitor position with take profit and delayed stop loss"""
        tp_price = min(entry_price + TAKE_PROFIT_SPREAD, 0.99)
        sl_price = max(entry_price - STOP_LOSS_SPREAD, 0.01)
        stop_loss_active_time = entry_time + STOP_LOSS_DELAY
        
        print(f"\n🎯 Exit Targets:")
        print(f"   Entry: ${entry_price:.4f}")
        print(f"   🚀 Take Profit: ${tp_price:.4f} (+${TAKE_PROFIT_SPREAD:.2f})")
        print(f"   🛡️ Stop Loss: ${sl_price:.4f} (-${STOP_LOSS_SPREAD:.2f})")
        print(f"   Stop Loss Activation: {STOP_LOSS_DELAY}s from entry")
        print(f"   SL activates at: {datetime.fromtimestamp(stop_loss_active_time, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
        
        while True:
            time.sleep(CHECK_INTERVAL)
            current_time = time.time()
            
            # Check current market price
            current_bid = self.get_best_bid(token_id)
            
            if current_bid:
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
                    print(f"   Profit: +${profit:.2f}")
                    return "take_profit"
                
                # Check stop loss (only after delay)
                if time_until_sl_active > 0:
                    # Stop loss not active yet
                    print(f"   Current: ${current_bid:.2f} | Entry: ${entry_price:.2f} | TP: ${tp_price:.2f} | SL activates in: {int(time_until_sl_active)}s", end="\r")
                else:
                    # Stop loss is now active
                    print(f"   Current: ${current_bid:.2f} | Entry: ${entry_price:.2f} | TP: ${tp_price:.2f} | SL: ${sl_price:.2f} [ACTIVE]", end="\r")
                    
                    if current_bid <= sl_price:
                        print(f"\n\n🛑 STOP LOSS TRIGGERED at ${current_bid:.2f}!")
                        
                        # Execute market sell
                        print("   Executing Market Sell...")
                        self.place_order(token_id, current_bid - 0.01, size, SELL)
                        
                        loss = (entry_price - current_bid) * size
                        print(f"   📉 Position closed.")
                        print(f"   Entry: ${entry_price:.4f}")
                        print(f"   Exit: ${current_bid:.2f}")
                        print(f"   Loss: -${loss:.2f}")
                        return "stop_loss"
            
            # Check if market has ended (15 min from start)
            # If so, position will auto-settle
            if current_time > entry_time + 900:
                print(f"\n\n⏰ Market ended - position will auto-settle")
                return "market_ended"

    def execute_mid_game_lock(self, market, market_start_time):
        """Execute mid-game lock strategy"""
        slug = market['slug']
        
        # Check if already traded
        if slug in self.traded_markets:
            return "already_traded"
        
        current_time = time.time()
        time_remaining = (market_start_time + 900) - current_time
        
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
        
        # Determine which side to enter based on price >= 0.90
        entry_token = None
        entry_side = None
        entry_price = None
        order_size = ORDER_SIZE
        
        # Check both YES and NO - enter whichever is >= MIN_ENTRY_PRICE
        # If both qualify, prefer the higher price (more confidence)
        if yes_price >= MIN_ENTRY_PRICE and no_price >= MIN_ENTRY_PRICE:
            # Both qualify - choose higher price
            if yes_price >= no_price:
                entry_token = market['yes_token']
                entry_side = "YES"
                entry_price = yes_price
            else:
                entry_token = market['no_token']
                entry_side = "NO"
                entry_price = no_price
        elif yes_price >= MIN_ENTRY_PRICE:
            entry_token = market['yes_token']
            entry_side = "YES"
            entry_price = yes_price
        elif no_price >= MIN_ENTRY_PRICE:
            entry_token = market['no_token']
            entry_side = "NO"
            entry_price = no_price
        else:
            # Neither side qualifies
            return "no_opportunity"
        
        print(f"\n\n{'='*60}")
        print(f"🎯 MID-GAME LOCK TRIGGERED - {entry_side}")
        print(f"{'='*60}")
        
        # Check balance
        balance = self.get_balance()
        required = entry_price * order_size
        
        if balance < required:
            print(f"❌ Insufficient funds. Need ${required:.2f}, have ${balance:.2f}")
            self.traded_markets.add(slug)
            return "insufficient_funds"
        
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
        
        # Monitor with take profit and stop loss
        print(f"\n💎 Active position management: TP/SL monitoring...")
        entry_time = time.time()
        result = self.monitor_with_tp_and_sl(entry_token, actual_entry_price, order_size, entry_time)
        
        # Mark as traded
        self.traded_markets.add(slug)
        print(f"\n✅ Trade cycle complete!\n")
        
        return "traded"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"📋 Strategy: Mid-Game Lock (YES or NO)")
        print(f"   Entry: Buy YES or NO @ ${MIN_ENTRY_PRICE:.2f}+ when 5-10min remaining")
        print(f"   Position size: {ORDER_SIZE} shares")
        print(f"   Take Profit: +${TAKE_PROFIT_SPREAD:.2f} (always active)")
        print(f"   Stop Loss: -${STOP_LOSS_SPREAD:.2f} (activates after {STOP_LOSS_DELAY}s)")
        print(f"   Max Slippage: ${MAX_ACCEPTABLE_SLIPPAGE:.2f}\n")
        
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
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = BTCMidGameBot()

    bot.run()
