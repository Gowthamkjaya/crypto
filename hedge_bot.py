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
# 🔧 CONFIGURATION
# ==========================================

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("❌ PRIVATE_KEY not found in environment variables!")
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Strategy Settings
DH_WATCH_WINDOW_MINUTES = 2    # Watch first 2 minutes
DH_DUMP_THRESHOLD = 0.15       # 15% drop triggers entry
DH_DUMP_TIMEFRAME = 3          # Check drop over 3 seconds
DH_SHARES_PER_LEG = 10          # Fixed shares per leg
DH_LEG1_STOP_LOSS = 0.20       # Stop loss at 20 cents for leg1
DH_EXIT_MAJORITY = 0.96        # Exit when majority reaches 96 cents
DH_EXIT_MINORITY = 0.06        # Exit when minority reaches 6 cents

# System Settings
CHECK_INTERVAL = 1
MIN_ORDER_SIZE = 0.1
TRADE_LOG_FILE = "hedge_trades.csv"
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

class HedgeBot:
    def __init__(self):
        print("\n🤖 Hedge Strategy Bot Starting...")
        
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
        
        # Hedge tracking
        self.leg1_active = False
        self.leg1_side = None
        self.leg1_price = None
        self.leg1_token = None
        self.leg1_shares = 0
        self.leg1_stop_order_id = None
        self.current_market = None
        
        # Price history
        self.yes_price_history = deque(maxlen=DH_DUMP_TIMEFRAME + 1)
        self.no_price_history = deque(maxlen=DH_DUMP_TIMEFRAME + 1)
        
        # Trade logging
        self.trade_logs = []
        self.initialize_trade_log()

    def initialize_trade_log(self):
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'leg1_side', 'leg1_price', 'leg1_shares',
                'leg2_side', 'leg2_price', 'leg2_shares',
                'combined_cost', 'exit_price_leg1', 'exit_price_leg2',
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

    def force_buy(self, token_id, price, size):
        """Force buy immediately with generous slippage"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            limit_price = min(0.99, round(price + 0.05, 2))
            
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
                
                # Check 1: Must be successful AND have an Order ID
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id and order_id != "":
                    print(f"   ✅ FILLED (ID: {order_id})")
                    return order_id
                else:
                    # Capture the reason why it didn't fill
                    error_msg = order_result.get('errorMsg', 'Unknown FOK kill')
                    print(f"   ❌ FAILED TO FILL. API Response: {order_result}")
                    return None
            
            return None
        except Exception as e:
            print(f"   ❌ Buy error: {e}")
            return None

    def force_sell(self, token_id, price, size):
        """Force sell immediately with generous slippage"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            limit_price = max(0.01, round(price - 0.05, 2))
            
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
                
                # Check 1: Must be successful AND have an Order ID
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id and order_id != "":
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

    def detect_dump(self, current_yes, current_no, time_since_start):
        """Detect if either side has dumped significantly"""
        if time_since_start > (DH_WATCH_WINDOW_MINUTES * 60):
            return None, None
        
        self.yes_price_history.append((time.time(), current_yes))
        self.no_price_history.append((time.time(), current_no))
        
        if len(self.yes_price_history) < 2 or len(self.no_price_history) < 2:
            return None, None
        
        # Check YES dump
        yes_old_time, yes_old_price = self.yes_price_history[0]
        yes_new_time, yes_new_price = self.yes_price_history[-1]
        yes_time_diff = yes_new_time - yes_old_time
        
        if yes_time_diff >= DH_DUMP_TIMEFRAME and yes_old_price > 0:
            yes_drop_pct = (yes_old_price - yes_new_price) / yes_old_price
            if yes_drop_pct >= DH_DUMP_THRESHOLD:
                return "YES", yes_drop_pct
        
        # Check NO dump
        no_old_time, no_old_price = self.no_price_history[0]
        no_new_time, no_new_price = self.no_price_history[-1]
        no_time_diff = no_new_time - no_old_time
        
        if no_time_diff >= DH_DUMP_TIMEFRAME and no_old_price > 0:
            no_drop_pct = (no_old_price - no_new_price) / no_old_price
            if no_drop_pct >= DH_DUMP_THRESHOLD:
                return "NO", no_drop_pct
        
        return None, None

    def execute_hedge_strategy(self, market, market_start_time):
        """Execute hedge strategy"""
        slug = market['slug']
        
        # Reset for new market
        if self.current_market != slug:
            self.current_market = slug
            self.leg1_active = False
            self.leg1_side = None
            self.leg1_price = None
            self.leg1_token = None
            self.leg1_shares = 0
            self.leg1_stop_order_id = None
            self.yes_price_history.clear()
            self.no_price_history.clear()
        
        if slug in self.traded_markets:
            return "already_traded"
        
        current_time = time.time()
        time_since_start = current_time - market_start_time
        
        yes_price = self.get_best_ask(market['yes_token'])
        no_price = self.get_best_ask(market['no_token'])
        
        if not yes_price or not no_price:
            return "no_prices"
        
        minutes_elapsed = int(time_since_start // 60)
        seconds_elapsed = int(time_since_start % 60)
        
        # LEG 1: Watch for dump
        if not self.leg1_active:
            if time_since_start > (DH_WATCH_WINDOW_MINUTES * 60):
                return "outside_watch_window"
            
            print(f"💥 [{minutes_elapsed}m {seconds_elapsed}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f}", end="\r")
            
            dump_side, dump_pct = self.detect_dump(yes_price, no_price, time_since_start)
            
            if dump_side:
                print(f"\n\n{'='*60}")
                print(f"💥 DUMP DETECTED - {dump_side} dropped {dump_pct*100:.1f}%!")
                print(f"{'='*60}")
                print(f"Market: {market['title']}")
                print(f"YES: ${yes_price:.2f} | NO: ${no_price:.2f}")
                
                entry_token = market['yes_token'] if dump_side == "YES" else market['no_token']
                entry_price = yes_price if dump_side == "YES" else no_price
                
                print(f"\n⚡ LEG 1: FORCE BUY {dump_side}")
                
                entry_id = self.force_buy(entry_token, entry_price, DH_SHARES_PER_LEG)
                
                if not entry_id:
                    print("❌ LEG 1 entry failed")
                    return "leg1_failed"
                
                self.leg1_active = True
                self.leg1_side = dump_side
                self.leg1_price = entry_price
                self.leg1_token = entry_token
                self.leg1_shares = DH_SHARES_PER_LEG
                
                print(f"✅ LEG 1 COMPLETE @ ${entry_price:.2f}")
                print(f"🛡️ Stop Loss: ${DH_LEG1_STOP_LOSS:.2f}")
                print(f"\n🔍 Watching for LEG 2 opportunity...")
        
        # Monitor LEG 1 stop loss and watch for LEG 2
        else:
            opposite_side = "NO" if self.leg1_side == "YES" else "YES"
            opposite_token = market['no_token'] if opposite_side == "NO" else market['yes_token']
            opposite_price = no_price if opposite_side == "NO" else yes_price
            
            # Check LEG 1 stop loss
            leg1_bid = self.get_best_bid(self.leg1_token)
            if leg1_bid and leg1_bid <= DH_LEG1_STOP_LOSS:
                print(f"\n\n🛑 LEG 1 STOP LOSS TRIGGERED @ ${leg1_bid:.2f}!")
                
                exit_id = self.force_sell(self.leg1_token, leg1_bid, self.leg1_shares)
                
                if exit_id:
                    loss = (DH_LEG1_STOP_LOSS - self.leg1_price) * self.leg1_shares
                    print(f"💰 Loss: ${loss:.2f}")
                    
                    self.session_losses += 1
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    self.leg1_active = False
                    return "stop_loss"
            
            # Check for LEG 2 opportunity
            combined_cost = self.leg1_price + opposite_price
            
            print(f"🔍 [{minutes_elapsed}m {seconds_elapsed}s] {opposite_side}: ${opposite_price:.2f} | Combined: ${combined_cost:.2f}", end="\r")
            
            if combined_cost < 0.95:  # Buffer for guaranteed profit
                profit_pct = ((1.0 - combined_cost) / combined_cost) * 100
                
                print(f"\n\n{'='*60}")
                print(f"🎯 HEDGE OPPORTUNITY!")
                print(f"{'='*60}")
                print(f"LEG 1: {self.leg1_side} @ ${self.leg1_price:.2f}")
                print(f"LEG 2: {opposite_side} @ ${opposite_price:.2f}")
                print(f"Combined: ${combined_cost:.2f}")
                print(f"Profit: ~{profit_pct:.1f}%")
                
                print(f"\n⚡ LEG 2: FORCE BUY {opposite_side}")
                
                leg2_id = self.force_buy(opposite_token, opposite_price, DH_SHARES_PER_LEG)
                
                if not leg2_id:
                    print("❌ LEG 2 entry failed")
                    return "leg2_failed"
                
                leg2_price = opposite_price
                
                print(f"✅ LEG 2 COMPLETE @ ${leg2_price:.2f}")
                print(f"\n💎 HEDGE COMPLETE! Monitoring for exit...")
                print(f"   Exit when majority ≥ ${DH_EXIT_MAJORITY:.2f} AND minority ≤ ${DH_EXIT_MINORITY:.2f}")
                
                # Monitor for exit
                leg1_token = self.leg1_token
                leg2_token = opposite_token
                
                while True:
                    time.sleep(CHECK_INTERVAL)
                    
                    leg1_bid = self.get_best_bid(leg1_token)
                    leg2_bid = self.get_best_bid(leg2_token)
                    
                    if not leg1_bid or not leg2_bid:
                        continue
                    
                    majority_price = max(leg1_bid, leg2_bid)
                    minority_price = min(leg1_bid, leg2_bid)
                    
                    print(f"   💹 Leg1: ${leg1_bid:.2f} | Leg2: ${leg2_bid:.2f} | Maj: ${majority_price:.2f} | Min: ${minority_price:.2f}", end="\r")
                    
                    if majority_price >= DH_EXIT_MAJORITY and minority_price <= DH_EXIT_MINORITY:
                        print(f"\n\n🚀 EXIT TARGETS HIT!")
                        print(f"   Majority: ${majority_price:.2f} ≥ ${DH_EXIT_MAJORITY:.2f}")
                        print(f"   Minority: ${minority_price:.2f} ≤ ${DH_EXIT_MINORITY:.2f}")
                        
                        # Sell both legs
                        print(f"\n⚡ FORCE SELLING BOTH LEGS")
                        exit1 = self.force_sell(leg1_token, leg1_bid, DH_SHARES_PER_LEG)
                        exit2 = self.force_sell(leg2_token, leg2_bid, DH_SHARES_PER_LEG)
                        
                        if exit1 and exit2:
                            actual_combined = self.leg1_price + leg2_price
                            pnl = (leg1_bid + leg2_bid - actual_combined) * DH_SHARES_PER_LEG
                            pnl_pct = ((leg1_bid + leg2_bid - actual_combined) / actual_combined) * 100
                            
                            print(f"✅ BOTH LEGS EXITED")
                            print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                            
                            trade_data = {
                                'timestamp': datetime.now().isoformat(),
                                'market_slug': slug,
                                'market_title': market['title'],
                                'leg1_side': self.leg1_side,
                                'leg1_price': self.leg1_price,
                                'leg1_shares': DH_SHARES_PER_LEG,
                                'leg2_side': opposite_side,
                                'leg2_price': leg2_price,
                                'leg2_shares': DH_SHARES_PER_LEG,
                                'combined_cost': actual_combined,
                                'exit_price_leg1': leg1_bid,
                                'exit_price_leg2': leg2_bid,
                                'gross_pnl': pnl,
                                'pnl_percent': pnl_pct,
                                'win_loss': 'WIN' if pnl > 0 else 'LOSS',
                                'session_trade_number': self.session_trades + 1,
                                'balance_before': self.get_balance(),
                                'balance_after': self.get_balance()
                            }
                            
                            self.log_trade(trade_data)
                            
                            if pnl > 0:
                                self.session_wins += 1
                            else:
                                self.session_losses += 1
                            
                            self.session_trades += 1
                            self.traded_markets.add(slug)
                            self.leg1_active = False
                            
                            return "hedge_complete"
        
        return "watching"

    def run(self):
        """Main bot loop"""
        print(f"\n🚀 Hedge Bot Running...")
        print(f"\n💥 HEDGE STRATEGY:")
        print(f"   Watch: First {DH_WATCH_WINDOW_MINUTES} min")
        print(f"   Dump: {DH_DUMP_THRESHOLD*100:.0f}% in {DH_DUMP_TIMEFRAME}s")
        print(f"   Shares: {DH_SHARES_PER_LEG} per leg")
        print(f"   Leg1 Stop: ${DH_LEG1_STOP_LOSS:.2f}")
        print(f"   Exit: Maj ${DH_EXIT_MAJORITY:.2f} & Min ${DH_EXIT_MINORITY:.2f}")
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

                        # ⭐ ADD THIS BLOCK HERE ⭐
                        # ===================================================
                        try:
                            print("🧹 New market detected! Cancelling all old orders...")
                            self.client.cancel_all()
                            time.sleep(1) # Safety pause to let backend sync
                            print("   ✅ Wallet unlocked & ready.")
                        except Exception as e:
                            print(f"   ⚠️ Cleanup warning: {e}")
                        # ===================================================

                    else:
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        wait_time = next_market_time - current_timestamp
                        print(f"⏳ Waiting {wait_time}s for next market")
                        time.sleep(min(wait_time, 60))
                        continue
                
                status = self.execute_hedge_strategy(current_market, market_timestamp)
                
                if status == "hedge_complete":
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
    bot = HedgeBot()

    bot.run()

