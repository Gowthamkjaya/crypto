import time
import requests
import pandas as pd
import os
from datetime import datetime, timedelta
from collections import deque
import json

# --- CONFIGURATION ---
TARGET_WALLETS = [
"0x7f69983eb28245bba0d5083502a78744a8f66162",
"0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d",
"0xe00740bce98a594e26861838885ab310ec3b548c"
]

OUTPUT_FILE = "whale_trades.csv"
POLL_INTERVAL = 3  # Seconds between checks
MAX_MEMORY_TRADES = 10000  # Keep only recent trade IDs in memory
MARKET_FILTER = None  # Set to "btc-updown" to only log BTC markets, None for all

# Alert thresholds (optional)
ALERT_MIN_SIZE = 10  # Alert if trade size > this
ALERT_MARKETS = ["btc-updown-15m"]  # Keywords to watch
# ---------------------

class WhaleLogger:
    def __init__(self):
        self.seen_trades = deque(maxlen=MAX_MEMORY_TRADES)
        self.session_stats = {wallet: {'trades': 0, 'volume': 0} for wallet in TARGET_WALLETS}
        self.last_request_time = {}
        self.failed_requests = 0
        self.initialize_csv()
        
    def initialize_csv(self):
        """Create CSV with headers if it doesn't exist"""
        if not os.path.exists(OUTPUT_FILE):
            pd.DataFrame(columns=[
                'timestamp', 'wallet', 'wallet_short', 'market_slug', 
                'side', 'price', 'size', 'value', 'transaction_hash'
            ]).to_csv(OUTPUT_FILE, index=False)
            print(f"📁 Created {OUTPUT_FILE}")
    
    def rate_limit_check(self, wallet):
        """Prevent hammering API too fast per wallet"""
        last = self.last_request_time.get(wallet, 0)
        elapsed = time.time() - last
        if elapsed < 1.0:  # Min 1 second between requests per wallet
            time.sleep(1.0 - elapsed)
        self.last_request_time[wallet] = time.time()
    
    def fetch_trades(self, wallet, limit=10):
        """Fetch recent trades with error handling"""
        self.rate_limit_check(wallet)
        
        # Try multiple API endpoints
        urls = [
            f"https://clob.polymarket.com/trades?maker={wallet}&limit={limit}",
            f"https://clob.polymarket.com/trades?taker={wallet}&limit={limit}",
        ]
        
        all_trades = []
        
        for url in urls:
            try:
                r = requests.get(url, timeout=10)
                
                if r.status_code == 429:  # Rate limited
                    print(f"⚠️ Rate limited. Sleeping 30s...")
                    time.sleep(30)
                    return []
                
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        all_trades.extend(data)
                    self.failed_requests = 0  # Reset on success
                    
            except requests.exceptions.RequestException as e:
                self.failed_requests += 1
                if self.failed_requests > 10:
                    print(f"❌ Too many failed requests. Sleeping 60s...")
                    time.sleep(60)
                    self.failed_requests = 0
                continue
            except Exception as e:
                print(f"⚠️ Error parsing response: {e}")
                continue
        
        return all_trades
    
    def parse_trade(self, trade_data, wallet):
        """Extract relevant info from trade"""
        try:
            # Handle different API response formats
            trade_id = trade_data.get('id') or trade_data.get('transaction_hash')
            
            if not trade_id:
                return None
            
            # Extract market slug from token ID or market field
            market = trade_data.get('market') or trade_data.get('asset_id', 'unknown')
            
            # Get side - sometimes it's in 'side', sometimes 'outcome'
            side = trade_data.get('side') or trade_data.get('outcome', 'UNKNOWN')
            
            # Calculate trade value
            price = float(trade_data.get('price', 0))
            size = float(trade_data.get('size', 0))
            value = price * size
            
            return {
                'id': trade_id,
                'timestamp': trade_data.get('timestamp') or datetime.now().isoformat(),
                'wallet': wallet,
                'wallet_short': f"{wallet[:6]}...{wallet[-4:]}",
                'market_slug': market,
                'side': side.upper(),
                'price': price,
                'size': size,
                'value': round(value, 2),
                'transaction_hash': trade_data.get('transaction_hash', '')
            }
        except Exception as e:
            print(f"⚠️ Error parsing trade: {e}")
            return None
    
    def should_alert(self, trade):
        """Check if trade meets alert criteria"""
        alerts = []
        
        if trade['size'] >= ALERT_MIN_SIZE:
            alerts.append(f"🐋 LARGE SIZE: ${trade['value']:.2f}")
        
        if ALERT_MARKETS:
            for keyword in ALERT_MARKETS:
                if keyword in trade['market_slug']:
                    alerts.append(f"🎯 TARGET MARKET")
                    break
        
        return alerts
    
    def log_trade(self, trade):
        """Log trade to console and CSV"""
        wallet_short = trade['wallet_short']
        
        # Console output
        time_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n🚨 [{time_str}] {wallet_short}")
        print(f"   Market: {trade['market_slug']}")
        print(f"   Side: {trade['side']} @ ${trade['price']:.3f}")
        print(f"   Size: {trade['size']} (${trade['value']:.2f})")
        
        # Check for alerts
        alerts = self.should_alert(trade)
        if alerts:
            for alert in alerts:
                print(f"   {alert}")
        
        # Save to CSV
        df = pd.DataFrame([{
            'timestamp': trade['timestamp'],
            'wallet': trade['wallet'],
            'wallet_short': trade['wallet_short'],
            'market_slug': trade['market_slug'],
            'side': trade['side'],
            'price': trade['price'],
            'size': trade['size'],
            'value': trade['value'],
            'transaction_hash': trade['transaction_hash']
        }])
        
        df.to_csv(OUTPUT_FILE, mode='a', header=False, index=False)
        
        # Update stats
        self.session_stats[trade['wallet']]['trades'] += 1
        self.session_stats[trade['wallet']]['volume'] += trade['value']
    
    def print_stats(self):
        """Print session statistics"""
        total_trades = sum(s['trades'] for s in self.session_stats.values())
        total_volume = sum(s['volume'] for s in self.session_stats.values())
        
        print(f"\n📊 SESSION STATS")
        print(f"   Total Trades Logged: {total_trades}")
        print(f"   Total Volume: ${total_volume:.2f}")
        
        for wallet, stats in self.session_stats.items():
            if stats['trades'] > 0:
                wallet_short = f"{wallet[:6]}...{wallet[-4:]}"
                print(f"   {wallet_short}: {stats['trades']} trades, ${stats['volume']:.2f}")
    
    def run(self):
        """Main monitoring loop"""
        print(f"\n🕵️ Whale Logger Active")
        print(f"📍 Monitoring {len(TARGET_WALLETS)} wallets")
        print(f"💾 Logging to: {OUTPUT_FILE}")
        if MARKET_FILTER:
            print(f"🔍 Filter: {MARKET_FILTER}")
        print(f"⏱️ Poll interval: {POLL_INTERVAL}s")
        print("\nWaiting for trades...\n")
        
        try:
            while True:
                for wallet in TARGET_WALLETS:
                    trades = self.fetch_trades(wallet)
                    
                    for trade_data in trades:
                        trade = self.parse_trade(trade_data, wallet)
                        
                        if not trade:
                            continue
                        
                        # Skip if already seen
                        if trade['id'] in self.seen_trades:
                            continue
                        
                        # Apply market filter
                        if MARKET_FILTER and MARKET_FILTER not in trade['market_slug']:
                            continue
                        
                        # New trade!
                        self.seen_trades.append(trade['id'])
                        self.log_trade(trade)
                
                time.sleep(POLL_INTERVAL)
                
        except KeyboardInterrupt:
            print("\n\n🛑 Stopping logger...")
            self.print_stats()
            print(f"\n📁 Full log saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    logger = WhaleLogger()
    logger.run()
