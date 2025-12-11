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
TOP_N_TOTAL = 100      # Fetch basic price/vol for 100 coins
TOP_N_DEEP = 100        # Only fetch Deep Metrics (L/S, CVD) for Top 25 to avoid blocks

# Symbol Mapping
THOUSAND_COINS = {'PEPE', 'BONK', 'FLOKI', 'SHIB', 'LUNC', 'XEC', 'SATS', 'RATS'}

def map_kraken_to_generic(kraken_symbol):
    """Maps Kraken 'XBT/USD:USD' -> 'BTC' & 'BTCUSDT'"""
    try:
        base = kraken_symbol.split('/')[0]
        if base == 'XBT': base = 'BTC'
        if base == 'XDG': base = 'DOGE'
        
        generic_pair = f"{base}USDT"
        if base.upper() in THOUSAND_COINS:
            return base, f"1000{base}USDT"
        return base, generic_pair
    except:
        return None, None

# -----------------------------------------------------------------------------
# 1. DEEP METRICS FETCHER (Using CCXT Implicit Methods)
# -----------------------------------------------------------------------------
async def fetch_deep_metrics(binance, bybit, symbol_usdt):
    """
    Fetches CVD (Binance) and L/S + Activity (Bybit) using CCXT.
    Returns: (LS_Ratio, CVD, Activity)
    """
    ls = 0.0
    cvd = 0.0
    act = 0.0
    
    # Random sleep to look like a human browsing
    await asyncio.sleep(random.uniform(1.0, 3.0))

    # A. BINANCE (CVD)
    try:
        # Implicit API call: public_get_futures_data_takerlongshortratio
        # Maps to: GET /futures/data/takerlongshortRatio
        b_data = await binance.public_get_futures_data_takerlongshortratio({
            'symbol': symbol_usdt,
            'period': '4h',
            'limit': 1
        })
        if b_data:
            buy = float(b_data[0]['buyVol'])
            sell = float(b_data[0]['sellVol'])
            cvd = round(buy - sell, 0)
    except Exception:
        pass # Fail silently (keep 0)

    # B. BYBIT (L/S Ratio & Activity)
    try:
        # 1. L/S Ratio (public_get_v5_market_account_ratio)
        ls_data = await bybit.public_get_v5_market_account_ratio({
            'category': 'linear',
            'symbol': symbol_usdt,
            'period': '4h',
            'limit': 1
        })
        if ls_data['retCode'] == 0 and ls_data['result']['list']:
            item = ls_data['result']['list'][0]
            buy_r = float(item['buyRatio'])
            sell_r = float(item['sellRatio'])
            if sell_r > 0: ls = round(buy_r / sell_r, 2)

        # 2. Activity/Turnover (public_get_v5_market_tickers)
        tick_data = await bybit.public_get_v5_market_tickers({
            'category': 'linear',
            'symbol': symbol_usdt
        })
        if tick_data['retCode'] == 0 and tick_data['result']['list']:
            act = float(tick_data['result']['list'][0].get('turnover24h', 0))
            
    except Exception:
        pass

    return ls, cvd, act

# -----------------------------------------------------------------------------
# 2. MAIN LOGIC
# -----------------------------------------------------------------------------
async def main():
    print("🔌 Initializing Exchanges...")
    kraken = ccxt.krakenfutures({'enableRateLimit': True})
    
    # Initialize Binance/Bybit for Deep Metrics
    # We use 'enableRateLimit' to automatically space out requests
    binance = ccxt.binanceusdm({'enableRateLimit': True}) 
    bybit = ccxt.bybit({'enableRateLimit': True})

    try:
        # A. FETCH KRAKEN (Base Layer)
        print("📊 Fetching Kraken Top 100...")
        tickers = await kraken.fetch_tickers()
        market_data = []
        
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')
        
        for symbol, data in tickers.items():
            if ':USD' in symbol:
                raw = data.get('info', {})
                price = data.get('last')
                vol_usd = raw.get('volumeQuote')
                
                # Volume Fallback
                if vol_usd is None:
                    vol_24h = raw.get('vol24h')
                    if vol_24h and price:
                        vol_usd = float(vol_24h) * price
                
                oi = raw.get('openInterest')
                funding = raw.get('fundingRate')
                
                if price and vol_usd:
                    base_coin, generic_usdt = map_kraken_to_generic(symbol)
                    market_data.append({
                        'Date': date_str, 'Time': time_str, 
                        'Symbol': symbol,   # Kraken Symbol
                        'Pair': generic_usdt, # USDT Pair for other exchanges
                        'Price': float(price), 
                        'Volume_24h': float(vol_usd),
                        'Open_Interest': float(oi) if oi else 0.0,
                        'Funding_Rate': float(funding) * 100 if funding else 0.0,
                        # Default Deep Metrics to 0
                        'LS_Ratio': 0.0, 'CVD_4h': 0.0, 'Activity_Score': 0.0
                    })
        
        # Sort Top 100
        all_coins = sorted(market_data, key=lambda x: x['Volume_24h'], reverse=True)[:TOP_N_TOTAL]
        
        # Identify Top N for Deep Scan
        deep_scan_coins = all_coins[:TOP_N_DEEP]
        print(f"✅ Found {len(all_coins)} coins. Deep Scanning Top {len(deep_scan_coins)}...")

        # B. FETCH DEEP METRICS (Sequential to avoid blocks)
        for i, coin in enumerate(deep_scan_coins):
            # Print progress to logs
            print(f"   Scanning {i+1}/{len(deep_scan_coins)}: {coin['Pair']}...")
            
            ls, cvd, act = await fetch_deep_metrics(binance, bybit, coin['Pair'])
            
            # Update the record in the main list (all_coins references the same dicts)
            coin['LS_Ratio'] = ls
            coin['CVD_4h'] = cvd
            coin['Activity_Score'] = act

        # C. PREPARE DATA FOR SHEETS
        final_rows = []
        for coin in all_coins:
            final_rows.append([
                coin['Date'], coin['Time'], coin['Symbol'], coin['Price'],
                coin['LS_Ratio'], coin['CVD_4h'], coin['Volume_24h'], 
                coin['Open_Interest'], coin['Funding_Rate'], coin['Activity_Score']
            ])
            
        return final_rows

    finally:
        await kraken.close()
        await binance.close()
        await bybit.close()

# -----------------------------------------------------------------------------
# 3. UPLOADER
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
        
        NEW_HEADERS = [
            "Date", "Time", "Symbol", "Price", 
            "LS_Ratio", "CVD_4h", "Volume_24h", 
            "Open_Interest", "Funding_Rate", "Activity_Score"
        ]
        
        # Force Update Headers if missing or mismatch
        if not sheet.get_all_values() or sheet.row_values(1) != NEW_HEADERS:
            print("⚠️ Updating Headers...")
            if sheet.get_all_values(): sheet.delete_row(1)
            sheet.insert_row(NEW_HEADERS, 1)

        sheet.append_rows(data)
        print("✅ Success! Data uploaded.")
        
    except Exception as e:
        print(f"❌ Error updating sheet: {e}")

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    data_rows = asyncio.run(main())
    if data_rows:
        update_google_sheet(data_rows)
