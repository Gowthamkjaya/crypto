import os
import time
import requests
import json
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime, timezone
import csv
import pandas as pd

# ==========================================
# 🔧 CONFIGURATION
# ==========================================

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("❌ PRIVATE_KEY not found in environment variables!")

POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Strategy Settings
MG_LOCK_WINDOW_START = 300     # Start at 5 minutes remaining
MG_LOCK_WINDOW_END = 600       # End at 10 minutes remaining
MG_MIN_ENTRY_PRICE = 0.80      # Minimum entry price
MG_MAX_ENTRY_PRICE = 0.84      # Maximum entry price
MG_MIN_BID_SIZE = 300          # Minimum liquidity required
MG_FIXED_SHARES = 20           # Fixed position size
MG_TAKE_PROFIT = 0.95          # Take profit at 95 cents
MG_STOP_LOSS = 0.44            # Stop loss at 44 cents

# System Settings
CHECK_INTERVAL = 1
MIN_ORDER_SIZE = 0.1
TRADE_LOG_FILE = "midgame_trades.csv"
ENABLE_EXCEL = True

# Setup addresses
from eth_account import Account
wallet = Account.from_key(PRIVATE_KEY)
print(f"🔑 Private key controls: {wallet.address}")
print(f"🦄 Polymarket shows: {POLYMARKET_ADDRESS}")

if wallet.address.lower() == POLYMARKET_ADDRESS.lower():
    print(f"✅ Direct match - using EOA mode")
    USE_PROXY = False
    SIGNATURE_TYPE = 0
    TRADING_ADDRESS = Web3.to_checksum_address(wallet.address)
