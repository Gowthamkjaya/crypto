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
from eth_account import Account

# ==========================================
# 🔧 CONFIGURATION - LIVE TRADING
# ==========================================

PRIVATE_KEY = "0xbbd185bb356315b5f040a2af2fa28549177f3087559bb76885033e9cf8e8bf34"
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Strategy Settings
OBSERVATION_START = 900      # Start recording at 15:00 remaining
DECISION_START = 540         # Start evaluating at 9:00 remaining
DECISION_END = 360           # Latest decision at 6:00 remaining

# Entry Criteria
MIN_MOMENTUM = 0.050         # Momentum must be > 0.050
MAX_MOMENTUM = 0.100         # Momentum must be < 0.100
MAX_ENTRY_PRICE = 0.60       # Entry price ≤ $0.60

# Position Management
POSITION_SIZE = 5            # 5 shares per trade
STOP_LOSS = 0.05       # Stop loss 5 cents below entry
TAKE_PROFIT = 0.96           # Take profit at $0.96

# System
CHECK_INTERVAL = 1
MIN_ORDER_SIZE = 0.1
TRADE_LOG_FILE = "momentum_live.csv"

# Setup addresses
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

# System setup
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
        prices = []
        price_list = self.yes_prices if side == "YES" else self.no_prices
        
        for i, ts in enumerate(self.timestamps):
            if start_time >= ts >= end_time:
                prices.append(price_list[i])
        
        if not prices:
            return None
        return sum(prices) / len(prices)
    
    def calculate_momentum(self, side="YES"):
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
# LIVE TRADING BOT
# ==========================================

