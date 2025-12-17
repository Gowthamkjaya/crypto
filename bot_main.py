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

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("❌ PRIVATE_KEY not found in environment variables!")
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

from eth_account import Account
wallet = Account.from_key(PRIVATE_KEY)
print(f"🔑 Private key controls: {wallet.address}")
print(f"🔑 Polymarket shows: {POLYMARKET_ADDRESS}")

if wallet.address.lower() == POLYMARKET_ADDRESS.lower():
    print(f"✅ Direct match - using EOA mode")
    USE_PROXY = False
    SIGNATURE_TYPE = 0
    TRADING_ADDRESS = Web3.to_checksum_address(wallet.address)
else:
    print(f"⚠️ Addresses differ - Polymarket uses proxy contract")
    USE_PROXY = True
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

MANUAL_SLUG = ""

# ==========================================
# 🎯 STRATEGY 1: EARLY SCALP (30-50s window)
# ==========================================
SCALP_ENTRY_WINDOW_START = 870  # After first 30s
SCALP_ENTRY_WINDOW_END = 850    # Before 50s elapsed
SCALP_MIN_ENTRY_PRICE = 0.40    # Must be >= $0.40
SCALP_MAX_ENTRY_PRICE = 0.48    # Must be <= $0.48
SCALP_POSITION_SIZE = 8
SCALP_SELL_TARGET = 0.04        # Sell at +4 cents
SCALP_STOP_LOSS_OFFSET = 0.07   # -7 cents SL
SCALP_STOP_LOSS_DELAY = 200
SCALP_SELL_TIMEOUT = 200

# ==========================================
# 🎯 STRATEGY 2: MID-GAME NO LOCK (5-10 min)
# ==========================================
LOCK_WINDOW_START = 300
LOCK_WINDOW_END = 600
MIN_ENTRY_PRICE = 0.90          # NO must be >= $0.90
LOCK_POSITION_SIZE = 15
TAKE_PROFIT_SPREAD = 0.05
STOP_LOSS_SPREAD = 0.05
MIN_STOP_LOSS_DELAY = 60
STOP_LOSS_BUFFER_TIME = 180
TRAILING_PROFIT_LOCK = 0.5

