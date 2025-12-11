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
TOP_N = 100            # Total coins to fetch from Kraken
DEEP_SCAN_LIMIT = 30   # Only scan top 30 for stealth metrics to avoid blocking
MAX_CONCURRENT = 3     # Slow and steady wins the race

# Rotating User Agents (To trick Binance/Bybit WAF)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

def get_header():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Referer": "https://www.google.com/"
    }

# -----------------------------------------------------------------------------
# 1. STEALTH METRICS (Robust Implementation)
# -----------------------------------------------------------------------------
def map_symbol(kraken_symbol):
    """Maps Kraken 'XBT/USD:USD' -> 'BTCUSDT'"""
    try:
        base = kraken_symbol.split('/')[0]
        if base == 'XBT': base = 'BTC'
        if base == 'XDG': base = 'DOGE'
        
        # Meme coin fix: check if we need 1000 prefix
        thousands = {'PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'SATS', 'RATS'}
        if base.upper() in thousands:
            return f"1000{base}USDT"
        return f"{base}USDT"
    except:
        return None

async def fetch_stealth_metrics(session, semaphore, kraken_symbol):
    """Fetches CVD and L/S Ratio with error handling."""
    ls_ratio = 0.0
    cvd = 0.0
    activity = 0.0
    
    target_symbol = map_symbol(kraken_symbol)
    if not target_symbol: return 0, 0, 0

    async with semaphore:
        await asyncio.sleep(random.uniform(0.5, 2.0)) # Random sleep to avoid blocks
        
        try:
            # 1. Binance CVD (Taker Buy/Sell)
            url_cvd = "https://fapi.binance.com/futures/data/takerlongshortRatio"
            async with session.get(url_cvd, params={'symbol': target_symbol, 'period': '4h', 'limit': 1}, 
                                 headers=get_header(), timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        cvd = round(float(data[0]['buyVol']) - float(data[0]['sellVol']), 0)

            # 2. Bybit L/S Ratio
            url_ls = "https://api.bybit.com/v5/market/account-ratio"
            async with session.get(url_ls, params={'category': 'linear', 'symbol': target_symbol, 'period': '4h', 'limit': 1},
                                 headers=get_header(), timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data['retCode'] == 0 and data['result']['list']:
                        item = data['result']['list'][0]
                        sell = float(item['sellRatio'])
                        if sell > 0: ls_ratio = round(float(item['buyRatio']) / sell, 2)
            
            # 3. Bybit Activity (Turnover)
            url_tick = "https://api.bybit.com/v5/market/tickers"
            async with session.get(url_tick, params={'category': 'linear', 'symbol': target_symbol},
                                 headers=get_header(), timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data['retCode'] == 0 and data['result']['list']:
                         activity = float(data['result']['list'][0].get('turnover24h', 0))

        except Exception:
            pass # Fail silently, return 0s, but DO NOT CRASH

    return ls_ratio, cvd, activity

# -----------------------------------------------------------------------------
# 2. MAIN DATA ENGINE
# -----------------------------------------------------------------------------
async def main():
    print("🚀 Script Started...")
    exchange = ccxt.krakenfutures({'enableRateLimit': True})
    
    final_rows = []

    try:
        # A. FETCH KRAKEN DATA (Base Layer)
        print("🔌 Fetching Kraken Tickers...")
        tickers = await exchange.fetch_tickers()
        
        market_data = []
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')

        for symbol, data in tickers.items():
            if ':USD' in symbol:
                raw = data.get('info', {})
                price = data.get('last')
                
                # Robust Volume calculation
                vol = raw.get('volumeQuote')
                if not vol:
                    v24 = raw.get('vol24h')
                    if v24 and price: vol = float(v24) * price
                
                if price and vol:
                    market_data.append({
                        'Symbol': symbol,
                        'Price': float(price),
                        'Volume': float(vol),
                        'OI': float(raw.get('openInterest', 0)),
                        'Funding': float(raw.get('fundingRate', 0)) * 100
                    })

        # Sort Top N
        top_coins = sorted(market_data, key=lambda x: x['Volume'], reverse=True)[:TOP_N]
        print(f"✅ Kraken Data: Found {len(top_coins)} coins.")

        # B. ENRICH WITH STEALTH METRICS (Parallel)
        print(f"🕵️ Deep Scanning Top {DEEP_SCAN_LIMIT} coins (Stealth Mode)...")
        
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        async with aiohttp.ClientSession() as session:
            tasks = []
            # Only scan the top N for deep metrics to save time/risk
            for i, coin in enumerate(top_coins):
                if i < DEEP_SCAN_LIMIT:
                    tasks.append(fetch_stealth_metrics(session, semaphore, coin['Symbol']))
                else:
                    # Just return 0s for lower volume coins
                    tasks.append(asyncio.sleep(0, result=(0, 0, 0)))
            
            results = await asyncio.gather(*tasks)

            # C. COMBINE DATA
            for i, coin in enumerate(top_coins):
                ls, cvd, act = results[i]
                final_rows.append([
                    date_str, time_str, coin['Symbol'], coin['Price'],
                    ls, cvd, coin['Volume'], coin['OI'], coin['Funding'], act
                ])
                
    except Exception as e:
        print(f"❌ CRITICAL ERROR IN DATA FETCH: {e}")
        return []

    finally:
        await exchange.close()
    
    return final_rows

# -----------------------------------------------------------------------------
# 3. GOOGLE SHEETS UPLOADER
# -----------------------------------------------------------------------------
def upload_to_sheets(data):
    if not data:
        print("⚠️ No data to upload!")
        return

    print(f"📈 Uploading {len(data)} rows to Google Sheets...")
    
    try:
        creds_json = os.environ.get('GCP_CREDENTIALS')
        if not creds_json: raise ValueError("GCP_CREDENTIALS missing!")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(creds_json), 
            ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        )
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1

        # HEADERS CHECK
        HEADERS = ["Date", "Time", "Symbol", "Price", "LS_Ratio", "CVD_4h", 
                   "Volume_24h", "Open_Interest", "Funding_Rate", "Activity_Score"]
        
        existing = sheet.get_all_values()
        
        if not existing:
            print("📝 Sheet is empty. Adding headers...")
            sheet.append_row(HEADERS)
        elif existing[0] != HEADERS:
            print("⚠️ Header mismatch. Overwriting headers...")
            sheet.update('A1:J1', [HEADERS]) # Force update top row

        sheet.append_rows(data)
        print("✅ SUCCESS: Data pushed to Sheets.")

    except Exception as e:
        print(f"❌ SHEET ERROR: {e}")

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    rows = asyncio.run(main())
    upload_to_sheets(rows)
