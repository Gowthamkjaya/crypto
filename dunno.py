# ==========================================
# 98-TO-99 SCALPING STRATEGY
# ==========================================
# Enter in last 3 minutes at exactly $0.98
# Exit at $0.99
# Stop loss at $0.90

import time
import requests
import json
from datetime import datetime, timezone
import csv
import os


class ScalpingBot:
    """Simple 98-to-99 scalping strategy"""
    
    def __init__(self):
        self.entry_price = 0.94  # Enter at exactly 98 cents
        self.take_profit = 0.99  # Exit at 99 cents
        self.stop_loss = 0.90  # Stop loss at 90 cents
        self.position_size = 2  # Trade 2 shares
        self.last_minutes_threshold = 180  # Last 3 minutes (180 seconds)
        self.log_file = "scalping_98_99_trades.csv"
        
        # Initialize CSV log
        self.initialize_log()
    
    def initialize_log(self):
        """Initialize CSV log file with headers"""
        if not os.path.exists(self.log_file):
            headers = [
                'timestamp',
                'market_slug',
                'market_title',
                'trade_number',
                'entered_side',
                'time_remaining_at_entry',
                'entry_time',
                'entry_price',
                'stop_loss_price',
                'take_profit_price',
                'exit_time',
                'exit_price',
                'exit_reason',
                'time_in_trade_seconds',
                'shares',
                'gross_pnl',
                'pnl_percentage',
                'result'
            ]
            
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Trade log initialized: {self.log_file}\n")
    
    def log_trade(self, trade_data):
        """Log trade to CSV"""
        try:
            with open(self.log_file, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
        except Exception as e:
            print(f"  ⚠️  Logging error: {e}")
    
    def get_current_price(self, token_id, clob_client):
        """Get current best bid price"""
        try:
            book = clob_client.get_order_book(token_id)
            if book.bids:
                return max(float(o.price) for o in book.bids)
            return 0
        except:
            return 0
    
    def place_order(self, clob_client, token_id, side, size=None, max_retries=5):
        """Place FOK order with retries"""
        if size is None:
            size = self.position_size
            
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY as PY_BUY, SELL as PY_SELL
            
            for attempt in range(max_retries):
                book = clob_client.get_order_book(token_id)
                
                if side == 'BUY':
                    if not book.asks:
                        time.sleep(0.5)
                        continue
                    price = min(float(o.price) for o in book.asks)
                else:
                    if not book.bids:
                        time.sleep(0.5)
                        continue
                    price = max(float(o.price) for o in book.bids)
                
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side=PY_BUY if side == 'BUY' else PY_SELL,
                    token_id=token_id
                )
                
                signed_order = clob_client.create_order(order_args)
                
                try:
                    resp = clob_client.post_order(signed_order, OrderType.FOK)
                    
                    if isinstance(resp, dict):
                        if resp.get('success') or resp.get('status') == 'matched':
                            print(f"  ✅ Order filled: {side} {size} @ ${price:.2f}")
                            return resp, price
                    elif resp and hasattr(resp, 'success'):
                        if resp.success:
                            print(f"  ✅ Order filled: {side} {size} @ ${price:.2f}")
                            return resp, price
                    
                except Exception as e:
                    if "couldn't be fully filled" in str(e):
                        print(f"  ⏳ Retry {attempt + 1}/{max_retries}...", end='\r')
                        time.sleep(0.2)
                        continue
                    else:
                        raise e
            
            print(f"\n  ❌ Failed after {max_retries} attempts")
            return None, None
            
        except Exception as e:
            print(f"  ❌ Order error: {e}")
            return None, None
    
    def trade_market(self, market, clob_client):
        """Main trading logic"""
        slug = market['slug']
        print(f"\n{'='*60}")
        print(f"🎯 NEW MARKET: {market['title']}")
        print(f"{'='*60}\n")
        
        market_timestamp = int(slug.split('-')[-1])
        market_end = market_timestamp + 900
        
        trade_count = 0
        in_position = False
        entry_token = None
        entry_price = 0
        entry_side = None
        entry_time = None
        time_remaining_at_entry = 0
        
        print(f"⏰ Waiting for last 3 minutes (180s remaining)...\n")
        
        while True:
            now = int(time.time())
            time_remaining = market_end - now
            
            if time_remaining <= 0:
                print(f"\n⏰ Market closed. Trades executed: {trade_count}")
                if in_position:
                    print(f"⚠️  Still in position - emergency closing...")
                    self.place_order(clob_client, entry_token, 'SELL')
                return
            
            try:
                # Get current prices
                yes_book = clob_client.get_order_book(market['yes_token'])
                no_book = clob_client.get_order_book(market['no_token'])
                
                yes_price = max(float(o.price) for o in yes_book.bids) if yes_book.bids else 0
                no_price = max(float(o.price) for o in no_book.bids) if no_book.bids else 0
                
                # If in position, monitor for exit
                if in_position:
                    current_price = self.get_current_price(entry_token, clob_client)
                    pnl = current_price - entry_price
                    pnl_pct = (pnl / entry_price) * 100 if entry_price > 0 else 0
                    time_in_trade = (datetime.now(timezone.utc) - entry_time).total_seconds()
                    
                    print(f"  📊 [{int(time_in_trade)}s] {entry_side}: ${current_price:.2f} | P&L: ${pnl:.2f} ({pnl_pct:+.1f}%) | Target: ${self.take_profit:.2f}", end='\r')
                    
                    exit_triggered = False
                    exit_reason = None
                    
                    # Check stop loss
                    if current_price <= self.stop_loss:
                        print(f"\n\n🛑 STOP LOSS HIT at ${current_price:.2f}")
                        exit_triggered = True
                        exit_reason = 'STOP_LOSS'
                    
                    # Check take profit
                    elif current_price >= self.take_profit:
                        print(f"\n\n🎯 TAKE PROFIT HIT at ${current_price:.2f}")
                        exit_triggered = True
                        exit_reason = 'TAKE_PROFIT'
                    
                    if exit_triggered:
                        # Force close position
                        print(f"🔚 Closing position...")
                        exit_time = datetime.now(timezone.utc)
                        close_order, close_price = self.place_order(clob_client, entry_token, 'SELL')
                        
                        if close_order:
                            actual_exit = close_price if close_price else current_price
                            final_pnl = (actual_exit - entry_price) * self.position_size
                            final_pnl_pct = (actual_exit - entry_price) / entry_price * 100
                            time_in_trade = (exit_time - entry_time).total_seconds()
                            
                            result = 'WIN' if final_pnl > 0 else 'LOSS' if final_pnl < 0 else 'BREAKEVEN'
                            
                            print(f"\n{'='*60}")
                            print(f"📊 TRADE #{trade_count} COMPLETE - {exit_reason}")
                            print(f"{'='*60}")
                            print(f"  Side: {entry_side}")
                            print(f"  Entry: ${entry_price:.2f} @ {entry_time.strftime('%H:%M:%S')}")
                            print(f"  Exit: ${actual_exit:.2f} @ {exit_time.strftime('%H:%M:%S')}")
                            print(f"  Time in Trade: {int(time_in_trade)}s")
                            print(f"  Shares: {self.position_size}")
                            print(f"  P&L: ${final_pnl:.2f} ({final_pnl_pct:+.1f}%)")
                            print(f"  Result: {result}")
                            print(f"{'='*60}\n")
                            
                            # Log to CSV
                            trade_data = {
                                'timestamp': datetime.now(timezone.utc).isoformat(),
                                'market_slug': slug,
                                'market_title': market['title'],
                                'trade_number': trade_count,
                                'entered_side': entry_side,
                                'time_remaining_at_entry': time_remaining_at_entry,
                                'entry_time': entry_time.isoformat(),
                                'entry_price': f"{entry_price:.4f}",
                                'stop_loss_price': f"{self.stop_loss:.4f}",
                                'take_profit_price': f"{self.take_profit:.4f}",
                                'exit_time': exit_time.isoformat(),
                                'exit_price': f"{actual_exit:.4f}",
                                'exit_reason': exit_reason,
                                'time_in_trade_seconds': int(time_in_trade),
                                'shares': self.position_size,
                                'gross_pnl': f"{final_pnl:.4f}",
                                'pnl_percentage': f"{final_pnl_pct:.2f}",
                                'result': result
                            }
                            self.log_trade(trade_data)
                            
                            # Reset position
                            in_position = False
                            entry_token = None
                            entry_price = 0
                            entry_side = None
                            entry_time = None
                            time_remaining_at_entry = 0
                            
                            print(f"⏰ Waiting for last 3 minutes...\n")
                        else:
                            print(f"  ⚠️  Sell failed, will retry...")
                
                # If NOT in position, look for entry
                else:
                    # Only enter in last 3 minutes
                    if time_remaining <= self.last_minutes_threshold:
                        # Check if YES is at exactly 0.98
                        if abs(yes_price - self.entry_price) < 0.001:  # Within 0.1 cent
                            trade_count += 1
                            entry_side = 'YES'
                            entry_token = market['yes_token']
                            
                            print(f"\n{'='*60}")
                            print(f"🎯 ENTRY SIGNAL #{trade_count}")
                            print(f"{'='*60}")
                            print(f"  Side: {entry_side}")
                            print(f"  Price: ${yes_price:.2f}")
                            print(f"  Time Remaining: {time_remaining}s")
                            print(f"{'='*60}\n")
                            
                            print(f"💸 Entering {entry_side} at ${yes_price:.2f}...")
                            
                            entry_time = datetime.now(timezone.utc)
                            time_remaining_at_entry = time_remaining
                            order, entry_price = self.place_order(clob_client, entry_token, 'BUY')
                            
                            if order:
                                in_position = True
                                print(f"\n📈 Position opened!")
                                print(f"   Entry: ${entry_price:.2f}")
                                print(f"   Stop Loss: ${self.stop_loss:.2f}")
                                print(f"   Take Profit: ${self.take_profit:.2f}\n")
                            else:
                                print(f"  ❌ Failed to enter\n")
                                trade_count -= 1
                        
                        # Check if NO is at exactly 0.98
                        elif abs(no_price - self.entry_price) < 0.001:  # Within 0.1 cent
                            trade_count += 1
                            entry_side = 'NO'
                            entry_token = market['no_token']
                            
                            print(f"\n{'='*60}")
                            print(f"🎯 ENTRY SIGNAL #{trade_count}")
                            print(f"{'='*60}")
                            print(f"  Side: {entry_side}")
                            print(f"  Price: ${no_price:.2f}")
                            print(f"  Time Remaining: {time_remaining}s")
                            print(f"{'='*60}\n")
                            
                            print(f"💸 Entering {entry_side} at ${no_price:.2f}...")
                            
                            entry_time = datetime.now(timezone.utc)
                            time_remaining_at_entry = time_remaining
                            order, entry_price = self.place_order(clob_client, entry_token, 'BUY')
                            
                            if order:
                                in_position = True
                                print(f"\n📈 Position opened!")
                                print(f"   Entry: ${entry_price:.2f}")
                                print(f"   Stop Loss: ${self.stop_loss:.2f}")
                                print(f"   Take Profit: ${self.take_profit:.2f}\n")
                            else:
                                print(f"  ❌ Failed to enter\n")
                                trade_count -= 1
                        else:
                            # Display monitoring
                            print(f"  ⏱️  [{time_remaining}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Waiting for $0.98...", end='\r')
                    else:
                        # Still waiting for last 3 minutes
                        mins = time_remaining // 60
                        secs = time_remaining % 60
                        print(f"  ⏰ [{mins}m {secs}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Waiting for last 3 min...", end='\r')
                
            except Exception as e:
                print(f"\n⚠️  Error: {e}")
            
            time.sleep(1)


