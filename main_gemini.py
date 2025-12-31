import time
import requests
import json
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
import csv
import os

# Import credentials from your existing config file
from newmain import (
    PRIVATE_KEY, HOST, CHAIN_ID, SIGNATURE_TYPE, USE_PROXY, 
    TRADING_ADDRESS, CHECK_INTERVAL, MIN_ORDER_SIZE
)

# ==========================================
# STRATEGY SETTINGS
# ==========================================
ENTRY_TIME = 600                # Enter when 10 minutes (600s) remain
ENTRY_THRESHOLD = 0.55          # Signal: Price > $0.55
STOP_LOSS_PRICE = 0.40          # Exit: Price <= $0.40
POSITION_SIZE = 5               # Number of shares to buy
MAX_ENTRY_PRICE = 0.58          # Max slippage tolerance (don't buy if price jumped to 0.70)

class StrategyBot:
    def __init__(self):
        print("\n📈 Momentum Strategy Bot Starting...")
        print(f"   • Strategy: Wait for 10m remaining mark")
        print(f"   • Entry:    If Price > ${ENTRY_THRESHOLD}")
        print(f"   • StopLoss: If Price <= ${STOP_LOSS_PRICE}\n")
        
        # Setup Polymarket client
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
        
        try:
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            print(f"✅ Connected as: {self.client.get_address()}\n")
        except Exception as e:
            print(f"⚠️ Connection Warning: {e}")
        
        self.traded_markets = set()
        self.log_file = "strategy_trades.csv"
        self.initialize_log()
    
    def initialize_log(self):
        """Initialize CSV log file"""
        if not os.path.exists(self.log_file):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'action', 'side', 'price', 'shares', 'pnl', 'reason'
            ]
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            print(f"📊 Trade log initialized: {self.log_file}\n")
    
    def log_trade(self, data):
        """Log trade to CSV"""
        try:
            with open(self.log_file, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=data.keys())
                writer.writerow(data)
            print(f"📝 Logged to CSV")
        except Exception as e:
            print(f"⚠️ Error logging: {e}")
    
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
    
    def get_price(self, token_id, side):
        """Get Best Ask (to Buy) or Best Bid (to Sell)"""
        try:
            book = self.client.get_order_book(token_id)
            if side == 'ASK':
                if book.asks:
                    return min(float(o.price) for o in book.asks)
            elif side == 'BID':
                if book.bids:
                    return max(float(o.price) for o in book.bids)
            return None
        except:
            return None
    
    def execute_order(self, token_id, side, price, size):
        """Execute Buy or Sell"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            # Smart Limits:
            # Buy: Cap price at MAX_ENTRY_PRICE or price + 0.02
            # Sell: Floor price at 0.01
            if side == BUY:
                limit_price = min(MAX_ENTRY_PRICE, round(price + 0.02, 2))
            else:
                limit_price = max(0.01, round(price - 0.02, 2))
            
            print(f"   ⚡ {'BUYING' if side == BUY else 'SELLING'} | Size: {size} | Limit: ${limit_price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price,
                size=size,
                side=side,
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
            print(f"   ❌ Order error: {e}")
            return None
    
    def hunt_market(self, market, market_start_time):
        slug = market['slug']
        if slug in self.traded_markets: return
        
        market_end_time = market_start_time + 900
        print(f"\n{'='*60}")
        print(f"🦅 Hunting: {market['title']}")
        print(f"{'='*60}")
        
        position = None # 'YES' or 'NO'
        entry_price = 0
        token_held = None
        
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # --- 1. Market Closed ---
            if time_remaining <= 0:
                print(f"\n🔔 MARKET CLOSED")
                self.traded_markets.add(slug)
                break
            
            # --- 2. Wait Phase (> 10m) ---
            # Wait until we are inside the window (e.g. < 605s)
            if time_remaining > ENTRY_TIME + 5:
                mins = int(time_remaining // 60)
                secs = int(time_remaining % 60)
                print(f"   ⏳ [{mins}m {secs}s] Waiting for signal check...", end="\r")
                time.sleep(1)
                continue
            
            # --- 3. Entry Phase (Around 10m left) ---
            if position is None:
                # If we missed the window by too much (e.g. < 300s left), skip to avoid bad R:R
                if time_remaining < 300:
                    print("\n⚠️ Missed entry window (too late). Skipping.")
                    self.traded_markets.add(slug)
                    break

                yes_bid = self.get_price(market['yes_token'], 'BID')
                no_bid = self.get_price(market['no_token'], 'BID')
                yes_ask = self.get_price(market['yes_token'], 'ASK')
                no_ask = self.get_price(market['no_token'], 'ASK')
                
                secs = int(time_remaining)
                y_p = f"${yes_bid:.2f}" if yes_bid else "..."
                n_p = f"${no_bid:.2f}" if no_bid else "..."
                print(f"   👀 [{secs}s] YES: {y_p} | NO: {n_p}      ", end="\r")
                
                # Check YES Signal
                if yes_bid and yes_bid > ENTRY_THRESHOLD:
                    print(f"\n\n🚀 SIGNAL: YES Bid (${yes_bid}) > ${ENTRY_THRESHOLD}")
                    if yes_ask and yes_ask <= MAX_ENTRY_PRICE:
                        if self.execute_order(market['yes_token'], BUY, yes_ask, POSITION_SIZE):
                            position = 'YES'
                            entry_price = yes_ask
                            token_held = market['yes_token']
                            self.log_trade({
                                'timestamp': datetime.now().isoformat(),
                                'market_slug': slug, 'market_title': market['title'],
                                'action': 'ENTRY', 'side': 'YES', 'price': entry_price,
                                'shares': POSITION_SIZE, 'pnl': 0, 'reason': 'Signal > 0.55'
                            })
                            print(f"💎 Position Taken: YES @ ${entry_price:.2f}\n")
                    else:
                        print(f"⚠️ Price too high (${yes_ask} > {MAX_ENTRY_PRICE}). Skipping entry.")
                        self.traded_markets.add(slug)
                        break

                # Check NO Signal
                elif no_bid and no_bid > ENTRY_THRESHOLD:
                    print(f"\n\n🚀 SIGNAL: NO Bid (${no_bid}) > ${ENTRY_THRESHOLD}")
                    if no_ask and no_ask <= MAX_ENTRY_PRICE:
                        if self.execute_order(market['no_token'], BUY, no_ask, POSITION_SIZE):
                            position = 'NO'
                            entry_price = no_ask
                            token_held = market['no_token']
                            self.log_trade({
                                'timestamp': datetime.now().isoformat(),
                                'market_slug': slug, 'market_title': market['title'],
                                'action': 'ENTRY', 'side': 'NO', 'price': entry_price,
                                'shares': POSITION_SIZE, 'pnl': 0, 'reason': 'Signal > 0.55'
                            })
                            print(f"💎 Position Taken: NO @ ${entry_price:.2f}\n")
                    else:
                        print(f"⚠️ Price too high (${no_ask} > {MAX_ENTRY_PRICE}). Skipping entry.")
                        self.traded_markets.add(slug)
                        break

                if position is None:
                    time.sleep(CHECK_INTERVAL)
                    continue

            # --- 4. Monitor Phase (Stop Loss) ---
            if position:
                # We check the BID price because that is what we sell into
                current_bid = self.get_price(token_held, 'BID')
                
                if current_bid:
                    pnl_curr = (current_bid - entry_price) * POSITION_SIZE
                    secs = int(time_remaining)
                    print(f"   🛡️ [{secs}s] Holding {position} | Price: ${current_bid:.2f} | PnL: ${pnl_curr:.2f}   ", end="\r")
                    
                    # STOP LOSS TRIGGER
                    if current_bid <= STOP_LOSS_PRICE:
                        print(f"\n\n🛑 STOP LOSS TRIGGERED: Price ${current_bid:.2f} <= ${STOP_LOSS_PRICE}")
                        self.execute_order(token_held, SELL, current_bid, POSITION_SIZE)
                        
                        self.log_trade({
                            'timestamp': datetime.now().isoformat(),
                            'market_slug': slug, 'market_title': market['title'],
                            'action': 'EXIT', 'side': position, 'price': current_bid,
                            'shares': POSITION_SIZE, 'pnl': (current_bid - entry_price) * POSITION_SIZE,
                            'reason': 'STOP LOSS'
                        })
                        self.traded_markets.add(slug)
                        break
                        
                time.sleep(CHECK_INTERVAL)

    def run(self):
        print(f"🚀 Bot Running...")
        current_market = None
        
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                current_timestamp = int(now_utc.timestamp())
                
                # Identify current 15m market window
                market_timestamp = (current_timestamp // 900) * 900
                expected_slug = f"btc-updown-15m-{market_timestamp}"
                
                if not current_market or current_market['slug'] != expected_slug:
                    print(f"\n🔍 Looking for: {expected_slug}")
                    current_market = self.get_market_from_slug(expected_slug)
                    
                    if current_market:
                        print(f"✅ Found! {current_market['title']}\n")
                        self.hunt_market(current_market, market_timestamp)
                    else:
                        print(f"⏳ Market not available yet...")
                        time.sleep(30)
                
                time.sleep(1)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = StrategyBot()
    bot.run()