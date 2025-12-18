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

# Your private key (this controls your Polymarket account)
PRIVATE_KEY = "0xbbd185bb356315b5f040a2af2fa28549177f3087559bb76885033e9cf8e8bf34"

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
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# Manual Override (optional - leave empty for auto-detection)
MANUAL_SLUG = ""  # e.g., "btc-updown-15m-1765593000"

# Slug generation for BTC 15min markets
INTERVAL = 900  # 15 minutes in seconds

# ==========================================
# 🎯 STRATEGY SELECTION
# ==========================================
# Choose which strategy to run: "ARBITRAGE", "MID_GAME", or "BOTH"
STRATEGY_MODE = "BOTH"

# ==========================================
# 🎯 ARBITRAGE STRATEGY SETTINGS
# ==========================================
# Entry timing - first 50 seconds window (870s to 850s remaining)
ARB_ENTRY_WINDOW_START = 870  # Start entry when seconds_until_close < 870
ARB_ENTRY_WINDOW_END = 850    # Must execute before 850s

# Entry criteria
ARB_MIN_FIRST_PRICE = 0.40    # Minimum price for first buy
ARB_MAX_FIRST_PRICE = 0.48    # Maximum price for first buy
ARB_RETRY_DELAY = 1           # Wait 1s if no qualifying opportunity

# Position sizing
ARB_POSITION_SIZE = 5         # 5 shares per trade

# Sell management
ARB_SELL_TIMEOUT = 200        # Try to sell for 200 seconds
ARB_PRICE_IMPROVEMENT = 0.01  # Add 1 cent for market orders

# Stop loss
ARB_STOP_LOSS_OFFSET = 0.07   # Sell at -$0.07 from first buy
ARB_STOP_LOSS_DELAY = 200     # Activate stop loss after 200s

# ==========================================
# 🎯 MID-GAME LOCK STRATEGY SETTINGS
# ==========================================
# Entry when 5-10 min remaining
MG_LOCK_WINDOW_START = 300  # Start at 5 minutes remaining
MG_LOCK_WINDOW_END = 600    # End at 10 minutes remaining
MG_MIN_ENTRY_PRICE = 0.91   # Buy YES or NO only if price >= 0.91
MG_ORDER_SIZE = 10          # Position size

# Exit Settings - TRAILING STOP LOSS with DYNAMIC activation
MG_TAKE_PROFIT_SPREAD = 0.05      # Take profit at +5 cents from entry
MG_STOP_LOSS_SPREAD = 0.03        # Initial stop loss at -3 cents from entry (tighter)
MG_MIN_STOP_LOSS_DELAY = 60       # Minimum wait time (1 minute)
MG_STOP_LOSS_BUFFER_TIME = 180    # Must activate SL by 3 minutes before market end
MG_TRAILING_PROFIT_LOCK = 0.7     # Lock in 70% of gains above entry (was 50%)
MG_MIN_PROFIT_FOR_TRAILING = 0.02 # Only activate trailing after +2 cent profit
MG_MAX_ACCEPTABLE_SLIPPAGE = 0.05 # Max 5 cent slippage on entry
MG_REQUEST_DELAY = 0.5            # Delay between API requests to avoid rate limiting

