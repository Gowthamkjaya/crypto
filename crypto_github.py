import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import aiohttp

# --- CONFIGURATION ---
SHEET_NAME = "crypto_history" 
TOP_N = 100            
DEEP_SCAN_LIMIT = 25   

# Headers to avoid blocks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

# Meme coins with 1000 prefix
THOUSAND_COINS = {'PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'XEC', 'SATS', 'RATS'}

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
    except:
        return None, None

# --- 2. FETCH METRICS (DIRECT HTTP - NO CCXT) ---
async def fetch_binance_cvd(session, symbol):
    """Fetch CVD from Binance using direct HTTP"""
    url = "https://fapi.binance.com/futures/data/takerlongshortRatio"
    params = {'symbol': symbol, 'period': '4h', 'limit': 1}
    
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    buy = float(data[0].get('buyVol', 0))
                    sell = float(data[0].get('sellVol', 0))
                    return round(buy - sell, 0)
    except Exception as e:
        print(f"   CVD error for {symbol}: {e}")
    
    return 0

async def fetch_bybit_metrics(session, symbol):
    """Fetch L/S Ratio and Activity from Bybit using direct HTTP"""
    ls_ratio = 0.0
    activity = 0.0
    
    # L/S Ratio endpoint
    ls_url = "https://api.bybit.com/v5/market/account-ratio"
    ls_params = {'category': 'linear', 'symbol': symbol, 'period': '4h', 'limit': 1}
    
    # Activity endpoint
    tick_url = "https://api.bybit.com/v5/market/tickers"
    tick_params = {'category': 'linear', 'symbol': symbol}
    
    try:
        # Fetch L/S Ratio
        async with session.get(ls_url, params=ls_params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('retCode') == 0 and data.get('result', {}).get('list'):
                    item = data['result']['list'][0]
                    buy_r = float(item.get('buyRatio', 0))
                    sell_r = float(item.get('sellRatio', 1))
                    if sell_r > 0:
                        ls_ratio = round(buy_r / sell_r, 2)
    except Exception as e:
        print(f"   L/S error for {symbol}: {e}")
    
    try:
        # Fetch Activity
        async with session.get(tick_url, params=tick_params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('retCode') == 0 and data.get('result', {}).get('list'):
                    activity = float(data['result']['list'][0].get('turnover24h', 0))
    except Exception as e:
        print(f"   Activity error for {symbol}: {e}")
    
    return ls_ratio, activity

async def fetch_deep_metrics(session, kraken_symbol):
    """Orchestrate fetching with fallback logic"""
    primary, fallback = map_symbol(kraken_symbol)
    
    if not primary:
        return 0, 0, 0
    
    # Try primary symbol
    cvd = await fetch_binance_cvd(session, primary)
    ls, act = await fetch_bybit_metrics(session, primary)
    
    # If all failed and fallback exists, try fallback
    if cvd == 0 and ls == 0 and fallback:
        print(f"   Trying fallback {fallback} for {kraken_symbol}")
        cvd = await fetch_binance_cvd(session, fallback)
        ls, act = await fetch_bybit_metrics(session, fallback)
    
    return ls, cvd, act

# --- 3. MAIN LOGIC ---
async def main():
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

        # --- B. DEEP SCAN WITH SHARED SESSION ---
        print(f"🕵️ Deep Scanning Top {DEEP_SCAN_LIMIT} coins...")
        
        final_rows = []
        
        # Create ONE shared aiohttp session for all requests
        async with aiohttp.ClientSession() as session:
            for i, coin in enumerate(top_coins):
                ls, cvd, act = 0, 0, 0
                
                # Only fetch deep metrics for top coins
                if i < DEEP_SCAN_LIMIT:
                    ls, cvd, act = await fetch_deep_metrics(session, coin['Symbol'])
                    
                    # Add delay to avoid rate limits
                    await asyncio.sleep(0.8)  # Increased delay for GitHub Actions
                
                final_rows.append([
                    date_str, time_str, coin['Symbol'], coin['Price'],
                    ls, cvd, coin['Volume'], coin['OI'], coin['Funding'], act
                ])
                
                if (i + 1) % 10 == 0:
                    print(f"   Processed {i + 1}/{len(top_coins)}")
        
        print(f"✅ Completed! Scanned {DEEP_SCAN_LIMIT} coins deeply.")

    finally:
        await kraken.close()
    
    return final_rows

# --- 4. UPLOADER ---
def upload_to_sheets(data):
    if not data:
        print("⚠️ No data to upload!")
        return

    print(f"📈 Uploading {len(data)} rows to Google Sheets...")
    
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
        HEADERS = ["Date", "Time", "Symbol", "Price", "LS_Ratio", "CVD_4h", 
                   "Volume_24h", "Open_Interest", "Funding_Rate", "Activity_Score"]
        
        existing = sheet.get_all_values()
        
        # Handle headers
        if not existing:
            print("📝 Sheet is empty. Adding headers...")
            sheet.append_row(HEADERS)
        elif existing[0] != HEADERS:
            print("⚠️ Header mismatch. Updating headers...")
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS, 1)

        # Upload data
        sheet.append_rows(data)
        print("✅ SUCCESS: Data pushed to Sheets.")
        
        # Print sample
        print("\n📊 Sample data uploaded:")
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

    print("\n" + "="*60)
    print("🔥 CRYPTO DATA COLLECTOR - GITHUB ACTIONS VERSION")
    print("="*60 + "\n")
    
    rows = asyncio.run(main())
    upload_to_sheets(rows)
    
    print("\n" + "="*60)
    print("✅ SCRIPT COMPLETED")
    print("="*60)
