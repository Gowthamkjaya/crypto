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
from pathlib import Path

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
MIN_ENTRY_PRICE = 0.50  # CHANGED: Enter between 50-70 cents (more room to profit)
MAX_ENTRY_PRICE = 0.70  # CHANGED: Avoid expensive positions
EXIT_SPREAD = 0.10      # CHANGED: Target +10 cents profit (better risk/reward)
STOP_LOSS_SPREAD = 0.08 # CHANGED: Allow -8 cents loss (asymmetric risk/reward favors you)
ORDER_SIZE = 5.0        # Buy 5 shares per trade
CHECK_INTERVAL = 2      # Check every 2 seconds

# NEW: Safety limits to prevent bad exits
MAX_ACCEPTABLE_SLIPPAGE = 0.05  # If entry slips more than 5 cents, abort trade
MIN_PROFIT_MARGIN = 0.05        # CHANGED: Minimum 5 cent profit required

# NEW: BTC Price Movement Filter
MIN_BTC_DISTANCE = 30.0  # Only trade if BTC is $30+ away from strike price
BTC_PRICE_API = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# Trade History CSV File
TRADE_HISTORY_FILE = "trade_history.csv"

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
        
        # Initialize trade history CSV
        self.init_trade_history()
        
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

    def init_trade_history(self):
        """Initialize CSV file for trade history"""
        file_exists = Path(TRADE_HISTORY_FILE).exists()
        
        if not file_exists:
            with open(TRADE_HISTORY_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'Timestamp',
                    'Market',
                    'Side',
                    'Intended Entry',
                    'Actual Entry',
                    'Slippage',
                    'Position Size',
                    'Take Profit',
                    'Stop Loss',
                    'Exit Price',
                    'Exit Type',
                    'P&L',
                    'P&L %',
                    'Status',
                    'BTC Price',
                    'Strike Price',
                    'BTC Distance'
                ])
            print(f"📊 Created trade history file: {TRADE_HISTORY_FILE}")
        else:
            print(f"📊 Using existing trade history: {TRADE_HISTORY_FILE}")
    
    def log_trade(self, trade_data):
        """Log trade to CSV file"""
        try:
            with open(TRADE_HISTORY_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade_data.get('timestamp', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')),
                    trade_data.get('market', ''),
                    trade_data.get('side', ''),
                    trade_data.get('intended_entry', 0),
                    trade_data.get('actual_entry', 0),
                    trade_data.get('slippage', 0),
                    trade_data.get('position_size', 0),
                    trade_data.get('take_profit', 0),
                    trade_data.get('stop_loss', 0),
                    trade_data.get('exit_price', 0),
                    trade_data.get('exit_type', ''),
                    trade_data.get('pnl', 0),
                    trade_data.get('pnl_pct', 0),
                    trade_data.get('status', ''),
                    trade_data.get('btc_price', 0),
                    trade_data.get('strike_price', 0),
                    trade_data.get('btc_distance', 0)
                ])
            print(f"📝 Trade logged to {TRADE_HISTORY_FILE}")
        except Exception as e:
            print(f"⚠️ Failed to log trade: {e}")

    def get_best_bid(self, token_id):
        """Get best available buying price (price we can sell into)"""
        try:
            book = self.client.get_order_book(token_id)
            if book.bids:
                # Return the highest price someone is willing to pay
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
            
            # Extract strike price from market title or description
            # Example title: "Will BTC be above $100,000 at 3:15 PM?"
            strike_price = None
            try:
                title = event.get('title', '')
                # Look for price pattern like "$100,000" or "$100000"
                import re
                price_match = re.search(r'\$([0-9,]+)', title)
                if price_match:
                    strike_price = float(price_match.group(1).replace(',', ''))
            except:
                pass
            
            return {
                'slug': slug,
                'yes_token': clob_ids[0],
                'no_token': clob_ids[1],
                'title': event.get('title', slug),
                'strike_price': strike_price
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

    def get_current_btc_price(self):
        """Get current BTC price from Binance"""
        try:
            resp = requests.get(BTC_PRICE_API, timeout=5)
            data = resp.json()
            return float(data['price'])
        except Exception as e:
            print(f"⚠️ Could not fetch BTC price: {e}")
            return None

    def get_actual_fill_price(self, order_id, max_retries=10):
        """
        🆕 CRITICAL: Get the ACTUAL price the order was filled at
        This prevents using intended price when actual price differs
        """
        print(f"   🔍 Fetching actual fill price for order {order_id}...")
        
        for attempt in range(max_retries):
            try:
                time.sleep(1)  # Wait for order to settle
                
                # Get order details from API
                order_details = self.client.get_order(order_id)
                
                if isinstance(order_details, dict):
                    # Check if order is filled
                    status = order_details.get('status', '')
                    
                    if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                        # Try to get actual fill price from different possible fields
                        actual_price = None
                        
                        # Method 1: Direct price field
                        if 'price' in order_details:
                            actual_price = float(order_details['price'])
                        
                        # Method 2: Average fill price
                        elif 'avgFillPrice' in order_details:
                            actual_price = float(order_details['avgFillPrice'])
                        
                        # Method 3: From trades array
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
        """Monitor prices, trade, and manage position (TP/SL)"""
        slug = market['slug']
        
        # Initialize trade data for logging
        trade_data = {
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'market': market['title'],
            'position_size': ORDER_SIZE,
            'strike_price': market.get('strike_price', 0)
        }
        
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
            intended_entry_price = yes_price
        else:
            entry_side = "NO"
            entry_token = market['no_token']
            intended_entry_price = no_price
        
        trade_data['side'] = entry_side
        trade_data['intended_entry'] = intended_entry_price
        
        # Show current status (compact)
        print(f"📊 YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Target: {entry_side} @ ${intended_entry_price:.2f}", end="\r")
        
        # Check if price is in acceptable range (0.80 - 0.95)
        if intended_entry_price < MIN_ENTRY_PRICE or intended_entry_price > MAX_ENTRY_PRICE:
            return "price_out_of_range"
        
        # ---------------------------------------------------------
        # 🆕 BTC PRICE DISTANCE CHECK
        # ---------------------------------------------------------
        if market.get('strike_price'):
            current_btc = self.get_current_btc_price()
            
            if current_btc:
                btc_distance = abs(current_btc - market['strike_price'])
                trade_data['btc_price'] = current_btc
                trade_data['btc_distance'] = btc_distance
                
                print(f"\n📍 BTC Check: Current ${current_btc:,.2f} | Strike ${market['strike_price']:,.2f} | Distance ${btc_distance:.2f}", end="\r")
                
                if btc_distance < MIN_BTC_DISTANCE:
                    # Not enough movement, skip this opportunity
                    return "insufficient_btc_movement"
            else:
                print(f"\n⚠️ Could not verify BTC price distance", end="\r")
        
        # ---------------------------------------------------------
        # 🎯 EXECUTE ENTRY
        # ---------------------------------------------------------
        print(f"\n\n{'='*60}")
        print(f"🎯 TRADE OPPORTUNITY FOUND!")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        
        if market.get('strike_price'):
            current_btc = self.get_current_btc_price()
            if current_btc:
                btc_distance = abs(current_btc - market['strike_price'])
                print(f"📍 BTC Distance: ${btc_distance:.2f} (Current: ${current_btc:,.2f}, Strike: ${market['strike_price']:,.2f})")
        
        # Check balance
        balance = self.get_balance()
        required = intended_entry_price * ORDER_SIZE
        
        if balance < required:
            print(f"❌ Insufficient funds. Need ${required:.2f}, have ${balance:.2f}")
            self.traded_markets.add(slug)
            trade_data['status'] = 'FAILED - Insufficient Funds'
            self.log_trade(trade_data)
            return "insufficient_funds"
        
        print(f"📉 Intended Entry: ${intended_entry_price:.2f}")

        # Execute entry
        print(f"\n⚡ Placing ENTRY order...")
        entry_id = self.place_order(entry_token, intended_entry_price, ORDER_SIZE, BUY)
        
        if not entry_id:
            print("❌ Entry failed")
            trade_data['status'] = 'FAILED - Entry Order Failed'
            self.log_trade(trade_data)
            return "entry_failed"
        
        print(f"✅ ENTRY ORDER PLACED! Order ID: {entry_id}")
        
        # ---------------------------------------------------------
        # 🆕 CRITICAL: GET ACTUAL FILL PRICE
        # ---------------------------------------------------------
        print(f"\n🔍 Verifying actual fill price...")
        actual_entry_price = self.get_actual_fill_price(entry_id)
        
        if not actual_entry_price:
            print(f"⚠️ Could not verify fill price, using fallback method...")
            # Fallback: Use current best bid as approximation
            time.sleep(2)
            actual_entry_price = self.get_best_bid(entry_token)
            
            if not actual_entry_price:
                print("❌ Critical: Cannot determine entry price. Aborting trade.")
                # Try to sell immediately at market
                self.place_order(entry_token, 0.0, ORDER_SIZE, SELL)
                trade_data['status'] = 'FAILED - Could Not Verify Entry Price'
                self.log_trade(trade_data)
                return "entry_failed"
        
        # Update trade data with actual entry
        trade_data['actual_entry'] = actual_entry_price
        trade_data['slippage'] = abs(actual_entry_price - intended_entry_price)
        
        # Calculate slippage
        slippage = abs(actual_entry_price - intended_entry_price)
        print(f"\n📊 ENTRY ANALYSIS:")
        print(f"   Intended: ${intended_entry_price:.4f}")
        print(f"   Actual:   ${actual_entry_price:.4f}")
        print(f"   Slippage: ${slippage:.4f} ({(slippage/intended_entry_price)*100:.2f}%)")
        
        # ---------------------------------------------------------
        # 🛡️ SLIPPAGE SAFETY CHECK
        # ---------------------------------------------------------
        if slippage > MAX_ACCEPTABLE_SLIPPAGE:
            print(f"\n🚨 EXCESSIVE SLIPPAGE DETECTED!")
            print(f"   Slippage (${slippage:.4f}) exceeds limit (${MAX_ACCEPTABLE_SLIPPAGE:.2f})")
            print(f"   Exiting trade immediately to prevent losses...")
            
            # Sell at current market price
            current_bid = self.get_best_bid(entry_token)
            if current_bid:
                self.place_order(entry_token, current_bid - 0.01, ORDER_SIZE, SELL)
            else:
                self.place_order(entry_token, actual_entry_price - 0.02, ORDER_SIZE, SELL)
            
            self.traded_markets.add(slug)
            trade_data['status'] = 'FAILED - Excessive Slippage'
            trade_data['exit_type'] = 'Emergency Exit'
            self.log_trade(trade_data)
            return "excessive_slippage"
        
        # ---------------------------------------------------------
        # 🎯 CALCULATE TP/SL BASED ON ACTUAL ENTRY
        # ---------------------------------------------------------
        tp_price = min(actual_entry_price + EXIT_SPREAD, 0.99)
        sl_price = max(actual_entry_price - STOP_LOSS_SPREAD, 0.01)
        
        # Ensure minimum profit margin
        profit_margin = tp_price - actual_entry_price
        if profit_margin < MIN_PROFIT_MARGIN:
            print(f"\n⚠️ WARNING: Profit margin (${profit_margin:.4f}) below minimum (${MIN_PROFIT_MARGIN:.2f})")
            print(f"   Adjusting TP to ensure minimum profit...")
            tp_price = min(actual_entry_price + MIN_PROFIT_MARGIN, 0.99)
        
        # Update trade data
        trade_data['take_profit'] = tp_price
        trade_data['stop_loss'] = sl_price
        
        print(f"\n📈 EXIT TARGETS (Based on ACTUAL entry ${actual_entry_price:.4f}):")
        print(f"   🚀 Take Profit: ${tp_price:.4f} (+${tp_price - actual_entry_price:.4f})")
        print(f"   🛡️ Stop Loss: ${sl_price:.4f} (-${actual_entry_price - sl_price:.4f})")

        # ---------------------------------------------------------
        # ⚡ PLACE TAKE PROFIT (LIMIT ORDER)
        # ---------------------------------------------------------
        print(f"\n⚡ Placing TAKE PROFIT (Limit Sell) at ${tp_price:.2f}...")
        
        # Retry logic for placing the exit order (shares take a second to settle)
        exit_id = None
        for i in range(5):
            exit_id = self.place_limit_order(entry_token, tp_price, ORDER_SIZE)
            if exit_id: 
                break
            time.sleep(2)

        if not exit_id:
            print("❌ Critical: Could not place Take Profit order. Selling manually now.")
            self.place_order(entry_token, 0.0, ORDER_SIZE, SELL) # Dump
            trade_data['status'] = 'FAILED - Could Not Place TP'
            trade_data['exit_type'] = 'Emergency Exit'
            self.log_trade(trade_data)
            return "exit_failed"

        # ---------------------------------------------------------
        # 🛡️ ACTIVE MONITORING (TP vs SL)
        # ---------------------------------------------------------
        print(f"\n👀 Monitoring position for TP or SL...")
        print(f"   Exit if Price >= ${tp_price:.2f} (Profit)")
        print(f"   Exit if Price <= ${sl_price:.2f} (Loss)")

        exit_occurred = False
        exit_price = 0
        exit_type = ""
        
        while True:
            time.sleep(2) # Check every 2 seconds

            # 1. Check if Take Profit was hit (Order filled)
            try:
                # Note: This checks the order status via API
                order_state = self.client.get_order(exit_id)
                status = order_state.get('status') if isinstance(order_state, dict) else order_state
                
                # 'MATCHED' or 'FILLED' means we sold at profit
                if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                    print(f"\n💰 TAKE PROFIT HIT! Sold at ${tp_price:.2f}")
                    print(f"   Entry: ${actual_entry_price:.4f}")
                    print(f"   Exit: ${tp_price:.2f}")
                    pnl = (tp_price - actual_entry_price) * ORDER_SIZE
                    print(f"   Profit: +${pnl:.2f}")
                    
                    exit_price = tp_price
                    exit_type = "Take Profit"
                    exit_occurred = True
                    break
            except Exception as e:
                pass # API glitch, keep monitoring

            # 2. Check current market price for Stop Loss
            current_bid = self.get_best_bid(entry_token)
            
            if current_bid:
                print(f"   Current: ${current_bid:.2f} | Entry: ${actual_entry_price:.2f} | SL: ${sl_price:.2f}", end="\r")

                if current_bid <= sl_price:
                    print(f"\n\n🛑 STOP LOSS TRIGGERED at ${current_bid:.2f}!")
                    
                    # A. Cancel the Limit Sell (Take Profit) order
                    print("   1. Canceling Take Profit order...")
                    try:
                        self.client.cancel(exit_id)
                        time.sleep(1) # Wait for cancel to propagate
                    except Exception as e:
                        print(f"   ⚠️ Cancel failed (might be already filled): {e}")

                    # B. Market Sell immediately AT CURRENT BID (not lower!)
                    print("   2. Executing Market Sell...")
                    # FIX: Sell at current bid or slightly above to ensure fill
                    sell_price = max(current_bid, sl_price - 0.01)  # Don't sell way below SL!
                    self.place_order(entry_token, sell_price, ORDER_SIZE, SELL)
                    
                    loss = (actual_entry_price - current_bid) * ORDER_SIZE
                    print(f"   📉 Position closed.")
                    print(f"   Entry: ${actual_entry_price:.4f}")
                    print(f"   Exit: ${sell_price:.2f}")  # Show actual sell price
                    print(f"   Loss: -${loss:.2f}")
                    
                    exit_price = sell_price  # Use actual sell price for logging
                    exit_type = "Stop Loss"
                    exit_occurred = True
                    break
            else:
                print("   ⚠️ Could not fetch price data...", end="\r")

        # ---------------------------------------------------------
        # 📊 LOG TRADE TO CSV
        # ---------------------------------------------------------
        if exit_occurred:
            pnl = (exit_price - actual_entry_price) * ORDER_SIZE
            pnl_pct = ((exit_price - actual_entry_price) / actual_entry_price) * 100
            
            trade_data['exit_price'] = exit_price
            trade_data['exit_type'] = exit_type
            trade_data['pnl'] = pnl
            trade_data['pnl_pct'] = pnl_pct
            trade_data['status'] = 'WIN' if pnl > 0 else 'LOSS'
            
            self.log_trade(trade_data)
        
        # Mark as traded
        self.traded_markets.add(slug)
        print(f"\n✅ Trade cycle complete!\n")
        
        return "traded"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"📋 Strategy:")
        print(f"   - Buy side: MORE EXPENSIVE (majority)")
        print(f"   - Price range: ${MIN_ENTRY_PRICE:.2f} - ${MAX_ENTRY_PRICE:.2f}")
        print(f"   - Position size: {ORDER_SIZE} shares")
        print(f"   - Take Profit: +${EXIT_SPREAD:.2f}")
        print(f"   - Stop Loss: -${STOP_LOSS_SPREAD:.2f}")
        print(f"   - Max Slippage: ${MAX_ACCEPTABLE_SLIPPAGE:.2f}")
        print(f"   - Min Profit Margin: ${MIN_PROFIT_MARGIN:.2f}")
        print(f"   - Min BTC Distance: ${MIN_BTC_DISTANCE:.2f}")
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