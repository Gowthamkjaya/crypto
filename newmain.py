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
from collections import deque

# ==========================================
# 🔧 CONFIGURATION
# ==========================================

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("❌ PRIVATE_KEY not found in environment variables!")

POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Strategy Settings - "The Iron Trend"
IT_ENTRY_TIME = 600              # Enter at exactly 8:00 remaining (halfway)
IT_OBSERVATION_START = 900       # Start recording data at 15:00 remaining
IT_MIN_PRICE = 0.65              # Minimum price floor
IT_MAX_DRAWDOWN = 0.15           # Max distance from peak
IT_MIN_STABILITY = 0.03          # 3% of time near current price (minimum)
IT_MAX_STABILITY = 0.40          # 40% maximum - avoid overly stable markets
IT_MIN_MOMENTUM = 0.08          # 2-minute momentum threshold
IT_MAX_ENTRY_PRICE = 0.80        # Don't chase above this
IT_POSITION_SIZE = 9             # Shares per trade
IT_TAKE_PROFIT = 0.96            # Victory lap exit
IT_STOP_LOSS = 0.15              # Widened stop loss
IT_TRAILING_STOP_TRIGGER = 0.95  # Move stop to breakeven at this price
IT_STABILITY_WINDOW = 0.05       # 5% window for stability calculation

# System Settings
CHECK_INTERVAL = 1
MIN_ORDER_SIZE = 0.1
TRADE_LOG_FILE = "iron_trend_trades.csv"
ENABLE_EXCEL = True

# Time Filter - Pause during US market open
PAUSE_START_HOUR = 15  # 15:00 UTC = 10:00 AM EST
PAUSE_END_HOUR = 16    # 16:00 UTC = 11:00 AM EST

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

class PriceHistory:
    """Track price history for metric calculations"""
    def __init__(self):
        self.timestamps = deque()
        self.yes_prices = deque()
        self.no_prices = deque()
        self.max_yes = 0
        self.max_no = 0
    
    def add_observation(self, timestamp, yes_price, no_price):
        """Add a price observation"""
        self.timestamps.append(timestamp)
        self.yes_prices.append(yes_price)
        self.no_prices.append(no_price)
        
        if yes_price > self.max_yes:
            self.max_yes = yes_price
        if no_price > self.max_no:
            self.max_no = no_price
    
    def get_drawdown(self, side):
        """Calculate drawdown: max_price - current_price"""
        if side == "YES":
            current = self.yes_prices[-1] if self.yes_prices else 0
            return self.max_yes - current
        else:
            current = self.no_prices[-1] if self.no_prices else 0
            return self.max_no - current
    
    def get_stability(self, side):
        """Calculate % of time spent within 5% of current price"""
        if not self.timestamps or len(self.timestamps) < 2:
            return 0
        
        prices = self.yes_prices if side == "YES" else self.no_prices
        current_price = prices[-1]
        
        time_in_range = 0
        total_time = 0
        
        for i in range(len(prices) - 1):
            time_delta = self.timestamps[i+1] - self.timestamps[i]
            total_time += time_delta
            
            # Check if price was within 5% of current
            if abs(prices[i] - current_price) <= (current_price * IT_STABILITY_WINDOW):
                time_in_range += time_delta
        
        return time_in_range / total_time if total_time > 0 else 0
    
    def get_momentum(self, side):
        """Calculate 2-minute momentum: current_price - price_2min_ago"""
        if not self.timestamps or len(self.timestamps) < 2:
            return 0
        
        prices = self.yes_prices if side == "YES" else self.no_prices
        current_price = prices[-1]
        current_time = self.timestamps[-1]
        
        # Find price from ~2 minutes ago
        target_time = current_time - 120
        closest_idx = 0
        min_diff = float('inf')
        
        for i, ts in enumerate(self.timestamps):
            diff = abs(ts - target_time)
            if diff < min_diff:
                min_diff = diff
                closest_idx = i
        
        old_price = prices[closest_idx]
        return current_price - old_price
    
    def clear(self):
        """Clear all history"""
        self.timestamps.clear()
        self.yes_prices.clear()
        self.no_prices.clear()
        self.max_yes = 0
        self.max_no = 0

