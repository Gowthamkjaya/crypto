"""
Crypto Trading Dashboard - ENHANCED VERSION
Complete analysis with reasoning, timelines, and comprehensive visualizations
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.express as px
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

st.set_page_config(page_title="Crypto Dashboard Pro", page_icon="📊", layout="wide")

# Custom CSS
st.markdown("""
<style>
    .signal-box {padding: 10px; border-radius: 5px; margin: 5px 0; font-weight: bold;}
    .bullish {background-color: #00ff0033; color: #00ff00;}
    .bearish {background-color: #ff000033; color: #ff0000;}
    .warning {background-color: #ffaa0033; color: #ffaa00;}
    .opportunity {background-color: #00aaff33; color: #00aaff;}
    .neutral {background-color: #88888833; color: #888888;}
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=300)
def load_data():
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(st.secrets["GCP_CREDENTIALS"]),
            ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        )
        sheet = gspread.authorize(creds).open("crypto_history").sheet1
        df = pd.DataFrame(sheet.get_all_records())
        df['DateTime'] = pd.to_datetime(df['Date'] + ' ' + df['Time'])
        for col in ['Price', 'Volume_24h', 'Open_Interest', 'Funding_Rate']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        st.error(f"Error: {e}")
        return pd.DataFrame()

def calculate_metrics(df):
    df = df.sort_values(['Symbol', 'DateTime'])
    for symbol in df['Symbol'].unique():
        mask = df['Symbol'] == symbol
        s = df[mask].copy()
        
        # Multi-timeframe changes
        df.loc[mask, 'Price_Δ_4h'] = s['Price'].pct_change(1) * 100
        df.loc[mask, 'Price_Δ_12h'] = s['Price'].pct_change(3) * 100
        df.loc[mask, 'Price_Δ_24h'] = s['Price'].pct_change(6) * 100
        
        df.loc[mask, 'OI_Δ_4h'] = s['Open_Interest'].pct_change(1) * 100
        df.loc[mask, 'OI_Δ_12h'] = s['Open_Interest'].pct_change(3) * 100
        df.loc[mask, 'OI_Δ_24h'] = s['Open_Interest'].pct_change(6) * 100
        
        df.loc[mask, 'Vol_Δ'] = s['Volume_24h'].pct_change() * 100
        df.loc[mask, 'Vol_MA3'] = s['Volume_24h'].rolling(3).mean()
        df.loc[mask, 'Vol_Spike'] = (s['Volume_24h'] / s['Volume_24h'].rolling(3).mean()) * 100
        df.loc[mask, 'Vol_OI_Ratio'] = (s['Volume_24h'] / s['Open_Interest'].replace(0, 1)) * 100
        
        df.loc[mask, 'FR_MA3'] = s['Funding_Rate'].rolling(3).mean()
        df.loc[mask, 'FR_Trend'] = s['Funding_Rate'].diff()
    
    return df.fillna(0)

def generate_signal(r):
    p, p24, oi, oi24, vol, spike, fr, vo = (
        r['Price_Δ_4h'], r['Price_Δ_24h'], r['OI_Δ_4h'], r['OI_Δ_24h'],
        r['Vol_Δ'], r['Vol_Spike'], r['Funding_Rate'], r['Vol_OI_Ratio']
    )
    
    if p > 2 and oi > 5 and vol > 20:
        return "🚀 STRONG BUY", "bullish", f"Price↑{p:.1f}% | OI↑{oi:.1f}% (longs opening) | Vol↑{vol:.1f}% (conviction) → Momentum trade"
    elif p > 1 and oi > 0 and vo > 50:
        return "📈 BUY", "bullish", f"Price↑{p:.1f}% | OI growing | High activity → Bullish trend"
    elif p < -2 and oi > 5 and vol > 20:
        return "💥 STRONG SELL", "bearish", f"Price↓{p:.1f}% | OI↑{oi:.1f}% (shorts opening) | Vol↑{vol:.1f}% → Real selling"
    elif p < -1 and oi > 0:
        return "📉 SELL", "bearish", f"Price↓{p:.1f}% | OI growing → Bearish positioning"
    elif fr > 0.05 and oi > 10:
        return "⚠️ OVERHEATED", "warning", f"Funding {fr:.3f}% (HIGH) | OI↑{oi:.1f}% → Overleveraged, correction risk"
    elif fr < -0.03 and p24 < -5:
        return "💎 OVERSOLD", "opportunity", f"Funding {fr:.3f}% (negative) | Price↓{p24:.1f}% (24h) → Bounce zone"
    elif p > 1 and oi < -3 and vol > 20:
        return "🔥 SHORT SQUEEZE", "bullish", f"Price↑{p:.1f}% | OI↓{oi:.1f}% (shorts closing) | Vol↑{vol:.1f}% → Forced buying"
    elif p < -1 and oi < -5 and vol > 20:
        return "⚡ LIQUIDATION", "bearish", f"Price↓{p:.1f}% | OI↓{oi:.1f}% (longs closing) | Vol↑{vol:.1f}% → Panic, possible bottom"
    elif abs(p) < 1 and oi > 5 and spike < 80:
        return "🧩 ACCUMULATION", "neutral", f"Price stable | OI↑{oi:.1f}% (building) → Coiling for breakout"
    elif spike > 150:
        return "⚡ HIGH ACTIVITY", "neutral", f"Volume spike {spike:.0f}% → Major event/momentum shift"
    
    return "😴 NEUTRAL", "neutral", "No significant patterns"

def create_unified_chart(df, symbol):
    coin = df[df['Symbol'] == symbol].sort_values('DateTime')
    if len(coin) < 2:
        return None
    
    fig = make_subplots(
        rows=4, cols=1,
        subplot_titles=('💰 Price & Trend', '📊 Volume & Open Interest', 
                       '💸 Funding Rate', '📈 Momentum (% Change)'),
        vertical_spacing=0.07, row_heights=[0.3, 0.25, 0.2, 0.25],
        specs=[[{"secondary_y": False}], [{"secondary_y": True}], 
               [{"secondary_y": False}], [{"secondary_y": False}]]
    )
    
    # Price + SMA
    fig.add_trace(go.Scatter(x=coin['DateTime'], y=coin['Price'], name='Price',
                            line=dict(color='#00ff00', width=2)), row=1, col=1)
    
    # Volume (bars) + OI (line)
    fig.add_trace(go.Bar(x=coin['DateTime'], y=coin['Volume_24h'], name='Volume',
                        marker_color='#1f77b4', opacity=0.6), row=2, col=1)
    fig.add_trace(go.Scatter(x=coin['DateTime'], y=coin['Open_Interest'], name='OI',
                            line=dict(color='#ff7f0e', width=2)), row=2, col=1, secondary_y=True)
    
    # Funding with zones
    colors = ['#ff0000' if x > 0.05 else '#00ff00' if x < -0.03 else '#888888' 
              for x in coin['Funding_Rate']]
    fig.add_trace(go.Bar(x=coin['DateTime'], y=coin['Funding_Rate'], name='Funding',
                        marker_color=colors), row=3, col=1)
    fig.add_hline(y=0.05, line_dash="dash", line_color="red", row=3, col=1)
    fig.add_hline(y=-0.03, line_dash="dash", line_color="green", row=3, col=1)
    
    # Momentum indicators
    fig.add_trace(go.Scatter(x=coin['DateTime'], y=coin['Price_Δ_4h'], name='Price Δ',
                            line=dict(color='#00ff00', width=2)), row=4, col=1)
    fig.add_trace(go.Scatter(x=coin['DateTime'], y=coin['OI_Δ_4h'], name='OI Δ',
                            line=dict(color='#ff7f0e', width=2)), row=4, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="white", row=4, col=1)
    
    fig.update_layout(height=1200, showlegend=True, template="plotly_dark",
                     hovermode='x unified', title_text=f"{symbol} - Complete Analysis")
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    fig.update_yaxes(title_text="OI", row=2, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Funding %", row=3, col=1)
    fig.update_yaxes(title_text="Change %", row=4, col=1)
    
    return fig

def show_guide(metric):
    guides = {
        "Price Δ": "**Price Change:** Shows momentum. >2% = strong, <0.5% = consolidation",
        "OI Δ": "**Open Interest:** Positive = new positions, negative = closing. OI↑+Price↑ = real demand",
        "Volume Δ": "**Volume:** Confirms moves. High vol + trend = conviction, low vol = weak",
        "Vol/OI": "**Turnover:** >100 = very active, <20 = quiet/coiling",
        "Funding": "**Funding Rate:** >0.05% = overheated longs, <-0.03% = oversold",
        "Vol Spike": "**Activity:** >150% = major event, often marks trend changes"
    }
    return guides.get(metric, "Select a metric to learn more")

def main():
    st.title("📊 Crypto Trading Dashboard PRO")
    st.markdown("**Multi-Timeframe Analysis | Signal Reasoning | Complete Market View**")
    
    df = load_data()
    if df.empty:
        st.error("No data available")
        return
    
    df = calculate_metrics(df)
    latest = df.sort_values('DateTime').groupby('Symbol').last().reset_index()
    
    sig_data = latest.apply(lambda r: pd.Series(generate_signal(r)), axis=1)
    latest['Signal'], latest['Type'], latest['Reasoning'] = sig_data[0], sig_data[1], sig_data[2]
    
    # Sidebar
    st.sidebar.header("🎯 Settings")
    with st.sidebar.expander("📖 Metric Guide"):
        metric = st.selectbox("Learn:", ["Price Δ", "OI Δ", "Volume Δ", "Vol/OI", "Funding", "Vol Spike"])
        st.info(show_guide(metric))
    
    lookback = st.sidebar.slider("Lookback (hours)", 4, 168, 48, 4)
    types = st.sidebar.multiselect("Signals", ['bullish', 'bearish', 'warning', 'opportunity', 'neutral'],
                                    default=['bullish', 'bearish', 'warning', 'opportunity'])
    top_n = st.sidebar.slider("Show Top", 5, 50, 15)
    
    filtered = latest[latest['Type'].isin(types)]
    
    # Metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("📊 Coins", len(latest))
    col2.metric("🟢 Bullish", len(latest[latest['Type'] == 'bullish']))
    col3.metric("🔴 Bearish", len(latest[latest['Type'] == 'bearish']))
    col4.metric("⚠️ Warnings", len(latest[latest['Type'] == 'warning']))
    col5.metric("💎 Opportunities", len(latest[latest['Type'] == 'opportunity']))
    
    # Tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "🎯 Signals & Reasoning", "📈 Multi-Metric View", "🔥 Movers", "📊 Heatmap", "📚 Framework"
    ])
    
    with tab1:
        st.header("🎯 Trading Signals with Full Reasoning")
        
        order = {'🚀 STRONG BUY': 1, '🔥 SHORT SQUEEZE': 2, '📈 BUY': 3, '⚠️ OVERHEATED': 4,
                '💎 OVERSOLD': 5, '🧩 ACCUMULATION': 6, '⚡ HIGH ACTIVITY': 7,
                '📉 SELL': 8, '💥 STRONG SELL': 9, '⚡ LIQUIDATION': 10, '😴 NEUTRAL': 11}
        filtered['Order'] = filtered['Signal'].map(order)
        filtered = filtered.sort_values('Order')
        
        for _, r in filtered.head(top_n).iterrows():
            with st.container():
                c1, c2 = st.columns([1, 3])
                with c1:
                    st.subheader(r['Symbol'].split('/')[0])
                    st.markdown(f'<div class="signal-box {r["Type"]}">{r["Signal"]}</div>', 
                               unsafe_allow_html=True)
                    st.metric("Price", f"${r['Price']:,.2f}")
                    
                    coin_data = df[df['Symbol'] == r['Symbol']]
                    if not coin_data.empty:
                        last_time = coin_data['DateTime'].max()
                        hrs = (datetime.now() - last_time).total_seconds() / 3600
                        st.caption(f"⏰ Updated: {hrs:.1f}h ago")
                
                with c2:
                    st.markdown("**📋 Why This Signal:**")
                    st.info(r['Reasoning'])
                    
                    ca, cb, cc, cd = st.columns(4)
                    ca.metric("Price Δ (4h)", f"{r['Price_Δ_4h']:+.2f}%")
                    ca.caption(f"24h: {r['Price_Δ_24h']:+.2f}%")
                    cb.metric("OI Δ (4h)", f"{r['OI_Δ_4h']:+.2f}%")
                    cb.caption(f"24h: {r['OI_Δ_24h']:+.2f}%")
                    cc.metric("Volume Δ", f"{r['Vol_Δ']:+.2f}%")
                    cc.caption(f"Spike: {r['Vol_Spike']:.0f}%")
                    cd.metric("Funding", f"{r['Funding_Rate']:.4f}%")
                    cd.caption(f"MA: {r['FR_MA3']:.4f}%")
                st.divider()
    
    with tab2:
        st.header("📈 Complete Multi-Metric Analysis")
        
        symbol = st.selectbox("Select Coin", sorted(df['Symbol'].unique()))
        coin = df[df['Symbol'] == symbol].sort_values('DateTime')
        
        if len(coin) > 1:
            fig = create_unified_chart(df, symbol)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
            
            latest_coin = coin.iloc[-1]
            st.subheader("🔑 Key Metrics")
            
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown("**📊 Price Action**")
                st.metric("Current", f"${latest_coin['Price']:,.2f}")
                st.metric("4h", f"{latest_coin['Price_Δ_4h']:+.2f}%")
                st.metric("12h", f"{latest_coin['Price_Δ_12h']:+.2f}%")
                st.metric("24h", f"{latest_coin['Price_Δ_24h']:+.2f}%")
            
            with c2:
                st.markdown("**💰 Positioning**")
                st.metric("OI", f"${latest_coin['Open_Interest']:,.0f}")
                st.metric("OI Δ (4h)", f"{latest_coin['OI_Δ_4h']:+.2f}%")
                st.metric("OI Δ (24h)", f"{latest_coin['OI_Δ_24h']:+.2f}%")
                status = "Growing" if latest_coin['OI_Δ_4h'] > 0 else "Declining"
                st.caption(f"Status: {status}")
            
            with c3:
                st.markdown("**⚡ Activity**")
                st.metric("Volume", f"${latest_coin['Volume_24h']:,.0f}")
                st.metric("Vol/OI", f"{latest_coin['Vol_OI_Ratio']:.1f}")
                st.metric("Spike", f"{latest_coin['Vol_Spike']:.0f}%")
                st.metric("Funding", f"{latest_coin['Funding_Rate']:.4f}%")
        else:
            st.info("Need more data")
    
    with tab3:
        st.header("🔥 Top Movers")
        c1, c2 = st.columns(2)
        
        with c1:
            st.subheader("📈 Gainers (24h)")
            gainers = latest.nlargest(10, 'Price_Δ_24h')[
                ['Symbol', 'Price_Δ_24h', 'OI_Δ_24h', 'Vol_Spike', 'Signal']]
            st.dataframe(gainers.style.format({
                'Price_Δ_24h': '{:+.2f}%', 'OI_Δ_24h': '{:+.2f}%', 'Vol_Spike': '{:.0f}%'
            }).background_gradient(subset=['Price_Δ_24h'], cmap='Greens'), 
            use_container_width=True, hide_index=True)
        
        with c2:
            st.subheader("📉 Losers (24h)")
            losers = latest.nsmallest(10, 'Price_Δ_24h')[
                ['Symbol', 'Price_Δ_24h', 'OI_Δ_24h', 'Vol_Spike', 'Signal']]
            st.dataframe(losers.style.format({
                'Price_Δ_24h': '{:+.2f}%', 'OI_Δ_24h': '{:+.2f}%', 'Vol_Spike': '{:.0f}%'
            }).background_gradient(subset=['Price_Δ_24h'], cmap='Reds'),
            use_container_width=True, hide_index=True)
    
    with tab4:
        st.header("📊 Market Heatmap")
        
        fig = px.scatter(latest, x='Open_Interest', y='Volume_24h', size='Vol_OI_Ratio',
                        color='Type', hover_data=['Symbol', 'Price_Δ_24h', 'Funding_Rate'],
                        title="OI vs Volume (size = activity)",
                        color_discrete_map={'bullish': '#00ff00', 'bearish': '#ff0000',
                                          'warning': '#ffaa00', 'opportunity': '#00aaff', 'neutral': '#888888'})
        fig.update_layout(template="plotly_dark", height=600)
        st.plotly_chart(fig, use_container_width=True)
    
    with tab5:
        st.header("📚 Trading Signal Framework")
        st.markdown("""
        ### 🎯 Signal Logic
        
        **🚀 STRONG BUY:** Price ↑ >2% + OI ↑ >5% + Volume ↑ >20%
        → New longs opening with conviction = Momentum trade
        
        **💥 STRONG SELL:** Price ↓ <-2% + OI ↑ >5% + Volume ↑ >20%
        → New shorts entering with volume = Real selling
        
        **🔥 SHORT SQUEEZE:** Price ↑ >1% + OI ↓ <-3% + Volume ↑ >20%
        → Shorts forced to close = Explosive but unstable
        
        **⚡ LIQUIDATION:** Price ↓ <-1% + OI ↓ <-5% + Volume ↑ >20%
        → Panic selling, often marks bottom
        
        **⚠️ OVERHEATED:** Funding >0.05% + OI ↑ >10%
        → Too many longs, correction risk
        
        **💎 OVERSOLD:** Funding <-0.03% + Price ↓ <-5% (24h)
        → Extreme fear, reversal zone
        
        **🧩 ACCUMULATION:** Price stable (±1%) + OI ↑ >5%
        → Positions building = Breakout coming
        
        ### 📊 How to Use
        1. Check **Signals tab** for current opportunities
        2. Use **Multi-Metric** view to confirm with charts
        3. Monitor **timeframes** (4h/12h/24h) for conviction
        4. Read **reasoning** to understand WHY
        5. Cross-reference with **Top Movers** for context
        """)

if __name__ == "__main__":
    main()
