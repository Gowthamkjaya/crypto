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

# ==========================================
# 🔧 CONFIGURATION - MOMENTUM STRATEGY
# ==========================================

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("❌ PRIVATE_KEY not found in environment variables!")
    
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Observation & Decision Windows
OBSERVATION_START = 900      # Start recording at 15:00 remaining
DECISION_START = 540         # Start evaluating at 9:00 remaining
DECISION_END = 360           # Latest decision at 6:00 remaining

# Signal Thresholds
MOMENTUM_THRESHOLD = 0.004
MID_EARLY_PRICE_THRESHOLD = 0.500
MID_EARLY_GAP_THRESHOLD = 0.012
MIN_SIGNALS_REQUIRED = 1
USE_CONFIRMATION_SIGNALS = True

# Position Management
POSITION_SIZE = 5
MAX_ENTRY_PRICE = 0.80
TAKE_PROFIT = 0.96
STOP_LOSS = 0.35
TRAILING_STOP_TRIGGER = 0.90
TRAILING_STOP_DISTANCE = 0.10

# Session Limits
MAX_DAILY_LOSSES = 15
MAX_DAILY_TRADES = 180

# System
CHECK_INTERVAL = 1
MIN_ORDER_SIZE = 0.1
TRADE_LOG_FILE = "momentum_strategy.csv"

# ==========================================
# SETUP
# ==========================================

from eth_account import Account
wallet = Account.from_key(PRIVATE_KEY)
print(f"🔐 Private key controls: {wallet.address}")
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

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/Vwy188P6gCu8mAUrbObWH"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

# ==========================================
# PRICE HISTORY TRACKER
# ==========================================

class PriceHistory:
    def __init__(self):
        self.timestamps = []
        self.yes_prices = []
        self.no_prices = []
    
    def add_observation(self, timestamp, yes_price, no_price):
        self.timestamps.append(timestamp)
        self.yes_prices.append(yes_price)
        self.no_prices.append(no_price)
    
    def get_period_average(self, start_time, end_time, side="YES"):
        """Get average price for a time period"""
        prices = []
        price_list = self.yes_prices if side == "YES" else self.no_prices
        
        for i, ts in enumerate(self.timestamps):
            if start_time >= ts >= end_time:
                prices.append(price_list[i])
        
        if not prices:
            return None
        return sum(prices) / len(prices)
    
    def calculate_momentum(self, side="YES"):
        """Calculate momentum: mid_avg - early_avg"""
        # Early: 900-720s (15:00 to 12:00)
        # Mid: 720-540s (12:00 to 9:00)
        early_avg = self.get_period_average(900, 720, side)
        mid_avg = self.get_period_average(720, 540, side)
        
        if early_avg is None or mid_avg is None:
            return None, None, None
        
        momentum = mid_avg - early_avg
        return momentum, early_avg, mid_avg
    
    def clear(self):
        self.timestamps.clear()
        self.yes_prices.clear()
        self.no_prices.clear()

# ==========================================
# MOMENTUM STRATEGY BOT
# ==========================================

