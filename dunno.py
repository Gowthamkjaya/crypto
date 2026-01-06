# ==========================================
# CRASH REVERSAL STRATEGY
# ==========================================
# Monitor from market start (900s remaining)
# Detect 35% crash in 5 seconds
# Enter opposite side
# Exit at $0.96

import time
import requests
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from collections import deque
import csv
import os


class CrashReversalTrader:
    """Trade the reversal after a crash"""
    
    def __init__(self):
        self.crash_threshold = 0.45  # 45% drop in 5 seconds (avoid dead zone 38-45%)
        self.lookback_seconds = 5
        self.take_profit_pct = 0.05  # 5% profit from entry (initial target)
        self.trailing_stop_pct = 0.05  # Lock in 5% profit with trailing stop
        self.max_take_profit = 0.98  # Let winners run to 98 cents
        self.stop_loss_pct = 0.50  # 50% loss from entry
        self.position_size = 2  # Always trade 2 shares
        self.min_entry_price = 0.10  # Don't enter below 10 cents
        self.max_entry_price = 0.90  # Don't enter above 90 cents
        self.trade_timeout_seconds = 60  # Kill breakeven trades after 60 seconds
        self.min_minute_in_cycle = 9  # Don't enter in first 9 minutes (minute 12 rule)
        self.price_history = {
            'YES': deque(maxlen=10),  # Last 10 seconds of prices
            'NO': deque(maxlen=10)
        }
        self.time_history = deque(maxlen=10)
        self.log_file = "crash_reversal_trades.csv"
        
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
                'crashed_side',
                'entered_side',
                'crash_percentage',
                'minute_in_cycle',
                'entry_time',
                'entry_price',
                'stop_loss_price',
                'initial_take_profit_price',
                'highest_price_reached',
                'trailing_stop_activated',
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
    
    def add_price_observation(self, yes_price, no_price):
        """Add price observation to history"""
        self.price_history['YES'].append(yes_price)
        self.price_history['NO'].append(no_price)
        self.time_history.append(time.time())
    
    def detect_crash(self):
        """
        Detect if either side crashed 35% in last 5 seconds
        
        Returns: ('YES' or 'NO' or None, crash_percent)
        """
        if len(self.price_history['YES']) < 6:
            return None, 0
        
        # Get prices from 5 seconds ago vs now
        current_yes = self.price_history['YES'][-1]
        current_no = self.price_history['NO'][-1]
        
        old_yes = self.price_history['YES'][-6]  # ~5 seconds ago
        old_no = self.price_history['NO'][-6]
        
        # Calculate drops
        yes_drop = (old_yes - current_yes) / old_yes if old_yes > 0 else 0
        no_drop = (old_no - current_no) / old_no if old_no > 0 else 0
        
        # Check if YES crashed
        if yes_drop >= self.crash_threshold:
            return 'YES', yes_drop
        
        # Check if NO crashed
        if no_drop >= self.crash_threshold:
            return 'NO', no_drop
        
        return None, 0
    
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
        """Main trading logic - monitor entire market for multiple crashes"""
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
        stop_loss_price = 0
        take_profit_price = 0
        crashed_side = None
        crash_pct = 0
        highest_price = 0
        trailing_stop_active = False
        trailing_stop_price = 0
        minute_in_cycle = 0
        
        # Reset price history
        self.price_history = {
            'YES': deque(maxlen=10),
            'NO': deque(maxlen=10)
        }
        self.time_history = deque(maxlen=10)
        
        print(f"👀 Monitoring for crashes (45%+ drop in 5s)...\n")
        
        while True:
            now = int(time.time())
            time_remaining = market_end - now
            
            if time_remaining <= 0:
                print(f"\n⏰ Market closed. Trades executed: {trade_count}")
                if in_position:
                    print(f"⚠️  Still in position - emergency closing...")
                    self.place_order(clob_client, entry_token, 'SELL')
                return
            
            # Get current prices
            try:
                yes_book = clob_client.get_order_book(market['yes_token'])
                no_book = clob_client.get_order_book(market['no_token'])
                
                yes_price = max(float(o.price) for o in yes_book.bids) if yes_book.bids else 0
                no_price = max(float(o.price) for o in no_book.bids) if no_book.bids else 0
                
                # Add to history
                self.add_price_observation(yes_price, no_price)
                
                # If in position, monitor for take profit, trailing stop, timeout, and stop loss
                if in_position:
                    current_price = self.get_current_price(entry_token, clob_client)
                    pnl = current_price - entry_price
                    pnl_pct = (pnl / entry_price) * 100 if entry_price > 0 else 0
                    time_in_trade = (datetime.now(timezone.utc) - entry_time).total_seconds()
                    
                    # Track highest price
                    if current_price > highest_price:
                        highest_price = current_price
                    
                    # Activate trailing stop once 5% profit is reached
                    if not trailing_stop_active and current_price >= take_profit_price:
                        trailing_stop_active = True
                        trailing_stop_price = entry_price * (1 + self.trailing_stop_pct)
                        print(f"\n✨ TRAILING STOP ACTIVATED at ${current_price:.2f}")
                        print(f"   Trailing Stop: ${trailing_stop_price:.2f} (locked in 5% profit)")
                        print(f"   Letting winner run to ${self.max_take_profit:.2f}...\n")
                    
                    # Update trailing stop as price rises
                    if trailing_stop_active:
                        new_trailing_stop = current_price * (1 - self.trailing_stop_pct)
                        if new_trailing_stop > trailing_stop_price:
                            trailing_stop_price = new_trailing_stop
                    
                    elapsed = 900 - time_remaining
                    if trailing_stop_active:
                        print(f"  📊 [{elapsed}s] {entry_side}: ${current_price:.2f} | High: ${highest_price:.2f} | TS: ${trailing_stop_price:.2f} | Max: ${self.max_take_profit:.2f}", end='\r')
                    else:
                        print(f"  📊 [{elapsed}s] {entry_side}: ${current_price:.2f} | P&L: ${pnl:.2f} ({pnl_pct:+.1f}%) | SL: ${stop_loss_price:.2f} | TP: ${take_profit_price:.2f}", end='\r')
                    
                    exit_triggered = False
                    exit_reason = None
                    
                    # Check stop loss FIRST
                    if current_price <= stop_loss_price:
                        print(f"\n\n🛑 STOP LOSS HIT at ${current_price:.2f}")
                        exit_triggered = True
                        exit_reason = 'STOP_LOSS'
                    
                    # Check trailing stop (if active)
                    elif trailing_stop_active and current_price <= trailing_stop_price:
                        print(f"\n\n📉 TRAILING STOP HIT at ${current_price:.2f}")
                        print(f"   Highest: ${highest_price:.2f} | Trailing Stop: ${trailing_stop_price:.2f}")
                        exit_triggered = True
                        exit_reason = 'TRAILING_STOP'
                    
                    # Check max take profit (98 cents)
                    elif current_price >= self.max_take_profit:
                        print(f"\n\n🚀 MAX TAKE PROFIT HIT at ${current_price:.2f}")
                        exit_triggered = True
                        exit_reason = 'MAX_TAKE_PROFIT'
                    
                    # Check 60-second timeout for breakeven trades
                    elif time_in_trade >= self.trade_timeout_seconds and abs(pnl_pct) < 2:
                        print(f"\n\n⏱️  60-SECOND TIMEOUT at ${current_price:.2f}")
                        print(f"   Trade stuck at breakeven for {int(time_in_trade)}s")
                        exit_triggered = True
                        exit_reason = 'TIMEOUT_BREAKEVEN'
                    
                    if exit_triggered:
                        # Force close position immediately
                        print(f"🔚 Force closing position...")
                        exit_time = datetime.now(timezone.utc)
                        close_order, close_price = self.place_order(clob_client, entry_token, 'SELL')
                        
                        if close_order:
                            # Use actual filled price
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
                            print(f"  Time in Trade: {int(time_in_trade)}s ({int(time_in_trade/60)}m {int(time_in_trade%60)}s)")
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
                                'crashed_side': crashed_side,
                                'entered_side': entry_side,
                                'crash_percentage': f"{crash_pct:.2%}",
                                'minute_in_cycle': minute_in_cycle,
                                'entry_time': entry_time.isoformat(),
                                'entry_price': f"{entry_price:.4f}",
                                'stop_loss_price': f"{stop_loss_price:.4f}",
                                'initial_take_profit_price': f"{take_profit_price:.4f}",
                                'highest_price_reached': f"{highest_price:.4f}",
                                'trailing_stop_activated': trailing_stop_active,
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
                            
                            # Reset position - COMPLETE closure before allowing new entries
                            in_position = False
                            entry_token = None
                            entry_price = 0
                            entry_side = None
                            entry_time = None
                            stop_loss_price = 0
                            take_profit_price = 0
                            crashed_side = None
                            crash_pct = 0
                            highest_price = 0
                            trailing_stop_active = False
                            trailing_stop_price = 0
                            minute_in_cycle = 0
                            
                            print(f"👀 Resuming crash monitoring...\n")
                        else:
                            # Sell failed - keep trying, DON'T reset position
                            print(f"  ⚠️  Sell failed, will retry on next tick...")
                
                # If NOT in position, look for crash
                else:
                    # Calculate minute in cycle (0-14)
                    current_minute_in_cycle = ((900 - time_remaining) // 60) % 15
                    
                    detected_crash_side, detected_crash_pct = self.detect_crash()
                    
                    if detected_crash_side:
                        # Check minute-in-cycle rule FIRST
                        if current_minute_in_cycle <= self.min_minute_in_cycle:
                            print(f"\n⚠️  Crash detected but in FIRST 3 MINUTES (minute {current_minute_in_cycle})")
                            print(f"   Waiting until minute >{self.min_minute_in_cycle} to enter trades")
                            print(f"   Skipping this crash...\n")
                            continue
                        
                        trade_count += 1
                        minute_in_cycle = current_minute_in_cycle
                        
                        print(f"\n{'='*60}")
                        print(f"🚨 CRASH #{trade_count} DETECTED!")
                        print(f"{'='*60}")
                        print(f"   Side: {detected_crash_side}")
                        print(f"   Drop: {detected_crash_pct:.1%}")
                        print(f"   Minute in Cycle: {minute_in_cycle}")
                        print(f"   YES Price: ${yes_price:.2f}")
                        print(f"   NO Price: ${no_price:.2f}")
                        print(f"{'='*60}\n")
                        
                        # Store crash info
                        crashed_side = detected_crash_side
                        crash_pct = detected_crash_pct
                        
                        # Enter opposite side
                        if crashed_side == 'YES':
                            entry_side = 'NO'
                            entry_token = market['no_token']
                            entry_price_estimate = no_price
                        else:
                            entry_side = 'YES'
                            entry_token = market['yes_token']
                            entry_price_estimate = yes_price
                        
                        # Check if entry price is within acceptable range
                        if entry_price_estimate < self.min_entry_price:
                            print(f"  ⚠️  {entry_side} price too low (${entry_price_estimate:.2f} < ${self.min_entry_price:.2f})")
                            print(f"  ⏭️  Skipping entry - low ROI potential\n")
                            trade_count -= 1  # Don't count skipped entry
                            continue
                        
                        if entry_price_estimate > self.max_entry_price:
                            print(f"  ⚠️  {entry_side} price too high (${entry_price_estimate:.2f} > ${self.max_entry_price:.2f})")
                            print(f"  ⏭️  Skipping entry - low ROI potential\n")
                            trade_count -= 1  # Don't count skipped entry
                            continue
                        
                        print(f"💸 Entering {entry_side} (opposite of crashed {crashed_side})...")
                        print(f"   Entry price estimate: ${entry_price_estimate:.2f}")
                        
                        entry_time = datetime.now(timezone.utc)
                        order, entry_price = self.place_order(clob_client, entry_token, 'BUY')
                        
                        if order:
                            # Calculate stop loss and take profit based on actual entry price
                            stop_loss_price = entry_price * (1 - self.stop_loss_pct)
                            take_profit_price = entry_price * (1 + self.take_profit_pct)
                            
                            in_position = True
                            print(f"\n📈 Position opened!")
                            print(f"   Entry: ${entry_price:.2f}")
                            print(f"   Stop Loss (-50%): ${stop_loss_price:.2f}")
                            print(f"   Take Profit (+5%): ${take_profit_price:.2f}\n")
                        else:
                            print(f"  ❌ Failed to enter, continuing to monitor...\n")
                            trade_count -= 1  # Don't count failed entry
                            # Reset crash data since entry failed
                            crashed_side = None
                            crash_pct = 0
                    
                    else:
                        # Display monitoring
                        elapsed = 900 - time_remaining
                        print(f"  ⏱️  [{elapsed}s] YES: ${yes_price:.2f} | NO: ${no_price:.2f} | Waiting for crash...", end='\r')
                
            except Exception as e:
                print(f"\n⚠️  Error: {e}")
            
            time.sleep(1)


def main():
    """Main loop"""
    from newmain import PRIVATE_KEY, HOST, CHAIN_ID, SIGNATURE_TYPE, USE_PROXY, TRADING_ADDRESS
    from py_clob_client.client import ClobClient
    
    print(f"\n{'='*60}")
    print(f"🔄 CRASH REVERSAL STRATEGY v2.0")
    print(f"{'='*60}")
    print(f"Strategy Rules:")
    print(f"  1. Monitor from market start (900s)")
    print(f"  2. Detect 45%+ crash in 5 seconds (avoid 38-45% dead zone)")
    print(f"  3. MINUTE 12 RULE: Only enter after minute 9 in cycle")
    print(f"  4. Enter OPPOSITE side (only if $0.10 - $0.90)")
    print(f"  5. Position Size: 2 shares")
    print(f"  6. Stop Loss: 50% loss from entry")
    print(f"  7. Take Profit: 5% initially, then trailing stop")
    print(f"  8. Trailing Stop: Lock in 5% profit, let winners run to $0.98")
    print(f"  9. 60-SECOND RULE: Kill breakeven trades after 60s")
    print(f" 10. Detailed CSV logging enabled")
    print(f"{'='*60}\n")
    
    trader = CrashReversalTrader()
    
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
            
            # Check if we should start monitoring this market
            time_into_market = current_timestamp - market_timestamp
            
            if not current_market or current_market['slug'] != expected_slug:
                # Only start if we're early enough (within first 30 seconds)
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
                        
                        trader.trade_market(current_market, client)
                    else:
                        print(f"⏳ Waiting for market...")
                        time.sleep(5)
                        
                except Exception as e:
                    print(f"⚠️  Error: {e}")
                    time.sleep(10)
            
            time.sleep(1)
            
        except KeyboardInterrupt:
            print("\n\n🛑 Trader stopped")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    main()