class IronTrendBot:
    def __init__(self):
        print("\n🤖 Iron Trend Strategy Bot Starting...")
        
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
        
        # Price history
        self.price_history = PriceHistory()
        
        # Trade logging
        self.trade_logs = []
        self.initialize_trade_log()

    def initialize_trade_log(self):
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'entry_side', 'entry_price', 'shares',
                'yes_price_at_entry', 'no_price_at_entry',
                'time_remaining_at_entry',
                'drawdown', 'stability', 'momentum', 'max_price',
                'exit_reason', 'exit_price', 'lowest_price_held',
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

    def is_pause_time(self):
        """Check if current time is during the pause window (US market open)"""
        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour
        return PAUSE_START_HOUR <= current_hour < PAUSE_END_HOUR

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

    def get_filled_amount(self, order_id):
        """Get the actual filled amount for an order"""
        try:
            time.sleep(0.5)
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
        """Force buy immediately - returns (order_id, filled_amount)"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None, 0
            
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
                    orderType=OrderType.GTC,
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id and str(order_id).strip() != "":
                    filled_amount = self.get_filled_amount(order_id)
                    if filled_amount > 0:
                        print(f"   ✅ FILLED {filled_amount} shares (ID: {order_id})")
                        return order_id, filled_amount
                    else:
                        print(f"   ⚠️ Order filled but could not verify amount, using requested size")
                        return order_id, size
                else:
                    print(f"   ❌ FAILED TO FILL. API Response: {order_result}")
                    return None, 0
            
            return None, 0
        except Exception as e:
            print(f"   ❌ Buy error: {e}")
            return None, 0

    def force_sell(self, token_id, price, size):
        """Force sell immediately"""
        try:
            size = int(size * 10) / 10.0
            
            if size < MIN_ORDER_SIZE:
                print(f"   ⚠️ Size too small after rounding: {size}")
                return None
            
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
                    orderType=OrderType.GTC,
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
                    print(f"   ❌ FAILED TO FILL. API Response: {order_result}")
                    return None
            
            return None
        except Exception as e:
            print(f"   ❌ Sell error: {e}")
            return None

    def calculate_metrics(self, side):
        prices = (
            self.price_history.yes_prices
            if side == "YES"
            else self.price_history.no_prices
        )

        # 🚨 Guard: no data
        if not prices:
            return None

        drawdown = self.price_history.get_drawdown(side)
        stability = self.price_history.get_stability(side)
        momentum = self.price_history.get_momentum(side)
        current_price = prices[-1]
        max_price = (
            self.price_history.max_yes
            if side == "YES"
            else self.price_history.max_no
        )

        return {
            'drawdown': drawdown,
            'stability': stability,
            'momentum': momentum,
            'current_price': current_price,
            'max_price': max_price
        }


    def check_entry_criteria(self, side):
        """Check if all Iron Trend criteria are met"""
        metrics = self.calculate_metrics(side)

        if metrics is None:
            return {
                'pass': False,
                'metrics': {
                    'current_price': 0,
                    'drawdown': 0,
                    'stability': 0,
                    'momentum': 0,
                    'max_price': 0
                },
                'checks': {}
            }
        
        # Criterion 1: Price Floor (> $0.65)
        price_check = metrics['current_price'] > IT_MIN_PRICE
        
        # Criterion 2: Drawdown (< $0.15)
        drawdown_check = metrics['drawdown'] < IT_MAX_DRAWDOWN
        
        # Criterion 3: Stability (between 3% and 40%)
        stability_min_check = metrics['stability'] > IT_MIN_STABILITY
        stability_max_check = metrics['stability'] <= IT_MAX_STABILITY
        stability_check = stability_min_check and stability_max_check
        
        # Criterion 4: Momentum (>= -$0.02)
        momentum_check = metrics['momentum'] >= IT_MIN_MOMENTUM
        
        # Criterion 5: Don't chase too high
        max_price_check = metrics['current_price'] <= IT_MAX_ENTRY_PRICE
        
        return {
            'pass': (price_check and drawdown_check and stability_check and 
                    momentum_check and max_price_check),
            'metrics': metrics,
            'checks': {
                'price': price_check,
                'drawdown': drawdown_check,
                'stability': stability_check,
                'stability_min': stability_min_check,
                'stability_max': stability_max_check,
                'momentum': momentum_check,
                'max_price': max_price_check
            }
        }

    def execute_iron_trend_strategy(self, market, market_start_time):
        """
        The Iron Trend Strategy:
        1. Observe 15:00 → 8:00 remaining (collect price data)
        2. At 8:00 remaining, evaluate both sides
        3. Enter the side that passes all criteria
        4. Hold to $0.96 take profit or $0.15 stop loss
        """
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
            return "already_traded"
        
        # Check if we're in pause time
        if self.is_pause_time():
            print(f"⏸️  PAUSED - US Market Open (15:00-16:00 UTC)")
            return "paused"
        
        # Clear history for new market
        self.price_history.clear()
        
        print(f"\n{'='*60}")
        print(f"📊 IRON TREND: OBSERVATION PHASE")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"Recording price data until 8:00 remaining...\n")
        
        # Phase 1: OBSERVATION (15:00 → 8:00)
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # Check if we've reached entry time
            if time_remaining <= IT_ENTRY_TIME:
                break
            
            # Don't start recording until we're in observation window
            if time_remaining > IT_OBSERVATION_START:
                return "too_early"
            
            # Get current prices
            yes_price = self.get_best_ask(market['yes_token'])
            no_price = self.get_best_ask(market['no_token'])
            
            if yes_price is not None and no_price is not None:
                self.price_history.add_observation(current_time, yes_price, no_price)
                
                minutes_remaining = int(time_remaining // 60)
                seconds_remaining = int(time_remaining % 60)
                obs_count = len(self.price_history.timestamps)
                print(f"📈 [{minutes_remaining}m {seconds_remaining}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Obs: {obs_count}", end="\r")
            
            time.sleep(CHECK_INTERVAL)

        MIN_OBSERVATIONS = 10

        if len(self.price_history.timestamps) < MIN_OBSERVATIONS:
            print("⚠️ Not enough observations – skipping market")
            self.traded_markets.add(slug)
            return "insufficient_data"

        
        # Phase 2: EVALUATION (At 8:00 remaining)
        print(f"\n\n{'='*60}")
        print(f"🔍 IRON TREND: EVALUATION PHASE")
        print(f"{'='*60}")
        print(f"Observations collected: {len(self.price_history.timestamps)}")
        print(f"Evaluating entry criteria...\n")
        
        # Check both sides
        yes_eval = self.check_entry_criteria("YES")
        no_eval = self.check_entry_criteria("NO")
        
        print(f"YES Side Analysis:")
        print(f"  Price: ${yes_eval['metrics']['current_price']:.2f} (> ${IT_MIN_PRICE:.2f}): {'✅' if yes_eval['checks']['price'] else '❌'}")
        print(f"  Drawdown: ${yes_eval['metrics']['drawdown']:.2f} (< ${IT_MAX_DRAWDOWN:.2f}): {'✅' if yes_eval['checks']['drawdown'] else '❌'}")
        print(f"  Stability: {yes_eval['metrics']['stability']:.1%} ({IT_MIN_STABILITY:.0%}-{IT_MAX_STABILITY:.0%}): {'✅' if yes_eval['checks']['stability'] else '❌'}")
        if not yes_eval['checks']['stability']:
            if not yes_eval['checks']['stability_min']:
                print(f"    → Too low (< {IT_MIN_STABILITY:.0%})")
            if not yes_eval['checks']['stability_max']:
                print(f"    → Too high (> {IT_MAX_STABILITY:.0%}) - Market too stable")
        print(f"  Momentum: ${yes_eval['metrics']['momentum']:+.2f} (>= ${IT_MIN_MOMENTUM:.2f}): {'✅' if yes_eval['checks']['momentum'] else '❌'}")
        print(f"  Max Price: ${yes_eval['metrics']['current_price']:.2f} (<= ${IT_MAX_ENTRY_PRICE:.2f}): {'✅' if yes_eval['checks']['max_price'] else '❌'}")
        print(f"  Result: {'🟢 PASS' if yes_eval['pass'] else '🔴 FAIL'}\n")
        
        print(f"NO Side Analysis:")
        print(f"  Price: ${no_eval['metrics']['current_price']:.2f} (> ${IT_MIN_PRICE:.2f}): {'✅' if no_eval['checks']['price'] else '❌'}")
        print(f"  Drawdown: ${no_eval['metrics']['drawdown']:.2f} (< ${IT_MAX_DRAWDOWN:.2f}): {'✅' if no_eval['checks']['drawdown'] else '❌'}")
        print(f"  Stability: {no_eval['metrics']['stability']:.1%} ({IT_MIN_STABILITY:.0%}-{IT_MAX_STABILITY:.0%}): {'✅' if no_eval['checks']['stability'] else '❌'}")
        if not no_eval['checks']['stability']:
            if not no_eval['checks']['stability_min']:
                print(f"    → Too low (< {IT_MIN_STABILITY:.0%})")
            if not no_eval['checks']['stability_max']:
                print(f"    → Too high (> {IT_MAX_STABILITY:.0%}) - Market too stable")
        print(f"  Momentum: ${no_eval['metrics']['momentum']:+.2f} (>= ${IT_MIN_MOMENTUM:.2f}): {'✅' if no_eval['checks']['momentum'] else '❌'}")
        print(f"  Max Price: ${no_eval['metrics']['current_price']:.2f} (<= ${IT_MAX_ENTRY_PRICE:.2f}): {'✅' if no_eval['checks']['max_price'] else '❌'}")
        print(f"  Result: {'🟢 PASS' if no_eval['pass'] else '🔴 FAIL'}\n")
        
        # Determine entry
        entry_side = None
        entry_token = None
        entry_eval = None
        
        if yes_eval['pass'] and not no_eval['pass']:
            entry_side = "YES"
            entry_token = market['yes_token']
            entry_eval = yes_eval
        elif no_eval['pass'] and not yes_eval['pass']:
            entry_side = "NO"
            entry_token = market['no_token']
            entry_eval = no_eval
        elif yes_eval['pass'] and no_eval['pass']:
            # Both pass - choose the stronger one (higher price)
            if yes_eval['metrics']['current_price'] > no_eval['metrics']['current_price']:
                entry_side = "YES"
                entry_token = market['yes_token']
                entry_eval = yes_eval
            else:
                entry_side = "NO"
                entry_token = market['no_token']
                entry_eval = no_eval
        
        if not entry_side:
            print(f"❌ NO ENTRY: Neither side passed all criteria")
            self.traded_markets.add(slug)
            return "no_signal"
        
        # Check balance
        current_balance = self.get_balance()
        max_cost = IT_POSITION_SIZE * entry_eval['metrics']['current_price']
        
        if max_cost > current_balance:
            print(f"⚠️ Insufficient balance: ${current_balance:.2f} < ${max_cost:.2f}")
            self.traded_markets.add(slug)
            return "insufficient_balance"
        
        # Phase 3: ENTRY
        print(f"\n{'='*60}")
        print(f"🎯 ENTERING TRADE")
        print(f"{'='*60}")
        print(f"Side: {entry_side} (The Iron Trend)")
        print(f"Entry Price: ${entry_eval['metrics']['current_price']:.2f}")
        print(f"Shares: {IT_POSITION_SIZE}")
        print(f"Take Profit: ${IT_TAKE_PROFIT:.2f}")
        print(f"Stop Loss: ${IT_STOP_LOSS:.2f}")
        
        entry_id, actual_shares = self.force_buy(
            entry_token, 
            entry_eval['metrics']['current_price'], 
            IT_POSITION_SIZE
        )
        
        if not entry_id or actual_shares == 0:
            print(f"❌ Entry failed")
            self.traded_markets.add(slug)
            return "entry_failed"
        
        entry_price = entry_eval['metrics']['current_price']
        
        print(f"✅ ENTRY FILLED @ ${entry_price:.2f}")
        print(f"📦 Actual Shares: {actual_shares}")
        
        # Get opposite side price for logging
        opposite_price = (no_eval['metrics']['current_price'] if entry_side == "YES" 
                         else yes_eval['metrics']['current_price'])
        
        # Initialize trade data
        trade_data = {
            'timestamp': datetime.now().isoformat(),
            'market_slug': slug,
            'market_title': market['title'],
            'entry_side': entry_side,
            'entry_price': entry_price,
            'shares': actual_shares,
            'yes_price_at_entry': yes_eval['metrics']['current_price'],
            'no_price_at_entry': no_eval['metrics']['current_price'],
            'time_remaining_at_entry': int(IT_ENTRY_TIME),
            'drawdown': entry_eval['metrics']['drawdown'],
            'stability': entry_eval['metrics']['stability'],
            'momentum': entry_eval['metrics']['momentum'],
            'max_price': entry_eval['metrics']['max_price'],
            'balance_before': current_balance,
            'session_trade_number': self.session_trades + 1,
        }
        
        # Phase 4: MONITOR POSITION
        print(f"\n{'='*60}")
        print(f"💎 MONITORING POSITION")
        print(f"{'='*60}")
        
        lowest_price = entry_price
        stop_loss = IT_STOP_LOSS
        trailing_stop_active = False
        
        while True:
            time.sleep(CHECK_INTERVAL)
            
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # Check if market closed
            if time_remaining <= 0:
                print(f"\n\n⏰ MARKET CLOSED")
                print(f"   Position went to $0.00")
                
                trade_data['exit_reason'] = 'MARKET_CLOSED'
                trade_data['exit_price'] = 0.00
                trade_data['lowest_price_held'] = lowest_price
                trade_data['gross_pnl'] = -entry_price * actual_shares
                trade_data['pnl_percent'] = -100.0
                trade_data['win_loss'] = 'LOSS'
                trade_data['balance_after'] = self.get_balance()
                
                self.log_trade(trade_data)
                self.session_losses += 1
                self.session_trades += 1
                self.traded_markets.add(slug)
                
                print(f"💰 P&L: ${trade_data['gross_pnl']:+.2f} ({trade_data['pnl_percent']:+.2f}%)")
                return "market_closed"
            
            current_bid = self.get_best_bid(entry_token)
            
            if not current_bid:
                continue
            
            # Track lowest price
            if current_bid < lowest_price:
                lowest_price = current_bid
            
            # Activate trailing stop if price hits trigger
            if current_bid >= IT_TRAILING_STOP_TRIGGER and not trailing_stop_active:
                stop_loss = entry_price  # Move to breakeven
                trailing_stop_active = True
                print(f"\n🔒 TRAILING STOP ACTIVATED: Stop moved to breakeven ${entry_price:.2f}")
            
            current_pnl = (current_bid - entry_price) * actual_shares
            
            status = "🔒 BE" if trailing_stop_active else ""
            print(f"   💹 Bid: ${current_bid:.2f} | Stop: ${stop_loss:.2f} {status} | Low: ${lowest_price:.2f} | P&L: ${current_pnl:+.2f}", end="\r")
            
            # Check stop loss first
            if current_bid <= stop_loss:
                print(f"\n\n🛑 STOP LOSS HIT @ ${current_bid:.2f}!")
                print(f"   Selling {actual_shares} shares...")
                
                exit_id = self.force_sell(entry_token, current_bid, actual_shares)
                
                if exit_id:
                    exit_price = current_bid
                    pnl = (exit_price - entry_price) * actual_shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    trade_data['exit_reason'] = 'STOP_LOSS' if not trailing_stop_active else 'TRAILING_STOP'
                    trade_data['exit_price'] = exit_price
                    trade_data['lowest_price_held'] = lowest_price
                    trade_data['gross_pnl'] = pnl
                    trade_data['pnl_percent'] = pnl_pct
                    trade_data['win_loss'] = 'LOSS' if pnl < 0 else 'BREAKEVEN'
                    trade_data['balance_after'] = self.get_balance()
                    
                    self.log_trade(trade_data)
                    
                    if pnl < 0:
                        self.session_losses += 1
                    
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    print(f"📉 Lowest price held: ${lowest_price:.2f}")
                    return "stop_loss"
                else:
                    print(f"⚠️ Stop loss exit failed, continuing to monitor...")
            
            # Check take profit
            if current_bid >= IT_TAKE_PROFIT:
                print(f"\n\n🚀 TAKE PROFIT @ ${current_bid:.2f}!")
                print(f"   Selling {actual_shares} shares...")
                
                exit_id = self.force_sell(entry_token, current_bid, actual_shares)
                
                if exit_id:
                    exit_price = current_bid
                    pnl = (exit_price - entry_price) * actual_shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    trade_data['exit_reason'] = 'TAKE_PROFIT'
                    trade_data['exit_price'] = exit_price
                    trade_data['lowest_price_held'] = lowest_price
                    trade_data['gross_pnl'] = pnl
                    trade_data['pnl_percent'] = pnl_pct
                    trade_data['win_loss'] = 'WIN'
                    trade_data['balance_after'] = self.get_balance()
                    
                    self.log_trade(trade_data)
                    self.session_wins += 1
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    print(f"📉 Lowest price held: ${lowest_price:.2f}")
                    return "take_profit"
                else:
                    print(f"⚠️ Exit failed, continuing to monitor...")

    def run(self):
        """Main bot loop"""
        print(f"\n🚀 Iron Trend Bot Running...")
        print(f"\n", "STRATEGY PARAMETERS:")
        print(f"   Entry Time: {IT_ENTRY_TIME}s remaining (8:00)")
        print(f"   Min Price: ${IT_MIN_PRICE:.2f}")
        print(f"   Max Drawdown: ${IT_MAX_DRAWDOWN:.2f}")
        print(f"   Min Stability: {IT_MIN_STABILITY:.0%}")
        print(f"   Max Stability: {IT_MAX_STABILITY:.0%} (avoid overly stable)")
        print(f"   Min Momentum: ${IT_MIN_MOMENTUM:.2f}")
        print(f"   Max Entry Price: ${IT_MAX_ENTRY_PRICE:.2f}")
        print(f"   Position Size: {IT_POSITION_SIZE} shares")
        print(f"   Take Profit: ${IT_TAKE_PROFIT:.2f}")
        print(f"   Stop Loss: ${IT_STOP_LOSS:.2f}")
        print(f"   Trailing Stop Trigger: ${IT_TRAILING_STOP_TRIGGER:.2f}")
        print(f"   Pause Time: {PAUSE_START_HOUR}:00-{PAUSE_END_HOUR}:00 UTC (US Market Open)")
        print(f"\n📊 Logging: {TRADE_LOG_FILE}\n")
        
        current_market = None
        
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                current_timestamp = int(now_utc.timestamp())
                
                # Check if we're in pause time
                if self.is_pause_time():
                    if not hasattr(self, '_pause_announced') or not self._pause_announced:
                        print(f"\n⏸️  PAUSED - US Market Open (15:00-16:00 UTC / 10:00-11:00 AM EST)")
                        print(f"   Bot will resume trading after 16:00 UTC\n")
                        self._pause_announced = True
                    time.sleep(60)  # Check every minute during pause
                    continue
                else:
                    if hasattr(self, '_pause_announced') and self._pause_announced:
                        print(f"\n▶️  RESUMING - US Market Open period ended\n")
                        self._pause_announced = False
                
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

                        # Cancel all old orders
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
                
                status = self.execute_iron_trend_strategy(current_market, market_timestamp)
                
                if status in ["take_profit", "stop_loss", "market_closed"]:
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
    bot = IronTrendBot()
    bot.run()
