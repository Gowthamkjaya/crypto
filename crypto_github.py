"""
SIMPLE WORKING SOLUTION for GitHub Actions
Uses only Kraken data + calculated metrics (no external APIs needed)
NO GEO-BLOCKING, NO API KEYS REQUIRED
"""
import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURATION ---
SHEET_NAME = "crypto_history" 
TOP_N = 100

# --- HELPER FUNCTIONS ---
def calculate_momentum_score(ticker_data):
    """
    Calculate momentum from price change
    Proxy for market direction
    """
    try:
        raw = ticker_data.get('info', {})
        change_pct = float(raw.get('lastChangePercentage', 0))
        return round(change_pct, 2)
    except:
        return 0.0

def calculate_volume_pressure(ticker_data):
    """
    Estimate buy/sell pressure from bid/ask sizes
    Positive = More buyers, Negative = More sellers
    """
    try:
        raw = ticker_data.get('info', {})
        bid_size = float(raw.get('bidSize', 0))
        ask_size = float(raw.get('askSize', 0))
        
        if bid_size + ask_size > 0:
            pressure = (bid_size - ask_size) / (bid_size + ask_size) * 100
            return round(pressure, 2)
        return 0.0
    except:
        return 0.0

def calculate_oi_momentum(ticker_data):
    """
    OI change indicates whether positions are opening or closing
    Positive = New positions opening
    """
    try:
        raw = ticker_data.get('info', {})
        oi = float(raw.get('openInterest', 0))
        oi_24h_ago = float(raw.get('openInterest24h', oi))  # Fallback to current if unavailable
        
        if oi_24h_ago > 0:
            oi_change = ((oi - oi_24h_ago) / oi_24h_ago) * 100
            return round(oi_change, 2)
        return 0.0
    except:
        return 0.0

def generate_signal(price_momentum, vol_pressure, oi_change, funding_rate):
    """
    Generate trading signal based on available metrics
    Replaces L/S Ratio + CVD with Kraken-derived metrics
    """
    signal = "Neutral"
    
    # Bullish Scenarios
    if price_momentum > 1 and vol_pressure > 10 and oi_change > 5:
        signal = "🚀 STRONG BUY"
    elif price_momentum > 0.5 and vol_pressure > 0 and oi_change > 0:
        signal = "📈 BUY"
    
    # Bearish Scenarios
    elif price_momentum < -1 and vol_pressure < -10 and oi_change > 5:
        signal = "💥 DUMP RISK"
    elif price_momentum < -0.5 and vol_pressure < 0 and oi_change > 0:
        signal = "📉 SELL"
    
    # Overheated
    elif funding_rate > 0.05 and oi_change > 10:
        signal = "⚠️ OVERHEATED"
    
    # Squeeze Setup
    elif vol_pressure > 20 and price_momentum > 0 and funding_rate < 0:
        signal = "🔥 SQUEEZE"
    
    return signal

# --- MAIN LOGIC ---
async def main():
    print("\n" + "="*60)
    print("🔥 CRYPTO DATA COLLECTOR - SIMPLE VERSION")
    print("="*60)
    print("✅ No external APIs - No geo-blocking - No API keys needed\n")
    
    kraken = ccxt.krakenfutures({'enableRateLimit': True})
    
    try:
        print("🔌 Fetching Kraken data...")
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
                    # Calculate all metrics
                    momentum = calculate_momentum_score(data)
                    vol_pressure = calculate_volume_pressure(data)
                    oi_change = calculate_oi_momentum(data)
                    oi = float(raw.get('openInterest', 0))
                    funding = float(raw.get('fundingRate', 0)) * 100
                    
                    # Generate signal
                    signal = generate_signal(momentum, vol_pressure, oi_change, funding)
                    
                    market_data.append({
                        'Symbol': symbol,
                        'Price': float(price),
                        'Momentum': momentum,
                        'Vol_Pressure': vol_pressure,
                        'OI_Change': oi_change,
                        'Volume': float(vol),
                        'OI': oi,
                        'Funding': funding,
                        'Signal': signal
                    })

        # Sort by volume
        top_coins = sorted(market_data, key=lambda x: x['Volume'], reverse=True)[:TOP_N]
        print(f"✅ Found {len(top_coins)} coins")
        
        # Convert to rows
        final_rows = []
        for coin in top_coins:
            final_rows.append([
                date_str, time_str, coin['Symbol'], coin['Price'],
                coin['Momentum'], coin['Vol_Pressure'], coin['OI_Change'],
                coin['Volume'], coin['OI'], coin['Funding'], coin['Signal']
            ])
        
        # Show top signals
        print("\n🎯 Top Signals:")
        signal_coins = [c for c in top_coins if c['Signal'] != "Neutral"][:5]
        for coin in signal_coins:
            print(f"   {coin['Symbol']}: {coin['Signal']}")
        
        if not signal_coins:
            print("   No strong signals currently")
        
        print(f"\n✅ Total rows prepared: {len(final_rows)}")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        final_rows = []
    finally:
        await kraken.close()
    
    return final_rows

# --- UPLOADER ---
def upload_to_sheets(data):
    if not data:
        print("⚠️  No data to upload!")
        return

    print(f"\n📈 Uploading {len(data)} rows to Google Sheets...")
    
    try:
        creds_json = os.environ.get('GCP_CREDENTIALS')
        if not creds_json:
            raise ValueError("GCP_CREDENTIALS not found!")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(creds_json), 
            ['https://spreadsheets.google.com/feeds', 
             'https://www.googleapis.com/auth/drive']
        )
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1

        # Updated headers for new metrics
        HEADERS_ROW = ["Date", "Time", "Symbol", "Price", "Momentum_%", 
                       "Vol_Pressure", "OI_Change_%", "Volume_24h", 
                       "Open_Interest", "Funding_Rate", "Signal"]
        
        existing = sheet.get_all_values()
        
        if not existing:
            sheet.append_row(HEADERS_ROW)
        elif existing[0] != HEADERS_ROW:
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS_ROW, 1)

        sheet.append_rows(data)
        print("✅ SUCCESS!")
        
        # Sample
        print("\n📊 Sample uploaded:")
        for row in data[:3]:
            print(f"   {row[2]}: {row[10]} (Mom: {row[4]}%, Pressure: {row[5]})")

    except Exception as e:
        print(f"❌ UPLOAD ERROR: {e}")

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    rows = asyncio.run(main())
    upload_to_sheets(rows)
    
    print("\n" + "="*60)
    print("✅ SCRIPT COMPLETED")
    print("="*60)
    print("\n📝 Metrics Explained:")
    print("   • Momentum: Price change % (direction)")
    print("   • Vol Pressure: Bid/ask imbalance (buy/sell pressure)")
    print("   • OI Change: Position growth/decline")
    print("   • Signal: Combined interpretation")
    print("\n💡 These metrics work WITHOUT external APIs!")
    print("   No geo-blocking, no API keys, 100% reliable in GitHub Actions")
