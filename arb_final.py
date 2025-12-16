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
    print(f"   We'll try proxy mode with signature_type=1 (Magic Link)")
    USE_PROXY = True
    SIGNATURE_TYPE = 1 
    # Use 1 for Magic Link / Email wallets
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# Manual Override (optional - leave empty for auto-detection)
MANUAL_SLUG = "" 

# e.g., "btc-updown-15m-1765593000"

# Slug generation for BTC 15min markets
INTERVAL = 900 

# 15 minutes in seconds

# ==========================================
# 🎯 ARBITRAGE STRATEGY SETTINGS
# ==========================================
# Entry timing
# Start entry when seconds_until_close < 870 (after first 30s)
ENTRY_WINDOW_START = 870 

# Must execute before 850s (within first 50s window)
ENTRY_WINDOW_END = 850 

# First buy must be on side with bid < $0.48
MAX_FIRST_BID = 0.48 

# Wait 2s if no qualifying bid found
RETRY_DELAY = 1 

# Position sizing
# 5 shares per side
POSITION_SIZE = 5 

# Target total cost per pair (YES + NO)
PAIR_TARGET_COST = 0.96 

# Second order management
# Wait 150s for second order to fill
SECOND_ORDER_TIMEOUT = 200 

# Max 3 cent slippage on second order
MAX_SLIPPAGE_SECOND = 0.03 

# Stop loss
# Sell at -$0.10 from first buy
STOP_LOSS_OFFSET = 0.07 

# Activate stop loss after 150s
STOP_LOSS_DELAY = 200 

# System settings
# Check every 2 seconds
CHECK_INTERVAL = 2 

# Order execution settings
# Price Improvement is used to ensure market order fills against the best ask
# FIXED CHANGE: Add 1 cents to ask price to ensure fill
PRICE_IMPROVEMENT = 0.01 