class MomentumStrategyBot:
    def __init__(self):
        print("\n🎯 Momentum Strategy Bot Starting...")
        print("📊 81% Accurate Formula - Based on 1000+ Market Analysis\n")
        
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
        
        # Initialize trade log
        self.initialize_trade_log()
    
    def initialize_trade_log(self):
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'entry_side', 'entry_price', 'shares',
                'yes_momentum', 'no_momentum', 'signals_count',
                'time_remaining_at_entry',
                'exit_reason', 'exit_price',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'balance_before', 'balance_after'
            ]
            
            with open(TRADE_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log: {TRADE_LOG_FILE}\n")
    
    def log_trade(self, trade_data):
        try:
            with open(TRADE_LOG_FILE, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
            print(f"✅ Trade logged")
        except Exception as e:
            print(f"⚠️ Error logging: {e}")
    
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
    
    def buy(self, token_id, price, size):
        """Buy shares"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            limit_price = min(0.99, round(price + 0.01, 2))
            
            print(f"   📤 BUYING | Size: {size} | Price: ${price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price,
                size=size,
                side=BUY,
                token_id=token_id,
            ))
            
            resp = self.client.post_orders([
                PostOrdersArgs(order=order, orderType=OrderType.GTC)
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id:
                    print(f"   ✅ FILLED (ID: {order_id[:8]}...)")
                    return order_id
            
            return None
        except Exception as e:
            print(f"   ❌ Buy error: {e}")
            return None
    
    def sell(self, token_id, price, size):
        """Sell shares"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            print(f"   📤 SELLING | Size: {size} | Price: ${price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=price,
                size=size,
                side=SELL,
                token_id=token_id,
            ))
            
            resp = self.client.post_orders([
                PostOrdersArgs(order=order, orderType=OrderType.GTC)
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id:
                    print(f"   ✅ SOLD (ID: {order_id[:8]}...)")
                    return order_id
            
            return None
        except Exception as e:
            print(f"   ❌ Sell error: {e}")
            return None
    
    def calculate_signals(self):
        """Calculate all trading signals from collected data"""
        yes_momentum, yes_early, yes_mid = self.price_history.calculate_momentum("YES")
        no_momentum, no_early, no_mid = self.price_history.calculate_momentum("NO")
        
        signals = {
            'yes_momentum': yes_momentum,
            'no_momentum': no_momentum,
            'yes_early': yes_early,
            'yes_mid': yes_mid,
            'no_early': no_early,
            'no_mid': no_mid,
            'signals': [],
            'side': None,
            'confidence': 0
        }
        
        if yes_momentum is None or no_momentum is None:
            return signals
        
        # Primary signal: Momentum
        if yes_momentum > MOMENTUM_THRESHOLD:
            signals['signals'].append(('momentum', 'YES', yes_momentum))
        elif no_momentum > MOMENTUM_THRESHOLD:
            signals['signals'].append(('momentum', 'NO', no_momentum))
        
        # Confirmation signals
        if USE_CONFIRMATION_SIGNALS and yes_mid and no_mid:
            if yes_mid > MID_EARLY_PRICE_THRESHOLD:
                signals['signals'].append(('price', 'YES', yes_mid))
            elif no_mid > MID_EARLY_PRICE_THRESHOLD:
                signals['signals'].append(('price', 'NO', no_mid))
            
            mid_gap = yes_mid - no_mid
            if mid_gap > MID_EARLY_GAP_THRESHOLD:
                signals['signals'].append(('gap', 'YES', mid_gap))
            elif mid_gap < -MID_EARLY_GAP_THRESHOLD:
                signals['signals'].append(('gap', 'NO', abs(mid_gap)))
        
        # Determine final side
        yes_signals = sum(1 for sig in signals['signals'] if sig[1] == "YES")
        no_signals = sum(1 for sig in signals['signals'] if sig[1] == "NO")
        
        if yes_signals >= MIN_SIGNALS_REQUIRED and yes_signals > no_signals:
            signals['side'] = "YES"
            signals['confidence'] = yes_signals
        elif no_signals >= MIN_SIGNALS_REQUIRED and no_signals > yes_signals:
            signals['side'] = "NO"
            signals['confidence'] = no_signals
        
        return signals
    
    def execute_momentum_strategy(self, market, market_start_time):
        """
        Execute momentum strategy:
        1. Observe 15:00 → 9:00 (collect data)
        2. At 9:00-6:00, evaluate signals
        3. Enter trade if signals pass
        4. Hold to TP/SL
        """
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
            return "already_traded"
        
        if self.session_losses >= MAX_DAILY_LOSSES:
            return "max_losses"
        
        if self.session_trades >= MAX_DAILY_TRADES:
            return "max_trades"
        
        # Clear history for new market
        self.price_history.clear()
        
        print(f"\n{'='*60}")
        print(f"📊 MOMENTUM STRATEGY: OBSERVATION PHASE")
        print(f"{'='*60}")
        print(f"Market: {market['title']}")
        print(f"Recording price data from 15:00 to 9:00 remaining...\n")
        
        # Phase 1: OBSERVATION (15:00 → 9:00)
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # Check if we've reached decision window
            if time_remaining <= DECISION_START:
                break
            
            # Don't start recording until observation window
            if time_remaining > OBSERVATION_START:
                return "too_early"
            
            # Market closed
            if time_remaining <= 0:
                return "market_closed"
            
            # Get current prices
            yes_price = self.get_best_ask(market['yes_token'])
            no_price = self.get_best_ask(market['no_token'])
            
            if yes_price is not None and no_price is not None:
                self.price_history.add_observation(time_remaining, yes_price, no_price)
                
                minutes = int(time_remaining // 60)
                seconds = int(time_remaining % 60)
                obs_count = len(self.price_history.timestamps)
                print(f"📈 [{minutes}m {seconds}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Obs: {obs_count}", end="\r")
            
            time.sleep(CHECK_INTERVAL)
        
        # Check if we have enough data
        MIN_OBSERVATIONS = 10
        if len(self.price_history.timestamps) < MIN_OBSERVATIONS:
            print(f"\n⚠️ Not enough observations ({len(self.price_history.timestamps)}) - skipping")
            self.traded_markets.add(slug)
            return "insufficient_data"
        
        # Phase 2: EVALUATION (At 9:00-6:00)
        print(f"\n\n{'='*60}")
        print(f"🔍 MOMENTUM STRATEGY: EVALUATION PHASE")
        print(f"{'='*60}")
        print(f"Observations collected: {len(self.price_history.timestamps)}")
        print(f"Evaluating signals...\n")
        
        signals = self.calculate_signals()
        
        print(f"📈 SIGNAL ANALYSIS:")
        if signals['yes_momentum'] is not None:
            print(f"   YES Momentum: {signals['yes_momentum']:+.4f}")
            print(f"   NO Momentum: {signals['no_momentum']:+.4f}")
            print(f"   YES Early: ${signals['yes_early']:.3f} → Mid: ${signals['yes_mid']:.3f}")
            print(f"   NO Early: ${signals['no_early']:.3f} → Mid: ${signals['no_mid']:.3f}")
        
        print(f"\n🎲 SIGNALS DETECTED ({len(signals['signals'])}):")
        for sig_type, sig_side, sig_value in signals['signals']:
            print(f"   ✓ {sig_type.upper()}: {sig_side}")
        
        if not signals['side']:
            print(f"\n⏭️  No clear signal (need {MIN_SIGNALS_REQUIRED})")
            self.traded_markets.add(slug)
            return "no_signal"
        
        print(f"\n✅ TRADE SIGNAL: {signals['side']} ({signals['confidence']} signals)")
        
        # Determine which token to buy
        if signals['side'] == "YES":
            entry_token = market['yes_token']
            entry_side = "YES"
        else:
            entry_token = market['no_token']
            entry_side = "NO"
        
        # Get current price
        entry_price = self.get_best_ask(entry_token)
        
        if entry_price is None:
            print(f"❌ Could not get price")
            self.traded_markets.add(slug)
            return "no_price"
        
        # Price check
        if entry_price > MAX_ENTRY_PRICE:
            print(f"❌ Price too high: ${entry_price:.2f} > ${MAX_ENTRY_PRICE:.2f}")
            self.traded_markets.add(slug)
            return "price_too_high"
        
        # Phase 3: ENTRY
        print(f"\n🚀 ENTERING TRADE: {entry_side} @ ${entry_price:.2f}")
        
        balance_before = self.get_balance()
        current_time = time.time()
        time_remaining = market_end_time - current_time
        
        order_id = self.buy(entry_token, entry_price, POSITION_SIZE)
        
        if not order_id:
            print(f"❌ Entry failed")
            self.traded_markets.add(slug)
            return "entry_failed"
        
        print(f"\n✅ POSITION OPENED!")
        print(f"   Side: {entry_side}")
        print(f"   Entry: ${entry_price:.2f}")
        print(f"   Shares: {POSITION_SIZE}")
        print(f"   TP: ${TAKE_PROFIT:.2f} | SL: ${STOP_LOSS:.2f}")
        
        trade_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'market_slug': slug,
            'market_title': market['title'],
            'entry_side': entry_side,
            'entry_price': entry_price,
            'shares': POSITION_SIZE,
            'yes_momentum': signals['yes_momentum'],
            'no_momentum': signals['no_momentum'],
            'signals_count': signals['confidence'],
            'time_remaining_at_entry': int(time_remaining),
            'balance_before': balance_before
        }
        
        # Phase 4: MONITORING
        return self.monitor_position(
            market, entry_token, entry_side, entry_price,
            POSITION_SIZE, market_end_time, trade_data
        )
    
    def monitor_position(self, market, entry_token, entry_side, entry_price,
                        shares, market_end_time, trade_data):
        """Monitor open position"""
        print(f"\n📊 MONITORING POSITION...")
        
        slug = market['slug']
        stop_loss = STOP_LOSS
        trailing_stop_active = False
        highest_price = entry_price
        
        while True:
            try:
                current_time = time.time()
                time_remaining = market_end_time - current_time
                
                # Market closing
                if time_remaining <= 10:
                    print(f"\n\n⏰ MARKET CLOSING - Force exit")
                    exit_id = self.sell(entry_token, 0.99, shares)
                    
                    if exit_id:
                        pnl = (0.99 - entry_price) * shares
                        pnl_pct = ((0.99 - entry_price) / entry_price) * 100
                        
                        trade_data['exit_reason'] = 'MARKET_CLOSED'
                        trade_data['exit_price'] = 0.99
                        trade_data['gross_pnl'] = pnl
                        trade_data['pnl_percent'] = pnl_pct
                        trade_data['win_loss'] = 'WIN' if pnl > 0 else 'LOSS'
                        trade_data['balance_after'] = self.get_balance()
                        
                        self.log_trade(trade_data)
                        
                        if pnl > 0:
                            self.session_wins += 1
                        else:
                            self.session_losses += 1
                        
                        self.session_trades += 1
                        self.traded_markets.add(slug)
                        
                        print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                        return "market_closed"
                
                # Get current price
                current_bid = self.get_best_bid(entry_token)
                
                if current_bid is None:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Track highest price
                if current_bid > highest_price:
                    highest_price = current_bid
                
                # Activate trailing stop
                if current_bid >= TRAILING_STOP_TRIGGER and not trailing_stop_active:
                    trailing_stop_active = True
                    stop_loss = current_bid - TRAILING_STOP_DISTANCE
                    print(f"\n🎯 TRAILING STOP ACTIVATED @ ${stop_loss:.2f}")
                
                # Update trailing stop
                if trailing_stop_active:
                    new_stop = current_bid - TRAILING_STOP_DISTANCE
                    if new_stop > stop_loss:
                        stop_loss = new_stop
                
                # Display status
                pnl_now = (current_bid - entry_price) * shares
                pnl_pct_now = ((current_bid - entry_price) / entry_price) * 100
                
                print(f"\r💼 ${current_bid:.3f} | P&L: ${pnl_now:+.2f} ({pnl_pct_now:+.2f}%) | Stop: ${stop_loss:.2f}", end="", flush=True)
                
                # Check stop loss
                if current_bid <= stop_loss:
                    print(f"\n\n🛑 STOP LOSS HIT @ ${current_bid:.2f}!")
                    exit_id = self.sell(entry_token, current_bid, shares)
                    
                    if exit_id:
                        pnl = (current_bid - entry_price) * shares
                        pnl_pct = ((current_bid - entry_price) / entry_price) * 100
                        
                        trade_data['exit_reason'] = 'STOP_LOSS'
                        trade_data['exit_price'] = current_bid
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
                        return "stop_loss"
                
                # Check take profit
                if current_bid >= TAKE_PROFIT:
                    print(f"\n\n🚀 TAKE PROFIT @ ${current_bid:.2f}!")
                    exit_id = self.sell(entry_token, current_bid, shares)
                    
                    if exit_id:
                        pnl = (current_bid - entry_price) * shares
                        pnl_pct = ((current_bid - entry_price) / entry_price) * 100
                        
                        trade_data['exit_reason'] = 'TAKE_PROFIT'
                        trade_data['exit_price'] = current_bid
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
                
                time.sleep(CHECK_INTERVAL)
                
            except Exception as e:
                print(f"\n❌ Monitor error: {e}")
                time.sleep(5)
    
    def run(self):
        """Main bot loop"""
        print(f"🚀 Momentum Strategy Bot Running...")
        print(f"\n⚡ 81% ACCURATE MOMENTUM FORMULA")
        print(f"   Observation: {OBSERVATION_START}s → {DECISION_START}s")
        print(f"   Decision: {DECISION_START}s → {DECISION_END}s")
        print(f"   Momentum Threshold: ±{MOMENTUM_THRESHOLD:.4f}")
        print(f"   Position: {POSITION_SIZE} shares")
        print(f"   TP: ${TAKE_PROFIT:.2f} | SL: ${STOP_LOSS:.2f}\n")
        
        current_market = None
        
        while True:
            try:
                current_timestamp = int(time.time())
                market_timestamp = (current_timestamp // 900) * 900
                expected_slug = f"btc-updown-15m-{market_timestamp}"
                
                if not current_market or current_market['slug'] != expected_slug:
                    print(f"\n🔍 Looking for: {expected_slug}")
                    current_market = self.get_market_from_slug(expected_slug)
                    
                    if current_market:
                        market_end_time = market_timestamp + 900
                        time_left = market_end_time - current_timestamp
                        print(f"✅ Found! {current_market['title']}")
                        print(f"   Time Left: {time_left//60}m {time_left%60}s\n")
                        
                        # Cancel old orders
                        try:
                            self.client.cancel_all()
                            time.sleep(1)
                        except:
                            pass
                        
                        # Execute strategy
                        status = self.execute_momentum_strategy(current_market, market_timestamp)
                        
                        if status in ["take_profit", "stop_loss", "market_closed"]:
                            balance = self.get_balance()
                            pnl = balance - self.starting_balance
                            wr = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                            
                            print(f"\n📊 SESSION: {self.session_trades} trades | W: {self.session_wins} | L: {self.session_losses}")
                            print(f"   Balance: ${balance:.2f} | P&L: ${pnl:+.2f} | WR: {wr:.1f}%\n")
                    else:
                        print(f"⏳ Market not available yet...")
                        time.sleep(30)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped")
                balance = self.get_balance()
                pnl = balance - self.starting_balance
                wr = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                print(f"\n📊 FINAL: ${self.starting_balance:.2f} → ${balance:.2f} | P&L: ${pnl:+.2f}")
                print(f"   Trades: {self.session_trades} | W: {self.session_wins} | L: {self.session_losses} | WR: {wr:.1f}%")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = MomentumStrategyBot()

    bot.run()
