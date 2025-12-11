import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# CONFIGURATION
SHEET_NAME = "crypto_history"  # Make sure this matches your Google Sheet name exactly

async def get_kraken_futures_data():
    print("🔌 Connecting to Kraken Futures...")
    exchange = ccxt.krakenfutures({'enableRateLimit': True})

    try:
        tickers = await exchange.fetch_tickers()
        market_data = []
        
        now = datetime.now()
        current_date = now.strftime('%Y-%m-%d')
        current_time = now.strftime('%H:%M:%S')
        
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
                    market_data.append([
                        current_date,
                        current_time,
                        symbol,
                        float(price),
                        float(vol_usd),
                        float(oi) if oi else 0,
                        (float(oi) * float(price)) if oi else 0,
                        float(funding) * 100 if funding else 0.0
                    ])
        
        # Sort by Volume (Top 100)
        # Sort by 5th column (Index 4) which is Volume
        top_100 = sorted(market_data, key=lambda x: x[4], reverse=True)[:100]
        
        print(f"✅ Fetched {len(top_100)} rows.")
        return top_100

    finally:
        await exchange.close()

def update_google_sheet(data):
    print("📈 Connecting to Google Sheets...")
    
    # Load credentials from GitHub Secret (Environment Variable)
    creds_json = os.environ.get('GCP_CREDENTIALS')
    
    if not creds_json:
        raise ValueError("❌ No GCP_CREDENTIALS found in environment variables!")

    creds_dict = json.loads(creds_json)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    
    # Open the sheet
    try:
        sheet = client.open(SHEET_NAME).sheet1
        
        # Check if sheet is empty, if so, add headers
        if not sheet.get_all_values():
            headers = ["Date", "Time", "Symbol", "Price", "Volume_24h", "Open_Interest", "Open_Interest_$", "Funding_Rate"]
            sheet.append_row(headers)
            
        # Append new data
        sheet.append_rows(data)
        print("✅ Successfully uploaded data to Google Sheet!")
        
    except Exception as e:
        print(f"❌ Error updating sheet: {e}")

if __name__ == "__main__":
    # Windows loop policy fix (only needed for local testing)
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 1. Get Data
    data_rows = asyncio.run(get_kraken_futures_data())
    
    # 2. Upload to Sheet
    if data_rows:
        update_google_sheet(data_rows)
    else:

        print("⚠️ No data to upload.")
