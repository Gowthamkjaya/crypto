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
# 🛑 USER CONFIGURATION
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
    SIGNATURE_TYPE = 1  # Use 1 for Magic Link / Email wallets
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# Manual Override (optional - leave empty for auto-detection)
MANUAL_SLUG = ""  # e.g., "btc-updown-15m-1765593000"

# Slug generation for BTC 15min markets
DAY_START = 1734048000  # Start of the day in UTC (adjust this to current day)
INTERVAL = 900  # 15 minutes in seconds

# Strategy Settings
MIN_ENTRY_PRICE = 0.60  # Don't enter below 60 cents (too risky)
MAX_ENTRY_PRICE = 0.95  # Don't enter above 95 cents (no profit room)
EXIT_SPREAD = 0.05      # Exit at +5 cents profit
ORDER_SIZE = 5.0        # Buy 5 shares per trade
CHECK_INTERVAL = 5      # Check every 5 seconds when market is active

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class BTCSniper:
    def __init__(self):
        print("🤖 BTC 15min Sniper Bot Starting...")
        
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
            # Silently skip markets that don't exist
            return None

    def generate_todays_slugs(self):
        """Generate all possible BTC 15min slugs for today"""
        # Get start of today in UTC
        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        day_start_timestamp = int(today_start.timestamp())
        
        # Generate 96 slugs (24 hours * 4 per hour)
        slugs = [
            f"btc-updown-15m-{day_start_timestamp + i * INTERVAL}"
            for i in range(96)
        ]
        return slugs

    def find_active_market(self):
        """Find the currently active 15min BTC market based on UTC time"""
        now_utc = datetime.now(timezone.utc)
        current_timestamp = int(now_utc.timestamp())
        
        print(f"🕐 Current UTC Time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"🔍 Searching for active market at timestamp: {current_timestamp}")
        
        # Generate today's slugs
        slugs = self.generate_todays_slugs()
        
        # Find the active market (current 15min window)
        # Market is active if: market_time <= current_time < market_time + 900
        active_markets = []
        
        for slug in slugs:
            market_timestamp = int(slug.split('-')[-1])
            market_end = market_timestamp + 900  # 15 minutes later
            
            # Check if this market is currently active
            if market_timestamp <= current_timestamp < market_end:
                market = self.get_market_from_slug(slug)
                if market:
                    time_left = market_end - current_timestamp
                    print(f"✅ ACTIVE MARKET FOUND!")
                    print(f"   Start: {datetime.fromtimestamp(market_timestamp, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                    print(f"   End: {datetime.fromtimestamp(market_end, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                    print(f"   Time Left: {time_left}s ({time_left//60}m {time_left%60}s)")
                    active_markets.append(market)
        
        if active_markets:
            return active_markets[0]
        
        # If no active market, find the next upcoming one
        print("⏳ No active market right now, checking for upcoming markets...")
        
        for slug in slugs:
            market_timestamp = int(slug.split('-')[-1])
            
            if market_timestamp > current_timestamp:
                market = self.get_market_from_slug(slug)
                if market:
                    wait_time = market_timestamp - current_timestamp
                    print(f"📅 NEXT MARKET:")
                    print(f"   Starts: {datetime.fromtimestamp(market_timestamp, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                    print(f"   Wait Time: {wait_time}s ({wait_time//60}m {wait_time%60}s)")
                    return None  # Don't trade yet
        
        print("❌ No markets found for today")
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

    def place_order(self, token_id, price, size, side):
        """Place a market order using official Polymarket API"""
        try:
            # Round price to nearest cent (0.01)
            price = round(price, 2)
            
            print(f"   🔧 Placing order: {size} shares @ ${price}")
            print(f"   🔧 Token: {token_id[:16]}...")
            print(f"   🔧 Side: {side}")
            
            # Use official post_orders method (plural)
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=self.client.create_order(OrderArgs(
                        price=price,
                        size=size,
                        side=side,
                        token_id=token_id,
                    )),
                    orderType=OrderType.FOK,  # Fill or Kill for immediate execution
                )
            ])
            
            print(f"   🔧 Response: {resp}")
            
            # Check response
            if resp and len(resp) > 0:
                order_result = resp[0]
                print(f"   🔧 Order result: {order_result}")
                
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

    def place_limit_order(self, token_id, price, size):
        """Place a limit sell order (exit) using official API"""
        try:
            # Round price to nearest cent (0.01)
            price = round(price, 2)
            
            print(f"   🔧 Placing limit sell: {size} shares @ ${price}")
            
            # Use official post_orders method
            resp = self.client.post_orders([
                PostOrdersArgs(
                    order=self.client.create_order(OrderArgs(
                        price=price,
                        size=size,
                        side=SELL,
                        token_id=token_id,
                    )),
                    orderType=OrderType.GTC,  # Good-til-cancelled for limit orders
                )
            ])
            
            print(f"   🔧 Exit response: {resp}")
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                print(f"   🔧 Exit result: {order_result}")
                
                if order_result.get('success') or order_result.get('orderID'):
                    return order_result.get('orderID', 'success')
                else:
                    error_msg = order_result.get('errorMsg') or str(order_result)
                    print(f"   ⚠️ Exit order failed: {error_msg}")
                    return None
            else:
                print(f"   ⚠️ Empty response")
                return None
                
        except Exception as e:
            print(f"   ❌ Exit order error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def monitor_prices_and_trade(self, market):
        """Monitor prices for a market and trade when conditions are met"""
        slug = market['slug']
        
        # Check if already traded
        if slug in self.traded_markets:
            return "already_traded"
        
        # Get current prices
        yes_price = self.get_best_ask(market['yes_token'])
        no_price = self.get_best_ask(market['no_token'])
        
        if not yes_price or not no_price:
            return "no_prices"
        
        # Determine the more expensive side (majority)
        if yes_price >= no_price:
            entry_side = "YES"
            entry_token = market['yes_token']
            entry_price = yes_price
        else:
            entry_side = "NO"
            entry_token = market['no_token']
            entry_price = no_price
        
        # Show current status (compact)
        print(f"📊 YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Target: {entry_side} @ ${entry_price:.2f}", end="\r")
        
        # Check if price is in acceptable range
        if entry_price < MIN_ENTRY_PRICE or entry_price > MAX_ENTRY_PRICE:
            return "price_out_of_range"
        
        # Price is good! Execute trade
        print(f"\n\n{'='*60}")
        print(f"🎯 TRADE OPPORTUNITY!")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"📊 Current Prices:")
        print(f"   YES: ${yes_price:.2f}")
        print(f"   NO:  ${no_price:.2f}")
        print(f"   Sum: ${yes_price + no_price:.2f}")
        print(f"🎯 Selected Side: {entry_side} at ${entry_price:.2f}")
        
        # Check balance
        balance = self.get_balance()
        required = entry_price * ORDER_SIZE
        
        print(f"\n💰 Balance: ${balance:.2f} USDC")
        print(f"💵 Required: ${required:.2f} USDC")
        
        if balance < required:
            print("❌ Insufficient funds")
            self.traded_markets.add(slug)
            return "insufficient_funds"
        
        # Calculate exit price
        exit_price = min(entry_price + EXIT_SPREAD, 0.99)
        
        print(f"\n🎯 TRADE PLAN:")
        print(f"   Side: {entry_side}")
        print(f"   Entry: ${entry_price:.2f}")
        print(f"   Size: {ORDER_SIZE} shares")
        print(f"   Exit: ${exit_price:.2f} (+${EXIT_SPREAD:.2f})")
        print(f"   Potential Profit: ${(exit_price - entry_price) * ORDER_SIZE:.2f}")
        
        # Execute entry
        print(f"\n⚡ Placing ENTRY order...")
        entry_id = self.place_order(entry_token, entry_price, ORDER_SIZE, BUY)
        
        if not entry_id:
            print("❌ Entry failed")
            return "entry_failed"
        
        print(f"✅ ENTRY FILLED! Order ID: {entry_id}")
        
        # Persistently try to place exit order
        print(f"\n⚡ Placing EXIT order at ${exit_price:.2f}...")
        
        exit_attempts = 0
        max_attempts = 20  # Try for 1 minute (20 attempts * 3 seconds)
        exit_id = None
        
        while exit_attempts < max_attempts and not exit_id:
            exit_id = self.place_limit_order(entry_token, exit_price, ORDER_SIZE)
            
            if exit_id:
                print(f"✅ EXIT ORDER PLACED! Order ID: {exit_id}")
                print(f"📌 Will sell when price reaches ${exit_price:.2f}")
                break
            else:
                exit_attempts += 1
                if exit_attempts < max_attempts:
                    print(f"   ⏳ Retry {exit_attempts}/{max_attempts} - waiting 3s for shares to settle...")
                    time.sleep(3)
                else:
                    print("⚠️ Exit order failed after all attempts - you'll need to sell manually")
        
        # Mark as traded
        self.traded_markets.add(slug)
        print(f"\n✅ Trade complete!\n")
        
        return "traded"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"📋 Strategy:")
        print(f"   - Buy side: MORE EXPENSIVE (majority)")
        print(f"   - Price range: ${MIN_ENTRY_PRICE:.2f} - ${MAX_ENTRY_PRICE:.2f}")
        print(f"   - Position size: {ORDER_SIZE} shares")
        print(f"   - Exit target: +${EXIT_SPREAD:.2f}")
        print(f"   - Price check: Every {CHECK_INTERVAL}s during active market\n")
        
        current_market = None
        
        while True:
            try:
                # Find what market should be active now
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
                    else:
                        current_market = self.get_market_from_slug(expected_slug)
                    
                    if current_market:
                        market_end = market_timestamp + 900
                        time_left = market_end - current_timestamp
                        print(f"✅ Active Market Found!")
                        print(f"   {current_market['title']}")
                        print(f"   Time Left: {time_left//60}m {time_left%60}s\n")
                    else:
                        # Market doesn't exist yet - wait for next window
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        wait_time = next_market_time - current_timestamp
                        
                        print(f"⏳ No active market. Next check in {wait_time}s")
                        print(f"   Next window: {datetime.fromtimestamp(next_market_time, tz=timezone.utc).strftime('%H:%M:%S')} UTC\n")
                        
                        time.sleep(min(wait_time, 60))
                        continue
                
                # Monitor prices and trade if conditions are met
                status = self.monitor_prices_and_trade(current_market)
                
                if status == "traded":
                    print("✅ Trade executed! Waiting for next market...")
                    time.sleep(10)
                elif status == "already_traded":
                    # Wait for next market window
                    next_market_time = ((current_timestamp // 900) + 1) * 900
                    wait_time = max(next_market_time - int(time.time()), 5)
                    print(f"\n⏭️  Already traded this market. Next market in {wait_time}s\n")
                    time.sleep(wait_time)
                else:
                    # Keep monitoring prices
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
    bot = BTCSniper()
    bot.run()