"""
CLEAN DATA COLLECTOR - Just pull raw data from Kraken
All analysis will be done in Streamlit dashboard
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

# --- MAIN LOGIC ---
async def main():
    print("\n" + "="*60)
    print("🔥 CRYPTO DATA COLLECTOR - RAW DATA ONLY")
    print("="*60)
    print("📊 Collecting: Price, Volume, OI, Funding Rate\n")
    
    kraken = ccxt.krakenfutures({'enableRateLimit': True})
    
    try:
        print("🔌 Fetching Kraken tickers...")
        tickers = await kraken.fetch_tickers()
        
        market_data = []
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')

        print(f"✅ Received {len(tickers)} tickers")

        for symbol, data in tickers.items():
            if ':USD' in symbol:
                raw = data.get('info', {})
                price = data.get('last')
                
                # Get volume - try USD volume first, then coin volume
                vol = raw.get('volumeQuote')
                if not vol or float(vol) == 0:
                    vol = raw.get('vol24h', 0)
                    if vol and price:
                        vol = float(vol) * price
                
                vol = float(vol) if vol else 0
                
                if price and vol > 0:
                    market_data.append({
                        'Symbol': symbol,
                        'Price': float(price),
                        'Volume_24h': vol,
                        'Open_Interest': float(raw.get('openInterest', 0)),
                        'Funding_Rate': float(raw.get('fundingRate', 0)) * 100  # Convert to %
                    })

        # Sort by volume
        top_coins = sorted(market_data, key=lambda x: x['Volume_24h'], reverse=True)[:TOP_N]
        
        print(f"✅ Processed {len(top_coins)} valid coins")
        
        # Convert to rows
        final_rows = []
        for coin in top_coins:
            final_rows.append([
                date_str, 
                time_str, 
                coin['Symbol'], 
                coin['Price'],
                coin['Volume_24h'], 
                coin['Open_Interest'], 
                coin['Funding_Rate']
            ])
        
        # Show top 5
        print("\n📊 Top 5 by Volume:")
        for coin in top_coins[:5]:
            base = coin['Symbol'].split('/')[0]
            print(f"   {base:8s} Price: ${coin['Price']:>10,.2f} | Vol: ${coin['Volume_24h']:>15,.0f} | OI: ${coin['Open_Interest']:>12,.0f}")
        
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

        # Simple headers - just raw data
        HEADERS_ROW = ["Date", "Time", "Symbol", "Price", "Volume_24h", 
                       "Open_Interest", "Funding_Rate"]
        
        existing = sheet.get_all_values()
        
        if not existing:
            print("📝 Creating new sheet with headers...")
            sheet.append_row(HEADERS_ROW)
        elif existing[0] != HEADERS_ROW:
            print("⚠️  Updating headers...")
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS_ROW, 1)

        sheet.append_rows(data)
        print("✅ SUCCESS! Data uploaded to Google Sheets")
        
        # Sample
        print("\n📊 Sample uploaded (first 3 rows):")
        for row in data[:3]:
            base = row[2].split('/')[0]
            print(f"   {base:8s} ${row[3]:>10,.2f} | Vol: ${row[4]:>15,.0f}")

    except Exception as e:
        print(f"❌ UPLOAD ERROR: {e}")
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
    print("\n📝 Data collected:")
    print("   • Date & Time (timestamp)")
    print("   • Symbol")
    print("   • Price")
    print("   • Volume 24h (USD)")
    print("   • Open Interest (USD)")
    print("   • Funding Rate (%)")
    print("\n🎨 Next: Build Streamlit dashboard for analysis!")
    print("   Dashboard will calculate: OI %, Momentum, Signals, etc.")
