import os
import time
import requests
import json
import csv
from web3 import Web3
from py_clob_client.client import ClobClient
from datetime import datetime, timedelta, timezone

# ==========================================
# 🔧 CONFIGURATION
# ==========================================

# Your private key (read-only access for price data)
PRIVATE_KEY = "0xbbd185bb356315b5f040a2af2fa28549177f3087559bb76885033e9cf8e8bf34"

# Polymarket address
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# Data collection settings
COLLECTION_INTERVAL = 1  # Collect data every 1 second
OUTPUT_FILE = "btc_15min_price_data.csv"

# ==========================================
# SYSTEM SETUP
# ==========================================
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

class BTCPriceCollector:
    def __init__(self):
        print("📊 BTC 15min Price Data Collector Starting...")
        
        # Setup Client (For Price Data)
        try:
            print(f"🔑 Setting up Polymarket client...")
            
            self.client = ClobClient(
                host=HOST, 
                key=PRIVATE_KEY, 
                chain_id=CHAIN_ID
            )
            
            # Use official method to create/derive API credentials
            print("🔑 Deriving API credentials...")
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            print(f"✅ Connected as: {self.client.get_address()}\n")
            
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            import traceback
            traceback.print_exc()
            exit()
        
        # Initialize CSV file
        self.init_csv()
        
        self.current_market = None
        self.market_start_time = None
        self.collected_markets = set()

    def init_csv(self):
        """Initialize CSV file with headers"""
        try:
            # Check if file exists
            file_exists = os.path.isfile(OUTPUT_FILE)
            
            with open(OUTPUT_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    # Write headers
                    writer.writerow([
                        'timestamp_utc',
                        'market_slug',
                        'market_title',
                        'seconds_until_close',
                        'yes_best_bid',
                        'yes_best_ask',
                        'yes_spread',
                        'no_best_bid',
                        'no_best_ask',
                        'no_spread',
                        'yes_bid_size',
                        'yes_ask_size',
                        'no_bid_size',
                        'no_ask_size',
                        'yes_order_count',
                        'no_order_count'
                    ])
                    print(f"✅ Created new CSV file: {OUTPUT_FILE}\n")
                else:
                    print(f"✅ Appending to existing CSV: {OUTPUT_FILE}\n")
        except Exception as e:
            print(f"❌ CSV initialization error: {e}")

    def get_market_from_slug(self, slug):
        """Get market details from a specific slug"""
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
        except Exception as e:
            return None

    def get_orderbook_data(self, token_id):
        """Get detailed orderbook data for a token"""
        try:
            book = self.client.get_order_book(token_id)
            
            best_bid = None
            best_ask = None
            bid_size = 0
            ask_size = 0
            
            if book.bids:
                best_bid = max(float(o.price) for o in book.bids)
                bid_size = sum(float(o.size) for o in book.bids if float(o.price) == best_bid)
            
            if book.asks:
                best_ask = min(float(o.price) for o in book.asks)
                ask_size = sum(float(o.size) for o in book.asks if float(o.price) == best_ask)
            
            spread = (best_ask - best_bid) if (best_bid and best_ask) else None
            
            return {
                'best_bid': best_bid,
                'best_ask': best_ask,
                'spread': spread,
                'bid_size': bid_size,
                'ask_size': ask_size,
                'order_count': len(book.bids) + len(book.asks)
            }
        except Exception as e:
            return {
                'best_bid': None,
                'best_ask': None,
                'spread': None,
                'bid_size': 0,
                'ask_size': 0,
                'order_count': 0
            }

    def collect_market_data(self, market, seconds_until_close):
        """Collect and save price data for current market"""
        try:
            # Get YES token data
            yes_data = self.get_orderbook_data(market['yes_token'])
            
            # Get NO token data
            no_data = self.get_orderbook_data(market['no_token'])
            
            # Current timestamp
            now_utc = datetime.now(timezone.utc)
            
            # Prepare row
            row = [
                now_utc.strftime('%Y-%m-%d %H:%M:%S'),
                market['slug'],
                market['title'],
                seconds_until_close,
                yes_data['best_bid'],
                yes_data['best_ask'],
                yes_data['spread'],
                no_data['best_bid'],
                no_data['best_ask'],
                no_data['spread'],
                yes_data['bid_size'],
                yes_data['ask_size'],
                no_data['bid_size'],
                no_data['ask_size'],
                yes_data['order_count'],
                no_data['order_count']
            ]
            
            # Write to CSV
            with open(OUTPUT_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(row)
            
            # Display current data (compact format)
            yes_bid_str = f"${yes_data['best_bid']:.4f}" if yes_data['best_bid'] else "N/A"
            yes_ask_str = f"${yes_data['best_ask']:.4f}" if yes_data['best_ask'] else "N/A"
            no_bid_str = f"${no_data['best_bid']:.4f}" if no_data['best_bid'] else "N/A"
            no_ask_str = f"${no_data['best_ask']:.4f}" if no_data['best_ask'] else "N/A"
            
            print(f"⏱️  T-{seconds_until_close:03d}s | YES: {yes_bid_str}/{yes_ask_str} | NO: {no_bid_str}/{no_ask_str}", end="\r")
            
            return True
            
        except Exception as e:
            print(f"\n❌ Data collection error: {e}")
            return False

    def find_active_market(self):
        """Find the currently active 15min BTC market"""
        now_utc = datetime.now(timezone.utc)
        current_timestamp = int(now_utc.timestamp())
        
        # Calculate current 15min window
        market_timestamp = (current_timestamp // 900) * 900
        market_end = market_timestamp + 900
        
        # Generate slug for current window
        slug = f"btc-updown-15m-{market_timestamp}"
        
        # Check if this is a new market
        if slug not in self.collected_markets:
            market = self.get_market_from_slug(slug)
            
            if market:
                self.collected_markets.add(slug)
                return market, market_timestamp, market_end
        
        # Check if we're still in the same market window
        if self.current_market and self.market_start_time == market_timestamp:
            return self.current_market, market_timestamp, market_end
        
        return None, market_timestamp, market_end

    def run(self):
        """Main collection loop"""
        print(f"🚀 Starting data collection...")
        print(f"📁 Output file: {OUTPUT_FILE}")
        print(f"⏱️  Collection interval: {COLLECTION_INTERVAL}s")
        print(f"📊 Collecting: Best Bid, Best Ask, Spread, Sizes, Order Count")
        print(f"\n{'='*80}\n")
        
        while True:
            try:
                # Find active market
                market, market_start, market_end = self.find_active_market()
                
                if not market:
                    # No active market - wait for next window
                    now = int(time.time())
                    next_window = ((now // 900) + 1) * 900
                    wait_time = next_window - now
                    
                    print(f"\n⏳ No active market. Next window in {wait_time}s...")
                    print(f"   Next start: {datetime.fromtimestamp(next_window, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                    
                    time.sleep(min(wait_time, 60))
                    continue
                
                # Update current market
                if self.current_market != market:
                    self.current_market = market
                    self.market_start_time = market_start
                    
                    print(f"\n{'='*80}")
                    print(f"📊 NEW MARKET DETECTED")
                    print(f"{'='*80}")
                    print(f"Title: {market['title']}")
                    print(f"Slug: {market['slug']}")
                    print(f"Start: {datetime.fromtimestamp(market_start, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                    print(f"End: {datetime.fromtimestamp(market_end, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                    print(f"Duration: 900 seconds (15 minutes)")
                    print(f"{'='*80}\n")
                
                # Calculate seconds until close
                now = int(time.time())
                seconds_until_close = market_end - now
                
                # Check if market is still active
                if seconds_until_close <= 0:
                    print(f"\n✅ Market closed. Waiting for next market...\n")
                    self.current_market = None
                    time.sleep(5)
                    continue
                
                # Collect data
                self.collect_market_data(market, seconds_until_close)
                
                # Wait for next collection
                time.sleep(COLLECTION_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Data collection stopped by user")
                print(f"📁 Data saved to: {OUTPUT_FILE}")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5)

if __name__ == "__main__":
    collector = BTCPriceCollector()
    collector.run()