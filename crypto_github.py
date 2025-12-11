import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import aiohttp
import sys

# --- CONFIGURATION ---
SHEET_NAME = "crypto_history" 
TOP_N = 100            
DEEP_SCAN_LIMIT = 25   

# Enhanced headers to avoid detection
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

# Meme coins with 1000 prefix
THOUSAND_COINS = {'PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'XEC', 'SATS', 'RATS'}

# --- DEBUG MODE ---
DEBUG = True  # Set to False to reduce logs

def debug_print(msg):
    """Print debug messages"""
    if DEBUG:
        print(f"[DEBUG] {msg}")
        sys.stdout.flush()

# --- 1. SYMBOL MAPPER ---
def map_symbol(kraken_symbol):
    """Convert Kraken symbol to Binance/Bybit format"""
    try:
        base = kraken_symbol.split('/')[0]
        if base == 'XBT': base = 'BTC'
        if base == 'XDG': base = 'DOGE'
        
        # Handle 1000-prefix coins
        if base.upper() in THOUSAND_COINS:
            return f"1000{base}USDT", f"{base}USDT"
        
        return f"{base}USDT", None
    except Exception as e:
        debug_print(f"Symbol mapping error for {kraken_symbol}: {e}")
        return None, None

# --- 2. FETCH METRICS (WITH DETAILED LOGGING) ---
async def fetch_binance_cvd(session, symbol):
    """Fetch CVD from Binance with enhanced error handling"""
    url = "https://fapi.binance.com/futures/data/takerlongshortRatio"
    params = {'symbol': symbol, 'period': '4h', 'limit': 1}
    
    try:
        debug_print(f"Fetching CVD for {symbol}...")
        async with session.get(url, params=params, headers=HEADERS, timeout=15) as resp:
            status = resp.status
            debug_print(f"  Binance CVD response status: {status}")
            
            if status == 200:
                data = await resp.json()
                debug_print(f"  Binance CVD data: {data}")
                
                if data and isinstance(data, list) and len(data) > 0:
                    buy = float(data[0].get('buyVol', 0))
                    sell = float(data[0].get('sellVol', 0))
                    cvd = round(buy - sell, 0)
                    debug_print(f"  ✅ CVD calculated: {cvd} (buy={buy}, sell={sell})")
                    return cvd
                else:
                    debug_print(f"  ⚠️ Empty or invalid data structure")
            elif status == 400:
                text = await resp.text()
                debug_print(f"  ❌ Bad request (400): {text}")
            elif status == 429:
                debug_print(f"  ⚠️ Rate limited (429)")
            else:
                text = await resp.text()
                debug_print(f"  ❌ Error {status}: {text}")
    except asyncio.TimeoutError:
        debug_print(f"  ⏱️ Timeout fetching CVD for {symbol}")
    except Exception as e:
        debug_print(f"  ❌ Exception fetching CVD: {type(e).__name__}: {e}")
    
    return 0

async def fetch_bybit_metrics(session, symbol):
    """Fetch L/S Ratio and Activity from Bybit with enhanced error handling"""
    ls_ratio = 0.0
    activity = 0.0
    
    # L/S Ratio endpoint
    ls_url = "https://api.bybit.com/v5/market/account-ratio"
    ls_params = {'category': 'linear', 'symbol': symbol, 'period': '4h', 'limit': 1}
    
    # Activity endpoint
    tick_url = "https://api.bybit.com/v5/market/tickers"
    tick_params = {'category': 'linear', 'symbol': symbol}
    
    try:
        debug_print(f"Fetching L/S Ratio for {symbol}...")
        async with session.get(ls_url, params=ls_params, headers=HEADERS, timeout=15) as resp:
            status = resp.status
            debug_print(f"  Bybit L/S response status: {status}")
            
            if status == 200:
                data = await resp.json()
                debug_print(f"  Bybit L/S data: {json.dumps(data, indent=2)}")
                
                ret_code = data.get('retCode', -1)
                if ret_code == 0 and data.get('result', {}).get('list'):
                    item = data['result']['list'][0]
                    buy_r = float(item.get('buyRatio', 0))
                    sell_r = float(item.get('sellRatio', 1))
                    if sell_r > 0:
                        ls_ratio = round(buy_r / sell_r, 2)
                        debug_print(f"  ✅ L/S Ratio: {ls_ratio} (buy={buy_r}, sell={sell_r})")
                else:
                    debug_print(f"  ⚠️ retCode={ret_code} or empty list")
            else:
                text = await resp.text()
                debug_print(f"  ❌ Error {status}: {text}")
    except asyncio.TimeoutError:
        debug_print(f"  ⏱️ Timeout fetching L/S for {symbol}")
    except Exception as e:
        debug_print(f"  ❌ Exception fetching L/S: {type(e).__name__}: {e}")
    
    try:
        debug_print(f"Fetching Activity for {symbol}...")
        async with session.get(tick_url, params=tick_params, headers=HEADERS, timeout=15) as resp:
            status = resp.status
            debug_print(f"  Bybit Activity response status: {status}")
            
            if status == 200:
                data = await resp.json()
                
                ret_code = data.get('retCode', -1)
                if ret_code == 0 and data.get('result', {}).get('list'):
                    activity = float(data['result']['list'][0].get('turnover24h', 0))
                    debug_print(f"  ✅ Activity: {activity}")
                else:
                    debug_print(f"  ⚠️ retCode={ret_code} or empty list")
            else:
                text = await resp.text()
                debug_print(f"  ❌ Error {status}: {text}")
    except asyncio.TimeoutError:
        debug_print(f"  ⏱️ Timeout fetching Activity for {symbol}")
    except Exception as e:
        debug_print(f"  ❌ Exception fetching Activity: {type(e).__name__}: {e}")
    
    return ls_ratio, activity

async def fetch_deep_metrics(session, kraken_symbol):
    """Orchestrate fetching with fallback logic"""
    primary, fallback = map_symbol(kraken_symbol)
    
    if not primary:
        return 0, 0, 0
    
    debug_print(f"\n{'='*60}")
    debug_print(f"Processing {kraken_symbol} -> {primary}")
    
    # Try primary symbol
    cvd = await fetch_binance_cvd(session, primary)
    ls, act = await fetch_bybit_metrics(session, primary)
    
    # If all failed and fallback exists, try fallback
    if cvd == 0 and ls == 0 and fallback:
        print(f"   ⚠️ Primary failed, trying fallback {fallback}")
        debug_print(f"Trying fallback symbol: {fallback}")
        cvd = await fetch_binance_cvd(session, fallback)
        ls, act = await fetch_bybit_metrics(session, fallback)
    
    debug_print(f"Final results: LS={ls}, CVD={cvd}, Activity={act}")
    debug_print(f"{'='*60}\n")
    
    return ls, cvd, act

# --- 3. MAIN LOGIC ---
async def main():
    print("\n" + "="*60)
    print("🔥 CRYPTO DATA COLLECTOR - GITHUB ACTIONS VERSION")
    print("="*60 + "\n")
    print("🚀 Script Started...")
    
    # Only use Kraken for base data
    kraken = ccxt.krakenfutures({'enableRateLimit': True})
    
    try:
        # --- A. FETCH KRAKEN (Base Layer) ---
        print("🔌 Fetching Kraken Tickers...")
        tickers = await kraken.fetch_tickers()
        
        market_data = []
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')

        for symbol, data in tickers.items():
            if ':USD' in symbol:
                raw = data.get('info', {})
                price = data.get('last')
                
                # Calculate volume
                vol = raw.get('volumeQuote')
                if not vol:
                    v24 = raw.get('vol24h')
                    if v24 and price:
                        vol = float(v24) * price
                
                if price and vol:
                    market_data.append({
                        'Symbol': symbol,
                        'Price': float(price),
                        'Volume': float(vol),
                        'OI': float(raw.get('openInterest', 0)),
                        'Funding': float(raw.get('fundingRate', 0)) * 100
                    })

        # Sort by volume
        top_coins = sorted(market_data, key=lambda x: x['Volume'], reverse=True)[:TOP_N]
        print(f"✅ Kraken Data: Found {len(top_coins)} coins.")
        print(f"   Top 3: {[c['Symbol'] for c in top_coins[:3]]}")

        # --- B. DEEP SCAN WITH SHARED SESSION ---
        print(f"\n🕵️ Deep Scanning Top {DEEP_SCAN_LIMIT} coins...")
        print(f"   Debug mode: {'ON' if DEBUG else 'OFF'}")
        
        final_rows = []
        success_count = 0
        
        # Create ONE shared aiohttp session with connection pooling
        connector = aiohttp.TCPConnector(limit=10, limit_per_host=2, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            for i, coin in enumerate(top_coins):
                ls, cvd, act = 0, 0, 0
                
                # Only fetch deep metrics for top coins
                if i < DEEP_SCAN_LIMIT:
                    ls, cvd, act = await fetch_deep_metrics(session, coin['Symbol'])
                    
                    if ls > 0 or cvd != 0 or act > 0:
                        success_count += 1
                        print(f"   ✅ {coin['Symbol']}: LS={ls}, CVD={cvd}, Activity={act:.0f}")
                    
                    # Add delay to avoid rate limits
                    await asyncio.sleep(1.2)  # Increased to 1.2s for GitHub
                
                final_rows.append([
                    date_str, time_str, coin['Symbol'], coin['Price'],
                    ls, cvd, coin['Volume'], coin['OI'], coin['Funding'], act
                ])
                
                if (i + 1) % 10 == 0:
                    print(f"   Processed {i + 1}/{len(top_coins)}")
        
        print(f"\n✅ Scan Complete!")
        print(f"   Success rate: {success_count}/{DEEP_SCAN_LIMIT} coins")
        print(f"   Total rows: {len(final_rows)}")

    except Exception as e:
        print(f"❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await kraken.close()
    
    return final_rows

# --- 4. UPLOADER ---
def upload_to_sheets(data):
    if not data:
        print("⚠️ No data to upload!")
        return

    print(f"\n📈 Uploading {len(data)} rows to Google Sheets...")
    
    try:
        creds_json = os.environ.get('GCP_CREDENTIALS')
        if not creds_json:
            raise ValueError("GCP_CREDENTIALS environment variable missing!")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(creds_json), 
            ['https://spreadsheets.google.com/feeds', 
             'https://www.googleapis.com/auth/drive']
        )
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1

        # Define headers
        HEADERS_ROW = ["Date", "Time", "Symbol", "Price", "LS_Ratio", "CVD_4h", 
                       "Volume_24h", "Open_Interest", "Funding_Rate", "Activity_Score"]
        
        existing = sheet.get_all_values()
        
        # Handle headers
        if not existing:
            print("📝 Sheet is empty. Adding headers...")
            sheet.append_row(HEADERS_ROW)
        elif existing[0] != HEADERS_ROW:
            print("⚠️ Header mismatch. Updating headers...")
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS_ROW, 1)

        # Upload data
        sheet.append_rows(data)
        print("✅ SUCCESS: Data pushed to Sheets.")
        
        # Print detailed sample with non-zero values
        print("\n📊 Sample data uploaded (first 5 with metrics):")
        shown = 0
        for row in data:
            if row[4] > 0 or row[5] != 0:  # LS_Ratio or CVD
                print(f"   {row[2]}: LS={row[4]}, CVD={row[5]}, Activity={row[9]:.0f}")
                shown += 1
                if shown >= 5:
                    break
        
        if shown == 0:
            print("   ⚠️ WARNING: No rows with metrics found!")
            print("   First 3 rows uploaded:")
            for row in data[:3]:
                print(f"   {row[2]}: LS={row[4]}, CVD={row[5]}")

    except Exception as e:
        print(f"❌ SHEET ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    rows = asyncio.run(main())
    upload_to_sheets(rows)
    
    print("\n" + "="*60)
    print("✅ SCRIPT COMPLETED")
    print("="*60)
