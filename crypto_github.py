import asyncio
import ccxt.async_support as ccxt
import aiohttp
import pandas as pd
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURATION ---
SHEET_NAME = "crypto_history" 
TOP_N = 100

# Stealth Mode Headers (To bypass API blocks)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

# Coins that often have '1000' prefix on Binance/Bybit
THOUSAND_COINS = {'PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'XEC', 'SATS', 'RATS'}

# -----------------------------------------------------------------------------
# 1. STEALTH HELPERS (Binance/Bybit Data)
# -----------------------------------------------------------------------------
def map_kraken_to_stealth(kraken_symbol):
    """Maps Kraken symbols (e.g. XBT/USD:USD) to generic USDT pairs."""
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
    """Fetches CVD from Binance."""
    url = "https://fapi.binance.com/futures/data/takerlongshortRatio"
    params = {'symbol': symbol, 'period': '4h', 'limit': 1}
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list):
                    buy = float(data[0]['buyVol'])
                    sell = float(data[0]['sellVol'])
                    return round(buy - sell, 0)
    except:
        pass
    return 0

async def fetch_bybit_metrics(session, symbol):
    """Fetches L/S Ratio and Activity (Turnover) from Bybit."""
    ls_url = "https://api.bybit.com/v5/market/account-ratio"
    ls_params = {'category': 'linear', 'symbol': symbol, 'period': '4h', 'limit': 1}
    tick_url = "https://api.bybit.com/v5/market/tickers"
    tick_params = {'category': 'linear', 'symbol': symbol}
    
    ls_ratio = 0.0
    activity = 0.0
    
    try:
        # L/S Ratio
        async with session.get(ls_url, params=ls_params, headers=HEADERS, timeout=5) as resp:
            data = await resp.json()
            if data['retCode'] == 0 and data['result']['list']:
                item = data['result']['list'][0]
                buy = float(item['buyRatio'])
                sell = float(item['sellRatio'])
                if sell > 0: ls_ratio = round(buy / sell, 2)
        
        # Activity
        async with session.get(tick_url, params=tick_params, headers=HEADERS, timeout=5) as resp:
            data = await resp.json()
            if data['retCode'] == 0 and data['result']['list']:
                activity = float(data['result']['list'][0].get('turnover24h', 0))
    except:
        pass
    return ls_ratio, activity

async def get_stealth_data(session, kraken_symbol):
    """Orchestrates the stealth fetch with fallback logic."""
    target, fallback = map_kraken_to_stealth(kraken_symbol)
    if not target: return 0, 0, 0

    cvd = await fetch_binance_cvd(session, target)
    ls, act = await fetch_bybit_metrics(session, target)
    
    # Retry with fallback if failed
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
        # A. Fetch Base Layer (Kraken)
        tickers = await exchange.fetch_tickers()
        market_data = []
        
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')
        
        print("📊 Processing Kraken Data...")
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
                        'Date': date_str,
                        'Time': time_str,
                        'Symbol': symbol,
                        'Price': float(price),
                        'Volume_24h': float(vol_usd),
                        'Open_Interest': float(oi) if oi else 0.0,
                        'Funding_Rate': float(funding) * 100 if funding else 0.0
                    })
        
        # Sort Top 100 by Volume
        top_coins = sorted(market_data, key=lambda x: x['Volume_24h'], reverse=True)[:TOP_N]
        print(f"✅ Found Top {len(top_coins)} coins. Fetching Stealth Metrics...")

        # B. Fetch Stealth Metrics (Parallel)
        async with aiohttp.ClientSession() as session:
            tasks = [get_stealth_data(session, coin['Symbol']) for coin in top_coins]
            results = await asyncio.gather(*tasks)
            
            final_rows = []
            for i, (ls, cvd, act) in enumerate(results):
                coin = top_coins[i]
                
                # C. Calculate Signal
                sig = "Neutral"
                if ls > 0 and ls < 0.8 and cvd > 0: sig = "🔥 SQUEEZE (Bull)"
                elif ls > 3.0 and cvd < 0: sig = "⚠️ TRAP (Bear)"
                elif ls > 4.0 and cvd > 0: sig = "🚀 FOMO"
                elif cvd < 0 and coin['Funding_Rate'] > 0.02: sig = "📉 DUMP RISK"
                
                # D. Format Row for Sheets
                # Columns: [Date, Time, Symbol, Price, Signal, LS_Ratio, CVD_4h, Volume, OI, Funding, Activity]
                final_rows.append([
                    coin['Date'],
                    coin['Time'],
                    coin['Symbol'],
                    coin['Price'],
                    sig,
                    ls,
                    cvd,
                    coin['Volume_24h'],
                    coin['Open_Interest'],
                    coin['Funding_Rate'],
                    act
                ])

        return final_rows

    finally:
        await exchange.close()

# -----------------------------------------------------------------------------
# 3. GOOGLE SHEETS UPLOADER
# -----------------------------------------------------------------------------
def update_google_sheet(data):
    print("📈 Connecting to Google Sheets...")
    
    creds_json = os.environ.get('GCP_CREDENTIALS')
    if not creds_json:
        raise ValueError("❌ No GCP_CREDENTIALS found in environment variables!")

    creds_dict = json.loads(creds_json)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    
    try:
        sheet = client.open(SHEET_NAME).sheet1
        
        # Check if sheet is empty and add NEW headers
        if not sheet.get_all_values():
            headers = [
                "Date", "Time", "Symbol", "Price", "Signal", 
                "LS_Ratio", "CVD_4h", "Volume_24h", "Open_Interest", 
                "Funding_Rate", "Activity_Score"
            ]
            sheet.append_row(headers)
            
        sheet.append_rows(data)
        print("✅ Successfully uploaded God Mode data to Google Sheet!")
        
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
