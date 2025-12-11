import asyncio
import ccxt.async_support as ccxt
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
DEEP_SCAN_LIMIT = 25   # Limit deep scan to top 25 to minimize block risk

# --- 1. SETUP EXCHANGES ---
async def get_exchanges():
    # Load exchanges with rate limiting enabled
    kraken = ccxt.krakenfutures({'enableRateLimit': True})
    
    # Binance USD-M Futures
    binance = ccxt.binanceusdm({
        'enableRateLimit': True, 
        'options': {'defaultType': 'future'}
    })
    bybit = ccxt.bybit({'enableRateLimit': True})
    
    return kraken, binance, bybit

# --- 2. STEALTH DATA FETCHER ---
async def fetch_deep_metrics(binance, bybit, symbol):
    """
    Fetches metrics using CCXT implicit API methods (Snake Case).
    """
    ls_ratio = 0.0
    cvd = 0.0
    activity = 0.0
    
    # Map Symbol (Kraken -> Generic)
    try:
        base = symbol.split('/')[0]
        if base == 'XBT': base = 'BTC'
        if base == 'XDG': base = 'DOGE'
        
        # Meme coin fix
        if base in ['PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'SATS', 'RATS']:
            target = f"1000{base}USDT"
        else:
            target = f"{base}USDT"
    except:
        return 0, 0, 0

    try:
        # A. BINANCE CVD (Taker Buy/Sell Volume)
        # Endpoint: GET /futures/data/takerlongshortRatio
        # CCXT Python uses snake_case: public_get_futures_data_takerlongshortratio
        cvd_data = await binance.public_get_futures_data_takerlongshortratio({
            'symbol': target, 'period': '4h', 'limit': 1
        })
        if cvd_data:
            buy = float(cvd_data[0]['buyVol'])
            sell = float(cvd_data[0]['sellVol'])
            cvd = round(buy - sell, 0)

        # B. BYBIT METRICS
        # 1. L/S Ratio
        ls_data = await bybit.v5_public_get_market_account_ratio({
            'category': 'linear', 'symbol': target, 'period': '4h', 'limit': 1
        })
        if ls_data['retCode'] == 0 and ls_data['result']['list']:
            item = ls_data['result']['list'][0]
            buy_r = float(item['buyRatio'])
            sell_r = float(item['sellRatio'])
            if sell_r > 0: ls_ratio = round(buy_r / sell_r, 2)

        # 2. Activity (Turnover)
        tick_data = await bybit.v5_public_get_market_tickers({
            'category': 'linear', 'symbol': target
        })
        if tick_data['retCode'] == 0 and tick_data['result']['list']:
            activity = float(tick_data['result']['list'][0].get('turnover24h', 0))

    except Exception:
        # Fail silently (return 0s) to keep the script running
        pass

    return ls_ratio, cvd, activity

# --- 3. MAIN LOGIC ---
async def main():
    print("🚀 Script Started...")
    kraken, binance, bybit = await get_exchanges()

    try:
        # --- CONNECTIVITY CHECK ---
        print("📡 Testing Connectivity...")
        try:
            # Test Binance with snake_case method
            await binance.public_get_futures_data_takerlongshortratio({'symbol': 'BTCUSDT', 'period': '4h', 'limit': 1})
            print("✅ Binance Connected!")
        except Exception as e:
            print(f"❌ BINANCE ERROR: {e}")

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

        # --- B. DEEP SCAN ---
        print(f"🕵️ Deep Scanning Top {DEEP_SCAN_LIMIT} coins...")
        
        final_rows = []
        for i, coin in enumerate(top_coins):
            ls, cvd, act = 0, 0, 0
            
            # Only fetch deep metrics for top coins
            if i < DEEP_SCAN_LIMIT:
                ls, cvd, act = await fetch_deep_metrics(binance, bybit, coin['Symbol'])
                # Random sleep to avoid rate limits
                await asyncio.sleep(0.5) 
            
            final_rows.append([
                date_str, time_str, coin['Symbol'], coin['Price'],
                ls, cvd, coin['Volume'], coin['OI'], coin['Funding'], act
            ])
            
            if i % 10 == 0: print(f"   Processed {i}/{len(top_coins)}")

    finally:
        await kraken.close()
        await binance.close()
        await bybit.close()
    
    return final_rows

# --- 4. UPLOADER ---
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

        # CLEAR & REWRITE STRATEGY (Safer than delete_row)
        print("🧹 Clearing old data to prevent errors...")
        sheet.clear()

        # HEADERS
        HEADERS = ["Date", "Time", "Symbol", "Price", "LS_Ratio", "CVD_4h", 
                   "Volume_24h", "Open_Interest", "Funding_Rate", "Activity_Score"]
        
        # Add Headers first
        sheet.append_row(HEADERS)
        
        # Add Data
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