# System settings
CHECK_INTERVAL = 2          # Check every 2 seconds
MIN_ORDER_SIZE = 0.1        # Minimum order size

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class CombinedBTCBot:
    def __init__(self):
        print("🤖 Combined BTC Trading Bot Starting...")
        print(f"📋 Strategy Mode: {STRATEGY_MODE}")
        
        # 1. Setup Web3 (For Balance)
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # 2. Setup Client (For Trading)
        try:
            print(f"🔐 Setting up Polymarket client...")
            
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
            print("🔐 Deriving API credentials...")
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            print(f"✅ Trading as: {self.client.get_address()}\n")
            
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            import traceback
            traceback.print_exc()
            exit()
            
        # Track markets and strategy execution
        self.traded_markets_arb = set()
        self.traded_markets_mg = set()
        self.skipped_markets_arb = set()
        self.active_arbitrage = False  # NEW: Block mid-game during arbitrage
        
        # Session tracking
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0

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
            time.sleep(0.1)  # Small delay to avoid rate limiting
            book = self.client.get_order_book(token_id)
            if book.asks:
                return min(float(o.price) for o in book.asks)
            return None
        except:
            return None

    def get_best_bid(self, token_id):
        """Get best available selling price"""
        try:
            time.sleep(0.1)  # Small delay to avoid rate limiting
            book = self.client.get_order_book(token_id)
            if book.bids:
                return max(float(o.price) for o in book.bids)
            return None
        except:
            return None

    def get_order_book_depth(self, token_id):
        """Get detailed order book information"""
        try:
            time.sleep(0.1)  # Small delay to avoid rate limiting
            book = self.client.get_order_book(token_id)
            
            ask_depth = len(book.asks) if book.asks else 0
            bid_depth = len(book.bids) if book.bids else 0
            best_ask = min(float(o.price) for o in book.asks) if book.asks else None
            best_bid = max(float(o.price) for o in book.bids) if book.bids else None
            
            ask_liquidity = 0
            if book.asks and best_ask:
                for order in book.asks:
                    if float(order.price) == best_ask:
                        ask_liquidity += float(order.size)
            
            return {
                'best_ask': best_ask,
                'best_bid': best_bid,
                'ask_depth': ask_depth,
                'bid_depth': bid_depth,
                'ask_liquidity': ask_liquidity
            }
        except Exception as e:
            print(f"   ⚠️ Error getting order book: {e}")
            return None

    def place_market_order(self, token_id, price, size, side):
        """Place a Fill-or-Kill (FOK) market order for ENTRY only"""
        try:
            if side == BUY:
                price = min(price + ARB_PRICE_IMPROVEMENT, 0.99)
            
            price = round(price, 2)
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Order size {size} below minimum {MIN_ORDER_SIZE}")
                return None
            
            print(f"   🔧 Placing FOK {side} order: {size} shares @ ${price:.2f}")
            
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
                    print(f"   ✅ FOK Order placed: {order_id}")
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    print(f"   ⚠️ FOK Order failed: {error_msg}")
                    return None
            else:
                print(f"   ⚠️ Empty or invalid response")
                return None
                
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def place_limit_order(self, token_id, price, size, side):
        """Place a GTC (Good-Til-Canceled) limit order for SELLING"""
        try:
            price = round(price, 2)
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Order size {size} below minimum {MIN_ORDER_SIZE}")
                return None
            
            print(f"   🔧 Placing GTC LIMIT {side} order: {size} shares @ ${price:.2f}")
            
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=self.client.create_order(OrderArgs(
                        price=price,
                        size=size,
                        side=side,
                        token_id=token_id,
                    )),
                    orderType=OrderType.GTC,
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                
                if order_result.get('success') or order_result.get('orderID'):
                    order_id = order_result.get('orderID', 'success')
                    print(f"   ✅ GTC Limit Order placed: {order_id}")
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    print(f"   ⚠️ GTC Order failed: {error_msg}")
                    return None
            else:
                print(f"   ⚠️ Empty or invalid response")
                return None
                
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def cancel_order(self, order_id):
        """Cancel an open order"""
        try:
            result = self.client.cancel(order_id)
            print(f"   🗑️ Order {order_id} cancelled")
            return True
        except Exception as e:
            print(f"   ⚠️ Cancel failed: {e}")
            return False

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

    def settle_market(self, condition_id):
        """Claim winnings from a settled market - DISABLED (method doesn't exist)"""
        try:
            print(f"\n💰 Settlement skipped (API method not available)")
            print(f"   Winnings will auto-settle after market resolves")
            return False
        except Exception as e:
            print(f"   ⚠️ Settlement error: {e}")
            return False

    # ==========================================
    # ARBITRAGE STRATEGY
    # ==========================================
    def execute_arbitrage_strategy(self, market, market_start_time):
        """Execute the modified arbitrage strategy - buy lowest side, then place GTC limit sell order"""
        slug = market['slug']
        
        if slug in self.traded_markets_arb:
            return "already_traded"
        
        if slug in self.skipped_markets_arb:
            return "already_skipped"
        
        current_time = time.time()
        seconds_until_close = (market_start_time + 900) - current_time
        
        # Check entry window
        if seconds_until_close >= ARB_ENTRY_WINDOW_START:
            print(f"⏳ [ARB] Waiting for entry window ({int(seconds_until_close)}s > {ARB_ENTRY_WINDOW_START}s)", end="\r")
            return "waiting_for_entry_window"
        
        if seconds_until_close < ARB_ENTRY_WINDOW_END:
            if slug not in self.skipped_markets_arb:
                print(f"\n⏰ [ARB] Entry window closed! ({int(seconds_until_close)}s < {ARB_ENTRY_WINDOW_END}s)")
                print(f"   Skipping this market, waiting for next one...\n")
                self.skipped_markets_arb.add(slug)
            return "entry_window_closed"
        
        # BLOCK MID-GAME STRATEGY
        self.active_arbitrage = True
        
        print(f"\n{'='*60}")
        print(f"🎯 ARBITRAGE ENTRY WINDOW ACTIVE")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"Seconds until close: {int(seconds_until_close)}")
        
        # Get order book information
        print(f"\n📊 Analyzing order books...")
        yes_book = self.get_order_book_depth(market['yes_token'])
        no_book = self.get_order_book_depth(market['no_token'])
        
        if not yes_book or not no_book:
            print("⚠️ Cannot get order book data, retrying...")
            self.active_arbitrage = False
            return "no_orderbook"
        
        yes_ask = yes_book['best_ask']
        no_ask = no_book['best_ask']
        
        if not yes_ask or not no_ask:
            print("⚠️ Cannot get prices, retrying...")
            self.active_arbitrage = False
            return "no_prices"
        
        print(f"\n📊 Current Market State:")
        print(f"   YES: ${yes_ask:.2f} (liquidity: {yes_book['ask_liquidity']:.2f})")
        print(f"   NO:  ${no_ask:.2f} (liquidity: {no_book['ask_liquidity']:.2f})")
        
        # Determine which side to buy (lowest price, must be between 0.40 and 0.48)
        first_buy_side = None
        first_buy_token = None
        first_buy_price = None
        first_buy_book = None
        
        # Check if YES qualifies
        yes_qualifies = ARB_MIN_FIRST_PRICE <= yes_ask <= ARB_MAX_FIRST_PRICE
        no_qualifies = ARB_MIN_FIRST_PRICE <= no_ask <= ARB_MAX_FIRST_PRICE
        
        if yes_qualifies and no_qualifies:
            # Both qualify - choose the lower price
            if yes_ask < no_ask:
                first_buy_side = "YES"
                first_buy_token = market['yes_token']
                first_buy_price = yes_ask
                first_buy_book = yes_book
            else:
                first_buy_side = "NO"
                first_buy_token = market['no_token']
                first_buy_price = no_ask
                first_buy_book = no_book
        elif yes_qualifies:
            first_buy_side = "YES"
            first_buy_token = market['yes_token']
            first_buy_price = yes_ask
            first_buy_book = yes_book
        elif no_qualifies:
            first_buy_side = "NO"
            first_buy_token = market['no_token']
            first_buy_price = no_ask
            first_buy_book = no_book
        
        if not first_buy_side:
            print(f"⏳ No side qualifies (must be ${ARB_MIN_FIRST_PRICE:.2f} - ${ARB_MAX_FIRST_PRICE:.2f}), waiting {ARB_RETRY_DELAY}s...")
            time.sleep(ARB_RETRY_DELAY)
            self.active_arbitrage = False
            return "no_opportunity"
        
        # Check liquidity
        if first_buy_book['ask_liquidity'] < ARB_POSITION_SIZE:
            print(f"⚠️ Insufficient liquidity on {first_buy_side}: {first_buy_book['ask_liquidity']:.2f} < {ARB_POSITION_SIZE}")
            print(f"   Waiting {ARB_RETRY_DELAY}s...")
            time.sleep(ARB_RETRY_DELAY)
            self.active_arbitrage = False
            return "insufficient_liquidity"
        
        print(f"\n✅ First Buy Opportunity:")
        print(f"   Side: {first_buy_side}")
        print(f"   Price: ${first_buy_price:.2f}")
        print(f"   Available liquidity: {first_buy_book['ask_liquidity']:.2f} shares")
        
        # Execute first buy
        print(f"\n⚡ Executing FIRST BUY ORDER (FOK - {first_buy_side})...")
        first_order_id = self.place_market_order(first_buy_token, first_buy_price, ARB_POSITION_SIZE, BUY)
        
        if not first_order_id:
            print("\n❌ First FOK order failed! Retrying...")
            time.sleep(ARB_RETRY_DELAY)
            self.active_arbitrage = False
            return "first_order_failed"
        
        time.sleep(1)
        
        # Verify first order fill
        first_filled, actual_first_price = self.check_order_status(first_order_id)
        
        if not first_filled or not actual_first_price:
            print("⚠️ FOK order execution verification failed. Aborting trade cycle.")
            self.traded_markets_arb.add(slug)
            self.active_arbitrage = False
            return "first_order_failed"
        
        print(f"✅ FIRST ORDER FILLED ({ARB_POSITION_SIZE} shares)!")
        first_order_time = time.time()
        print(f"   Actual fill: ${actual_first_price:.2f}")
        
        # Now place GTC LIMIT SELL ORDER
        print(f"\n🔄 PLACING GTC LIMIT SELL ORDER...")
        print(f"   Strategy: Place limit order at profitable price, monitor and adjust")
        
        # Determine the opposite token (the one we didn't buy)
        opposite_token = market['yes_token'] if first_buy_side == "NO" else market['no_token']
        opposite_side = "YES" if first_buy_side == "NO" else "NO"
        
        start_sell_time = time.time()
        stop_loss_active_time = first_order_time + ARB_STOP_LOSS_DELAY
        stop_loss_price = max(actual_first_price - ARB_STOP_LOSS_OFFSET, 0.01)
        
        current_sell_order_id = None
        last_sell_price = None
        
        while True:
            elapsed = time.time() - start_sell_time
            
            if elapsed >= ARB_SELL_TIMEOUT:
                print(f"\n\n⏰ Sell timeout ({ARB_SELL_TIMEOUT}s reached)")
                print(f"   Cancelling any open orders and executing emergency exit...")
                
                if current_sell_order_id:
                    self.cancel_order(current_sell_order_id)
                
                # Emergency market sell
                opposite_ask = self.get_best_ask(opposite_token)
                if opposite_ask:
                    sell_price = min(opposite_ask, 0.99)
                    emergency_order = self.place_market_order(first_buy_token, sell_price, ARB_POSITION_SIZE, SELL)
                    
                    if emergency_order:
                        time.sleep(2)
                        filled, final_price = self.check_order_status(emergency_order)
                        if filled and final_price:
                            result = (final_price - actual_first_price) * ARB_POSITION_SIZE
                            print(f"   📊 Emergency exit completed")
                            print(f"   Final P&L: ${result:+.2f}")
                
                self.traded_markets_arb.add(slug)
                self.active_arbitrage = False
                return "sell_timeout"
            
            # Check if current sell order is filled
            if current_sell_order_id:
                sell_filled, actual_sell_price = self.check_order_status(current_sell_order_id)
                
                if sell_filled and actual_sell_price:
                    print(f"\n\n🎉 SELL ORDER FILLED!")
                    print(f"{'='*60}")
                    print(f"   Buy:  {first_buy_side} @ ${actual_first_price:.2f}")
                    print(f"   Sell: {first_buy_side} @ ${actual_sell_price:.2f}")
                    print(f"   P&L: ${(actual_sell_price - actual_first_price) * ARB_POSITION_SIZE:+.2f}")
                    print(f"{'='*60}")
                    
                    if actual_sell_price > actual_first_price:
                        self.session_wins += 1
                    else:
                        self.session_losses += 1
                    
                    self.session_trades += 1
                    self.traded_markets_arb.add(slug)
                    self.active_arbitrage = False
                    return "arbitrage_complete"
            
            # Get current market state
            opposite_ask = self.get_best_ask(opposite_token)
            current_bid = self.get_best_bid(first_buy_token)
            
            if opposite_ask and current_bid:
                # Check stop loss (only after delay)
                time_until_sl = stop_loss_active_time - time.time()
                
                if time_until_sl <= 0 and opposite_ask <= stop_loss_price:
                    print(f"\n\n🛑 STOP LOSS TRIGGERED at ${opposite_ask:.2f}!")
                    print(f"   Cancelling open order and executing stop loss...")
                    
                    if current_sell_order_id:
                        self.cancel_order(current_sell_order_id)
                    
                    # Execute stop loss with FOK
                    sl_order = self.place_market_order(first_buy_token, opposite_ask, ARB_POSITION_SIZE, SELL)
                    
                    if sl_order:
                        time.sleep(2)
                        filled, final_price = self.check_order_status(sl_order)
                        if filled and final_price:
                            loss = (actual_first_price - final_price) * ARB_POSITION_SIZE
                            print(f"   📉 Stop loss executed")
                            print(f"   Loss: -${loss:.2f}")
                    
                    self.session_losses += 1
                    self.session_trades += 1
                    self.traded_markets_arb.add(slug)
                    self.active_arbitrage = False
                    return "stop_loss"
                
                # Determine optimal sell price (use opposite side's ask to cross spread)
                optimal_sell_price = min(opposite_ask, 0.99)
                
                # Only place/update order if price has changed
                if optimal_sell_price != last_sell_price:
                    # Cancel existing order if any
                    if current_sell_order_id:
                        self.cancel_order(current_sell_order_id)
                        time.sleep(0.5)
                    
                    # Place new GTC limit sell order
                    print(f"\n   📝 Placing GTC sell @ ${optimal_sell_price:.2f} (matching {opposite_side} ask)")
                    current_sell_order_id = self.place_limit_order(first_buy_token, optimal_sell_price, ARB_POSITION_SIZE, SELL)
                    last_sell_price = optimal_sell_price
                
                estimated_pnl = (optimal_sell_price - actual_first_price) * ARB_POSITION_SIZE
                print(f"   💹 Current: ${current_bid:.2f} | Target: ${optimal_sell_price:.2f} | Entry: ${actual_first_price:.2f} | Est P&L: ${estimated_pnl:+.2f} | Time: {int(ARB_SELL_TIMEOUT - elapsed)}s", end="\r")
            
            time.sleep(CHECK_INTERVAL)
        
        self.session_trades += 1
        self.traded_markets_arb.add(slug)
        self.active_arbitrage = False
        return "sell_timeout"

    # ==========================================
    # MID-GAME LOCK STRATEGY
    # ==========================================
    def calculate_dynamic_sl_delay(self, entry_time, market_end_time):
        """Calculate how long to wait before activating stop loss"""
        time_until_market_end = market_end_time - entry_time
        max_delay = time_until_market_end - MG_STOP_LOSS_BUFFER_TIME
        actual_delay = max(MG_MIN_STOP_LOSS_DELAY, min(max_delay, 300))
        return actual_delay

    def monitor_with_trailing_stop(self, token_id, entry_price, size, entry_time, market_end_time):
        """Monitor position with take profit and DYNAMIC TRAILING stop loss"""
        tp_price = min(entry_price + MG_TAKE_PROFIT_SPREAD, 0.99)
        initial_sl_price = max(entry_price - MG_STOP_LOSS_SPREAD, 0.01)
        
        dynamic_sl_delay = self.calculate_dynamic_sl_delay(entry_time, market_end_time)
        stop_loss_active_time = entry_time + dynamic_sl_delay
        
        trailing_stop = initial_sl_price
        highest_bid = entry_price
        trailing_activated = False
        
        print(f"\n🎯 Exit Targets (DYNAMIC TRAILING STOP):")
        print(f"   Entry: ${entry_price:.4f}")
        print(f"   🚀 Take Profit: ${tp_price:.4f} (+${MG_TAKE_PROFIT_SPREAD:.2f})")
        print(f"   🛡️ Initial Stop Loss: ${initial_sl_price:.4f} (-${MG_STOP_LOSS_SPREAD:.2f})")
        print(f"   📈 Trailing activates after +${MG_MIN_PROFIT_FOR_TRAILING:.2f} profit")
        print(f"   📈 Trailing Stop: Locks in {int(MG_TRAILING_PROFIT_LOCK*100)}% of gains above entry")
        print(f"   ⏰ Dynamic SL Activation: {int(dynamic_sl_delay)}s from entry")
        
        while True:
            time.sleep(CHECK_INTERVAL + MG_REQUEST_DELAY)  # Add delay to avoid rate limiting
            current_time = time.time()
            
            time_until_end = market_end_time - current_time
            if time_until_end <= MG_STOP_LOSS_BUFFER_TIME:
                print(f"\n\n⏰ Entering final 3 minutes - forcing exit at market price")
                current_bid = self.get_best_bid(token_id)
                if current_bid:
                    print("   Executing final market exit...")
                    time.sleep(MG_REQUEST_DELAY)
                    self.place_market_order(token_id, current_bid - 0.01, size, SELL)
                    pnl = (current_bid - entry_price) * size
                    print(f"   📊 Position closed (time-based exit)")
                    print(f"   P&L: ${pnl:+.2f}")
                    return "time_exit"
            
            current_bid = self.get_best_bid(token_id)
            
            if current_bid:
                # CHECK TAKE PROFIT FIRST (before updating highest_bid)
                # Use a buffer to trigger slightly before the exact TP price
                tp_trigger_threshold = tp_price - 0.005  # Trigger 0.5 cents before TP
                
                if current_bid >= tp_trigger_threshold:
                    print(f"\n\n💰 TAKE PROFIT TRIGGERED at ${current_bid:.2f}!")
                    print(f"   (Target was ${tp_price:.2f}, triggered at ${tp_trigger_threshold:.2f})")
                    print("   Executing Take Profit sell...")
                    time.sleep(MG_REQUEST_DELAY)
                    self.place_market_order(token_id, current_bid, size, SELL)
                    profit = (current_bid - entry_price) * size
                    print(f"   📈 Position closed at profit!")
                    print(f"   Profit: +${profit:.2f}")
                    return "take_profit"
                
                # Update trailing stop logic - only activate after minimum profit
                if current_bid > highest_bid:
                    highest_bid = current_bid
                
                profit_from_entry = highest_bid - entry_price
                
                # Only activate trailing stop if we've made minimum profit
                if profit_from_entry >= MG_MIN_PROFIT_FOR_TRAILING:
                    if not trailing_activated:
                        print(f"\n\n   🎯 TRAILING STOP ACTIVATED! (Profit: +${profit_from_entry:.2f})")
                        trailing_activated = True
                    
                    locked_profit = entry_price + (profit_from_entry * MG_TRAILING_PROFIT_LOCK)
                    trailing_stop = max(initial_sl_price, locked_profit)
                
                time_until_sl_active = max(0, stop_loss_active_time - current_time)
                
                # Check trailing stop loss (only after delay AND if activated)
                if time_until_sl_active > 0:
                    trail_status = f"Trailing: {'✓ Active' if trailing_activated else f'Waiting ({time_until_sl_active}s)'}"

    def execute_mid_game_lock(self, market, market_start_time):
        """Execute mid-game lock strategy - BLOCKED during active arbitrage"""
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        # BLOCK if arbitrage is active
        if self.active_arbitrage:
            return "blocked_by_arbitrage"
        
        if slug in self.traded_markets_mg:
            return "already_traded"
        
        current_time = time.time()
        time_remaining = market_end_time - current_time
        
        if time_remaining < MG_LOCK_WINDOW_START or time_remaining > MG_LOCK_WINDOW_END:
            return "outside_window"
        
        yes_price = self.get_best_ask(market['yes_token'])
        no_price = self.get_best_ask(market['no_token'])
        
        if not yes_price or not no_price:
            return "no_prices"
        
        minutes_remaining = int(time_remaining // 60)
        seconds_remaining = int(time_remaining % 60)
        print(f"📊 [MG] [{minutes_remaining}m {seconds_remaining}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f}", end="\r")
        
        entry_token = None
        entry_side = None
        entry_price = None
        order_size = MG_ORDER_SIZE
        
        if yes_price >= MG_MIN_ENTRY_PRICE and no_price >= MG_MIN_ENTRY_PRICE:
            if yes_price >= no_price:
                entry_token = market['yes_token']
                entry_side = "YES"
                entry_price = yes_price
            else:
                entry_token = market['no_token']
                entry_side = "NO"
                entry_price = no_price
        elif yes_price >= MG_MIN_ENTRY_PRICE:
            entry_token = market['yes_token']
            entry_side = "YES"
            entry_price = yes_price
        elif no_price >= MG_MIN_ENTRY_PRICE:
            entry_token = market['no_token']
            entry_side = "NO"
            entry_price = no_price
        else:
            return "no_opportunity"
        
        print(f"\n\n{'='*60}")
        print(f"🎯 MID-GAME LOCK TRIGGERED - {entry_side}")
        print(f"{'='*60}")
        
        balance = self.get_balance()
        required = entry_price * order_size
        
        if balance < required:
            print(f"❌ Insufficient funds. Need ${required:.2f}, have ${balance:.2f}")
            if market.get('condition_id'):
                self.settle_market(market['condition_id'])
                time.sleep(2)
                balance = self.get_balance()
                if balance < required:
                    print(f"   Still insufficient after settlement: ${balance:.2f}")
                    self.traded_markets_mg.add(slug)
                    return "insufficient_funds"
                else:
                    print(f"   ✅ Balance restored: ${balance:.2f}")
        
        print(f"Market: {market['title']}")
        print(f"Time Remaining: {minutes_remaining}m {seconds_remaining}s")
        print(f"📊 YES: ${yes_price:.2f} | NO: ${no_price:.2f}")
        print(f"📈 Entry Side: {entry_side} @ ${entry_price:.2f}")
        
        print(f"\n⚡ Placing ENTRY order...")
        entry_id = self.place_market_order(entry_token, entry_price, order_size, BUY)
        
        if not entry_id:
            print("❌ Entry failed")
            return "entry_failed"
        
        print(f"✅ ENTRY ORDER PLACED! Order ID: {entry_id}")
        
        print(f"\n🔍 Verifying actual fill price...")
        actual_entry_price = self.get_actual_fill_price(entry_id)
        
        if not actual_entry_price:
            print(f"⚠️ Could not verify fill price, using fallback...")
            time.sleep(2)
            actual_entry_price = self.get_best_bid(entry_token)
            
            if not actual_entry_price:
                print("❌ Critical: Cannot determine entry price. Aborting trade.")
                self.place_market_order(entry_token, 0.01, order_size, SELL)
                return "entry_failed"
        
        slippage = abs(actual_entry_price - entry_price)
        print(f"\n📊 ENTRY ANALYSIS:")
        print(f"   Intended: ${entry_price:.4f}")
        print(f"   Actual:   ${actual_entry_price:.4f}")
        print(f"   Slippage: ${slippage:.4f} ({(slippage/entry_price)*100:.2f}%)")
        
        if slippage > MG_MAX_ACCEPTABLE_SLIPPAGE:
            print(f"\n🚨 EXCESSIVE SLIPPAGE DETECTED!")
            print(f"   Exiting trade immediately...")
            current_bid = self.get_best_bid(entry_token)
            if current_bid:
                self.place_market_order(entry_token, current_bid - 0.01, order_size, SELL)
            self.traded_markets_mg.add(slug)
            return "excessive_slippage"
        
        print(f"\n💎 Active position management: DYNAMIC TRAILING STOP...")
        entry_time = time.time()
        result = self.monitor_with_trailing_stop(entry_token, actual_entry_price, order_size, entry_time, market_end_time)
        
        self.session_trades += 1
        if result in ["take_profit", "time_exit"]:
            self.session_wins += 1
        else:
            self.session_losses += 1
        
        current_balance = self.get_balance()
        session_pnl = current_balance - self.starting_balance
        win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
        
        print(f"\n📊 SESSION STATS:")
        print(f"   Starting Balance: ${self.starting_balance:.2f}")
        print(f"   Current Balance: ${current_balance:.2f}")
        print(f"   Session P&L: ${session_pnl:+.2f}")
        print(f"   Trades: {self.session_trades} | Wins: {self.session_wins} | Losses: {self.session_losses}")
        print(f"   Win Rate: {win_rate:.1f}%")
        
        if market.get('condition_id'):
            print(f"\n💰 Attempting immediate settlement...")
            time.sleep(5)
            self.settle_market(market['condition_id'])
        
        self.traded_markets_mg.add(slug)
        print(f"\n✅ Trade cycle complete!\n")
        
        return "traded"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"\n📋 STRATEGY CONFIGURATION:")
        
        if STRATEGY_MODE in ["ARBITRAGE", "BOTH"]:
            print(f"\n🎯 ARBITRAGE STRATEGY:")
            print(f"   Entry Window: {ARB_ENTRY_WINDOW_START}s to {ARB_ENTRY_WINDOW_END}s remaining")
            print(f"   Entry Range: ${ARB_MIN_FIRST_PRICE:.2f} - ${ARB_MAX_FIRST_PRICE:.2f}")
            print(f"   Position: {ARB_POSITION_SIZE} shares")
            print(f"   Strategy: Buy lowest side, place GTC limit sell order")
            print(f"   Sell Timeout: {ARB_SELL_TIMEOUT}s")
            print(f"   Stop Loss: -${ARB_STOP_LOSS_OFFSET:.2f} (after {ARB_STOP_LOSS_DELAY}s)")
        
        if STRATEGY_MODE in ["MID_GAME", "BOTH"]:
            print(f"\n🎯 MID-GAME LOCK STRATEGY:")
            print(f"   Entry Window: {MG_LOCK_WINDOW_START}s to {MG_LOCK_WINDOW_END}s remaining")
            print(f"   Entry: ${MG_MIN_ENTRY_PRICE:.2f}+ on either side")
            print(f"   Position: {MG_ORDER_SIZE} shares")
            print(f"   Take Profit: +${MG_TAKE_PROFIT_SPREAD:.2f}")
            print(f"   Stop Loss: -${MG_STOP_LOSS_SPREAD:.2f} (dynamic)")
            print(f"   Trailing: {int(MG_TRAILING_PROFIT_LOCK*100)}% profit lock")
            print(f"   ⚠️ BLOCKED during active arbitrage trades")
        
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
                        
                        # Clear skipped markets for new market
                        self.skipped_markets_arb.clear()
                    else:
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        wait_time = next_market_time - current_timestamp
                        print(f"⏳ No active market. Next check in {wait_time}s")
                        time.sleep(min(wait_time, 60))
                        continue
                
                # Execute strategies based on mode
                arb_status = None
                mg_status = None
                
                if STRATEGY_MODE in ["ARBITRAGE", "BOTH"]:
                    arb_status = self.execute_arbitrage_strategy(current_market, market_timestamp)
                
                if STRATEGY_MODE in ["MID_GAME", "BOTH"]:
                    mg_status = self.execute_mid_game_lock(current_market, market_timestamp)
                
                # Handle status results
                if arb_status in ["arbitrage_complete", "stop_loss", "sell_timeout"]:
                    print("\n✅ [ARB] Trade cycle complete!")
                    time.sleep(5)
                
                if mg_status == "traded":
                    print("✅ [MG] Trade executed!")
                    time.sleep(5)
                
                if mg_status == "blocked_by_arbitrage":
                    # Just wait silently
                    time.sleep(CHECK_INTERVAL)
                
                if arb_status == "already_traded" and mg_status == "already_traded":
                    next_market_time = ((current_timestamp // 900) + 1) * 900
                    wait_time = max(next_market_time - int(time.time()), 5)
                    print(f"\n⏭️ All strategies complete. Next market in {wait_time}s\n")
                    time.sleep(wait_time)
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
    bot = CombinedBTCBot()
    bot.run()