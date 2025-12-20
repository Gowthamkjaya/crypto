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
print(f"📍 Polymarket shows: {POLYMARKET_ADDRESS}")

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
# 🎯 MID-GAME STRATEGY SETTINGS
# ==========================================
# Entry when 5-10 min remaining
MG_LOCK_WINDOW_START = 300  # Start at 5 minutes remaining
MG_LOCK_WINDOW_END = 600    # End at 10 minutes remaining

# Entry criteria
MG_MIN_ENTRY_PRICE = 0.80   # Minimum entry price
MG_MAX_ENTRY_PRICE = 0.84   # Maximum entry price (strictly avoid >0.90)
MG_MIN_BID_SIZE = 300       # Minimum liquidity required

# Position sizing
MG_WALLET_PERCENTAGE = 0.50  # Use 50% of wallet balance for each trade (0.50 = 50%)

# Exit Settings - SIMPLE
MG_TAKE_PROFIT = 0.95       # Take profit at $0.95
MG_STOP_LOSS = 0.62         # Stop loss at $0.70

# System settings
CHECK_INTERVAL = 2          # Check every 2 seconds
MIN_ORDER_SIZE = 0.1        # Minimum order size

# Trade Logging
TRADE_LOG_FILE = "polymarket_trades.csv"
ENABLE_EXCEL = True  # Set to True to also generate Excel files

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class SimpleMidGameBot:
    def __init__(self):
        print("🤖 Simple Mid-Game Trading Bot Starting...")
        print("📉 TRADING NO (DOWN) ONLY")
        
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
            
        # Track markets
        self.traded_markets = set()
        
        # Session tracking
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        
        # Trade logging
        self.trade_logs = []
        self.initialize_trade_log()

    def initialize_trade_log(self):
        """Initialize CSV file with headers if it doesn't exist"""
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'entry_side', 'entry_time', 'intended_entry_price', 'actual_entry_price',
                'entry_size', 'actual_shares_purchased', 'yes_price_at_entry', 'no_price_at_entry', 
                'time_remaining_at_entry', 'bid_size_at_entry',
                'exit_reason', 'exit_time', 'exit_price', 'time_in_trade_seconds',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'session_trade_number', 'balance_before', 'balance_after', 'session_pnl_running'
            ]
            
            with open(TRADE_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log initialized: {TRADE_LOG_FILE}")

    def log_trade(self, trade_data):
        """Append trade data to CSV and optionally Excel"""
        try:
            # Add to in-memory list
            self.trade_logs.append(trade_data)
            
            # Append to CSV
            with open(TRADE_LOG_FILE, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
            
            # Generate Excel if enabled
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
            time.sleep(0.1)
            book = self.client.get_order_book(token_id)
            if book.asks:
                return min(float(o.price) for o in book.asks)
            return None
        except:
            return None

    def get_best_bid(self, token_id):
        """Get best available selling price"""
        try:
            time.sleep(0.1)
            book = self.client.get_order_book(token_id)
            if book.bids:
                return max(float(o.price) for o in book.bids)
            return None
        except:
            return None

    def get_order_book_depth(self, token_id):
        """Get detailed order book information including bid size"""
        try:
            time.sleep(0.1)
            book = self.client.get_order_book(token_id)
            
            best_ask = min(float(o.price) for o in book.asks) if book.asks else None
            best_bid = max(float(o.price) for o in book.bids) if book.bids else None
            
            # Calculate total bid size (liquidity available to sell into)
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

    def place_market_order(self, token_id, price, size, side):
        """Place a Fill-or-Kill (FOK) market order"""
        try:
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
            # Query the user's balance for this specific token
            balance = self.client.get_balance(token_id)
            if balance:
                return float(balance)
            return 0.0
        except Exception as e:
            print(f"   ⚠️ Error getting position size: {e}")
            return 0.0

    def execute_trade(self, market, market_start_time):
        """Execute simple mid-game lock strategy - NO (DOWN) ONLY"""
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
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
        print(f"📊 [{minutes_remaining}m {seconds_remaining}s] YES: ${yes_price:.2f} (Bids: {yes_book['bid_size']:.0f}) | NO: ${no_price:.2f} (Bids: {no_book['bid_size']:.0f})", end="\r")
        
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
        
        # Round to 2 decimal places and check minimum
        order_size = round(order_size, 2)
        
        if order_size < MIN_ORDER_SIZE:
            print(f"\n⚠️ Calculated order size {order_size:.2f} is below minimum {MIN_ORDER_SIZE}")
            return "insufficient_balance"
        
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
        }
        
        # Execute entry order
        entry_start_time = time.time()
        trade_data['entry_time'] = datetime.fromtimestamp(entry_start_time).isoformat()
        
        print(f"\n⚡ Executing ENTRY order...")
        entry_id = self.place_market_order(entry_token, entry_price, order_size, BUY)
        
        if not entry_id:
            print("❌ Entry failed")
            return "entry_failed"
        
        # Verify entry fill and get ACTUAL position size
        time.sleep(2)
        filled, actual_entry_price = self.check_order_status(entry_id)
        
        if not filled or not actual_entry_price:
            print(f"⚠️ Could not verify entry fill")
            return "entry_failed"
        
        # Get the ACTUAL number of shares we purchased (may differ from order size)
        time.sleep(1)
        actual_shares_purchased = self.get_actual_position_size(entry_token)
        
        if actual_shares_purchased <= 0:
            print(f"⚠️ Could not determine actual position size, using order size as fallback")
            actual_shares_purchased = MG_ORDER_SIZE
        
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
            
            # Check Take Profit
            if current_bid >= MG_TAKE_PROFIT:
                print(f"\n\n🚀 TAKE PROFIT TRIGGERED @ ${current_bid:.2f}!")
                
                # Execute exit with ACTUAL share count
                exit_id = self.place_market_order(entry_token, current_bid, actual_shares_purchased, SELL)
                
                if exit_id:
                    time.sleep(2)
                    exit_filled, exit_price = self.check_order_status(exit_id)
                    
                    if exit_filled and exit_price:
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
                        self.traded_markets.add(slug)
                        
                        print(f"✅ EXIT FILLED @ ${exit_price:.2f}")
                        print(f"📦 Shares sold: {actual_shares_purchased:.2f}")
                        print(f"💰 P&L: ${trade_data['gross_pnl']:+.2f} ({trade_data['pnl_percent']:+.2f}%)")
                        return "take_profit"
            
            # Check Stop Loss
            if current_bid <= MG_STOP_LOSS:
                print(f"\n\n🛑 STOP LOSS TRIGGERED @ ${current_bid:.2f}!")
                
                # Execute exit with ACTUAL share count
                exit_id = self.place_market_order(entry_token, current_bid, actual_shares_purchased, SELL)
                
                if exit_id:
                    time.sleep(2)
                    exit_filled, exit_price = self.check_order_status(exit_id)
                    
                    if exit_filled and exit_price:
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
                        self.traded_markets.add(slug)
                        
                        print(f"✅ EXIT FILLED @ ${exit_price:.2f}")
                        print(f"📦 Shares sold: {actual_shares_purchased:.2f}")
                        print(f"💰 P&L: ${trade_data['gross_pnl']:+.2f} ({trade_data['pnl_percent']:+.2f}%)")
                        return "stop_loss"

    def run(self):
        """Main bot loop"""
        print(f"🚀 Bot is now running...")
        print(f"\n📋 STRATEGY CONFIGURATION:")
        print(f"   Trading Side: NO (DOWN) ONLY")
        print(f"   Entry Window: {MG_LOCK_WINDOW_START}s to {MG_LOCK_WINDOW_END}s remaining")
        print(f"   Entry Price Range: ${MG_MIN_ENTRY_PRICE:.2f} - ${MG_MAX_ENTRY_PRICE:.2f}")
        print(f"   Minimum Bid Size: {MG_MIN_BID_SIZE} shares")
        print(f"   Position Size: {MG_WALLET_PERCENTAGE*100:.0f}% of wallet balance")
        print(f"   Take Profit: ${MG_TAKE_PROFIT:.2f}")
        print(f"   Stop Loss: ${MG_STOP_LOSS:.2f}")
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
                
                # Execute strategy
                status = self.execute_trade(current_market, market_timestamp)
                
                # Handle results
                if status in ["take_profit", "stop_loss"]:
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
                
                if status == "already_traded":
                    next_market_time = ((current_timestamp // 900) + 1) * 900
                    wait_time = max(next_market_time - int(time.time()), 5)
                    print(f"\n⏭️ Trade complete. Next market in {wait_time}s\n")
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
                print(f"\n📊 Trade log saved: {TRADE_LOG_FILE}")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = SimpleMidGameBot()
    bot.run()