else:
    print(f"⚠️ Addresses differ - using proxy mode")
    USE_PROXY = True
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# System setup
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/Vwy188P6gCu8mAUrbObWH"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class MidGameBot:
    def __init__(self):
        print("\n🤖 Mid Game Strategy Bot Starting...")
        
        # Setup Web3
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # Setup Client
        try:
            print(f"🔗 Setting up Polymarket client...")
            
            if USE_PROXY:
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID, 
                    signature_type=SIGNATURE_TYPE,
                    funder=TRADING_ADDRESS
                )
            else:
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID
                )
            
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            print(f"✅ Trading as: {self.client.get_address()}\n")
            
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            exit()
        
        # Tracking
        self.traded_markets = set()
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        
        # Trade logging
        self.trade_logs = []
        self.initialize_trade_log()

    def initialize_trade_log(self):
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'entry_side', 'entry_price', 'shares',
                'yes_price_at_entry', 'no_price_at_entry',
                'time_remaining_at_entry', 'bid_size_at_entry',
                'exit_reason', 'exit_price',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'session_trade_number', 'balance_before', 'balance_after'
            ]
            
            with open(TRADE_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log initialized: {TRADE_LOG_FILE}")

    def log_trade(self, trade_data):
        try:
            self.trade_logs.append(trade_data)
            
            with open(TRADE_LOG_FILE, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
            
            if ENABLE_EXCEL:
                df = pd.DataFrame(self.trade_logs)
                excel_file = TRADE_LOG_FILE.replace('.csv', '.xlsx')
                df.to_excel(excel_file, index=False, engine='openpyxl')
            
            print(f"✅ Trade logged")
            
        except Exception as e:
            print(f"⚠️ Error logging trade: {e}")

    def get_balance(self):
        try:
            raw_bal = self.usdc_contract.functions.balanceOf(TRADING_ADDRESS).call()
            decimals = self.usdc_contract.functions.decimals().call()
            return raw_bal / (10 ** decimals)
        except:
            return 0.0

    def get_market_from_slug(self, slug):
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
        except:
            return None

    def get_best_ask(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            if book.asks:
                return min(float(o.price) for o in book.asks)
            return None
        except:
            return None

    def get_best_bid(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            if book.bids:
                return max(float(o.price) for o in book.bids)
            return None
        except:
            return None

    def get_order_book_depth(self, token_id):
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
        except:
            return None

    def get_filled_amount(self, order_id):
        """Get the actual filled amount for an order"""
        try:
            time.sleep(0.5)  # Brief pause to let order settle
            order = self.client.get_order(order_id)
            if order:
                filled = float(order.size_matched) if hasattr(order, 'size_matched') else 0
                print(f"   📊 Order {order_id[:8]}... filled: {filled} shares")
                return filled
            return 0
        except Exception as e:
            print(f"   ⚠️ Could not verify fill amount: {e}")
            return 0

    def force_buy(self, token_id, price, size):
        """Force buy immediately with generous slippage - returns (order_id, filled_amount)"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None, 0
            
            # Use generous limit to ensure fill
            limit_price = min(0.99, round(price + 0.01, 2))
            
            print(f"   ⚡ FORCE BUY | Size: {size} | Price: ${price:.2f} | Limit: ${limit_price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price,
                size=size,
                side=BUY,
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
                
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id and str(order_id).strip() != "":
                    # Get actual filled amount
                    filled_amount = self.get_filled_amount(order_id)
                    if filled_amount > 0:
                        print(f"   ✅ FILLED {filled_amount} shares (ID: {order_id})")
                        return order_id, filled_amount
                    else:
                        print(f"   ⚠️ Order filled but could not verify amount, using requested size")
                        return order_id, size
                else:
                    error_msg = order_result.get('errorMsg', 'Unknown FOK kill')
                    print(f"   ❌ FAILED TO FILL. API Response: {order_result}")
                    return None, 0
            
            return None, 0
        except Exception as e:
            print(f"   ❌ Buy error: {e}")
            return None, 0

    def force_sell(self, token_id, price, size):
        """Force sell immediately with generous slippage"""
        try:
            # Round down to nearest 0.1 to ensure we don't oversell
            size = int(size * 10) / 10.0
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Size too small after rounding: {size}")
                return None
            
            # Use generous limit to ensure fill
            limit_price = max(0.01, round(price - 0.01, 2))
            
            print(f"   ⚡ FORCE SELL | Size: {size} | Price: ${price:.2f} | Limit: ${limit_price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price,
                size=size,
                side=SELL,
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
                
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id and str(order_id).strip() != "":
                    print(f"   ✅ FILLED (ID: {order_id})")
                    return order_id
                else:
                    error_msg = order_result.get('errorMsg', 'Unknown FOK kill')
                    print(f"   ❌ FAILED TO FILL. API Response: {order_result}")
                    return None
            
            return None
        except Exception as e:
            print(f"   ❌ Sell error: {e}")
            return None

    def execute_midgame_strategy(self, market, market_start_time):
        """Execute mid-game strategy - NO (DOWN) only"""
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
            return "already_traded"
        
        current_time = time.time()
        time_remaining = market_end_time - current_time
        
        # Check if in entry window
        if time_remaining < MG_LOCK_WINDOW_START or time_remaining > MG_LOCK_WINDOW_END:
            return "outside_window"
        
        # Get prices
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
        
        # Check NO (DOWN) side only
        if not (MG_MIN_ENTRY_PRICE <= no_price <= MG_MAX_ENTRY_PRICE and 
                no_book['bid_size'] >= MG_MIN_BID_SIZE):
            return "no_opportunity"
        
        # Entry signal found
        entry_token = market['no_token']
        entry_side = "NO"
        entry_price = no_price
        order_size = MG_FIXED_SHARES
        bid_size = no_book['bid_size']
        
        print(f"\n\n{'='*60}")
        print(f"🎯 MID-GAME ENTRY - {entry_side} (DOWN)")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"Time: {minutes_remaining}m {seconds_remaining}s")
        print(f"YES: ${yes_price:.2f} | NO: ${no_price:.2f}")
        print(f"Entry: {entry_side} @ ${entry_price:.2f}")
        print(f"Liquidity: {bid_size:.0f} shares")
        print(f"Shares: {order_size}")
        
        # Check balance
        current_balance = self.get_balance()
        max_cost = order_size * entry_price
        
        if max_cost > current_balance:
            print(f"⚠️ Insufficient balance: ${current_balance:.2f} < ${max_cost:.2f}")
            self.traded_markets.add(slug)
            return "insufficient_balance"
        
        # Execute entry
        print(f"\n⚡ FORCE ENTERING")
        
        entry_id, actual_shares = self.force_buy(entry_token, entry_price, order_size)
        
        if not entry_id or actual_shares == 0:
            print(f"❌ Entry failed")
            self.traded_markets.add(slug)
            return "entry_failed"
        
        print(f"✅ ENTRY FILLED @ ${entry_price:.2f}")
        print(f"📦 Actual Shares Bought: {actual_shares}")
        print(f"\n🎯 Targets:")
        print(f"   Take Profit: ${MG_TAKE_PROFIT:.2f}")
        print(f"   Stop Loss: ${MG_STOP_LOSS:.2f}")
        
        # Initialize trade data with actual shares
        trade_data = {
            'timestamp': datetime.now().isoformat(),
            'market_slug': slug,
            'market_title': market['title'],
            'entry_side': entry_side,
            'entry_price': entry_price,
            'shares': actual_shares,  # Use actual filled amount
            'yes_price_at_entry': yes_price,
            'no_price_at_entry': no_price,
            'time_remaining_at_entry': int(time_remaining),
            'bid_size_at_entry': bid_size,
            'balance_before': current_balance,
            'session_trade_number': self.session_trades + 1,
        }
        
        # Monitor position
        print(f"\n💎 Monitoring...")
        
        while True:
            time.sleep(CHECK_INTERVAL)
            
            current_bid = self.get_best_bid(entry_token)
            
            if not current_bid:
                continue
            
            current_pnl = (current_bid - entry_price) * actual_shares
            
            print(f"   💹 Bid: ${current_bid:.2f} | P&L: ${current_pnl:+.2f}", end="\r")
            
            # Check take profit
            if current_bid >= MG_TAKE_PROFIT:
                print(f"\n\n🚀 TAKE PROFIT @ ${current_bid:.2f}!")
                print(f"   Selling {actual_shares} shares...")
                
                exit_id = self.force_sell(entry_token, current_bid, actual_shares)
                
                if exit_id:
                    exit_price = current_bid
                    pnl = (exit_price - entry_price) * actual_shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    trade_data['exit_reason'] = 'TAKE_PROFIT'
                    trade_data['exit_price'] = exit_price
                    trade_data['gross_pnl'] = pnl
                    trade_data['pnl_percent'] = pnl_pct
                    trade_data['win_loss'] = 'WIN'
                    trade_data['balance_after'] = self.get_balance()
                    
                    self.log_trade(trade_data)
                    self.session_wins += 1
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    return "take_profit"
            
            # Check stop loss
            elif current_bid <= MG_STOP_LOSS:
                print(f"\n\n🛑 STOP LOSS @ ${current_bid:.2f}!")
                print(f"   Selling {actual_shares} shares...")
                
                exit_id = self.force_sell(entry_token, current_bid, actual_shares)
                
                if exit_id:
                    exit_price = current_bid
                    pnl = (exit_price - entry_price) * actual_shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    trade_data['exit_reason'] = 'STOP_LOSS'
                    trade_data['exit_price'] = exit_price
                    trade_data['gross_pnl'] = pnl
                    trade_data['pnl_percent'] = pnl_pct
                    trade_data['win_loss'] = 'LOSS'
                    trade_data['balance_after'] = self.get_balance()
                    
                    self.log_trade(trade_data)
                    self.session_losses += 1
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    return "stop_loss"

    def run(self):
        """Main bot loop"""
        print(f"\n🚀 Mid Game Bot Running...")
        print(f"\n📊 MID-GAME STRATEGY (NO ONLY):")
        print(f"   Window: {MG_LOCK_WINDOW_START}s-{MG_LOCK_WINDOW_END}s remaining")
        print(f"   Entry: ${MG_MIN_ENTRY_PRICE:.2f}-${MG_MAX_ENTRY_PRICE:.2f}")
        print(f"   Min Bids: {MG_MIN_BID_SIZE}")
        print(f"   Shares: {MG_FIXED_SHARES}")
        print(f"   TP: ${MG_TAKE_PROFIT:.2f} | SL: ${MG_STOP_LOSS:.2f}")
        print(f"\n📊 Logging: {TRADE_LOG_FILE}\n")
        
        current_market = None
        
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                current_timestamp = int(now_utc.timestamp())
                
                market_timestamp = (current_timestamp // 900) * 900
                expected_slug = f"btc-updown-15m-{market_timestamp}"
                
                if not current_market or current_market['slug'] != expected_slug:
                    print(f"\n🔍 Looking for: {expected_slug}")
                    current_market = self.get_market_from_slug(expected_slug)
                    
                    if current_market:
                        market_end = market_timestamp + 900
                        time_left = market_end - current_timestamp
                        print(f"✅ Found! {current_market['title']}")
                        print(f"   Time Left: {time_left//60}m {time_left%60}s\n")

                        # Cancel all old orders when new market detected
                        try:
                            print("🧹 New market detected! Cancelling all old orders...")
                            self.client.cancel_all()
                            time.sleep(1)
                            print("   ✅ Wallet unlocked & ready.")
                        except Exception as e:
                            print(f"   ⚠️ Cleanup warning: {e}")

                    else:
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        wait_time = next_market_time - current_timestamp
                        print(f"⏳ Waiting {wait_time}s for next market")
                        time.sleep(min(wait_time, 60))
                        continue
                
                status = self.execute_midgame_strategy(current_market, market_timestamp)
                
                if status in ["take_profit", "stop_loss"]:
                    current_balance = self.get_balance()
                    session_pnl = current_balance - self.starting_balance
                    win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                    
                    print(f"\n📊 SESSION: Trades: {self.session_trades} | W: {self.session_wins} | L: {self.session_losses}")
                    print(f"   Balance: ${current_balance:.2f} | P&L: ${session_pnl:+.2f} | WR: {win_rate:.1f}%\n")
                    
                    time.sleep(5)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped")
                current_balance = self.get_balance()
                session_pnl = current_balance - self.starting_balance
                win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                print(f"\n📊 FINAL: ${self.starting_balance:.2f} → ${current_balance:.2f} | P&L: ${session_pnl:+.2f}")
                print(f"   Trades: {self.session_trades} | W: {self.session_wins} | L: {self.session_losses} | WR: {win_rate:.1f}%")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = MidGameBot()
    bot.run()
