import os
import time
import requests
import json
from datetime import datetime, timezone
import csv

# ==========================================
# 🔧 CONFIGURATION - PAPER TRADING MODE
# ==========================================

# Paper Trading Settings
PAPER_TRADING_MODE = True  # Set to False for live trading
PAPER_TRADE_LOG = "paper_trades.csv"

# Observation & Decision Windows
OBSERVATION_START = 900      # Start recording at 15:00 remaining
DECISION_START = 540         # Start evaluating at 9:00 remaining
DECISION_END = 360           # Latest decision at 6:00 remaining

# Signal Thresholds (FIXED)
MOMENTUM_THRESHOLD = 0.004   # Only trade when momentum > +0.004
MIN_SIGNALS_REQUIRED = 1      # Momentum only (pure strategy)
USE_CONFIRMATION_SIGNALS = False  # Disable - they don't help

# Position Management (IMPROVED)
POSITION_SIZE = 10
MAX_ENTRY_PRICE = 0.60       # Lowered from 0.80 for better R:R
TAKE_PROFIT = 0.96
STOP_LOSS = 0.55             # Raised from 0.35 to reduce losses
TRAILING_STOP_TRIGGER = 0.90
TRAILING_STOP_DISTANCE = 0.10

# System
CHECK_INTERVAL = 1

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
# PAPER TRADING BOT
# ==========================================

