import time
import requests
import json
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY
import csv
import os

from newmain import (
    PRIVATE_KEY, HOST, CHAIN_ID, SIGNATURE_TYPE, USE_PROXY, 
    TRADING_ADDRESS, CHECK_INTERVAL, MIN_ORDER_SIZE
)

# ==========================================
# SIMPLE STRATEGY SETTINGS
# ==========================================
ENTRY_PRICE = 0.02              # Buy when price hits 2 cents
ENTRY_TIME = 900                 # Start looking in last 60 seconds
POSITION_SIZE = 5               # 5 shares

class Simple2CentBot:
    def __init__(self):
        print("\n💸 Simple 2 Cent Bot Starting...")
        print("   Buy 5 shares at $0.02 in last minute\n")
        
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
        
        api_creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(api_creds)
        print(f"✅ Connected as: {self.client.get_address()}\n")
        
        self.traded_markets = set()
        
        # Trade logging
        self.log_file = "simple_trades.csv"
        self.initialize_log()
    
    def initialize_log(self):
        """Initialize CSV log file"""
        if not os.path.exists(self.log_file):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'side', 'entry_price', 'shares', 'time_remaining'
            ]
            
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log: {self.log_file}\n")
    
    def log_trade(self, trade_data):
        """Log trade to CSV"""
        try:
            with open(self.log_file, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
            print(f"✅ Trade logged")
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
    
    def get_best_ask(self, token_id):
        try:
            book = self.client.get_order_book(token_id)
            if book.asks:
                return min(float(o.price) for o in book.asks)
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
            
            print(f"   ⚡ BUYING | Size: {size} | Price: ${price:.2f}")
            
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
    
    def hunt_market(self, market, market_start_time):
        """Wait for last minute, buy at 2 cents"""
        slug = market['slug']
        
        if slug in self.traded_markets:
            return
        
        market_end_time = market_start_time + 900
        
        print(f"\n{'='*60}")
        print(f"💸 {market['title']}")
        print(f"{'='*60}")
        print(f"Waiting for last minute...\n")
        
        traded = False
        
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # Market closed
            if time_remaining <= 0:
                print(f"\n⏰ MARKET CLOSED")
                self.traded_markets.add(slug)
                break
            
            # Wait until last minute
            if time_remaining > ENTRY_TIME:
                mins = int(time_remaining // 60)
                secs = int(time_remaining % 60)
                print(f"   ⏰ [{mins}m {secs}s] Waiting for last minute...", end="\r")
                time.sleep(1)
                continue
            
            # Already traded this market
            if traded:
                secs = int(time_remaining)
                print(f"   ✅ [{secs}s] Trade complete, waiting for close...", end="\r")
                time.sleep(1)
                continue
            
            # Get current prices
            yes_ask = self.get_best_ask(market['yes_token'])
            no_ask = self.get_best_ask(market['no_token'])
            
            if not yes_ask or not no_ask:
                time.sleep(CHECK_INTERVAL)
                continue
            
            secs = int(time_remaining)
            print(f"   👀 [{secs}s] YES: ${yes_ask:.2f} | NO: ${no_ask:.2f}", end="\r")
            
            # Check YES at 2 cents
            if yes_ask <= ENTRY_PRICE:
                print(f"\n\n🎯 YES @ ${yes_ask:.2f}!")
                
                order_id = self.buy(market['yes_token'], yes_ask, POSITION_SIZE)
                
                if order_id:
                    trade_data = {
                        'timestamp': datetime.now().isoformat(),
                        'market_slug': slug,
                        'market_title': market['title'],
                        'side': 'YES',
                        'entry_price': yes_ask,
                        'shares': POSITION_SIZE,
                        'time_remaining': int(time_remaining)
                    }
                    
                    self.log_trade(trade_data)
                    traded = True
                    print(f"💎 Bought {POSITION_SIZE} YES @ ${yes_ask:.2f}\n")
            
            # Check NO at 2 cents
            elif no_ask <= ENTRY_PRICE:
                print(f"\n\n🎯 NO @ ${no_ask:.2f}!")
                
                order_id = self.buy(market['no_token'], no_ask, POSITION_SIZE)
                
                if order_id:
                    trade_data = {
                        'timestamp': datetime.now().isoformat(),
                        'market_slug': slug,
                        'market_title': market['title'],
                        'side': 'NO',
                        'entry_price': no_ask,
                        'shares': POSITION_SIZE,
                        'time_remaining': int(time_remaining)
                    }
                    
                    self.log_trade(trade_data)
                    traded = True
                    print(f"💎 Bought {POSITION_SIZE} NO @ ${no_ask:.2f}\n")
            
            time.sleep(CHECK_INTERVAL)
    
    def run(self):
        """Main loop"""
        print(f"🚀 Bot Running...")
        print(f"📋 Strategy:")
        print(f"   • Wait for last {ENTRY_TIME} seconds")
        print(f"   • Buy {POSITION_SIZE} shares when price hits ${ENTRY_PRICE:.2f}")
        print(f"   • That's it!\n")
        
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
                        print(f"✅ Found! {current_market['title']}\n")
                        self.hunt_market(current_market, market_timestamp)
                    else:
                        print(f"⏳ Market not available yet...")
                        time.sleep(30)
                
                time.sleep(1)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped")
                print(f"📄 Log: {self.log_file}")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = Simple2CentBot()
    bot.run()