# Minimum order size
MIN_ORDER_SIZE = 0.1 

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class BTCArbitrageBot:
    def __init__(self):
        print("🤖 BTC Arbitrage Bot Starting...")
        
        # 1. Setup Web3 (For Balance)
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # 2. Setup Client (For Trading)
        try:
            print(f"🔐 Setting up Polymarket client...")
            
            if USE_PROXY:
                print(f"   Mode: Proxy with Magic Link (signature_type={SIGNATURE_TYPE})")
                print(f"   Funder: {TRADING_ADDRESS}")
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID, 
                    signature_type=SIGNATURE_TYPE,
                    funder=TRADING_ADDRESS
                )
            else:
                print(f"   Mode: EOA (direct trading from {TRADING_ADDRESS})")
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
            
        # Track markets we've already traded
        self.traded_markets = set() 
        
        # Track markets we skipped due to timeout
        self.skipped_markets = set() 

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

    def get_order_book_depth(self, token_id):
        """Get detailed order book information"""
        try:
            book = self.client.get_order_book(token_id)
            
            ask_depth = len(book.asks) if book.asks else 0
            
            bid_depth = len(book.bids) if book.bids else 0
            
            best_ask = min(float(o.price) for o in book.asks) if book.asks else None
            
            best_bid = max(float(o.price) for o in book.bids) if book.bids else None
            
            # Calculate total liquidity at best ask
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
            print(f"   ⚠️ Error getting order book: {e}")
            
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

    def place_market_order(self, token_id, price, size, side):
        """
        Place a Fill-or-Kill (FOK) order (Polymarket's implementation of strict Market/FAK).
        This executes the FULL amount or cancels the entire order immediately.
        """
        try:
            # For BUY orders, use the global PRICE_IMPROVEMENT (0.05)
            if side == BUY:
                # The price sent here is the limit of what we are willing to pay. 
                # We add the price improvement to ensure the order takes the best ask.
                price = min(price + PRICE_IMPROVEMENT, 0.99)
            
            price = round(price, 2)
            
            # Note: The OrderType.FOK here ensures the "filled for 5 shares" part of the request
            # is handled: it either fills completely (5 shares) or is killed.
            print(f"   🔧 Placing FOK {side} order: {size} shares @ ${price:.2f}")
            
            # Validate order size
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Order size {size} below minimum {MIN_ORDER_SIZE}")
                
                return None
            
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=self.client.create_order(OrderArgs(
                        price=price,
                        size=size,
                        side=side,
                        token_id=token_id,
                    )),
                    # Explicitly using FOK as requested (FAK equivalent on this platform)
                    orderType=OrderType.FOK, 
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                
                if order_result.get('success') or order_result.get('orderID'):
                    order_id = order_result.get('orderID', 'success')
                    
                    print(f"   ✅ FOK Order placed: {order_id}")
                    
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    
                    print(f"   ⚠️ FOK Order failed: {error_msg}")
                    
                    return None
            else:
                print(f"   ⚠️ Empty or invalid response")
                
                return None
                
        except Exception as e:
            print(f"   ❌ Order error: {e}")
            import traceback
            traceback.print_exc()
            
            return None

    def place_limit_order(self, token_id, price, size, side):
        """Place a Good-Til-Cancelled limit order"""
        try:
            price = round(price, 2)
            
            print(f"   📋 Placing LIMIT {side} order: {size} shares @ ${price:.2f}")
            
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
                    
                    print(f"   ✅ Limit order placed: {order_id}")
                    
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg') or order_result.get('error') or str(order_result)
                    
                    print(f"   ⚠️ Limit order failed: {error_msg}")
                    
                    return None
            else:
                print(f"   ⚠️ Empty or invalid response")
                
                return None
                
        except Exception as e:
            print(f"   ❌ Limit order error: {e}")
            
            return None

    def cancel_order(self, order_id):
        """Cancel an open order"""
        try:
            print(f"   🚫 Cancelling order {order_id}...")
            
            self.client.cancel(order_id)
            
            print(f"   ✅ Order cancelled")
            
            return True
        except Exception as e:
            print(f"   ⚠️ Cancel failed: {e}")
            
            return False

    def check_order_status(self, order_id):
        """Check if order has been filled and return the fill price"""
        try:
            order_details = self.client.get_order(order_id)
            
            if isinstance(order_details, dict):
                status = order_details.get('status', '')
                
                # Check for full fill (required for FOK)
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

    def execute_arbitrage_strategy(self, market, market_start_time):
        """Execute the arbitrage strategy"""
        slug = market['slug']
        
        # Check if already traded or skipped
        if slug in self.traded_markets:
            return "already_traded"
        
        if slug in self.skipped_markets:
            return "already_skipped"
        
        current_time = time.time()
        seconds_until_close = (market_start_time + 900) - current_time
        
        # CHECK ENTRY WINDOW
        if seconds_until_close >= ENTRY_WINDOW_START:
            print(f"⏳ Waiting for entry window ({int(seconds_until_close)}s > {ENTRY_WINDOW_START}s)", end="\r")
            
            return "waiting_for_entry_window"
        
        # CRITICAL: If we're past the entry window end, skip this market
        if seconds_until_close < ENTRY_WINDOW_END:
            if slug not in self.skipped_markets:
                print(f"\n⏰ Entry window closed! ({int(seconds_until_close)}s < {ENTRY_WINDOW_END}s)")
                print(f"   Skipping this market, waiting for next one...\n")
                
                self.skipped_markets.add(slug)
                
            return "entry_window_closed"
        
        print(f"\n{'='*60}")
        print(f"🎯 ARBITRAGE ENTRY WINDOW ACTIVE")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"Seconds until close: {int(seconds_until_close)}")
        
        # Get detailed order book information
        print(f"\n📊 Analyzing order books...")
        
        yes_book = self.get_order_book_depth(market['yes_token'])
        
        no_book = self.get_order_book_depth(market['no_token'])
        
        if not yes_book or not no_book:
            print("⚠️ Cannot get order book data, retrying...")
            
            return "no_orderbook"
        
        yes_ask = yes_book['best_ask']
        
        no_ask = no_book['best_ask']
        
        if not yes_ask or not no_ask:
            print("⚠️ Cannot get prices, retrying...")
            
            return "no_prices"
        
        print(f"\n📊 Current Market State:")
        print(f"   YES: ${yes_ask:.2f} (liquidity: {yes_book['ask_liquidity']:.2f})")
        print(f"   NO:  ${no_ask:.2f} (liquidity: {no_book['ask_liquidity']:.2f})")
        
        # Determine which side has lower ask (cheaper to buy)
        first_buy_side = None
        
        first_buy_token = None
        
        first_buy_price = None
        
        first_buy_book = None
        
        if yes_ask < no_ask and yes_ask < MAX_FIRST_BID:
            first_buy_side = "YES"
            
            first_buy_token = market['yes_token']
            
            first_buy_price = yes_ask
            
            first_buy_book = yes_book
            
        elif no_ask < yes_ask and no_ask < MAX_FIRST_BID:
            first_buy_side = "NO"
            
            first_buy_token = market['no_token']
            
            first_buy_price = no_ask
            
            first_buy_book = no_book
        
        # Fallback: if prices are equal and < MAX_FIRST_BID, choose YES
        elif yes_ask == no_ask and yes_ask < MAX_FIRST_BID:
            first_buy_side = "YES"
            
            first_buy_token = market['yes_token']
            
            first_buy_price = yes_ask
            
            first_buy_book = yes_book


        # Check if we have a qualifying first buy
        if not first_buy_side:
            print(f"⏳ No side qualifies (both > ${MAX_FIRST_BID:.2f}), waiting {RETRY_DELAY}s...")
            
            time.sleep(RETRY_DELAY)
            
            return "no_opportunity"
        
        # Check if there's enough liquidity (MUST be > POSITION_SIZE for FOK)
        if first_buy_book['ask_liquidity'] < POSITION_SIZE:
            print(f"⚠️ Insufficient liquidity on {first_buy_side}: {first_buy_book['ask_liquidity']:.2f} < {POSITION_SIZE}")
            print(f"   Waiting {RETRY_DELAY}s...")
            
            time.sleep(RETRY_DELAY)
            
            return "insufficient_liquidity"
        
        print(f"\n✅ First Buy Opportunity:")
        print(f"   Side: {first_buy_side}")
        print(f"   Price: ${first_buy_price:.2f}")
        print(f"   Available liquidity: {first_buy_book['ask_liquidity']:.2f} shares")
        
        # Determine second side and limit price
        second_buy_side = "NO" if first_buy_side == "YES" else "YES"
        
        second_buy_token = market['no_token'] if first_buy_side == "YES" else market['yes_token']
        
        # Recalculate based on current first_buy_price
        second_buy_price = round(PAIR_TARGET_COST - first_buy_price, 2)
        
        print(f"\n📋 Calculated Second Buy:")
        print(f"   Side: {second_buy_side}")
        print(f"   Limit Price: ${second_buy_price:.2f}")
        print(f"   Total Pair Cost: ${first_buy_price:.2f} + ${second_buy_price:.2f} = ${first_buy_price + second_buy_price:.2f}")
        
        # Balance Check REMOVED per user request
        
        # EXECUTE FIRST BUY ORDER (FOK/FAK Market Order)
        print(f"\n⚡ Executing FIRST BUY ORDER (FOK/FAK - {first_buy_side})...")
        print(f"   Target fill price: ${first_buy_price:.2f}")
        print(f"   Order limit price: ${min(first_buy_price + PRICE_IMPROVEMENT, 0.99):.2f} (with {PRICE_IMPROVEMENT:.2f} improvement)")
        
        first_order_id = self.place_market_order(first_buy_token, first_buy_price, POSITION_SIZE, BUY)
        
        if not first_order_id:
            print("\n❌ First FOK/FAK order failed! It was either not filled completely or immediately. Retrying...")
            
            time.sleep(RETRY_DELAY)
            
            return "first_order_failed"
        
        time.sleep(1) 
        
        # Give the exchange a moment to process the order
        
        # Verify first order fill price (FOK means it was fully filled if the ID exists)
        first_filled, actual_first_price = self.check_order_status(first_order_id)
        
        if not first_filled or not actual_first_price:
            # This should ideally not happen if place_market_order returned an ID, 
            # but serves as a safety check.
            print("⚠️ FOK/FAK order execution verification failed. Aborting trade cycle.")
            
            self.traded_markets.add(slug)
            
            return "first_order_failed"


        print(f"✅ FIRST ORDER FILLED (3 shares)!")
        
        first_order_time = time.time()
        
        print(f"   Actual fill: ${actual_first_price:.2f}")
        
        first_buy_price = actual_first_price
        
        # Recalculate second buy price based on actual fill
        second_buy_price = round(PAIR_TARGET_COST - first_buy_price, 2)
        
        # --- MINIMUM PRICE CHECK (Kept for safety) ---
        # Ensure the calculated price is never less than the absolute minimum price (0.01)
        if second_buy_price < 0.01:
            print(f"❌ Calculated second buy price (${second_buy_price:.2f}) is too low (min $0.01). Aborting trade cycle.")
            
            # Since the first position is open, we must sell it immediately to mitigate loss.
            current_bid = self.get_best_bid(first_buy_token)
            
            if current_bid:
                print(f"   Selling first position at current bid: ${current_bid:.2f} (FOK)")
                
                # Use current bid for market sell
                self.place_market_order(first_buy_token, current_bid, POSITION_SIZE, SELL)
            else:
                print("   WARNING: Could not sell first position due to no market bid. Manual intervention needed.")
            
            self.traded_markets.add(slug)
            
            return "second_price_too_low"
        # --- END MINIMUM PRICE CHECK ---
        
        print(f"   Adjusted second limit: ${second_buy_price:.2f}")
        
        # ==================================================
        # 🕑 FIXED 2-SECOND DELAY
        # ==================================================
        print("\n⏳ Delaying 2 seconds before placing second order...")
        time.sleep(2)
        print("✅ Delay finished. Placing second order.")
        # ==================================================
        
        # PLACE SECOND LIMIT ORDER (WITH PERSISTENT RETRY)
        print(f"\n📋 Placing SECOND LIMIT ORDER ({second_buy_side})...")
        
        second_order_id = None
        
        # Persistent retry loop starts here
        while second_order_id is None:
            
            # 1. Attempt to place the order
            second_order_id = self.place_limit_order(second_buy_token, second_buy_price, POSITION_SIZE, BUY)
            
            if second_order_id is None:
                # 2. If placement fails, check market time
                current_time = time.time()
                market_end_time = market_start_time + 900
                seconds_remaining = market_end_time - current_time

                if seconds_remaining < 10: 
                    # CRITICAL: Market closing soon, stop spamming and exit
                    print(f"CRITICAL: Limit order failed. Market closing in < 10s. Cannot place.")
                    break

                # 3. Print status and wait before retrying
                print(f"⚠️ Limit order failed to place. Retrying in {RETRY_DELAY}s... ({int(seconds_remaining)}s left)")
                time.sleep(RETRY_DELAY)

        if second_order_id is None:
            # If the loop broke without placing the order (likely due to market expiry)
            print("\n❌ Second limit order failed persistently. Could not place order.")
            print("⚠️ Unhedged position! Initiating emergency market sell of first side.")

            current_bid = self.get_best_bid(first_buy_token)
            if current_bid:
                print(f"   Selling first position at current bid: ${current_bid:.2f} (FOK)")
                self.place_market_order(first_buy_token, current_bid, POSITION_SIZE, SELL)
            else:
                print("   WARNING: Could not sell first position due to no market bid. Manual intervention needed.")
            
            self.traded_markets.add(slug)
            
            return "second_order_failed_persistent"

        print(f"✅ Second limit order placed successfully.")
        
        # MONITOR SECOND ORDER FOR 150 SECONDS
        print(f"\n⏱️ Monitoring second order for {SECOND_ORDER_TIMEOUT} seconds...")
        
        start_monitor = time.time()
        
        stop_loss_active_time = first_order_time + STOP_LOSS_DELAY
        
        stop_loss_price = max(first_buy_price - STOP_LOSS_OFFSET, 0.01)
        
        while True:
            elapsed = time.time() - start_monitor
            
            if elapsed >= SECOND_ORDER_TIMEOUT:
                print(f"\n⏰ Second order timeout ({SECOND_ORDER_TIMEOUT}s reached)")
                
                break
            
            # Check if second order filled
            second_filled, actual_second_price = self.check_order_status(second_order_id)
            
            if second_filled:
                print(f"\n\n🎉 ARBITRAGE COMPLETE!")
                print(f"{'='*60}")
                print(f"   First Buy:  {first_buy_side} @ ${first_buy_price:.2f}")
                print(f"   Second Buy: {second_buy_side} @ ${actual_second_price:.2f}")
                print(f"   Total Cost: ${(first_buy_price + actual_second_price) * POSITION_SIZE:.2f}")
                print(f"   Locked profit per pair: ${(1.00 - first_buy_price - actual_second_price):.2f}")
                print(f"   Total locked profit: ${((1.00 - first_buy_price - actual_second_price) * POSITION_SIZE):.2f}")
                print(f"{'='*60}")
                
                # Check slippage on second order
                slippage = abs(actual_second_price - second_buy_price)
                
                if slippage > MAX_SLIPPAGE_SECOND:
                    print(f"⚠️ Warning: Second order slippage ${slippage:.2f} exceeded ${MAX_SLIPPAGE_SECOND:.2f}")
                
                self.traded_markets.add(slug)
                
                return "arbitrage_complete"
            
            # Check stop loss (only after delay)
            time_until_sl = stop_loss_active_time - time.time()
            
            current_bid = self.get_best_bid(first_buy_token)
            
            if time_until_sl <= 0 and current_bid:
                if current_bid <= stop_loss_price:
                    print(f"\n\n🛑 STOP LOSS TRIGGERED at ${current_bid:.2f}!")
                    
                    self.cancel_order(second_order_id)
                    
                    time.sleep(1)
                    
                    print(f"   Selling {first_buy_side} position...")
                    
                    # Use a market order to sell immediately (FOK order)
                    self.place_market_order(first_buy_token, current_bid, POSITION_SIZE, SELL)
                    
                    loss = (first_buy_price - current_bid) * POSITION_SIZE
                    
                    print(f"   📉 Loss: -${loss:.2f}")
                    
                    self.traded_markets.add(slug)
                    
                    return "stop_loss"
            
            # Status update
            remaining = int(SECOND_ORDER_TIMEOUT - elapsed)
            
            if time_until_sl > 0:
                print(f"   ⏳ Waiting for second order fill... {remaining}s remaining | SL in {int(time_until_sl)}s", end="\r")
                
            else:
                print(f"   ⏳ Waiting for second order fill... {remaining}s remaining | SL: ${stop_loss_price:.2f} [ACTIVE] | Current: ${current_bid:.2f if current_bid else 'N/A'}", end="\r")
            
            time.sleep(CHECK_INTERVAL)
        
        # Second order didn't fill - cancel and execute stop loss
        print(f"\n\n⚠️ Second order not filled in {SECOND_ORDER_TIMEOUT}s")
        print(f"   Cancelling limit order...")
        
        self.cancel_order(second_order_id)
        
        time.sleep(1)
        
        print(f"   Executing stop loss sell on {first_buy_side}...")
        
        current_bid = self.get_best_bid(first_buy_token)
        
        if current_bid:
            # We use a market order with the best available bid as the limit to sell immediately
            sell_price = max(stop_loss_price, current_bid - 0.01) 
            
            self.place_market_order(first_buy_token, sell_price, POSITION_SIZE, SELL)
            
            # Estimate result based on current bid
            result = (current_bid - first_buy_price) * POSITION_SIZE
            
            if result > 0:
                print(f"   📊 Estimated Result: +${result:.2f} (from market sell)")
                
            else:
                print(f"   📊 Estimated Result: -${abs(result):.2f} (from market sell)")
                
        else:
            print("   ⚠️ Cannot get current bid for immediate sell. Manual intervention needed.")
        
        self.traded_markets.add(slug)
        
        return "second_order_timeout"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        
        print(f"📋 Strategy: Arbitrage Lock")
        print(f"   Entry Window: Between {ENTRY_WINDOW_START}s and {ENTRY_WINDOW_END}s remaining")
        print(f"   Entry: Buy lowest side < ${MAX_FIRST_BID:.2f} (FOK/FAK)")
        print(f"   Second: Limit buy other side @ ${PAIR_TARGET_COST:.2f} - first_price (GTC - Persistent Retry)")
        print(f"   Delay: 2 seconds between orders")
        print(f"   Position: {POSITION_SIZE} shares per side")
        print(f"   Price Improvement: +${PRICE_IMPROVEMENT:.2f} on market orders")
        print(f"   Second order timeout: {SECOND_ORDER_TIMEOUT}s")
        print(f"   Stop Loss: -${STOP_LOSS_OFFSET:.2f} (after {STOP_LOSS_DELAY}s)")
        print(f"   Max Slippage: ${MAX_SLIPPAGE_SECOND:.2f}\n")
        
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
                        print(f"   {current_market['title']}")
                        print(f"   Time Left: {time_left//60}m {time_left%60}s\n")
                        
                        # Clear skipped markets when new market starts
                        if current_market['slug'] not in self.skipped_markets:
                            self.skipped_markets.clear()
                            
                    else:
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        
                        wait_time = next_market_time - current_timestamp
                        
                        print(f"⏳ No active market. Next check in {wait_time}s")
                        
                        time.sleep(min(wait_time, 60))
                        
                        continue
                
                # Execute arbitrage strategy
                status = self.execute_arbitrage_strategy(current_market, market_timestamp)
                
                if status in ["arbitrage_complete", "stop_loss", "second_order_timeout", "second_price_too_low", "second_order_failed_persistent"]:
                    print("\n✅ Trade cycle complete! Waiting for next market...")
                    
                    time.sleep(10)
                    
                elif status == "already_traded":
                    next_market_time = ((current_timestamp // 900) + 1) * 900
                    
                    wait_time = max(next_market_time - int(time.time()), 5)
                    
                    print(f"\n⭐️ Already traded this market. Next market in {wait_time}s\n")
                    
                    time.sleep(wait_time)
                    
                elif status == "already_skipped":
                    # Market was skipped, wait for next one
                    next_market_time = ((current_timestamp // 900) + 1) * 900
                    
                    wait_time = max(next_market_time - int(time.time()), 5)
                    
                    time.sleep(min(wait_time, 30))
                    
                elif status == "entry_window_closed":
                    # Entry window closed, wait for next market
                    time.sleep(CHECK_INTERVAL)
                    
                elif status == "waiting_for_entry_window":
                    # Still waiting for entry window
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
    bot = BTCArbitrageBot()
    
    bot.run()