class MomentumLiveBot:
    def __init__(self):
        print("\n" + "="*80)
        print("🚀 LIVE TRADING - Momentum Strategy")
        print("="*80)
        print("\n⚠️  REAL MONEY AT RISK - Trading with real funds")
        
        # Setup Web3
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # Setup ClobClient
        print(f"\n🔗 Connecting to Polymarket...")
        try:
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
            print(f"✅ Trading as: {self.client.get_address()}")
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            exit()
        
        # Get balance
        self.starting_balance = self.get_balance()
        print(f"💰 Starting Balance: ${self.starting_balance:.2f}\n")
        
        self.traded_markets = set()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        self.price_history = PriceHistory()
        
        self.initialize_log()
    
    def initialize_log(self):
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'entry_side', 'entry_price', 'shares', 'order_id',
                'yes_momentum', 'no_momentum',
                'exit_reason', 'exit_price', 'exit_order_id',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'balance_before', 'balance_after'
            ]
            
            with open(TRADE_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log: {TRADE_LOG_FILE}\n")
    
    def log_trade(self, trade_data):
        with open(TRADE_LOG_FILE, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=trade_data.keys())
            writer.writerow(trade_data)
    
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
        """Place buy order"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            limit_price = min(0.99, round(price + 0.01, 2))
            
            print(f"   📤 BUYING | Size: {size} | Price: ${price:.2f} | Limit: ${limit_price:.2f}")
            
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
                
                if success and order_id:
                    print(f"   ✅ FILLED (Order ID: {order_id})")
                    return order_id
            
            print(f"   ❌ Order failed")
            return None
        except Exception as e:
            print(f"   ❌ Buy error: {e}")
            return None
    
    def sell(self, token_id, price, size):
        """Place sell order"""
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
                PostOrdersArgs(
                    order=order,
                    orderType=OrderType.GTC,
                )
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                order_id = order_result.get('orderID')
                success = order_result.get('success')
                
                if success and order_id:
                    print(f"   ✅ SOLD (Order ID: {order_id})")
                    return order_id
            
            print(f"   ❌ Sell failed")
            return None
        except Exception as e:
            print(f"   ❌ Sell error: {e}")
            return None
    
    def calculate_signals(self):
        yes_momentum, yes_early, yes_mid = self.price_history.calculate_momentum("YES")
        no_momentum, no_early, no_mid = self.price_history.calculate_momentum("NO")
        
        return {
            'yes_momentum': yes_momentum,
            'no_momentum': no_momentum,
            'yes_early': yes_early,
            'yes_mid': yes_mid,
            'no_early': no_early,
            'no_mid': no_mid,
            'side': None
        }
    
    def validate_momentum(self, momentum):
        """Check if momentum is within range: 0.050 < momentum < 0.100"""
        if momentum is None:
            return False
        return MIN_MOMENTUM < momentum < MAX_MOMENTUM
    
    def execute_strategy(self, market, market_start_time):
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
            return "already_traded"
        
        self.price_history.clear()
        
        print(f"\n{'='*80}")
        print(f"📊 OBSERVATION: {market['title']}")
        print(f"{'='*80}")
        print(f"Recording 15:00 → 9:00...\n")
        
        # Phase 1: OBSERVATION
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            if time_remaining <= DECISION_START:
                break
            
            if time_remaining > OBSERVATION_START:
                return "too_early"
            
            if time_remaining <= 0:
                return "market_closed"
            
            yes_price = self.get_best_ask(market['yes_token'])
            no_price = self.get_best_ask(market['no_token'])
            
            if yes_price is not None and no_price is not None:
                self.price_history.add_observation(time_remaining, yes_price, no_price)
                
                minutes = int(time_remaining // 60)
                seconds = int(time_remaining % 60)
                obs_count = len(self.price_history.timestamps)
                print(f"📈 [{minutes:02d}:{seconds:02d}] YES: ${yes_price:.3f} | NO: ${no_price:.3f} | Obs: {obs_count}", end="\r")
            
            time.sleep(CHECK_INTERVAL)
        
        if len(self.price_history.timestamps) < 10:
            print(f"\n⚠️ Insufficient data")
            self.traded_markets.add(slug)
            return "insufficient_data"
        
        # Phase 2: EVALUATION
        print(f"\n\n{'='*80}")
        print(f"🔍 EVALUATION")
        print(f"{'='*80}")
        
        signals = self.calculate_signals()
        
        print(f"\n📊 MOMENTUM:")
        print(f"   YES: {signals['yes_momentum']:+.4f}")
        print(f"   NO:  {signals['no_momentum']:+.4f}")
        print(f"\n✅ VALID RANGE: 0.050 < momentum < 0.100")
        
        # Determine which side to trade
        yes_valid = self.validate_momentum(signals['yes_momentum'])
        no_valid = self.validate_momentum(signals['no_momentum'])
        
        print(f"\n   YES valid: {'✅' if yes_valid else '❌'}")
        print(f"   NO valid:  {'✅' if no_valid else '❌'}")
        
        entry_side = None
        entry_token = None
        
        if yes_valid and not no_valid:
            entry_side = "YES"
            entry_token = market['yes_token']
        elif no_valid and not yes_valid:
            entry_side = "NO"
            entry_token = market['no_token']
        elif yes_valid and no_valid:
            # Both valid - choose stronger momentum
            if signals['yes_momentum'] > signals['no_momentum']:
                entry_side = "YES"
                entry_token = market['yes_token']
            else:
                entry_side = "NO"
                entry_token = market['no_token']
        
        if not entry_side:
            print(f"\n⏭️  No valid signal")
            self.traded_markets.add(slug)
            return "no_signal"
        
        print(f"\n✅ SIGNAL: {entry_side}")
        
        # Get entry price
        entry_price = self.get_best_ask(entry_token)
        if entry_price is None:
            print(f"\n❌ No price")
            self.traded_markets.add(slug)
            return "no_price"
        
        # Price check
        if entry_price > MAX_ENTRY_PRICE:
            print(f"\n❌ Price too high: ${entry_price:.3f} > ${MAX_ENTRY_PRICE:.2f}")
            self.traded_markets.add(slug)
            return "price_too_high"
        
        # Calculate stop loss (5 cents below entry)
        stop_loss = STOP_LOSS
        
        print(f"\n💰 TRADE SETUP:")
        print(f"   Entry: ${entry_price:.3f}")
        print(f"   Size: {POSITION_SIZE} shares")
        print(f"   Stop: ${stop_loss:.2f} (fixed)")
        print(f"   TP: ${TAKE_PROFIT:.2f}")
        
        # Phase 3: ENTRY
        current_time = time.time()
        time_remaining = market_end_time - current_time
        balance_before = self.get_balance()
        
        print(f"\n{'='*80}")
        print(f"🚀 ENTERING TRADE")
        print(f"{'='*80}")
        
        order_id = self.buy(entry_token, entry_price, POSITION_SIZE)
        
        if not order_id:
            print(f"\n❌ Entry failed")
            self.traded_markets.add(slug)
            return "entry_failed"
        
        print(f"\n✅ POSITION OPENED")
        
        trade_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'market_slug': slug,
            'market_title': market['title'],
            'entry_side': entry_side,
            'entry_price': entry_price,
            'shares': POSITION_SIZE,
            'order_id': order_id,
            'yes_momentum': signals['yes_momentum'],
            'no_momentum': signals['no_momentum'],
            'balance_before': balance_before
        }
        
        # Phase 4: MONITORING
        return self.monitor_position(
            market, entry_token, entry_price, stop_loss,
            POSITION_SIZE, market_end_time, trade_data
        )
    
    def monitor_position(self, market, entry_token, entry_price, stop_loss,
                        shares, market_end_time, trade_data):
        print(f"\n📊 MONITORING POSITION...")
        
        slug = market['slug']
        
        while True:
            try:
                current_time = time.time()
                time_remaining = market_end_time - current_time
                
                # Market closing
                if time_remaining <= 10:
                    exit_price = self.get_best_bid(entry_token) or 0.99
                    exit_id = self.sell(entry_token, exit_price, shares)
                    
                    pnl = (exit_price - entry_price) * shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    print(f"\n\n⏰ MARKET CLOSED")
                    print(f"   Exit: ${exit_price:.3f}")
                    print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    
                    trade_data.update({
                        'exit_reason': 'MARKET_CLOSED',
                        'exit_price': exit_price,
                        'exit_order_id': exit_id,
                        'gross_pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'win_loss': 'WIN' if pnl > 0 else 'LOSS',
                        'balance_after': self.get_balance()
                    })
                    
                    self.log_trade(trade_data)
                    self.session_trades += 1
                    if pnl > 0:
                        self.session_wins += 1
                    else:
                        self.session_losses += 1
                    self.traded_markets.add(slug)
                    
                    return "market_closed"
                
                # Get current price
                current_bid = self.get_best_bid(entry_token)
                if current_bid is None:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                pnl_now = (current_bid - entry_price) * shares
                pnl_pct_now = ((current_bid - entry_price) / entry_price) * 100
                
                mins = int(time_remaining // 60)
                secs = int(time_remaining % 60)
                print(f"\r⏱️  [{mins:02d}:{secs:02d}] ${current_bid:.3f} | P&L: ${pnl_now:+.2f} ({pnl_pct_now:+.2f}%)", end="", flush=True)
                
                # Check stop loss
                if current_bid <= stop_loss:
                    exit_id = self.sell(entry_token, current_bid, shares)
                    
                    pnl = (current_bid - entry_price) * shares
                    pnl_pct = ((current_bid - entry_price) / entry_price) * 100
                    
                    print(f"\n\n🛑 STOP LOSS @ ${current_bid:.3f}")
                    print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    
                    trade_data.update({
                        'exit_reason': 'STOP_LOSS',
                        'exit_price': current_bid,
                        'exit_order_id': exit_id,
                        'gross_pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'win_loss': 'LOSS',
                        'balance_after': self.get_balance()
                    })
                    
                    self.log_trade(trade_data)
                    self.session_trades += 1
                    self.session_losses += 1
                    self.traded_markets.add(slug)
                    
                    return "stop_loss"
                
                # Check take profit
                if current_bid >= TAKE_PROFIT:
                    exit_id = self.sell(entry_token, current_bid, shares)
                    
                    pnl = (current_bid - entry_price) * shares
                    pnl_pct = ((current_bid - entry_price) / entry_price) * 100
                    
                    print(f"\n\n🚀 TAKE PROFIT @ ${current_bid:.3f}")
                    print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    
                    trade_data.update({
                        'exit_reason': 'TAKE_PROFIT',
                        'exit_price': current_bid,
                        'exit_order_id': exit_id,
                        'gross_pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'win_loss': 'WIN',
                        'balance_after': self.get_balance()
                    })
                    
                    self.log_trade(trade_data)
                    self.session_trades += 1
                    self.session_wins += 1
                    self.traded_markets.add(slug)
                    
                    return "take_profit"
                
                time.sleep(CHECK_INTERVAL)
                
            except Exception as e:
                print(f"\n❌ Error: {e}")
                time.sleep(5)
    
    def run(self):
        print(f"{'='*80}")
        print(f"🚀 LIVE TRADING BOT STARTED")
        print(f"{'='*80}")
        print(f"\n⚙️  STRATEGY:")
        print(f"   Momentum: 0.050 < momentum < 0.100")
        print(f"   Max entry: ${MAX_ENTRY_PRICE:.2f}")
        print(f"   Stop loss: $0.05 (fixed)")
        print(f"   Take profit: ${TAKE_PROFIT:.2f}")
        print(f"   Position: {POSITION_SIZE} shares")
        print(f"\n⚠️  LIVE TRADING - Real money at risk\n")
        
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
                        print(f"✅ Found: {current_market['title']}")
                        print(f"   Time: {time_left//60}m {time_left%60}s\n")
                        
                        try:
                            self.client.cancel_all()
                            time.sleep(1)
                        except:
                            pass
                        
                        status = self.execute_strategy(current_market, market_timestamp)
                        
                        if status in ["take_profit", "stop_loss", "market_closed"]:
                            balance = self.get_balance()
                            pnl = balance - self.starting_balance
                            wr = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                            
                            print(f"\n{'='*80}")
                            print(f"📊 SESSION")
                            print(f"{'='*80}")
                            print(f"   Trades: {self.session_trades} | W: {self.session_wins} | L: {self.session_losses}")
                            print(f"   Balance: ${balance:.2f} | P&L: ${pnl:+.2f} | WR: {wr:.1f}%")
                            print(f"{'='*80}\n")
                    else:
                        print(f"⏳ Waiting...")
                        time.sleep(30)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print(f"\n\n{'='*80}")
                print(f"🛑 BOT STOPPED")
                print(f"{'='*80}")
                balance = self.get_balance()
                pnl = balance - self.starting_balance
                wr = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                print(f"\n📊 FINAL:")
                print(f"   Trades: {self.session_trades} | W: {self.session_wins} | L: {self.session_losses}")
                print(f"   Balance: ${self.starting_balance:.2f} → ${balance:.2f}")
                print(f"   P&L: ${pnl:+.2f} | WR: {wr:.1f}%")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = MomentumLiveBot()
    bot.run()