class PaperTradingBot:
    def __init__(self):
        print("\n" + "="*80)
        print("📝 PAPER TRADING MODE - Momentum Strategy")
        print("="*80)
        print("\n💡 This bot will SIMULATE trades without placing real orders")
        print("   It will show you what WOULD have happened\n")
        
        self.traded_markets = set()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        self.session_pnl = 0
        
        self.price_history = PriceHistory()
        self.initialize_log()
    
    def initialize_log(self):
        if not os.path.exists(PAPER_TRADE_LOG):
            headers = [
                'timestamp', 'market_slug', 'market_title',
                'entry_side', 'entry_price', 'shares',
                'yes_momentum', 'no_momentum',
                'yes_early', 'yes_mid', 'no_early', 'no_mid',
                'signal_valid', 'signals_count',
                'time_remaining_at_entry',
                'exit_reason', 'exit_price', 'exit_time_remaining',
                'highest_price', 'lowest_price',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'risk_reward_ratio'
            ]
            
            with open(PAPER_TRADE_LOG, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            
            print(f"📊 Paper trade log: {PAPER_TRADE_LOG}\n")
    
    def log_trade(self, trade_data):
        with open(PAPER_TRADE_LOG, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=trade_data.keys())
            writer.writerow(trade_data)
    
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
        """Get best ask price from Polymarket API"""
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            resp = requests.get(url, timeout=5).json()
            
            if 'asks' in resp and resp['asks']:
                return float(resp['asks'][0]['price'])
            return None
        except:
            return None
    
    def get_best_bid(self, token_id):
        """Get best bid price from Polymarket API"""
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            resp = requests.get(url, timeout=5).json()
            
            if 'bids' in resp and resp['bids']:
                return float(resp['bids'][0]['price'])
            return None
        except:
            return None
    
    def validate_signal(self, signals, entry_side):
        """
        CRITICAL FIX: Validate that momentum is actually positive
        This prevents the bug that caused 36% of trades to fail
        """
        if entry_side == "YES":
            mom = signals['yes_momentum']
            if mom <= MOMENTUM_THRESHOLD:
                print(f"   ❌ INVALID: YES momentum {mom:+.4f} below threshold +{MOMENTUM_THRESHOLD}")
                return False
            print(f"   ✅ VALID: YES momentum {mom:+.4f} > +{MOMENTUM_THRESHOLD}")
        else:
            mom = signals['no_momentum']
            if mom <= MOMENTUM_THRESHOLD:
                print(f"   ❌ INVALID: NO momentum {mom:+.4f} below threshold +{MOMENTUM_THRESHOLD}")
                return False
            print(f"   ✅ VALID: NO momentum {mom:+.4f} > +{MOMENTUM_THRESHOLD}")
        
        return True
    
    def calculate_signals(self):
        """Calculate all trading signals"""
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
        
        # Primary signal: Momentum (MUST be positive)
        if yes_momentum > MOMENTUM_THRESHOLD:
            signals['signals'].append(('momentum', 'YES', yes_momentum))
        
        if no_momentum > MOMENTUM_THRESHOLD:
            signals['signals'].append(('momentum', 'NO', no_momentum))
        
        # Confirmation signals (if enabled)
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
        
        # Determine side based on momentum strength
        yes_signals = [s for s in signals['signals'] if s[1] == 'YES']
        no_signals = [s for s in signals['signals'] if s[1] == 'NO']
        
        if len(yes_signals) >= MIN_SIGNALS_REQUIRED and len(yes_signals) > len(no_signals):
            signals['side'] = "YES"
            signals['confidence'] = len(yes_signals)
        elif len(no_signals) >= MIN_SIGNALS_REQUIRED and len(no_signals) > len(yes_signals):
            signals['side'] = "NO"
            signals['confidence'] = len(no_signals)
        
        return signals
    
    def simulate_trade(self, market, market_start_time):
        """
        Main strategy execution - SIMULATED
        This runs the full strategy but doesn't place real trades
        """
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
            return "already_traded"
        
        self.price_history.clear()
        
        print(f"\n{'='*80}")
        print(f"📊 OBSERVATION PHASE: {market['title']}")
        print(f"{'='*80}")
        print(f"Recording price data from 15:00 → 9:00 remaining...\n")
        
        # Phase 1: OBSERVATION (15:00 → 9:00)
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
        
        # Check if we have enough data
        if len(self.price_history.timestamps) < 10:
            print(f"\n⚠️ Insufficient data ({len(self.price_history.timestamps)} obs)")
            self.traded_markets.add(slug)
            return "insufficient_data"
        
        # Phase 2: EVALUATION
        print(f"\n\n{'='*80}")
        print(f"🔍 EVALUATION PHASE")
        print(f"{'='*80}")
        print(f"Observations: {len(self.price_history.timestamps)}\n")
        
        signals = self.calculate_signals()
        
        print(f"📊 MOMENTUM ANALYSIS:")
        if signals['yes_momentum'] is not None:
            print(f"   YES: Early ${signals['yes_early']:.3f} → Mid ${signals['yes_mid']:.3f} = {signals['yes_momentum']:+.4f}")
            print(f"   NO:  Early ${signals['no_early']:.3f} → Mid ${signals['no_mid']:.3f} = {signals['no_momentum']:+.4f}")
        
        print(f"\n🎲 SIGNALS ({len(signals['signals'])}):")
        for sig_type, sig_side, sig_value in signals['signals']:
            print(f"   ✓ {sig_type.upper()}: {sig_side}")
        
        if not signals['side']:
            print(f"\n⏭️  No signal (need {MIN_SIGNALS_REQUIRED})")
            self.traded_markets.add(slug)
            return "no_signal"
        
        entry_side = signals['side']
        entry_token = market['yes_token'] if entry_side == "YES" else market['no_token']
        
        # Get entry price
        entry_price = self.get_best_ask(entry_token)
        if entry_price is None:
            print(f"\n❌ Could not get price")
            self.traded_markets.add(slug)
            return "no_price"
        
        # VALIDATION CHECK (Critical fix)
        print(f"\n🔍 SIGNAL VALIDATION:")
        if not self.validate_signal(signals, entry_side):
            print(f"\n⛔ TRADE BLOCKED - Invalid signal (negative momentum)")
            self.traded_markets.add(slug)
            return "invalid_signal"
        
        # Price check
        if entry_price > MAX_ENTRY_PRICE:
            print(f"\n❌ Price too high: ${entry_price:.3f} > ${MAX_ENTRY_PRICE:.2f}")
            self.traded_markets.add(slug)
            return "price_too_high"
        
        # Calculate R:R
        risk = entry_price - STOP_LOSS
        reward = TAKE_PROFIT - entry_price
        rr_ratio = reward / risk if risk > 0 else 0
        
        print(f"\n💰 RISK/REWARD:")
        print(f"   Entry: ${entry_price:.3f}")
        print(f"   Risk: ${risk:.3f} | Reward: ${reward:.3f}")
        print(f"   R:R Ratio: 1:{rr_ratio:.2f}")
        
        if rr_ratio < 1.0:
            print(f"   ⚠️ Warning: Poor R:R ratio (< 1:1)")
        
        # Phase 3: SIMULATED ENTRY
        current_time = time.time()
        time_remaining = market_end_time - current_time
        
        print(f"\n{'='*80}")
        print(f"📝 SIMULATED ENTRY")
        print(f"{'='*80}")
        print(f"   Side: {entry_side}")
        print(f"   Price: ${entry_price:.3f}")
        print(f"   Shares: {POSITION_SIZE}")
        print(f"   Time: {int(time_remaining)}s remaining")
        print(f"\n   💡 In live mode, would BUY {POSITION_SIZE} shares here")
        
        trade_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'market_slug': slug,
            'market_title': market['title'],
            'entry_side': entry_side,
            'entry_price': entry_price,
            'shares': POSITION_SIZE,
            'yes_momentum': signals['yes_momentum'],
            'no_momentum': signals['no_momentum'],
            'yes_early': signals['yes_early'],
            'yes_mid': signals['yes_mid'],
            'no_early': signals['no_early'],
            'no_mid': signals['no_mid'],
            'signal_valid': True,
            'signals_count': signals['confidence'],
            'time_remaining_at_entry': int(time_remaining),
            'risk_reward_ratio': rr_ratio
        }
        
        # Phase 4: SIMULATED MONITORING
        return self.simulate_monitoring(
            market, entry_token, entry_side, entry_price,
            market_end_time, trade_data
        )
    
    def simulate_monitoring(self, market, entry_token, entry_side, entry_price,
                           market_end_time, trade_data):
        """Simulate position monitoring"""
        print(f"\n📊 MONITORING SIMULATED POSITION...")
        print(f"   Watching for TP: ${TAKE_PROFIT:.2f} | SL: ${STOP_LOSS:.2f}\n")
        
        slug = market['slug']
        stop_loss = STOP_LOSS
        trailing_stop_active = False
        highest_price = entry_price
        lowest_price = entry_price
        
        while True:
            try:
                current_time = time.time()
                time_remaining = market_end_time - current_time
                
                # Market closing
                if time_remaining <= 10:
                    exit_price = self.get_best_bid(entry_token) or 0.99
                    pnl = (exit_price - entry_price) * POSITION_SIZE
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    print(f"\n\n⏰ MARKET CLOSED")
                    print(f"   Exit: ${exit_price:.3f}")
                    print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    
                    trade_data.update({
                        'exit_reason': 'MARKET_CLOSED',
                        'exit_price': exit_price,
                        'exit_time_remaining': int(time_remaining),
                        'highest_price': highest_price,
                        'lowest_price': lowest_price,
                        'gross_pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'win_loss': 'WIN' if pnl > 0 else 'LOSS'
                    })
                    
                    self.log_trade(trade_data)
                    self.session_trades += 1
                    if pnl > 0:
                        self.session_wins += 1
                    else:
                        self.session_losses += 1
                    self.session_pnl += pnl
                    self.traded_markets.add(slug)
                    
                    return "market_closed"
                
                # Get current price
                current_bid = self.get_best_bid(entry_token)
                if current_bid is None:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Track price range
                if current_bid > highest_price:
                    highest_price = current_bid
                if current_bid < lowest_price:
                    lowest_price = current_bid
                
                # Trailing stop logic
                if current_bid >= TRAILING_STOP_TRIGGER and not trailing_stop_active:
                    trailing_stop_active = True
                    stop_loss = current_bid - TRAILING_STOP_DISTANCE
                    print(f"\n🎯 Trailing stop activated @ ${stop_loss:.2f}")
                
                if trailing_stop_active:
                    new_stop = current_bid - TRAILING_STOP_DISTANCE
                    if new_stop > stop_loss:
                        stop_loss = new_stop
                
                # Display status
                pnl_now = (current_bid - entry_price) * POSITION_SIZE
                pnl_pct_now = ((current_bid - entry_price) / entry_price) * 100
                
                mins = int(time_remaining // 60)
                secs = int(time_remaining % 60)
                print(f"\r⏱️  [{mins:02d}:{secs:02d}] ${current_bid:.3f} | P&L: ${pnl_now:+.2f} ({pnl_pct_now:+.2f}%) | Stop: ${stop_loss:.2f}", end="", flush=True)
                
                # Check stop loss
                if current_bid <= stop_loss:
                    pnl = (current_bid - entry_price) * POSITION_SIZE
                    pnl_pct = ((current_bid - entry_price) / entry_price) * 100
                    
                    print(f"\n\n🛑 STOP LOSS HIT @ ${current_bid:.3f}")
                    print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    
                    trade_data.update({
                        'exit_reason': 'STOP_LOSS',
                        'exit_price': current_bid,
                        'exit_time_remaining': int(time_remaining),
                        'highest_price': highest_price,
                        'lowest_price': lowest_price,
                        'gross_pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'win_loss': 'LOSS' if pnl < 0 else 'BREAKEVEN'
                    })
                    
                    self.log_trade(trade_data)
                    self.session_trades += 1
                    if pnl < 0:
                        self.session_losses += 1
                    self.session_pnl += pnl
                    self.traded_markets.add(slug)
                    
                    return "stop_loss"
                
                # Check take profit
                if current_bid >= TAKE_PROFIT:
                    pnl = (current_bid - entry_price) * POSITION_SIZE
                    pnl_pct = ((current_bid - entry_price) / entry_price) * 100
                    
                    print(f"\n\n🚀 TAKE PROFIT @ ${current_bid:.3f}")
                    print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    
                    trade_data.update({
                        'exit_reason': 'TAKE_PROFIT',
                        'exit_price': current_bid,
                        'exit_time_remaining': int(time_remaining),
                        'highest_price': highest_price,
                        'lowest_price': lowest_price,
                        'gross_pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'win_loss': 'WIN'
                    })
                    
                    self.log_trade(trade_data)
                    self.session_trades += 1
                    self.session_wins += 1
                    self.session_pnl += pnl
                    self.traded_markets.add(slug)
                    
                    return "take_profit"
                
                time.sleep(CHECK_INTERVAL)
                
            except Exception as e:
                print(f"\n❌ Error: {e}")
                time.sleep(5)
    
    def run(self):
        """Main bot loop"""
        print(f"{'='*80}")
        print(f"🚀 PAPER TRADING BOT STARTED")
        print(f"{'='*80}")
        print(f"\n⚙️  CONFIGURATION:")
        print(f"   Momentum threshold: +{MOMENTUM_THRESHOLD:.4f}")
        print(f"   Max entry: ${MAX_ENTRY_PRICE:.2f}")
        print(f"   Stop loss: ${STOP_LOSS:.2f}")
        print(f"   Take profit: ${TAKE_PROFIT:.2f}")
        print(f"   Position size: {POSITION_SIZE} shares")
        print(f"   Confirmation signals: {USE_CONFIRMATION_SIGNALS}")
        print(f"\n💡 All trades are SIMULATED - no real money at risk\n")
        
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
                        print(f"   Time remaining: {time_left//60}m {time_left%60}s\n")
                        
                        status = self.simulate_trade(current_market, market_timestamp)
                        
                        if status in ["take_profit", "stop_loss", "market_closed"]:
                            wr = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                            
                            print(f"\n{'='*80}")
                            print(f"📊 SESSION SUMMARY")
                            print(f"{'='*80}")
                            print(f"   Trades: {self.session_trades}")
                            print(f"   Wins: {self.session_wins} | Losses: {self.session_losses}")
                            print(f"   Win Rate: {wr:.1f}%")
                            print(f"   Total P&L: ${self.session_pnl:+.2f}")
                            print(f"{'='*80}\n")
                    else:
                        print(f"⏳ Market not available yet...")
                        time.sleep(30)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print(f"\n\n{'='*80}")
                print(f"🛑 PAPER TRADING STOPPED")
                print(f"{'='*80}")
                wr = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                print(f"\n📊 FINAL RESULTS:")
                print(f"   Total trades: {self.session_trades}")
                print(f"   Wins: {self.session_wins} | Losses: {self.session_losses}")
                print(f"   Win rate: {wr:.1f}%")
                print(f"   Total P&L: ${self.session_pnl:+.2f}")
                print(f"\n📄 Check {PAPER_TRADE_LOG} for detailed results")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)

if __name__ == "__main__":
    bot = PaperTradingBot()
    bot.run()
