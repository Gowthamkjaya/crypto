import time
import requests
import json
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
import csv
import os

from newmain import (
    PRIVATE_KEY, HOST, CHAIN_ID, SIGNATURE_TYPE, USE_PROXY, 
    TRADING_ADDRESS, CHECK_INTERVAL, MIN_ORDER_SIZE
)

# ==========================================
# SIMPLE DOUBLER STRATEGY
# ==========================================
ENTRY_PRICE = 0.04              # Buy when price hits 2 cents
ENTRY_TIME = 900                 # Start looking in last 60 seconds
POSITION_SIZE = 5               # 5 shares
PROFIT_TARGET = 2.0             # Sell at 100% profit (2x)

class Simple2CentDoubler:
    def __init__(self):
        print("\n💸 Simple 2 Cent Doubler Bot Starting...")
        print("   Buy at $0.02, sell at 100% profit\n")
        
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
        self.log_file = "doubler_trades.csv"
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        self.initialize_log()
    
    def initialize_log(self):
        """Initialize CSV log file"""
        if not os.path.exists(self.log_file):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'side', 'entry_price', 'shares', 'entry_time_remaining',
                'exit_reason', 'exit_price', 'pnl', 'pnl_percent'
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
            
            # Update stats
            if trade_data.get('pnl', 0) > 0:
                self.session_wins += 1
            else:
                self.session_losses += 1
            self.session_trades += 1
            
            print(f"✅ Trade logged")
            
            # Show session stats
            win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
            print(f"📊 Session: {self.session_trades} trades | W: {self.session_wins} | L: {self.session_losses} | WR: {win_rate:.1f}%\n")
            
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
    
    def sell(self, token_id, price, size):
        """Sell shares"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            limit_price = max(0.01, round(price - 0.01, 2))
            
            print(f"   ⚡ SELLING | Size: {size} | Price: ${price:.2f}")
            
            order = self.client.create_order(OrderArgs(
                price=limit_price,
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
    
    def hunt_market(self, market, market_start_time):
        """Buy at 2 cents, sell at 100% profit"""
        slug = market['slug']
        
        if slug in self.traded_markets:
            return
        
        market_end_time = market_start_time + 900
        
        print(f"\n{'='*60}")
        print(f"💸 {market['title']}")
        print(f"{'='*60}")
        print(f"Waiting for last minute...\n")
        
        position = None  # {'side': 'YES', 'token': '...', 'entry_price': 0.02, 'shares': 5}
        
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # Market closed
            if time_remaining <= 0:
                print(f"\n⏰ MARKET CLOSED")
                
                # Log position as loss if still holding
                if position:
                    pnl = -position['entry_price'] * position['shares']
                    pnl_pct = -100.0
                    
                    trade_data = {
                        'timestamp': datetime.now().isoformat(),
                        'market_slug': slug,
                        'market_title': market['title'],
                        'side': position['side'],
                        'entry_price': position['entry_price'],
                        'shares': position['shares'],
                        'entry_time_remaining': position['entry_time_remaining'],
                        'exit_reason': 'MARKET_CLOSED',
                        'exit_price': 0.00,
                        'pnl': pnl,
                        'pnl_percent': pnl_pct
                    }
                    
                    self.log_trade(trade_data)
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
                
                self.traded_markets.add(slug)
                break
            
            # Phase 1: ENTRY - Wait until last minute
            if position is None:
                if time_remaining > ENTRY_TIME:
                    mins = int(time_remaining // 60)
                    secs = int(time_remaining % 60)
                    print(f"   ⏰ [{mins}m {secs}s] Waiting for last minute...", end="\r")
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
                        position = {
                            'side': 'YES',
                            'token': market['yes_token'],
                            'entry_price': yes_ask,
                            'shares': POSITION_SIZE,
                            'entry_time_remaining': int(time_remaining),
                            'target_price': yes_ask * PROFIT_TARGET
                        }
                        
                        print(f"💎 Bought {POSITION_SIZE} YES @ ${yes_ask:.2f}")
                        print(f"🎯 Target: ${position['target_price']:.2f} (100% profit)\n")
                
                # Check NO at 2 cents
                elif no_ask <= ENTRY_PRICE:
                    print(f"\n\n🎯 NO @ ${no_ask:.2f}!")
                    
                    order_id = self.buy(market['no_token'], no_ask, POSITION_SIZE)
                    
                    if order_id:
                        position = {
                            'side': 'NO',
                            'token': market['no_token'],
                            'entry_price': no_ask,
                            'shares': POSITION_SIZE,
                            'entry_time_remaining': int(time_remaining),
                            'target_price': no_ask * PROFIT_TARGET
                        }
                        
                        print(f"💎 Bought {POSITION_SIZE} NO @ ${no_ask:.2f}")
                        print(f"🎯 Target: ${position['target_price']:.2f} (100% profit)\n")
            
            # Phase 2: EXIT - Monitor for 100% profit
            else:
                current_bid = self.get_best_bid(position['token'])
                
                if not current_bid:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Calculate current P&L
                current_pnl = (current_bid - position['entry_price']) * position['shares']
                current_pnl_pct = ((current_bid - position['entry_price']) / position['entry_price']) * 100
                
                secs = int(time_remaining)
                print(f"   💹 [{secs}s] {position['side']} Bid: ${current_bid:.2f} | Target: ${position['target_price']:.2f} | P&L: ${current_pnl:+.2f} ({current_pnl_pct:+.1f}%)", end="\r")
                
                # Check if we hit 100% profit target
                if current_bid >= position['target_price']:
                    print(f"\n\n🚀 100% PROFIT @ ${current_bid:.2f}!")
                    
                    exit_id = self.sell(position['token'], current_bid, position['shares'])
                    
                    if exit_id:
                        pnl = (current_bid - position['entry_price']) * position['shares']
                        pnl_pct = ((current_bid - position['entry_price']) / position['entry_price']) * 100
                        
                        trade_data = {
                            'timestamp': datetime.now().isoformat(),
                            'market_slug': slug,
                            'market_title': market['title'],
                            'side': position['side'],
                            'entry_price': position['entry_price'],
                            'shares': position['shares'],
                            'entry_time_remaining': position['entry_time_remaining'],
                            'exit_reason': 'PROFIT_TARGET',
                            'exit_price': current_bid,
                            'pnl': pnl,
                            'pnl_percent': pnl_pct
                        }
                        
                        self.log_trade(trade_data)
                        
                        print(f"✅ Sold {position['shares']} {position['side']} @ ${current_bid:.2f}")
                        print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)\n")
                        
                        position = None
                        self.traded_markets.add(slug)
                        break
            
            time.sleep(CHECK_INTERVAL)
    
    def run(self):
        """Main loop"""
        print(f"🚀 Simple Doubler Bot Running...")
        print(f"📋 Strategy:")
        print(f"   • Wait for last {ENTRY_TIME} seconds")
        print(f"   • Buy {POSITION_SIZE} shares when price hits ${ENTRY_PRICE:.2f}")
        print(f"   • Sell when price doubles (100% profit)")
        print(f"   • Example: Buy @ $0.02 → Sell @ $0.04\n")
        
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
                
                if self.session_trades > 0:
                    win_rate = (self.session_wins / self.session_trades * 100)
                    print(f"\n📊 FINAL SESSION:")
                    print(f"   Trades: {self.session_trades}")
                    print(f"   Wins: {self.session_wins}")
                    print(f"   Losses: {self.session_losses}")
                    print(f"   Win Rate: {win_rate:.1f}%")
                
                print(f"\n📄 Log: {self.log_file}")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = Simple2CentDoubler()
    bot.run()
