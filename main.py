"""
Crypto Trading Dashboard - Streamlit App
Analyzes historical data from Google Sheets
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# Page config
st.set_page_config(
    page_title="Crypto Trading Dashboard",
    page_icon="📊",
    layout="wide"
)

# --- DATA LOADING ---
@st.cache_data(ttl=300)  # Cache for 5 minutes
def load_data():
    """Load data from Google Sheets"""
    try:
        creds_json = st.secrets.get("GCP_CREDENTIALS")
        if not creds_json:
            st.error("GCP_CREDENTIALS not found in secrets!")
            return pd.DataFrame()
        
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(creds_json),
            ['https://spreadsheets.google.com/feeds', 
             'https://www.googleapis.com/auth/drive']
        )
        client = gspread.authorize(creds)
        sheet = client.open("crypto_history").sheet1
        
        data = sheet.get_all_records()
        df = pd.DataFrame(data)
        
        # Convert data types
        df['DateTime'] = pd.to_datetime(df['Date'] + ' ' + df['Time'])
        df['Price'] = pd.to_numeric(df['Price'], errors='coerce')
        df['Volume_24h'] = pd.to_numeric(df['Volume_24h'], errors='coerce')
        df['Open_Interest'] = pd.to_numeric(df['Open_Interest'], errors='coerce')
        df['Funding_Rate'] = pd.to_numeric(df['Funding_Rate'], errors='coerce')
        
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()

# --- CALCULATIONS ---
def calculate_metrics(df):
    """Calculate all derived metrics"""
    if df.empty:
        return df
    
    df = df.sort_values(['Symbol', 'DateTime'])
    
    # Calculate changes for each symbol
    for symbol in df['Symbol'].unique():
        mask = df['Symbol'] == symbol
        symbol_data = df[mask].copy()
        
        # Price momentum (% change)
        df.loc[mask, 'Price_Change_%'] = symbol_data['Price'].pct_change() * 100
        
        # OI change (% change)
        df.loc[mask, 'OI_Change_%'] = symbol_data['Open_Interest'].pct_change() * 100
        
        # Volume momentum (current vs previous)
        df.loc[mask, 'Volume_Change_%'] = symbol_data['Volume_24h'].pct_change() * 100
        
        # Volume/OI Ratio (activity indicator)
        df.loc[mask, 'Vol_OI_Ratio'] = (symbol_data['Volume_24h'] / symbol_data['Open_Interest']) * 100
    
    # Fill NaN with 0 for first entries
    df.fillna(0, inplace=True)
    
    return df

def generate_signals(row):
    """Generate trading signals based on metrics"""
    price_chg = row['Price_Change_%']
    oi_chg = row['OI_Change_%']
    vol_chg = row['Volume_Change_%']
    funding = row['Funding_Rate']
    vol_oi = row['Vol_OI_Ratio']
    
    # Strong Bullish
    if price_chg > 2 and oi_chg > 5 and vol_chg > 20:
        return "🚀 STRONG BUY", "bullish"
    
    # Bullish
    elif price_chg > 1 and oi_chg > 0 and vol_oi > 50:
        return "📈 BUY", "bullish"
    
    # Strong Bearish
    elif price_chg < -2 and oi_chg > 5 and vol_chg > 20:
        return "💥 STRONG SELL", "bearish"
    
    # Bearish
    elif price_chg < -1 and oi_chg > 0:
        return "📉 SELL", "bearish"
    
    # Overheated
    elif funding > 0.05 and oi_chg > 10:
        return "⚠️ OVERHEATED", "warning"
    
    # Oversold
    elif funding < -0.03 and price_chg < -2:
        return "💎 OVERSOLD", "opportunity"
    
    # Short Squeeze Setup
    elif price_chg > 1 and oi_chg < 0 and vol_chg > 20:
        return "🔥 SHORT SQUEEZE", "bullish"
    
    # Long Liquidation
    elif price_chg < -1 and oi_chg < -5 and vol_chg > 20:
        return "⚡ LONG LIQUIDATION", "bearish"
    
    # High Activity
    elif vol_oi > 100:
        return "📊 HIGH ACTIVITY", "neutral"
    
    return "😴 NEUTRAL", "neutral"

# --- MAIN APP ---
def main():
    st.title("📊 Crypto Trading Dashboard")
    st.markdown("Real-time analysis of Price, Volume, OI, and Funding Rate")
    
    # Load data
    with st.spinner("Loading data from Google Sheets..."):
        df = load_data()
    
    if df.empty:
        st.error("No data available. Check your Google Sheets connection.")
        return
    
    # Calculate metrics
    df = calculate_metrics(df)
    
    # Get latest data only
    latest_df = df.sort_values('DateTime').groupby('Symbol').last().reset_index()
    
    # Generate signals
    latest_df[['Signal', 'Signal_Type']] = latest_df.apply(
        lambda row: pd.Series(generate_signals(row)), axis=1
    )
    
    # Sidebar filters
    st.sidebar.header("🔍 Filters")
    
    # Time range filter
    lookback_hours = st.sidebar.slider("Lookback Period (hours)", 4, 168, 24, 4)
    cutoff_time = datetime.now() - timedelta(hours=lookback_hours)
    df_filtered = df[df['DateTime'] >= cutoff_time]
    
    # Signal filter
    signal_types = st.sidebar.multiselect(
        "Signal Types",
        options=['bullish', 'bearish', 'warning', 'opportunity', 'neutral'],
        default=['bullish', 'bearish', 'warning', 'opportunity']
    )
    
    # Filter by signals
    signals_df = latest_df[latest_df['Signal_Type'].isin(signal_types)]
    
    # Top coins selector
    top_n = st.sidebar.slider("Show Top N Coins", 5, 50, 10)
    
    # --- METRICS ROW ---
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Total Coins", len(latest_df))
    with col2:
        bullish = len(latest_df[latest_df['Signal_Type'] == 'bullish'])
        st.metric("🟢 Bullish Signals", bullish)
    with col3:
        bearish = len(latest_df[latest_df['Signal_Type'] == 'bearish'])
        st.metric("🔴 Bearish Signals", bearish)
    with col4:
        warnings = len(latest_df[latest_df['Signal_Type'] == 'warning'])
        st.metric("⚠️ Warnings", warnings)
    
    # --- MAIN CONTENT ---
    tab1, tab2, tab3, tab4 = st.tabs(["🎯 Signals", "📈 Coin Analysis", "🔥 Top Movers", "📊 Market Overview"])
    
    # TAB 1: SIGNALS
    with tab1:
        st.header("🎯 Trading Signals")
        
        # Sort by signal strength (custom order)
        signal_order = {
            '🚀 STRONG BUY': 1,
            '🔥 SHORT SQUEEZE': 2,
            '📈 BUY': 3,
            '⚠️ OVERHEATED': 4,
            '💎 OVERSOLD': 5,
            '📊 HIGH ACTIVITY': 6,
            '📉 SELL': 7,
            '💥 STRONG SELL': 8,
            '⚡ LONG LIQUIDATION': 9,
            '😴 NEUTRAL': 10
        }
        signals_df['Signal_Order'] = signals_df['Signal'].map(signal_order)
        signals_df = signals_df.sort_values('Signal_Order')
        
        # Display signals table
        display_cols = ['Symbol', 'Signal', 'Price', 'Price_Change_%', 'OI_Change_%', 
                        'Volume_Change_%', 'Funding_Rate', 'Vol_OI_Ratio']
        
        st.dataframe(
            signals_df[display_cols].head(top_n).style.format({
                'Price': '${:,.2f}',
                'Price_Change_%': '{:+.2f}%',
                'OI_Change_%': '{:+.2f}%',
                'Volume_Change_%': '{:+.2f}%',
                'Funding_Rate': '{:.4f}%',
                'Vol_OI_Ratio': '{:.1f}'
            }),
            use_container_width=True,
            hide_index=True
        )
    
    # TAB 2: COIN ANALYSIS
    with tab2:
        st.header("📈 Individual Coin Analysis")
        
        # Coin selector
        selected_symbol = st.selectbox(
            "Select Coin",
            options=sorted(df['Symbol'].unique()),
            index=0
        )
        
        coin_df = df[df['Symbol'] == selected_symbol].sort_values('DateTime')
        
        if len(coin_df) > 1:
            col1, col2 = st.columns(2)
            
            with col1:
                # Price chart
                fig_price = go.Figure()
                fig_price.add_trace(go.Scatter(
                    x=coin_df['DateTime'],
                    y=coin_df['Price'],
                    mode='lines+markers',
                    name='Price',
                    line=dict(color='#00ff00', width=2)
                ))
                fig_price.update_layout(
                    title=f"{selected_symbol} - Price History",
                    xaxis_title="Time",
                    yaxis_title="Price (USD)",
                    template="plotly_dark",
                    height=400
                )
                st.plotly_chart(fig_price, use_container_width=True)
                
                # Volume chart
                fig_vol = go.Figure()
                fig_vol.add_trace(go.Bar(
                    x=coin_df['DateTime'],
                    y=coin_df['Volume_24h'],
                    name='Volume',
                    marker_color='#1f77b4'
                ))
                fig_vol.update_layout(
                    title="Volume 24h",
                    xaxis_title="Time",
                    yaxis_title="Volume (USD)",
                    template="plotly_dark",
                    height=300
                )
                st.plotly_chart(fig_vol, use_container_width=True)
            
            with col2:
                # Open Interest chart
                fig_oi = go.Figure()
                fig_oi.add_trace(go.Scatter(
                    x=coin_df['DateTime'],
                    y=coin_df['Open_Interest'],
                    mode='lines+markers',
                    name='Open Interest',
                    line=dict(color='#ff7f0e', width=2),
                    fill='tozeroy'
                ))
                fig_oi.update_layout(
                    title="Open Interest",
                    xaxis_title="Time",
                    yaxis_title="OI (USD)",
                    template="plotly_dark",
                    height=400
                )
                st.plotly_chart(fig_oi, use_container_width=True)
                
                # Funding Rate chart
                fig_fr = go.Figure()
                fig_fr.add_trace(go.Scatter(
                    x=coin_df['DateTime'],
                    y=coin_df['Funding_Rate'],
                    mode='lines+markers',
                    name='Funding Rate',
                    line=dict(color='#d62728', width=2)
                ))
                fig_fr.add_hline(y=0.05, line_dash="dash", line_color="red", annotation_text="Overheated")
                fig_fr.add_hline(y=-0.03, line_dash="dash", line_color="green", annotation_text="Oversold")
                fig_fr.update_layout(
                    title="Funding Rate",
                    xaxis_title="Time",
                    yaxis_title="Funding Rate (%)",
                    template="plotly_dark",
                    height=300
                )
                st.plotly_chart(fig_fr, use_container_width=True)
            
            # Metrics table
            st.subheader("📊 Latest Metrics")
            latest = coin_df.iloc[-1]
            
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Price", f"${latest['Price']:,.2f}", f"{latest['Price_Change_%']:+.2f}%")
            with col2:
                st.metric("OI", f"${latest['Open_Interest']:,.0f}", f"{latest['OI_Change_%']:+.2f}%")
            with col3:
                st.metric("Volume", f"${latest['Volume_24h']:,.0f}", f"{latest['Volume_Change_%']:+.2f}%")
            with col4:
                st.metric("Funding", f"{latest['Funding_Rate']:.4f}%")
        else:
            st.info("Not enough historical data for this coin yet.")
    
    # TAB 3: TOP MOVERS
    with tab3:
        st.header("🔥 Top Movers")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("📈 Biggest Gainers")
            gainers = latest_df.nlargest(10, 'Price_Change_%')[['Symbol', 'Price_Change_%', 'OI_Change_%', 'Volume_Change_%']]
            st.dataframe(
                gainers.style.format({
                    'Price_Change_%': '{:+.2f}%',
                    'OI_Change_%': '{:+.2f}%',
                    'Volume_Change_%': '{:+.2f}%'
                }),
                use_container_width=True,
                hide_index=True
            )
        
        with col2:
            st.subheader("📉 Biggest Losers")
            losers = latest_df.nsmallest(10, 'Price_Change_%')[['Symbol', 'Price_Change_%', 'OI_Change_%', 'Volume_Change_%']]
            st.dataframe(
                losers.style.format({
                    'Price_Change_%': '{:+.2f}%',
                    'OI_Change_%': '{:+.2f}%',
                    'Volume_Change_%': '{:+.2f}%'
                }),
                use_container_width=True,
                hide_index=True
            )
        
        st.subheader("⚡ Highest Volume Growth")
        vol_movers = latest_df.nlargest(10, 'Volume_Change_%')[['Symbol', 'Volume_Change_%', 'Vol_OI_Ratio', 'Signal']]
        st.dataframe(
            vol_movers.style.format({
                'Volume_Change_%': '{:+.2f}%',
                'Vol_OI_Ratio': '{:.1f}'
            }),
            use_container_width=True,
            hide_index=True
        )
    
    # TAB 4: MARKET OVERVIEW
    with tab4:
        st.header("📊 Market Overview")
        
        # OI vs Volume scatter
        fig_scatter = px.scatter(
            latest_df,
            x='Open_Interest',
            y='Volume_24h',
            size='Vol_OI_Ratio',
            color='Signal_Type',
            hover_data=['Symbol', 'Price_Change_%', 'Funding_Rate'],
            title="Open Interest vs Volume (size = Vol/OI Ratio)",
            labels={'Open_Interest': 'Open Interest (USD)', 'Volume_24h': 'Volume 24h (USD)'},
            color_discrete_map={
                'bullish': '#00ff00',
                'bearish': '#ff0000',
                'warning': '#ffaa00',
                'opportunity': '#00aaff',
                'neutral': '#888888'
            }
        )
        fig_scatter.update_layout(template="plotly_dark", height=500)
        st.plotly_chart(fig_scatter, use_container_width=True)
        
        # Funding Rate distribution
        col1, col2 = st.columns(2)
        
        with col1:
            fig_funding = px.histogram(
                latest_df,
                x='Funding_Rate',
                nbins=30,
                title="Funding Rate Distribution",
                labels={'Funding_Rate': 'Funding Rate (%)'}
            )
            fig_funding.update_layout(template="plotly_dark", height=400)
            st.plotly_chart(fig_funding, use_container_width=True)
        
        with col2:
            fig_oi_chg = px.histogram(
                latest_df,
                x='OI_Change_%',
                nbins=30,
                title="OI Change Distribution",
                labels={'OI_Change_%': 'OI Change (%)'}
            )
            fig_oi_chg.update_layout(template="plotly_dark", height=400)
            st.plotly_chart(fig_oi_chg, use_container_width=True)

if __name__ == "__main__":
    main()
