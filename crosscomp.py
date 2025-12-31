import time
import requests
import json
import csv
from web3 import Web3
from py_clob_client.client import ClobClient
from eth_account import Account
from datetime import datetime, timezone

# Configuration
PRIVATE_KEY = "0xbbd185bb356315b5f040a2af2fa28549177f3087559bb76885033e9cf8e8bf34"
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

wallet = Account.from_key(PRIVATE_KEY)

if wallet.address.lower() == POLYMARKET_ADDRESS.lower():
    USE_PROXY = False
    SIGNATURE_TYPE = 0
    TRADING_ADDRESS = Web3.to_checksum_address(wallet.address)
else:
    USE_PROXY = True
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Setup client
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
        chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE
    )

def get_btc_spot():
    """Get BTC spot price from Binance"""
    try:
        r = requests.get('https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT', timeout=2)
        return float(r.json()['price'])
    except:
        return None

def get_market(slug):
    """Get market from slug"""
    try:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        resp = requests.get(url, timeout=10).json()
        if resp and len(resp) > 0:
            event = resp[0]
            raw_ids = event['markets'][0].get('clobTokenIds')
            clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            return clob_ids[0], clob_ids[1]  # YES, NO
    except:
        pass
    return None, None

def get_book_data(token_id):
    """Get best bid and bid size"""
    try:
        book = client.get_order_book(token_id)
        if book.bids:
            best_bid = max(book.bids, key=lambda x: float(x.price))
            return float(best_bid.price), float(best_bid.size)
    except:
        pass
    return None, None

# Main
print("Starting data collection...")

current_ts = int(time.time())
market_ts = (current_ts // 900) * 900
slug = f"btc-updown-15m-{market_ts}"

yes_token, no_token = get_market(slug)

if not yes_token:
    print(f"Market not found: {slug}")
    exit(1)

print(f"Market: {slug}")
print(f"YES token: {yes_token}")
print(f"NO token: {no_token}")
print(f"Starting collection...\n")

# CSV file
csv_file = f"btc_data_{market_ts}.csv"
f = open(csv_file, 'w', newline='')
writer = csv.writer(f)
writer.writerow(['timestamp', 'datetime', 'market_slug', 'btc_spot', 'yes_bid', 'yes_size', 'no_bid', 'no_size'])

count = 0
market_end = market_ts + 900

try:
    while True:
        now = int(time.time())
        
        if now >= market_end:
            print(f"\nMarket closed. Data saved to {csv_file}")
            break
        
        btc = get_btc_spot()
        yes_bid, yes_size = get_book_data(yes_token)
        no_bid, no_size = get_book_data(no_token)
        
        dt = datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        writer.writerow([now, dt, slug, btc, yes_bid, yes_size, no_bid, no_size])
        f.flush()
        
        count += 1
        if count % 60 == 0:
            time_left = market_end - now
            print(f"[{count:3d}] BTC: ${btc:.2f} | YES: ${yes_bid:.4f} | NO: ${no_bid:.4f} | Time left: {time_left}s")
        
        time.sleep(1)

except KeyboardInterrupt:
    print(f"\nStopped. Data saved to {csv_file}")

finally:
    f.close()