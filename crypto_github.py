import asyncio
import ccxt.async_support as ccxt
import aiohttp
import pandas as pd
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import random

# --- CONFIGURATION ---
SHEET_NAME = "crypto_history" 
TOP_N = 100
MAX_CONCURRENT_REQUESTS = 2  # Ultra-low to avoid detection

# Rotating User Agents to bypass blocking
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

THOUSAND_COINS = {'PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'XEC', 'SATS', 'RATS'}

# -----------------------------------------------------------------------------
# 1. STEALTH HELPERS
# -----------------------------------------------------------------------------
def get_random_header():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Referer": "https://www.google.com/"
    }

def map_kraken_to_stealth(kraken_symbol):
    try:
        base = kraken_symbol.split('/')[0]
        if base == 'XBT': base = 'BTC'
        if base == 'XDG': base = 'DOGE'
        generic = f"{base}USDT"
        if base.upper() in THOUSAND_COINS:
            return f"1000{base}USDT", generic
        return generic, None
    except:
        return None, None

async def fetch_binance_cvd(session, symbol):
    """Tries FAPI (USDT) then DAPI (COIN) if blocked."""
    # 1. Try Main Futures API (FAPI)
    url_fapi = "https://fapi.binance.com/futures/data/takerlongshortRatio"
    params = {'symbol': symbol, 'period': '4h', 'limit': 1}
    
    try:
        async with session.get(url_fapi, params=params, headers=get_random_header(), timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list):
                    return round(float(data[0]['buyVol']) - float(data[0]['sellVol']), 0)
    except:
        pass

    # 2. Fallback: Coin-Margined API (DAPI) - Often less strict
    # Note: DAPI symbols are like 'BTCUSD_PERP'
    symbol_dapi = symbol.replace("USDT", "USD_PERP")
    url_dapi = "https://dapi.binance.com/futures/data/takerlongshortRatio"
    params['pair'] = symbol.replace("USDT", "USD") # DAPI uses pair/symbol differently
    
    try:
        async with session.get(url_dapi, params=params, headers=get_random_header(), timeout=5) as resp:
             if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list):
                    return round(float(data[0]['buyVol']) - float(data[0]['sellVol']), 0)
    except:
        pass
        
    return 0

async def fetch_bybit_metrics(session, symbol):
    """Fetches Bybit Data."""
    ls_url = "https://api.bybit.com/v5/market/account-ratio"
    ls_params = {'category': 'linear', 'symbol': symbol, 'period': '4h', 'limit': 1}
    tick_url = "https://api.bybit.com/v5/market/tickers"
    tick_params = {'category': 'linear', 'symbol': symbol}
    
    ls = 0.0
    act = 0.0
    
    try:
        # L/S Ratio
        async with session.get(ls_url, params=ls_params, headers=get_random_header(), timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data['retCode'] == 0 and data['result']['list']:
                    item = data['result']['list'][0]
                    buy = float(item['buyRatio'])
                    sell = float(item['sellRatio'])
                    if sell > 0: ls = round(buy / sell, 2)
        
        # Activity
        async with session.get(tick_url, params=tick_params, headers=get_random_header(), timeout=5) as resp:
             if resp.status == 200:
                data = await resp.json()
                if data['retCode'] == 0 and data['result']['list']:
                    act = float(data['result']['list'][0].get('turnover24h', 0))
    except:
        pass
        
    return ls, act

async def get_stealth_data_throttled(session, semaphore, kraken_symbol):
    async with semaphore:
        await asyncio.sleep(random.uniform(0.5, 1.5)) # Longer human-like delay
        
        target, fallback = map_kraken_to_stealth(kraken_symbol)
        if not target: return 0, 0, 0

        cvd = await fetch_binance_cvd(session, target)
        ls, act = await fetch_bybit_metrics(session, target)
        
        if cvd == 0 and ls == 0 and fallback:
            cvd = await fetch_binance_cvd(session, fallback)
            ls, act = await fetch_bybit_metrics(session, fallback)

        return ls, cvd, act

# -----------------------------------------------------------------------------
# 2. MAIN DATA ENGINE
# -----------------------------------------------------------------------------
async def get_enriched_data():
    print("🔌 Connecting to Kraken Futures...")
    exchange = ccxt.krakenfutures({'enableRateLimit': True})

    try:
        tickers = await exchange.fetch_tickers()
        market_data = []
        
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')
        
        print("📊 Fetching Kraken Data...")
        for symbol, data in tickers.items():
            if ':USD' in symbol:
                raw = data.get('info', {})
                price = data.get('last')
                vol_usd = raw.get('volumeQuote')
                if vol_usd is None:
                    vol_24h = raw.get('vol24h')
                    if vol_24h and price:
                        vol_usd = float(vol_24h) * price
                
                oi = raw.get('openInterest')
                funding = raw.get('fundingRate')
                
                if price and vol_usd:
                    market_data.append({
                        'Date': date_str, 'Time': time_str, 'Symbol': symbol,
                        'Price': float(price), 'Volume_24h': float(vol_usd),
                        'Open_Interest': float(oi) if oi else 0.0,
                        'Funding_Rate': float(funding) * 100 if funding else 0.0
                    })
        
        top_coins = sorted(market_data, key=lambda x: x['Volume_24h'], reverse=True)[:TOP_N]
        print(f"✅ Found {len(top_coins)} coins. Starting Stealth Fetch...")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        async with aiohttp.ClientSession() as session:
            tasks = [get_stealth_data_throttled(session, semaphore, coin['Symbol']) for coin in top_coins]
            results = await asyncio.gather(*tasks)

            final_rows = []
            for i, (ls, cvd, act) in enumerate(results):
                coin = top_coins[i]
                final_rows.append([
                    coin['Date'], coin['Time'], coin['Symbol'], coin['Price'],
                    ls, cvd, coin['Volume_24h'], coin['Open_Interest'], 
                    coin['Funding_Rate'], act
                ])

        return final_rows

    finally:
        await exchange.close()

# -----------------------------------------------------------------------------
# 3. GOOGLE SHEETS UPLOADER (FIXED HEADERS)
# -----------------------------------------------------------------------------
def update_google_sheet(data):
    print("📈 Connecting to Google Sheets...")
    
    creds_json = os.environ.get('GCP_CREDENTIALS')
    if not creds_json: raise ValueError("No GCP_CREDENTIALS found!")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), 
             ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive'])
    client = gspread.authorize(creds)
    
    try:
        sheet = client.open(SHEET_NAME).sheet1
        
        # --- HEADER FIX LOGIC ---
        NEW_HEADERS = [
            "Date", "Time", "Symbol", "Price", 
            "LS_Ratio", "CVD_4h", "Volume_24h", 
            "Open_Interest", "Funding_Rate", "Activity_Score"
        ]
        
        # Check if first row exists and matches new headers
        existing_headers = sheet.row_values(1)
        
        if existing_headers != NEW_HEADERS:
            print("⚠️ Headers mismatch. Updating headers...")
            if existing_headers:
                # If there are old headers, remove them first to avoid duplicates
                sheet.delete_row(1)
            sheet.insert_row(NEW_HEADERS, 1)
        # -------------------------

        sheet.append_rows(data)
        print("✅ Success! Data uploaded.")
        
    except Exception as e:
        print(f"❌ Error updating sheet: {e}")

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    data_rows = asyncio.run(get_enriched_data())
    if data_rows:
        update_google_sheet(data_rows)
    else:
        print("⚠️ No data to upload.")
