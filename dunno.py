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


class CrashReversalTrader:
    """Trade the reversal after a crash"""
    
    def __init__(self):
        self.crash_threshold = 0.35  # 35% drop in 5 seconds
        self.lookback_seconds = 5
        self.take_profit = 0.96
        self.price_history = {
            'YES': deque(maxlen=10),  # Last 10 seconds of prices
            'NO': deque(maxlen=10)
        }
        self.time_history = deque(maxlen=10)
    
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
    
    def place_order(self, clob_client, token_id, side, size=2, max_retries=5):
        """Place FOK order with retries"""
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
        
        # Reset price history
        self.price_history = {
            'YES': deque(maxlen=10),
            'NO': deque(maxlen=10)
        }
        self.time_history = deque(maxlen=10)
        
        print(f"👀 Monitoring for crashes (35% drop in 5s)...\n")
        
        while True:
            now = int(time.time())
            time_remaining = market_end - now
            
            if time_remaining <= 0:
                print(f"\n⏰ Market closed. Trades executed: {trade_count}")
                if in_position:
                    print(f"⚠️  Still in position - closing at market...")
                    self.place_order(clob_client, entry_token, 'SELL', size=2)
                return
            
            # Get current prices
            try:
                yes_book = clob_client.get_order_book(market['yes_token'])
                no_book = clob_client.get_order_book(market['no_token'])
                
                yes_price = max(float(o.price) for o in yes_book.bids) if yes_book.bids else 0
                no_price = max(float(o.price) for o in no_book.bids) if no_book.bids else 0
                
                # Add to history
                self.add_price_observation(yes_price, no_price)
                
                # If in position, monitor for take profit
                if in_position:
                    current_price = self.get_current_price(entry_token, clob_client)
                    pnl = current_price - entry_price
                    pnl_pct = (pnl / entry_price) * 100 if entry_price > 0 else 0
                    
                    elapsed = 900 - time_remaining
                    print(f"  📊 [{elapsed}s] {entry_side}: ${current_price:.2f} | P&L: ${pnl:.2f} ({pnl_pct:+.1f}%) | Target: ${self.take_profit:.2f}", end='\r')
                    
                    # Check take profit
                    if current_price >= self.take_profit:
                        print(f"\n\n🎯 TAKE PROFIT HIT at ${current_price:.2f}")
                        
                        # Force close position immediately
                        print(f"🔚 Force closing position...")
                        close_order, close_price = self.place_order(clob_client, entry_token, 'SELL', size=2)
                        
                        if close_order:
                            # Use actual filled price
                            actual_exit = close_price if close_price else current_price
                            final_pnl = (actual_exit - entry_price) * 2  # Total P&L
                            final_pnl_pct = (actual_exit - entry_price) / entry_price * 100
                            
                            print(f"\n{'='*60}")
                            print(f"📊 TRADE #{trade_count} COMPLETE")
                            print(f"{'='*60}")
                            print(f"  Side: {entry_side}")
                            print(f"  Entry: ${entry_price:.2f}")
                            print(f"  Exit: ${actual_exit:.2f}")
                            print(f"  Shares: 2")
                            print(f"  P&L: ${final_pnl:.2f} ({final_pnl_pct:+.1f}%)")
                            print(f"{'='*60}\n")
                            
                            # Reset position
                            in_position = False
                            entry_token = None
                            entry_price = 0
                            entry_side = None
                            
                            print(f"👀 Resuming crash monitoring...\n")
                        else:
                            # Sell failed - keep trying
                            print(f"  ⚠️  Sell failed, will retry on next tick...")
                        entry_token = None
                        entry_price = 0
                        entry_side = None
                        
                        print(f"👀 Resuming crash monitoring...\n")
                
                # If NOT in position, look for crash
                else:
                    crashed_side, crash_pct = self.detect_crash()
                    
                    if crashed_side:
                        trade_count += 1
                        
                        print(f"\n{'='*60}")
                        print(f"🚨 CRASH #{trade_count} DETECTED!")
                        print(f"{'='*60}")
                        print(f"   Side: {crashed_side}")
                        print(f"   Drop: {crash_pct:.1%}")
                        print(f"   YES Price: ${yes_price:.2f}")
                        print(f"   NO Price: ${no_price:.2f}")
                        print(f"{'='*60}\n")
                        
                        # Enter opposite side
                        if crashed_side == 'YES':
                            entry_side = 'NO'
                            entry_token = market['no_token']
                        else:
                            entry_side = 'YES'
                            entry_token = market['yes_token']
                        
                        print(f"💸 Entering {entry_side} (opposite of crashed {crashed_side})...")
                        
                        order, entry_price = self.place_order(clob_client, entry_token, 'BUY', size=2)
                        
                        if order:
                            in_position = True
                            print(f"\n📈 Position opened. Monitoring for ${self.take_profit:.2f}...\n")
                        else:
                            print(f"  ❌ Failed to enter, continuing to monitor...\n")
                            trade_count -= 1  # Don't count failed entry
                    
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
    print(f"🔄 CRASH REVERSAL STRATEGY")
    print(f"{'='*60}")
    print(f"Strategy:")
    print(f"  1. Monitor from market start (900s)")
    print(f"  2. Detect 35% crash in 5 seconds")
    print(f"  3. Enter OPPOSITE side of crash")
    print(f"  4. Exit at $0.96 (take profit)")
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