# ==========================================
# SYSTEM SETTINGS
# ==========================================
CHECK_INTERVAL = 2
MAX_ACCEPTABLE_SLIPPAGE = 0.05
RETRY_DELAY = 1
MIN_ORDER_SIZE = 0.1
PRICE_IMPROVEMENT = 0.01

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class CombinedBTCBot:
    def __init__(self):
        print("🤖 Combined BTC Bot Starting...")
        print("   Strategy 1: Early Scalp (buy low @$0.40-$0.48, sell +$0.04)")
        print("   Strategy 2: Mid-Game NO Lock (@$0.90+ with trailing stop)\n")
        
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        try:
            if USE_PROXY:
                self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, 
                                        signature_type=SIGNATURE_TYPE, funder=TRADING_ADDRESS)
            else:
                self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
            
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            print(f"✅ Trading as: {self.client.get_address()}\n")
        except Exception as e:
            print(f"❌ Failed: {e}")
            exit()
        
        self.scalp_executed = set()
        self.lock_executed = set()
        self.starting_balance = self.get_balance()

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
            if not resp:
                return None
            event = resp[0]
            raw_ids = event['markets'][0].get('clobTokenIds')
            clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            return {
                'slug': slug,
                'yes_token': clob_ids[0],
                'no_token': clob_ids[1],
                'title': event.get('title', slug),
                'condition_id': event['markets'][0].get('conditionId')
            }
        except:
            return None

    def get_order_book_depth(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            best_ask = min(float(o.price) for o in book.asks) if book.asks else None
            best_bid = max(float(o.price) for o in book.bids) if book.bids else None
            ask_liquidity = sum(float(o.size) for o in book.asks if float(o.price) == best_ask) if book.asks and best_ask else 0
            return {'best_ask': best_ask, 'best_bid': best_bid, 'ask_liquidity': ask_liquidity}
        except:
            return None

    def get_best_bid(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            return max(float(o.price) for o in book.bids) if book.bids else None
        except:
            return None

    def get_best_ask(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            return min(float(o.price) for o in book.asks) if book.asks else None
        except:
            return None

    def place_market_order(self, token_id, price, size, side):
        try:
            if side == BUY:
                price = min(price + PRICE_IMPROVEMENT, 0.99)
            price = round(price, 2)
            if size < MIN_ORDER_SIZE:
                return None
            
            resp = self.client.post_orders([
                PostOrdersArgs(order=self.client.create_order(OrderArgs(
                    price=price, size=size, side=side, token_id=token_id)), orderType=OrderType.FOK)
            ])
            
            if resp and len(resp) > 0:
                order_result = resp[0]
                if order_result.get('success') or order_result.get('orderID'):
                    return order_result.get('orderID', 'success')
            return None
        except:
            return None

    def place_limit_order(self, token_id, price, size, side):
        try:
            price = round(price, 2)
            resp = self.client.post_orders([
                PostOrdersArgs(order=self.client.create_order(OrderArgs(
                    price=price, size=size, side=side, token_id=token_id)), orderType=OrderType.GTC)
            ])
            if resp and len(resp) > 0:
                order_result = resp[0]
                if order_result.get('success') or order_result.get('orderID'):
                    return order_result.get('orderID', 'success')
            return None
        except:
            return None

    def cancel_order(self, order_id):
        try:
            self.client.cancel(order_id)
            return True
        except:
            return False

    def check_order_status(self, order_id):
        try:
            order_details = self.client.get_order(order_id)
            if isinstance(order_details, dict):
                status = order_details.get('status', '')
                if status in ['MATCHED', 'FILLED', 'COMPLETED']:
                    actual_price = order_details.get('price') or order_details.get('avgFillPrice')
                    if actual_price:
                        return True, float(actual_price)
            return False, None
        except:
            return False, None

    # ==========================================
    # STRATEGY 1: EARLY SCALP
    # ==========================================
    def execute_scalp(self, market, market_start_time):
        slug = market['slug']
        if slug in self.scalp_executed:
            return "scalp_done"
        
        current_time = time.time()
        seconds_left = (market_start_time + 900) - current_time
        
        if seconds_left >= SCALP_ENTRY_WINDOW_START:
            return "scalp_waiting"
        if seconds_left < SCALP_ENTRY_WINDOW_END:
            self.scalp_executed.add(slug)
            return "scalp_window_closed"
        
        print(f"\n{'='*60}")
        print(f"🎯 SCALP STRATEGY ACTIVE")
        print(f"{'='*60}")
        
        yes_book = self.get_order_book_depth(market['yes_token'])
        no_book = self.get_order_book_depth(market['no_token'])
        
        if not yes_book or not no_book:
            return "no_data"
        
        yes_ask = yes_book['best_ask']
        no_ask = no_book['best_ask']
        
        if not yes_ask or not no_ask:
            return "no_prices"
        
        print(f"📊 YES: ${yes_ask:.2f} | NO: ${no_ask:.2f}")
        
        # Find lowest side in range [$0.40-$0.48]
        entry_token, entry_side, entry_price, entry_book = None, None, None, None
        
        if yes_ask < no_ask and SCALP_MIN_ENTRY_PRICE <= yes_ask <= SCALP_MAX_ENTRY_PRICE:
            entry_token, entry_side, entry_price, entry_book = market['yes_token'], "YES", yes_ask, yes_book
        elif no_ask < yes_ask and SCALP_MIN_ENTRY_PRICE <= no_ask <= SCALP_MAX_ENTRY_PRICE:
            entry_token, entry_side, entry_price, entry_book = market['no_token'], "NO", no_ask, no_book
        elif yes_ask == no_ask and SCALP_MIN_ENTRY_PRICE <= yes_ask <= SCALP_MAX_ENTRY_PRICE:
            entry_token, entry_side, entry_price, entry_book = market['yes_token'], "YES", yes_ask, yes_book
        
        if not entry_token:
            time.sleep(RETRY_DELAY)
            return "no_scalp_opp"
        
        if entry_book['ask_liquidity'] < SCALP_POSITION_SIZE:
            time.sleep(RETRY_DELAY)
            return "low_liquidity"
        
        print(f"\n✅ SCALP Entry: {entry_side} @ ${entry_price:.2f}")
        print(f"   Target Sell: ${entry_price + SCALP_SELL_TARGET:.2f}")
        
        # BUY
        buy_id = self.place_market_order(entry_token, entry_price, SCALP_POSITION_SIZE, BUY)
        if not buy_id:
            return "buy_failed"
        
        time.sleep(1)
        buy_filled, buy_price = self.check_order_status(buy_id)
        if not buy_filled:
            return "buy_failed"
        
        print(f"✅ BUY FILLED @ ${buy_price:.2f}")
        buy_time = time.time()
        
        # SELL LIMIT
        sell_price = min(round(buy_price + SCALP_SELL_TARGET, 2), 0.99)
        sell_id = self.place_limit_order(entry_token, sell_price, SCALP_POSITION_SIZE, SELL)
        
        if not sell_id:
            # Emergency exit
            bid = self.get_best_bid(entry_token)
            if bid:
                self.place_market_order(entry_token, bid, SCALP_POSITION_SIZE, SELL)
            self.scalp_executed.add(slug)
            return "sell_failed"
        
        print(f"✅ SELL LIMIT @ ${sell_price:.2f}")
        
        # Monitor
        start = time.time()
        sl_time = buy_time + SCALP_STOP_LOSS_DELAY
        sl_price = max(buy_price - SCALP_STOP_LOSS_OFFSET, 0.01)
        
        while True:
            elapsed = time.time() - start
            if elapsed >= SCALP_SELL_TIMEOUT:
                break
            
            # Check sell fill
            sell_filled, sell_actual = self.check_order_status(sell_id)
            if sell_filled:
                profit = (sell_actual - buy_price) * SCALP_POSITION_SIZE
                print(f"\n🎉 SCALP COMPLETE! Profit: +${profit:.2f}")
                self.scalp_executed.add(slug)
                return "scalp_complete"
            
            # Check SL
            bid = self.get_best_bid(entry_token)
            if time.time() >= sl_time and bid and bid <= sl_price:
                print(f"\n🛑 SCALP SL at ${bid:.2f}")
                self.cancel_order(sell_id)
                time.sleep(1)
                self.place_market_order(entry_token, bid, SCALP_POSITION_SIZE, SELL)
                self.scalp_executed.add(slug)
                return "scalp_sl"
            
            time.sleep(CHECK_INTERVAL)
        
        # Timeout
        self.cancel_order(sell_id)
        time.sleep(1)
        bid = self.get_best_bid(entry_token)
        if bid:
            self.place_market_order(entry_token, bid, SCALP_POSITION_SIZE, SELL)
        self.scalp_executed.add(slug)
        return "scalp_timeout"

    # ==========================================
    # STRATEGY 2: MID-GAME NO LOCK
    # ==========================================
    def execute_lock(self, market, market_start_time):
        slug = market['slug']
        if slug in self.lock_executed:
            return "lock_done"
        
        market_end = market_start_time + 900
        time_left = market_end - time.time()
        
        if time_left < LOCK_WINDOW_START or time_left > LOCK_WINDOW_END:
            return "lock_outside_window"
        
        no_price = self.get_best_ask(market['no_token'])
        if not no_price or no_price < MIN_ENTRY_PRICE:
            return "no_lock_opp"
        
        print(f"\n{'='*60}")
        print(f"🎯 LOCK STRATEGY - NO @ ${no_price:.2f}")
        print(f"{'='*60}")
        
        # BUY
        buy_id = self.place_market_order(market['no_token'], no_price, LOCK_POSITION_SIZE, BUY)
        if not buy_id:
            return "lock_buy_failed"
        
        time.sleep(1)
        buy_filled, buy_price = self.check_order_status(buy_id)
        if not buy_filled:
            return "lock_buy_failed"
        
        print(f"✅ LOCK BUY @ ${buy_price:.2f}")
        
        # Monitor with trailing stop
        entry_time = time.time()
        tp = min(buy_price + TAKE_PROFIT_SPREAD, 0.99)
        sl = max(buy_price - STOP_LOSS_SPREAD, 0.01)
        
        sl_delay = max(MIN_STOP_LOSS_DELAY, min(time_left - STOP_LOSS_BUFFER_TIME, 300))
        sl_time = entry_time + sl_delay
        
        trailing_sl = sl
        highest = buy_price
        
        print(f"🎯 TP: ${tp:.2f} | SL: ${sl:.2f} | Dynamic delay: {int(sl_delay)}s")
        
        while True:
            time.sleep(CHECK_INTERVAL)
            time_left = market_end - time.time()
            
            if time_left <= STOP_LOSS_BUFFER_TIME:
                bid = self.get_best_bid(market['no_token'])
                if bid:
                    self.place_market_order(market['no_token'], bid, LOCK_POSITION_SIZE, SELL)
                self.lock_executed.add(slug)
                return "lock_time_exit"
            
            bid = self.get_best_bid(market['no_token'])
            if not bid:
                continue
            
            if bid > highest:
                highest = bid
                gain = highest - buy_price
                if gain > 0:
                    trailing_sl = max(sl, buy_price + gain * TRAILING_PROFIT_LOCK)
            
            if bid >= tp:
                print(f"\n💰 LOCK TP at ${bid:.2f}")
                self.place_market_order(market['no_token'], bid, LOCK_POSITION_SIZE, SELL)
                self.lock_executed.add(slug)
                return "lock_tp"
            
            if time.time() >= sl_time and bid <= trailing_sl:
                print(f"\n🛑 LOCK TRAILING SL at ${bid:.2f}")
                self.place_market_order(market['no_token'], bid, LOCK_POSITION_SIZE, SELL)
                self.lock_executed.add(slug)
                return "lock_sl"

    # ==========================================
    # MAIN LOOP
    # ==========================================
    def run(self):
        print(f"🚀 Combined Bot Running...")
        print(f"   Scalp: 30-50s window, buy $0.40-$0.48, sell +$0.04")
        print(f"   Lock: 5-10min window, buy NO @$0.90+, trailing SL\n")
        
        current_market = None
        
        while True:
            try:
                now = datetime.now(timezone.utc)
                ts = int(now.timestamp())
                market_ts = (ts // 900) * 900
                slug = f"btc-updown-15m-{market_ts}"
                
                if not current_market or current_market['slug'] != slug:
                    print(f"\n🔍 Looking for: {slug}")
                    
                    if MANUAL_SLUG:
                        current_market = self.get_market_from_slug(MANUAL_SLUG)
                        market_ts = int(MANUAL_SLUG.split('-')[-1])
                    else:
                        current_market = self.get_market_from_slug(slug)
                    
                    if current_market:
                        print(f"✅ Found: {current_market['title']}\n")
                        self.scalp_executed.discard(slug)
                        self.lock_executed.discard(slug)
                    else:
                        time.sleep(30)
                        continue
                
                # Try SCALP first
                scalp_status = self.execute_scalp(current_market, market_ts)
                
                # Try LOCK
                lock_status = self.execute_lock(current_market, market_ts)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n🛑 Stopped")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = CombinedBTCBot()

    bot.run()