def main():
    """Main loop"""
    from newmain import PRIVATE_KEY, HOST, CHAIN_ID, SIGNATURE_TYPE, USE_PROXY, TRADING_ADDRESS
    from py_clob_client.client import ClobClient
    
    print(f"\n{'='*60}")
    print(f"💰 98-TO-99 SCALPING STRATEGY")
    print(f"{'='*60}")
    print(f"Strategy:")
    print(f"  1. Wait for last 3 minutes (180s remaining)")
    print(f"  2. Enter side at exactly $0.98")
    print(f"  3. Exit at $0.99 (1 cent profit)")
    print(f"  4. Stop loss: $0.90")
    print(f"  5. Position size: 2 shares")
    print(f"  6. Detailed CSV logging enabled")
    print(f"{'='*60}\n")
    
    bot = ScalpingBot()
    
    print("Connecting to Polymarket...")
    if USE_PROXY:
        client = ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=TRADING_ADDRESS
        )
    else:
        client = ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID
        )
    
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    print(f"✅ Connected\n")
    
    current_market = None
    
    while True:
        try:
            current_timestamp = int(time.time())
            market_timestamp = (current_timestamp // 900) * 900
            expected_slug = f"btc-updown-15m-{market_timestamp}"
            
            time_into_market = current_timestamp - market_timestamp
            
            if not current_market or current_market['slug'] != expected_slug:
                if time_into_market > 30:
                    next_market_in = 900 - time_into_market
                    print(f"⏭️  Too late for current market ({time_into_market}s elapsed)")
                    print(f"⏳ Next market in {next_market_in}s...")
                    time.sleep(min(30, next_market_in))
                    continue
                
                print(f"🔍 Looking for: {expected_slug}")
                
                try:
                    url = f"https://gamma-api.polymarket.com/events?slug={expected_slug}"
                    resp = requests.get(url, timeout=10).json()
                    
                    if resp and len(resp) > 0:
                        event = resp[0]
                        raw_ids = event['markets'][0].get('clobTokenIds')
                        clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
                        
                        current_market = {
                            'slug': expected_slug,
                            'yes_token': clob_ids[0],
                            'no_token': clob_ids[1],
                            'title': event.get('title', expected_slug)
                        }
                        
                        print(f"✅ Found! Trading...\n")
                        
                        bot.trade_market(current_market, client)
                    else:
                        print(f"⏳ Waiting for market...")
                        time.sleep(5)
                        
                except Exception as e:
                    print(f"⚠️  Error: {e}")
                    time.sleep(10)
            
            time.sleep(1)
            
        except KeyboardInterrupt:
            print("\n\n🛑 Bot stopped")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    main()
