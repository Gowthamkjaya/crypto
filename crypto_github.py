"""
GitHub-compatible crypto data collector using CoinGlass API
CoinGlass aggregates data from multiple exchanges without geo-blocking
"""
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

# CoinGlass API (Free tier: 100 calls/day)
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', '')  # Optional: better rates with API key

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "coinglassSecret": COINGLASS_API_KEY  # Only used if API key is set
}

# --- 1. SYMBOL MAPPER ---
def map_symbol_to_coinglass(kraken_symbol):
    """Convert Kraken symbol to CoinGlass format"""
    try:
        base = kraken_symbol.split('/')[0]
        if base == 'XBT': base = 'BTC'
        if base == 'XDG': base = 'DOGE'
        
        # CoinGlass uses uppercase symbols
        return base.upper()
    except:
        return None

# --- 2. FETCH FROM COINGLASS ---
async def fetch_coinglass_metrics(session, symbol):
    """
    Fetch L/S Ratio, OI, and Liquidations from CoinGlass
    CoinGlass aggregates data from Binance, Bybit, OKX, etc.
    """
    ls_ratio = 0.0
    liquidations_24h = 0.0
    global_oi = 0.0
    
    # CoinGlass endpoints
    base_url = "https://open-api.coinglass.com/public/v2"
    
    try:
        # 1. Long/Short Ratio (aggregated across exchanges)
        ls_url = f"{base_url}/indicator/long_short_ratio"
        params = {'symbol': symbol, 'ex': 'Binance'}  # Default to Binance
        
        async with session.get(ls_url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('success') and data.get('data'):
                    # Get latest ratio
                    latest = data['data'][0] if isinstance(data['data'], list) else data['data']
                    long_rate = float(latest.get('longRate', 0))
                    short_rate = float(latest.get('shortRate', 1))
                    if short_rate > 0:
                        ls_ratio = round(long_rate / short_rate, 2)
                    print(f"   ✅ {symbol} L/S Ratio: {ls_ratio}")
            else:
                print(f"   ⚠️ {symbol} L/S API returned {resp.status}")
    
    except Exception as e:
        print(f"   ❌ Error fetching L/S for {symbol}: {e}")
    
    try:
        # 2. Liquidation Data (24h)
        liq_url = f"{base_url}/liquidation_history"
        params = {'symbol': symbol, 'time_type': '1'}  # 1 = 24h
        
        async with session.get(liq_url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('success') and data.get('data'):
                    liq_data = data['data']
                    # Sum long + short liquidations
                    liquidations_24h = float(liq_data.get('totalLong', 0)) + float(liq_data.get('totalShort', 0))
                    print(f"   ✅ {symbol} Liquidations 24h: ${liquidations_24h:,.0f}")
    
    except Exception as e:
        print(f"   ❌ Error fetching liquidations for {symbol}: {e}")
    
    try:
        # 3. Open Interest (aggregated)
        oi_url = f"{base_url}/open_interest"
        params = {'symbol': symbol}
        
        async with session.get(oi_url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('success') and data.get('data'):
                    # Get total OI across exchanges
                    oi_list = data['data']
                    if isinstance(oi_list, list) and len(oi_list) > 0:
                        global_oi = sum(float(item.get('openInterest', 0)) for item in oi_list)
                    elif isinstance(oi_list, dict):
                        global_oi = float(oi_list.get('openInterest', 0))
                    print(f"   ✅ {symbol} Global OI: ${global_oi:,.0f}")
    
    except Exception as e:
        print(f"   ❌ Error fetching OI for {symbol}: {e}")
    
    # Delay to avoid rate limits (free tier)
    await asyncio.sleep(1.5)
    
    return ls_ratio, liquidations_24h, global_oi

# --- 3. FALLBACK: CALCULATE CVD FROM KRAKEN ---
def calculate_cvd_from_kraken(ticker_info):
    """
    Estimate CVD from Kraken's bid/ask volume
    Not as accurate as exchange data but works without API blocks
    """
    try:
        raw = ticker_info.get('info', {})
        bid_vol = float(raw.get('bidVolume', 0))
        ask_vol = float(raw.get('askVolume', 0))
        
        # CVD approximation: bid volume (buy pressure) - ask volume (sell pressure)
        return round(bid_vol - ask_vol, 0)
    except:
        return 0

# --- 4. MAIN LOGIC ---
async def main():
    print("\n" + "="*60)
    print("🔥 CRYPTO DATA COLLECTOR - COINGLASS VERSION")
    print("="*60 + "\n")
    print("🚀 Script Started...")
    
    if COINGLASS_API_KEY:
        print(f"✅ Using CoinGlass API Key (better rate limits)")
    else:
        print(f"⚠️ No API key - using free tier (limited)")
    
    # Only use Kraken for base data
    kraken = ccxt.krakenfutures({'enableRateLimit': True})
    
    try:
        # --- A. FETCH KRAKEN (Base Layer) ---
        print("\n🔌 Fetching Kraken Tickers...")
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
                        'Funding': float(raw.get('fundingRate', 0)) * 100,
                        'CVD_Estimate': calculate_cvd_from_kraken(data),
                        'Ticker_Info': data  # Store for later use
                    })

        # Sort by volume
        top_coins = sorted(market_data, key=lambda x: x['Volume'], reverse=True)[:TOP_N]
        print(f"✅ Kraken Data: Found {len(top_coins)} coins.")
        print(f"   Top 5: {[c['Symbol'] for c in top_coins[:5]]}")

        # --- B. DEEP SCAN WITH COINGLASS ---
        print(f"\n🕵️ Fetching enhanced metrics for top {DEEP_SCAN_LIMIT} coins...")
        print(f"   Using CoinGlass aggregated data (no geo-blocks!)")
        
        final_rows = []
        success_count = 0
        
        async with aiohttp.ClientSession() as session:
            for i, coin in enumerate(top_coins):
                ls, liq, global_oi = 0, 0, 0
                cvd = coin['CVD_Estimate']  # Use Kraken estimate as fallback
                
                # Only fetch deep metrics for top coins
                if i < DEEP_SCAN_LIMIT:
                    cg_symbol = map_symbol_to_coinglass(coin['Symbol'])
                    if cg_symbol:
                        print(f"\n📊 Processing {coin['Symbol']} -> {cg_symbol}")
                        ls, liq, global_oi = await fetch_coinglass_metrics(session, cg_symbol)
                        
                        if ls > 0 or liq > 0:
                            success_count += 1
                
                # Use global OI if available, otherwise use Kraken OI
                final_oi = global_oi if global_oi > 0 else coin['OI']
                
                final_rows.append([
                    date_str, time_str, coin['Symbol'], coin['Price'],
                    ls, cvd, coin['Volume'], final_oi, coin['Funding'], liq
                ])
                
                if (i + 1) % 10 == 0:
                    print(f"\n   Progress: {i + 1}/{len(top_coins)}")
        
        print(f"\n✅ Scan Complete!")
        print(f"   Success rate: {success_count}/{DEEP_SCAN_LIMIT} coins")
        print(f"   Total rows: {len(final_rows)}")

    except Exception as e:
        print(f"❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        final_rows = []
    finally:
        await kraken.close()
    
    return final_rows

# --- 5. UPLOADER ---
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

        # Define headers (changed Activity to Liquidations_24h)
        HEADERS_ROW = ["Date", "Time", "Symbol", "Price", "LS_Ratio", "CVD_Est", 
                       "Volume_24h", "Open_Interest", "Funding_Rate", "Liquidations_24h"]
        
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
        
        # Print sample with non-zero values
        print("\n📊 Sample data uploaded:")
        shown = 0
        for row in data:
            if row[4] > 0:  # Has L/S ratio
                print(f"   {row[2]}: LS={row[4]}, CVD={row[5]}, Liq24h=${row[9]:,.0f}")
                shown += 1
                if shown >= 5:
                    break
        
        if shown == 0:
            print("   ⚠️ No advanced metrics available (check API key/limits)")

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
    print("\n💡 TIP: Set COINGLASS_API_KEY env var for better rate limits")
    print("   Get free API key at: https://www.coinglass.com/CryptoApi")
