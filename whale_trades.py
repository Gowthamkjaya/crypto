import time
import requests
import pandas as pd
import os
from datetime import datetime
import json

# --- CONFIGURATION ---
INPUT_FILE = "whale_trades.csv"  # From whale logger
OUTPUT_FILE = "whale_trades_enriched.csv"
POLL_INTERVAL = 5  # Check for new trades every N seconds
SAVE_RAW_JSON = True  # Save full API responses
RAW_JSON_DIR = "trade_snapshots"
# ---------------------

class TradeEnricher:
    def __init__(self):
        self.processed_trades = set()
        self.load_processed_trades()
        self.initialize_output()
        
        if SAVE_RAW_JSON and not os.path.exists(RAW_JSON_DIR):
            os.makedirs(RAW_JSON_DIR)
            print(f"📁 Created {RAW_JSON_DIR}/")
    
    def load_processed_trades(self):
        """Load already processed trade IDs to avoid duplicates"""
        if os.path.exists(OUTPUT_FILE):
            try:
                df = pd.read_csv(OUTPUT_FILE)
                self.processed_trades = set(df['trade_id'].dropna())
                print(f"📚 Loaded {len(self.processed_trades)} processed trades")
            except:
                pass
    
    def initialize_output(self):
        """Create enriched CSV with extended columns"""
        if not os.path.exists(OUTPUT_FILE):
            pd.DataFrame(columns=[
                # Original trade info
                'trade_id', 'timestamp', 'wallet', 'market_slug', 'side', 
                'trade_price', 'trade_size', 'trade_value',
                
                # Market state at trade time
                'market_title', 'market_end_time', 'time_remaining_seconds',
                'market_closed', 'market_volume', 'market_liquidity',
                
                # Orderbook state
                'yes_best_bid', 'yes_best_ask', 'yes_bid_volume', 'yes_ask_volume',
                'no_best_bid', 'no_best_ask', 'no_bid_volume', 'no_ask_volume',
                'spread', 'implied_probability',
                
                # Price context (before trade)
                'yes_price_5min_ago', 'yes_price_1min_ago',
                'price_momentum', 'volatility_score',
                
                # Whale behavior analysis
                'unusual_size', 'contrarian_trade', 'market_maker_activity',
                
                # Outcome tracking (fill later)
                'winning_side', 'trade_profit_loss', 'market_resolution_time'
                
            ]).to_csv(OUTPUT_FILE, index=False)
            print(f"📁 Created {OUTPUT_FILE}")
    
    def get_market_info(self, market_slug):
        """Fetch market metadata"""
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={market_slug}"
            r = requests.get(url, timeout=10)
            
            if r.status_code == 200:
                data = r.json()
                if data and len(data) > 0:
                    event = data[0]
                    market = event.get('markets', [{}])[0]
                    
                    return {
                        'title': event.get('title', 'Unknown'),
                        'end_time': event.get('endDate') or market.get('endDate'),
                        'closed': market.get('closed', False),
                        'volume': float(market.get('volume', 0)),
                        'liquidity': float(market.get('liquidity', 0)),
                        'tokens': json.loads(market.get('clobTokenIds', '[]'))
                    }
        except Exception as e:
            print(f"⚠️ Error fetching market info: {e}")
        
        return None
    
    def get_orderbook(self, token_id):
        """Fetch current orderbook for a token"""
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            r = requests.get(url, timeout=10)
            
            if r.status_code == 200:
                book = r.json()
                
                bids = book.get('bids', [])
                asks = book.get('asks', [])
                
                best_bid = max([float(b['price']) for b in bids]) if bids else 0
                best_ask = min([float(a['price']) for a in asks]) if asks else 0
                bid_volume = sum([float(b['size']) for b in bids])
                ask_volume = sum([float(a['size']) for a in asks])
                
                return {
                    'best_bid': best_bid,
                    'best_ask': best_ask,
                    'bid_volume': round(bid_volume, 2),
                    'ask_volume': round(ask_volume, 2),
                    'spread': round(best_ask - best_bid, 4) if best_ask and best_bid else 0
                }
        except Exception as e:
            print(f"⚠️ Error fetching orderbook: {e}")
        
        return None
    
    def get_price_history(self, token_id, lookback_minutes=5):
        """Get recent price movements"""
        try:
            # Note: This endpoint may vary - adjust based on actual API
            url = f"https://clob.polymarket.com/prices-history?token_id={token_id}&interval=1m&limit={lookback_minutes}"
            r = requests.get(url, timeout=10)
            
            if r.status_code == 200:
                history = r.json()
                
                if history and len(history) > 0:
                    prices = [float(p['price']) for p in history]
                    
                    price_5min = prices[0] if len(prices) > 4 else None
                    price_1min = prices[-2] if len(prices) > 1 else None
                    current = prices[-1]
                    
                    # Calculate momentum
                    momentum = 0
                    if price_5min:
                        momentum = ((current - price_5min) / price_5min) * 100
                    
                    # Calculate volatility (standard deviation)
                    volatility = pd.Series(prices).std() if len(prices) > 2 else 0
                    
                    return {
                        'price_5min_ago': price_5min,
                        'price_1min_ago': price_1min,
                        'momentum': round(momentum, 2),
                        'volatility': round(volatility, 4)
                    }
        except Exception as e:
            print(f"⚠️ Error fetching price history: {e}")
        
        return {
            'price_5min_ago': None,
            'price_1min_ago': None,
            'momentum': 0,
            'volatility': 0
        }
    
    def calculate_time_remaining(self, end_time_str):
        """Calculate seconds until market close"""
        try:
            end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
            now = datetime.now(end_time.tzinfo)
            delta = (end_time - now).total_seconds()
            return max(0, int(delta))
        except:
            return None
    
    def analyze_trade_context(self, trade, market_info, orderbook_yes, orderbook_no):
        """Analyze if trade is unusual or contrarian"""
        analysis = {
            'unusual_size': False,
            'contrarian_trade': False,
            'market_maker': False
        }
        
        # Check if trade size is large relative to orderbook
        if orderbook_yes and orderbook_no:
            avg_book_size = (orderbook_yes['bid_volume'] + orderbook_yes['ask_volume'] + 
                           orderbook_no['bid_volume'] + orderbook_no['ask_volume']) / 4
            
            if trade['trade_size'] > avg_book_size * 0.5:
                analysis['unusual_size'] = True
        
        # Check if trade is contrarian (buying the less likely side)
        if trade['side'] == 'YES' and orderbook_yes:
            if orderbook_yes['best_ask'] < 0.30:  # Buying unlikely outcome
                analysis['contrarian_trade'] = True
        elif trade['side'] == 'NO' and orderbook_no:
            if orderbook_no['best_ask'] < 0.30:
                analysis['contrarian_trade'] = True
        
        # Check for market maker behavior (placing limit orders on both sides)
        # This would require tracking multiple trades - simplified here
        analysis['market_maker'] = False
        
        return analysis
    
    def save_raw_snapshot(self, trade_id, data):
        """Save complete API responses for later deep analysis"""
        if not SAVE_RAW_JSON:
            return
        
        try:
            filename = f"{RAW_JSON_DIR}/trade_{trade_id}_{int(time.time())}.json"
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"⚠️ Error saving raw snapshot: {e}")
    
    def enrich_trade(self, trade_row):
        """Fetch and combine all contextual data for a trade"""
        print(f"\n🔍 Enriching trade: {trade_row['wallet'][:8]}... on {trade_row['market_slug']}")
        
        trade_id = f"{trade_row['wallet']}_{trade_row['timestamp']}"
        
        # Get market info
        market_info = self.get_market_info(trade_row['market_slug'])
        if not market_info:
            print(f"   ⚠️ Could not fetch market info")
            return None
        
        print(f"   ✓ Market: {market_info['title']}")
        
        # Calculate time remaining
        time_remaining = self.calculate_time_remaining(market_info['end_time']) if market_info['end_time'] else None
        if time_remaining:
            print(f"   ✓ Time remaining: {time_remaining}s ({time_remaining//60}m {time_remaining%60}s)")
        
        # Get orderbooks for both sides
        yes_token = market_info['tokens'][0] if len(market_info['tokens']) > 0 else None
        no_token = market_info['tokens'][1] if len(market_info['tokens']) > 1 else None
        
        orderbook_yes = self.get_orderbook(yes_token) if yes_token else None
        orderbook_no = self.get_orderbook(no_token) if no_token else None
        
        if orderbook_yes:
            print(f"   ✓ YES book: Bid ${orderbook_yes['best_bid']:.3f} | Ask ${orderbook_yes['best_ask']:.3f}")
        if orderbook_no:
            print(f"   ✓ NO book: Bid ${orderbook_no['best_bid']:.3f} | Ask ${orderbook_no['best_ask']:.3f}")
        
        # Get price history
        active_token = yes_token if trade_row['side'] == 'YES' else no_token
        price_history = self.get_price_history(active_token) if active_token else {}
        
        if price_history.get('momentum'):
            print(f"   ✓ Price momentum: {price_history['momentum']:+.2f}%")
        
        # Analyze trade context
        analysis = self.analyze_trade_context(trade_row, market_info, orderbook_yes, orderbook_no)
        
        flags = []
        if analysis['unusual_size']: flags.append("🐋 LARGE SIZE")
        if analysis['contrarian_trade']: flags.append("🎲 CONTRARIAN")
        if flags:
            print(f"   🚩 Flags: {', '.join(flags)}")
        
        # Save raw snapshot
        raw_data = {
            'trade': trade_row.to_dict(),
            'market': market_info,
            'orderbook_yes': orderbook_yes,
            'orderbook_no': orderbook_no,
            'price_history': price_history
        }
        self.save_raw_snapshot(trade_id, raw_data)
        
        # Compile enriched row
        enriched = {
            'trade_id': trade_id,
            'timestamp': trade_row['timestamp'],
            'wallet': trade_row['wallet'],
            'market_slug': trade_row['market_slug'],
            'side': trade_row['side'],
            'trade_price': trade_row['price'],
            'trade_size': trade_row['size'],
            'trade_value': trade_row.get('value', trade_row['price'] * trade_row['size']),
            
            # Market state
            'market_title': market_info['title'],
            'market_end_time': market_info['end_time'],
            'time_remaining_seconds': time_remaining,
            'market_closed': market_info['closed'],
            'market_volume': market_info['volume'],
            'market_liquidity': market_info['liquidity'],
            
            # Orderbook
            'yes_best_bid': orderbook_yes['best_bid'] if orderbook_yes else None,
            'yes_best_ask': orderbook_yes['best_ask'] if orderbook_yes else None,
            'yes_bid_volume': orderbook_yes['bid_volume'] if orderbook_yes else None,
            'yes_ask_volume': orderbook_yes['ask_volume'] if orderbook_yes else None,
            'no_best_bid': orderbook_no['best_bid'] if orderbook_no else None,
            'no_best_ask': orderbook_no['best_ask'] if orderbook_no else None,
            'no_bid_volume': orderbook_no['bid_volume'] if orderbook_no else None,
            'no_ask_volume': orderbook_no['ask_volume'] if orderbook_no else None,
            'spread': orderbook_yes['spread'] if orderbook_yes else None,
            'implied_probability': orderbook_yes['best_bid'] if orderbook_yes and trade_row['side'] == 'YES' else orderbook_no['best_bid'] if orderbook_no else None,
            
            # Price history
            'yes_price_5min_ago': price_history.get('price_5min_ago'),
            'yes_price_1min_ago': price_history.get('price_1min_ago'),
            'price_momentum': price_history.get('momentum'),
            'volatility_score': price_history.get('volatility'),
            
            # Analysis
            'unusual_size': analysis['unusual_size'],
            'contrarian_trade': analysis['contrarian_trade'],
            'market_maker_activity': analysis['market_maker'],
            
            # Outcome (to be filled later)
            'winning_side': None,
            'trade_profit_loss': None,
            'market_resolution_time': None
        }
        
        print(f"   ✅ Enrichment complete")
        
        return enriched
    
    def run_live(self):
        """Monitor whale_trades.csv and enrich new trades in real-time"""
        print(f"\n📊 Trade Enricher Active")
        print(f"📥 Reading from: {INPUT_FILE}")
        print(f"💾 Enriching to: {OUTPUT_FILE}")
        if SAVE_RAW_JSON:
            print(f"📸 Snapshots: {RAW_JSON_DIR}/")
        print(f"⏱️ Poll interval: {POLL_INTERVAL}s\n")
        
        print("Waiting for new trades...\n")
        
        try:
            while True:
                # Check if source file exists
                if not os.path.exists(INPUT_FILE):
                    print(f"⏳ Waiting for {INPUT_FILE}...", end="\r")
                    time.sleep(POLL_INTERVAL)
                    continue
                
                # Read new trades
                try:
                    df = pd.read_csv(INPUT_FILE)
                    
                    # Process each unprocessed trade
                    for idx, row in df.iterrows():
                        trade_id = f"{row['wallet']}_{row['timestamp']}"
                        
                        if trade_id in self.processed_trades:
                            continue
                        
                        # Enrich the trade
                        enriched = self.enrich_trade(row)
                        
                        if enriched:
                            # Save to CSV
                            pd.DataFrame([enriched]).to_csv(OUTPUT_FILE, mode='a', header=False, index=False)
                            self.processed_trades.add(trade_id)
                            print(f"   💾 Saved to {OUTPUT_FILE}\n")
                        
                        # Rate limit API calls
                        time.sleep(2)
                
                except Exception as e:
                    print(f"⚠️ Error reading trades: {e}")
                
                time.sleep(POLL_INTERVAL)
                
        except KeyboardInterrupt:
            print(f"\n\n🛑 Enricher stopped")
            print(f"📊 Processed {len(self.processed_trades)} trades")
            print(f"💾 Data saved to: {OUTPUT_FILE}")
            if SAVE_RAW_JSON:
                print(f"📸 Raw snapshots in: {RAW_JSON_DIR}/")
    
    def backfill(self):
        """Process all existing trades in whale_trades.csv"""
        print(f"\n🔄 Backfilling historical trades from {INPUT_FILE}...")
        
        if not os.path.exists(INPUT_FILE):
            print(f"❌ {INPUT_FILE} not found!")
            return
        
        df = pd.read_csv(INPUT_FILE)
        total = len(df)
        
        print(f"📚 Found {total} trades to process\n")
        
        for idx, row in df.iterrows():
            trade_id = f"{row['wallet']}_{row['timestamp']}"
            
            if trade_id in self.processed_trades:
                print(f"[{idx+1}/{total}] Skipping (already processed)", end="\r")
                continue
            
            print(f"[{idx+1}/{total}] Processing...")
            enriched = self.enrich_trade(row)
            
            if enriched:
                pd.DataFrame([enriched]).to_csv(OUTPUT_FILE, mode='a', header=False, index=False)
                self.processed_trades.add(trade_id)
            
            # Be nice to API
            time.sleep(3)
        
        print(f"\n\n✅ Backfill complete! Processed {len(self.processed_trades)} trades")
        print(f"💾 Data saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    import sys
    
    enricher = TradeEnricher()
    
    # Check if backfill mode
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        enricher.backfill()
    else:
        enricher.run_live()
