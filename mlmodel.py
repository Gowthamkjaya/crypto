import os
import time
import requests
import json
import numpy as np
import pandas as pd
import pickle
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime, timezone
import csv

# ==========================================
# 🔧 CONFIGURATION
# ==========================================

PRIVATE_KEY = "0xbbd185bb356315b5f040a2af2fa28549177f3087559bb76885033e9cf8e8bf34"
POLYMARKET_ADDRESS = "0xC47167d407A91965fAdc7aDAb96F0fF586566bF7"

# ML Strategy Settings
ML_MODEL_PATH = "polymarket_predictor.pkl"
ML_CONFIDENCE_THRESHOLD = 0.95  # Only trade when confidence > 85%
ML_DATA_COLLECTION_TIME = 420   # Collect 7 minutes of data before prediction

# Position Settings
POSITION_SIZE = 5                   # Shares per trade
ENTRY_SLIPPAGE = 0.02               # Max 2 cents slippage on entry
TAKE_PROFIT_PERCENT = 0.20          # Exit at 20% gain from entry (0.20 = 20%)
STOP_LOSS_PERCENT = 0.40            # Stop loss at 40% loss from entry (0.40 = 40%)cls
TRAILING_STOP = True                # Use trailing stop
TRAILING_STOP_PERCENT = 0.15        # Trail by 15% from highest (0.15 = 15%)

# System Settings
CHECK_INTERVAL = 1
MIN_ORDER_SIZE = 0.1
TRADE_LOG_FILE = "ml_trading_bot_trades.csv"
ORDERBOOK_LOG_FILE = "orderbook_data_live.csv"
ENABLE_EXCEL = True

# Setup addresses
from eth_account import Account
wallet = Account.from_key(PRIVATE_KEY)
print(f"🔑 Private key controls: {wallet.address}")
print(f"🦄 Polymarket shows: {POLYMARKET_ADDRESS}")

if wallet.address.lower() == POLYMARKET_ADDRESS.lower():
    print(f"✅ Direct match - using EOA mode")
    USE_PROXY = False
    SIGNATURE_TYPE = 0
    TRADING_ADDRESS = Web3.to_checksum_address(wallet.address)
else:
    print(f"⚠️ Addresses differ - using proxy mode")
    USE_PROXY = True
    SIGNATURE_TYPE = 1
    TRADING_ADDRESS = Web3.to_checksum_address(POLYMARKET_ADDRESS)

# System setup
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/Vwy188P6gCu8mAUrbObWH"
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CHECKSUM = Web3.to_checksum_address(USDC_E_CONTRACT)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')


class MLPredictor:
    """Live ML predictor that collects data and makes predictions"""
    
    def __init__(self, model_path):
        print(f"🧠 Loading ML model from {model_path}...")
        with open(model_path, 'rb') as f:
            model_data = pickle.load(f)
        
        self.models = model_data['models']
        self.scaler = model_data['scaler']
        self.feature_names = model_data['feature_names']
        self.best_model_name = model_data['best_model_name']
        
        # Use Logistic Regression for better generalization
        if 'Logistic Regression' in self.models:
            self.best_model_name = 'Logistic Regression'
            print(f"   Using Logistic Regression (best generalization)")
        
        print(f"✅ Loaded {self.best_model_name}")
        
        self.current_market_data = []
        self.prediction_made = False
    
    def reset(self):
        """Reset for new market"""
        self.current_market_data = []
        self.prediction_made = False
    
    def add_snapshot(self, snapshot):
        """Add orderbook snapshot"""
        self.current_market_data.append(snapshot)
    
    def can_predict(self):
        """Check if we have enough data"""
        if len(self.current_market_data) < 10:
            return False
        
        if self.prediction_made:
            return False
        
        df = pd.DataFrame(self.current_market_data)
        
        # Calculate how long we have actually been collecting data
        collection_duration = df['time_elapsed'].max() - df['time_elapsed'].min()
        
        # FIXED: Prioritize time-based collection
        # Only predict after 7 minutes UNLESS we're very late in the market (< 3 mins remaining)
        if collection_duration >= ML_DATA_COLLECTION_TIME:
            return True
        
        # Emergency fallback: If market has < 3 minutes left and we have at least 2 mins of data
        time_remaining = df['seconds_until_close'].iloc[-1] if len(df) > 0 else 999
        if time_remaining < 180 and collection_duration >= 120:
            print(f"   ⚠️ Emergency prediction: Market closing in {time_remaining}s, using {collection_duration}s of data")
            return True
        
        return False
    
    def make_prediction(self):
        """Generate prediction from collected data"""
        if len(self.current_market_data) == 0:
            return None
        
        df = pd.DataFrame(self.current_market_data)
        
        # FIX: Try to get first 7 mins, but fallback to ALL data if late entry
        df_7min = df[df['time_elapsed'] <= ML_DATA_COLLECTION_TIME].copy()
        
        if len(df_7min) == 0:
            print("   ⚠️ Late entry detected: Using all available data instead of first 7 mins")
            df_7min = df.copy()
        
        # Extract features
        features = self._extract_features(df_7min)
        
        # specific safety check if extraction returned all zeros due to empty data
        if features.get('yes_price_mean', 0) == 0:
            return None

        X = pd.DataFrame([features])[self.feature_names]
        X_scaled = self.scaler.transform(X)
        
        # Predict
        model = self.models[self.best_model_name]
        prediction = model.predict(X_scaled)[0]
        probabilities = model.predict_proba(X_scaled)[0]
        
        self.prediction_made = True
        
        return {
            'prediction': 'YES' if prediction == 1 else 'NO',
            'yes_probability': probabilities[1],
            'no_probability': probabilities[0],
            'confidence': max(probabilities),
            'model': self.best_model_name,
            'data_points': len(df_7min)
        }
    
    def _extract_features(self, df):
        """Extract features (same as training)"""
        features = {}
        
        # SAFETY CHECK: If dataframe is empty, return zeros to prevent crash
        if len(df) == 0:
            return {k: 0 for k in self.feature_names} # Return dummy zeros

        def safe_divide(num, den, default=0):
            if den == 0 or np.isnan(den) or np.isnan(num):
                return default
            result = num / den
            return default if np.isinf(result) or np.isnan(result) else result
        
        def safe_std(arr):
            if len(arr) <= 1:
                return 0
            result = np.std(arr)
            return 0 if np.isnan(result) or np.isinf(result) else result
        
        # Price features - UPDATED: Removed deprecated method='ffill'
        yes_prices = df['yes_best_bid'].ffill().fillna(0.5).values
        no_prices = df['no_best_bid'].ffill().fillna(0.5).values
        
        features['yes_price_mean'] = np.mean(yes_prices)
        features['yes_price_std'] = safe_std(yes_prices)
        features['yes_price_min'] = np.min(yes_prices) if len(yes_prices) > 0 else 0
        features['yes_price_max'] = np.max(yes_prices) if len(yes_prices) > 0 else 0
        features['yes_price_range'] = features['yes_price_max'] - features['yes_price_min']
        features['yes_price_final'] = yes_prices[-1] if len(yes_prices) > 0 else 0
        features['yes_price_first'] = yes_prices[0] if len(yes_prices) > 0 else 0
        features['yes_price_change'] = features['yes_price_final'] - features['yes_price_first']
        features['yes_price_change_pct'] = safe_divide(features['yes_price_change'], features['yes_price_first'], 0)
        
        features['no_price_mean'] = np.mean(no_prices)
        features['no_price_std'] = safe_std(no_prices)
        features['no_price_min'] = np.min(no_prices) if len(no_prices) > 0 else 0
        features['no_price_max'] = np.max(no_prices) if len(no_prices) > 0 else 0
        features['no_price_range'] = features['no_price_max'] - features['no_price_min']
        features['no_price_final'] = no_prices[-1] if len(no_prices) > 0 else 0
        features['no_price_first'] = no_prices[0] if len(no_prices) > 0 else 0
        features['no_price_change'] = features['no_price_final'] - features['no_price_first']
        features['no_price_change_pct'] = safe_divide(features['no_price_change'], features['no_price_first'], 0)
        
        # Momentum
        if len(yes_prices) > 1:
            yes_momentum = np.diff(yes_prices)
            features['yes_momentum_mean'] = np.mean(yes_momentum)
            features['yes_momentum_std'] = safe_std(yes_momentum)
            features['yes_positive_momentum_pct'] = np.mean(yes_momentum > 0)
            
            no_momentum = np.diff(no_prices)
            features['no_momentum_mean'] = np.mean(no_momentum)
            features['no_momentum_std'] = safe_std(no_momentum)
            features['no_positive_momentum_pct'] = np.mean(no_momentum > 0)
        else:
            features['yes_momentum_mean'] = 0
            features['yes_momentum_std'] = 0
            features['yes_positive_momentum_pct'] = 0.5
            features['no_momentum_mean'] = 0
            features['no_momentum_std'] = 0
            features['no_positive_momentum_pct'] = 0.5
        
        # Spreads
        yes_spreads = df['yes_spread'].fillna(0).values
        no_spreads = df['no_spread'].fillna(0).values
        features['yes_spread_mean'] = np.mean(yes_spreads)
        features['yes_spread_std'] = safe_std(yes_spreads)
        features['no_spread_mean'] = np.mean(no_spreads)
        features['no_spread_std'] = safe_std(no_spreads)
        
        # Volume
        yes_bid_sizes = df['yes_bid_size'].fillna(0).values
        yes_ask_sizes = df['yes_ask_size'].fillna(0).values
        no_bid_sizes = df['no_bid_size'].fillna(0).values
        no_ask_sizes = df['no_ask_size'].fillna(0).values
        
        features['yes_bid_size_mean'] = np.mean(yes_bid_sizes)
        features['yes_bid_size_std'] = safe_std(yes_bid_sizes)
        features['yes_ask_size_mean'] = np.mean(yes_ask_sizes)
        features['no_bid_size_mean'] = np.mean(no_bid_sizes)
        features['no_bid_size_std'] = safe_std(no_bid_sizes)
        features['no_ask_size_mean'] = np.mean(no_ask_sizes)
        
        # Imbalance
        features['yes_order_imbalance'] = features['yes_bid_size_mean'] - features['yes_ask_size_mean']
        features['no_order_imbalance'] = features['no_bid_size_mean'] - features['no_ask_size_mean']
        
        # Order counts
        yes_order_counts = df['yes_order_count'].fillna(0).values
        no_order_counts = df['no_order_count'].fillna(0).values
        features['yes_order_count_mean'] = np.mean(yes_order_counts)
        features['no_order_count_mean'] = np.mean(no_order_counts)
        features['order_count_ratio'] = safe_divide(features['yes_order_count_mean'], features['no_order_count_mean'], 1)
        
        # Relative strength
        features['yes_vs_no_price_diff'] = features['yes_price_final'] - features['no_price_final']
        features['yes_vs_no_momentum_diff'] = features['yes_momentum_mean'] - features['no_momentum_mean']
        features['yes_vs_no_volume_ratio'] = safe_divide(features['yes_bid_size_mean'], features['no_bid_size_mean'], 1)
        
        # Volatility
        features['yes_volatility'] = safe_divide(features['yes_price_std'], features['yes_price_mean'], 0)
        features['no_volatility'] = safe_divide(features['no_price_std'], features['no_price_mean'], 0)
        
        # Time-based
        mid_point = len(df) // 2
        if mid_point > 0:
            first_half = df.iloc[:mid_point]
            second_half = df.iloc[mid_point:]
            
            yes_first = first_half['yes_best_bid'].ffill().fillna(0.5).values
            yes_second = second_half['yes_best_bid'].ffill().fillna(0.5).values
            no_first = first_half['no_best_bid'].ffill().fillna(0.5).values
            no_second = second_half['no_best_bid'].ffill().fillna(0.5).values
            
            features['yes_price_first_half_mean'] = np.mean(yes_first)
            features['yes_price_second_half_mean'] = np.mean(yes_second)
            features['yes_price_acceleration'] = features['yes_price_second_half_mean'] - features['yes_price_first_half_mean']
            
            features['no_price_first_half_mean'] = np.mean(no_first)
            features['no_price_second_half_mean'] = np.mean(no_second)
            features['no_price_acceleration'] = features['no_price_second_half_mean'] - features['no_price_first_half_mean']
        else:
            features['yes_price_first_half_mean'] = features['yes_price_mean']
            features['yes_price_second_half_mean'] = features['yes_price_mean']
            features['yes_price_acceleration'] = 0
            features['no_price_first_half_mean'] = features['no_price_mean']
            features['no_price_second_half_mean'] = features['no_price_mean']
            features['no_price_acceleration'] = 0
        
        # Final safety
        for key, value in features.items():
            if np.isnan(value) or np.isinf(value):
                features[key] = 0
        
        return features


class MLTradingBot:
    """ML-driven trading bot for Polymarket"""
    
    def __init__(self):
        print("\n🤖 ML Trading Bot Starting...")
        
        # Setup Web3
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc_contract = self.w3.eth.contract(address=USDC_CHECKSUM, abi=ERC20_ABI)
        
        # Setup Polymarket Client
        try:
            print(f"🔗 Setting up Polymarket client...")
            
            if USE_PROXY:
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID, 
                    signature_type=SIGNATURE_TYPE,
                    funder=TRADING_ADDRESS
                )
            else:
                self.client = ClobClient(
                    host=HOST, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID
                )
            
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            print(f"✅ Trading as: {self.client.get_address()}\n")
            
        except Exception as e:
            print(f"❌ Connection Failed: {e}")
            exit()
        
        # Load ML model
        self.ml_predictor = MLPredictor(ML_MODEL_PATH)
        
        # Tracking
        self.traded_markets = set()
        self.starting_balance = self.get_balance()
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        
        # Logging
        self.trade_logs = []
        self.orderbook_logs = []
        self.initialize_logs()
    
    def initialize_logs(self):
        """Initialize CSV logs"""
        # Trade log
        if not os.path.exists(TRADE_LOG_FILE):
            trade_headers = [
                'timestamp', 'market_slug', 'market_title',
                'ml_prediction', 'ml_confidence', 'yes_probability', 'no_probability',
                'entry_side', 'entry_price', 'shares',
                'exit_reason', 'exit_price', 'highest_price',
                'gross_pnl', 'pnl_percent', 'win_loss',
                'session_trade_number', 'balance_before', 'balance_after'
            ]
            with open(TRADE_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_headers)
                writer.writeheader()
            print(f"📊 Trade log: {TRADE_LOG_FILE}")
        
        # Orderbook log
        if not os.path.exists(ORDERBOOK_LOG_FILE):
            ob_headers = [
                'timestamp_utc', 'market_slug', 'market_title',
                'seconds_until_close', 'yes_best_bid', 'yes_best_ask', 'yes_spread',
                'no_best_bid', 'no_best_ask', 'no_spread',
                'yes_bid_size', 'yes_ask_size', 'no_bid_size', 'no_ask_size',
                'yes_order_count', 'no_order_count'
            ]
            with open(ORDERBOOK_LOG_FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=ob_headers)
                writer.writeheader()
            print(f"📊 Orderbook log: {ORDERBOOK_LOG_FILE}")
    
    def log_trade(self, trade_data):
        """Log completed trade"""
        try:
            self.trade_logs.append(trade_data)
            with open(TRADE_LOG_FILE, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade_data.keys())
                writer.writerow(trade_data)
            
            if ENABLE_EXCEL:
                df = pd.DataFrame(self.trade_logs)
                excel_file = TRADE_LOG_FILE.replace('.csv', '.xlsx')
                df.to_excel(excel_file, index=False, engine='openpyxl')
        except Exception as e:
            print(f"⚠️ Error logging trade: {e}")
    
    def log_orderbook(self, ob_data):
        """Log orderbook snapshot"""
        try:
            with open(ORDERBOOK_LOG_FILE, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=ob_data.keys())
                writer.writerow(ob_data)
        except:
            pass
    
    def get_balance(self):
        """Get USDC balance"""
        try:
            raw_bal = self.usdc_contract.functions.balanceOf(TRADING_ADDRESS).call()
            decimals = self.usdc_contract.functions.decimals().call()
            return raw_bal / (10 ** decimals)
        except:
            return 0.0
    
    def get_market_from_slug(self, slug):
        """Fetch market data from slug"""
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            resp = requests.get(url, timeout=10).json()
            
            if not resp or len(resp) == 0:
                return None
            
            event = resp[0]
            raw_ids = event['markets'][0].get('clobTokenIds')
            clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            
            return {
                'slug': slug,
                'yes_token': clob_ids[0],
                'no_token': clob_ids[1],
                'title': event.get('title', slug)
            }
        except:
            return None
    
    def get_orderbook_snapshot(self, market):
        """Get complete orderbook snapshot"""
        try:
            # Get YES orderbook
            yes_book = self.client.get_order_book(market['yes_token'])
            yes_bid = max(float(o.price) for o in yes_book.bids) if yes_book.bids else 0.01
            yes_ask = min(float(o.price) for o in yes_book.asks) if yes_book.asks else 0.99
            yes_bid_size = sum(float(o.size) for o in yes_book.bids) if yes_book.bids else 0
            yes_ask_size = sum(float(o.size) for o in yes_book.asks) if yes_book.asks else 0
            yes_order_count = len(yes_book.bids) + len(yes_book.asks)
            
            # Get NO orderbook
            no_book = self.client.get_order_book(market['no_token'])
            no_bid = max(float(o.price) for o in no_book.bids) if no_book.bids else 0.01
            no_ask = min(float(o.price) for o in no_book.asks) if no_book.asks else 0.99
            no_bid_size = sum(float(o.size) for o in no_book.bids) if no_book.bids else 0
            no_ask_size = sum(float(o.size) for o in no_book.asks) if no_book.asks else 0
            no_order_count = len(no_book.bids) + len(no_book.asks)
            
            return {
                'yes_best_bid': yes_bid,
                'yes_best_ask': yes_ask,
                'yes_spread': yes_ask - yes_bid,
                'no_best_bid': no_bid,
                'no_best_ask': no_ask,
                'no_spread': no_ask - no_bid,
                'yes_bid_size': yes_bid_size,
                'yes_ask_size': yes_ask_size,
                'no_bid_size': no_bid_size,
                'no_ask_size': no_ask_size,
                'yes_order_count': yes_order_count,
                'no_order_count': no_order_count
            }
        except Exception as e:
            print(f"⚠️ Orderbook snapshot error: {e}")
            return None
    
    def force_buy(self, token_id, price, size, max_retries=10, retry_delay=2):
        """Execute immediate buy order with retry logic"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None, 0
            
            limit_price = min(0.99, round(price + ENTRY_SLIPPAGE, 2))
            
            print(f"   ⚡ BUYING | Size: {size} | Price: ${price:.2f} | Limit: ${limit_price:.2f}")
            
            for attempt in range(max_retries):
                try:
                    order = self.client.create_order(OrderArgs(
                        price=limit_price,
                        size=size,
                        side=BUY,
                        token_id=token_id,
                    ))
                    
                    resp = self.client.post_orders([
                        PostOrdersArgs(order=order, orderType=OrderType.FOK)
                    ])
                    
                    if resp and len(resp) > 0:
                        result = resp[0]
                        order_id = result.get('orderID')
                        success = result.get('success')
                        
                        if success and order_id:
                            time.sleep(0.5)
                            order_info = self.client.get_order(order_id)
                            filled = float(order_info.size_matched) if hasattr(order_info, 'size_matched') else size
                            
                            if filled >= MIN_ORDER_SIZE:
                                print(f"   ✅ FILLED {filled} shares (attempt {attempt + 1}/{max_retries})")
                                return order_id, filled
                            else:
                                print(f"   ⚠️ Partial fill: {filled} shares (attempt {attempt + 1}/{max_retries})")
                        else:
                            print(f"   ⚠️ Order failed (attempt {attempt + 1}/{max_retries}): {result}")
                    
                    # If we get here, order didn't fill - adjust price and retry
                    if attempt < max_retries - 1:
                        # Get current best ask and adjust limit price up slightly
                        snapshot = self.get_orderbook_snapshot({'yes_token': token_id, 'no_token': token_id})
                        if snapshot:
                            # Determine which side we're on
                            current_ask = snapshot['yes_best_ask'] if token_id else snapshot['no_best_ask']
                            limit_price = min(0.99, round(current_ask + 0.01, 2))
                            print(f"   🔄 Adjusting limit to ${limit_price:.2f}, retrying...")
                        
                        time.sleep(retry_delay)
                    
                except Exception as e:
                    print(f"   ⚠️ Attempt {attempt + 1} error: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    continue
            
            print(f"   ❌ Failed to fill after {max_retries} attempts")
            return None, 0
            
        except Exception as e:
            print(f"   ❌ Buy error: {e}")
            return None, 0
    
    def force_sell(self, token_id, price, size, max_retries=10, retry_delay=2):
        """Execute immediate sell order with retry logic"""
        try:
            size = round(size, 1)
            if size < MIN_ORDER_SIZE:
                return None
            
            limit_price = max(0.01, round(price - 0.01, 2))
            
            print(f"   ⚡ SELLING | Size: {size} | Price: ${price:.2f}")
            
            for attempt in range(max_retries):
                try:
                    order = self.client.create_order(OrderArgs(
                        price=limit_price,
                        size=size,
                        side=SELL,
                        token_id=token_id,
                    ))
                    
                    resp = self.client.post_orders([
                        PostOrdersArgs(order=order, orderType=OrderType.FOK)
                    ])
                    
                    if resp and len(resp) > 0:
                        result = resp[0]
                        if result.get('success'):
                            print(f"   ✅ SOLD (attempt {attempt + 1}/{max_retries})")
                            return result.get('orderID')
                        else:
                            print(f"   ⚠️ Sell failed (attempt {attempt + 1}/{max_retries}): {result}")
                    
                    # Adjust price down slightly for next retry
                    if attempt < max_retries - 1:
                        snapshot = self.get_orderbook_snapshot({'yes_token': token_id, 'no_token': token_id})
                        if snapshot:
                            current_bid = snapshot['yes_best_bid'] if token_id else snapshot['no_best_bid']
                            limit_price = max(0.01, round(current_bid - 0.01, 2))
                            print(f"   🔄 Adjusting limit to ${limit_price:.2f}, retrying...")
                        
                        time.sleep(retry_delay)
                        
                except Exception as e:
                    print(f"   ⚠️ Attempt {attempt + 1} error: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    continue
            
            print(f"   ❌ Failed to sell after {max_retries} attempts")
            return None
            
        except Exception as e:
            print(f"   ❌ Sell error: {e}")
            return None
    
    def execute_ml_strategy(self, market, market_start_time):
        """
        ML-Driven Strategy:
        1. Collect 7 minutes of orderbook data
        2. Make ML prediction
        3. If confidence > 75%, enter trade
        4. Manage position with trailing stop
        """
        slug = market['slug']
        market_end_time = market_start_time + 900
        
        if slug in self.traded_markets:
            return "already_traded"
        
        print(f"\n{'='*70}")
        print(f"🧠 ML STRATEGY - DATA COLLECTION")
        print(f"{'='*70}")
        print(f"Market: {market['title']}")
        print(f"Collecting 7 minutes of orderbook data...\n")
        
        # Reset ML predictor
        self.ml_predictor.reset()
        
        # Phase 1: COLLECT DATA (7 minutes)
        data_collection_start = time.time()
        
        while True:
            current_time = time.time()
            time_remaining = market_end_time - current_time
            time_elapsed = 900 - time_remaining
            
            # Check if market ended
            if time_remaining <= 0:
                print(f"\n⏰ Market ended before prediction")
                self.traded_markets.add(slug)
                return "market_ended"
            
            # Get orderbook snapshot
            snapshot = self.get_orderbook_snapshot(market)
            
            if snapshot:
                # Add to ML data
                snapshot['timestamp_utc'] = datetime.now(timezone.utc)
                snapshot['market_slug'] = slug
                snapshot['market_title'] = market['title']
                snapshot['seconds_until_close'] = int(time_remaining)
                snapshot['time_elapsed'] = int(time_elapsed)
                
                self.ml_predictor.add_snapshot(snapshot)
                
                # Log to CSV
                self.log_orderbook(snapshot)
                
                # Progress display
                progress = min(100, (time_elapsed / ML_DATA_COLLECTION_TIME) * 100)
                mins = int(time_elapsed // 60)
                secs = int(time_elapsed % 60)
                print(f"📊 [{mins}m {secs}s] YES: ${snapshot['yes_best_bid']:.2f} | NO: ${snapshot['no_best_bid']:.2f} | Progress: {progress:.0f}%", end="\r")
            
            # Check if ready to predict
            if self.ml_predictor.can_predict():
                break
            
            time.sleep(CHECK_INTERVAL)
        
        # Phase 2: MAKE PREDICTION
        print(f"\n\n{'='*70}")
        print(f"🎯 GENERATING ML PREDICTION")
        print(f"{'='*70}")
        
        prediction = self.ml_predictor.make_prediction()
        
        if not prediction:
            print("❌ Prediction failed")
            self.traded_markets.add(slug)
            return "prediction_failed"
        
        print(f"\n🧠 ML PREDICTION:")
        print(f"   Predicted Winner: {prediction['prediction']}")
        print(f"   Confidence: {prediction['confidence']:.1%}")
        print(f"   YES Probability: {prediction['yes_probability']:.1%}")
        print(f"   NO Probability: {prediction['no_probability']:.1%}")
        print(f"   Model: {prediction['model']}")
        print(f"   Data Points: {prediction['data_points']}")
        
        # Phase 3: DECIDE TO TRADE
        if prediction['confidence'] < ML_CONFIDENCE_THRESHOLD:
            print(f"\n⚠️ CONFIDENCE TOO LOW ({prediction['confidence']:.1%} < {ML_CONFIDENCE_THRESHOLD:.0%})")
            print(f"   SKIPPING TRADE")
            self.traded_markets.add(slug)
            return "low_confidence"
        
        print(f"\n✅ HIGH CONFIDENCE SIGNAL!")
        
        # Determine which side to trade
        trade_side = prediction['prediction']
        token_to_buy = market['yes_token'] if trade_side == 'YES' else market['no_token']
        
        # Get current price
        current_time = time.time()
        time_remaining = market_end_time - current_time
        snapshot = self.get_orderbook_snapshot(market)
        
        if not snapshot:
            print("❌ Could not get prices")
            self.traded_markets.add(slug)
            return "no_prices"
        
        entry_price = snapshot['yes_best_ask'] if trade_side == 'YES' else snapshot['no_best_ask']
        
        # Check if price is reasonable
        if entry_price > 0.80:
            print(f"⚠️ Entry price too high: ${entry_price:.2f}")
            self.traded_markets.add(slug)
            return "price_too_high"
        
        # Check balance
        current_balance = self.get_balance()
        max_cost = POSITION_SIZE * (entry_price + ENTRY_SLIPPAGE)
        
        if max_cost > current_balance:
            print(f"⚠️ Insufficient balance: ${current_balance:.2f} < ${max_cost:.2f}")
            self.traded_markets.add(slug)
            return "insufficient_balance"
        
        # Phase 4: ENTER TRADE
        print(f"\n{'='*70}")
        print(f"🎯 ENTERING TRADE")
        print(f"{'='*70}")
        print(f"Side: {trade_side}")
        print(f"Entry Price: ${entry_price:.2f}")
        print(f"Shares: {POSITION_SIZE}")
        print(f"Max Cost: ${max_cost:.2f}")
        
        order_id, actual_shares = self.force_buy(token_to_buy, entry_price, POSITION_SIZE)
        
        if not order_id or actual_shares == 0:
            print(f"❌ Entry failed")
            self.traded_markets.add(slug)
            return "entry_failed"
        
        print(f"✅ POSITION OPENED")
        print(f"   Shares: {actual_shares}")
        print(f"   Entry: ${entry_price:.2f}")
        
        # Initialize trade data
        trade_data = {
            'timestamp': datetime.now().isoformat(),
            'market_slug': slug,
            'market_title': market['title'],
            'ml_prediction': prediction['prediction'],
            'ml_confidence': prediction['confidence'],
            'yes_probability': prediction['yes_probability'],
            'no_probability': prediction['no_probability'],
            'entry_side': trade_side,
            'entry_price': entry_price,
            'shares': actual_shares,
            'balance_before': current_balance,
            'session_trade_number': self.session_trades + 1
        }
        
        # Phase 5: MANAGE POSITION
        print(f"\n{'='*70}")
        print(f"💎 MANAGING POSITION")
        print(f"{'='*70}")
        
        # Calculate percentage-based targets
        take_profit_price = min(0.99, entry_price * (1 + TAKE_PROFIT_PERCENT))
        stop_loss_price = max(0.01, entry_price * (1 - STOP_LOSS_PERCENT))
        
        print(f"Entry Price: ${entry_price:.2f}")
        print(f"Take Profit: ${take_profit_price:.2f} (+{TAKE_PROFIT_PERCENT*100:.0f}%)")
        print(f"Stop Loss: ${stop_loss_price:.2f} (-{STOP_LOSS_PERCENT*100:.0f}%)")
        if TRAILING_STOP:
            print(f"Trailing Stop: {TRAILING_STOP_PERCENT*100:.0f}% from peak")
        
        highest_bid = entry_price
        trailing_stop_price = entry_price * (1 - TRAILING_STOP_PERCENT)
        
        while True:
            time.sleep(CHECK_INTERVAL)
            
            current_time = time.time()
            time_remaining = market_end_time - current_time
            
            # Market closed
            if time_remaining <= 0:
                print(f"\n\n⏰ MARKET CLOSED")
                
                # Check final price
                snapshot = self.get_orderbook_snapshot(market)
                if snapshot:
                    final_bid = snapshot['yes_best_bid'] if trade_side == 'YES' else snapshot['no_best_bid']
                    if final_bid > 0.50:
                        # We won!
                        exit_price = 1.0
                        pnl = (exit_price - entry_price) * actual_shares
                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                        win_loss = 'WIN'
                        exit_reason = 'MARKET_SETTLED_WIN'
                        self.session_wins += 1
                    else:
                        # We lost
                        exit_price = 0.0
                        pnl = -entry_price * actual_shares
                        pnl_pct = -100.0
                        win_loss = 'LOSS'
                        exit_reason = 'MARKET_SETTLED_LOSS'
                        self.session_losses += 1
                else:
                    exit_price = 0.0
                    pnl = -entry_price * actual_shares
                    pnl_pct = -100.0
                    win_loss = 'LOSS'
                    exit_reason = 'MARKET_CLOSED'
                    self.session_losses += 1
                
                trade_data['exit_reason'] = exit_reason
                trade_data['exit_price'] = exit_price
                trade_data['highest_price'] = highest_bid
                trade_data['gross_pnl'] = pnl
                trade_data['pnl_percent'] = pnl_pct
                trade_data['win_loss'] = win_loss
                trade_data['balance_after'] = self.get_balance()
                
                self.log_trade(trade_data)
                self.session_trades += 1
                self.traded_markets.add(slug)
                
                print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                return exit_reason.lower()
            
            # Get current bid
            snapshot = self.get_orderbook_snapshot(market)
            if not snapshot:
                continue
            
            current_bid = snapshot['yes_best_bid'] if trade_side == 'YES' else snapshot['no_best_bid']
            
            # Update highest bid and trailing stop
            if current_bid > highest_bid:
                highest_bid = current_bid
                if TRAILING_STOP:
                    trailing_stop_price = highest_bid * (1 - TRAILING_STOP_PERCENT)
            
            current_pnl = (current_bid - entry_price) * actual_shares
            current_pnl_pct = ((current_bid - entry_price) / entry_price) * 100
            time_left_str = f"{int(time_remaining//60)}m {int(time_remaining%60)}s"
            
            print(f"   💹 [{time_left_str}] Bid: ${current_bid:.2f} | High: ${highest_bid:.2f} | P&L: ${current_pnl:+.2f} ({current_pnl_pct:+.1f}%)", end="\r")
            
            # Check take profit
            if current_bid >= take_profit_price:
                print(f"\n\n🚀 TAKE PROFIT HIT @ ${current_bid:.2f} (+{current_pnl_pct:.1f}%)!")
                
                exit_id = self.force_sell(token_to_buy, current_bid, actual_shares)
                
                if exit_id:
                    exit_price = current_bid
                    pnl = (exit_price - entry_price) * actual_shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    trade_data['exit_reason'] = 'TAKE_PROFIT'
                    trade_data['exit_price'] = exit_price
                    trade_data['highest_price'] = highest_bid
                    trade_data['gross_pnl'] = pnl
                    trade_data['pnl_percent'] = pnl_pct
                    trade_data['win_loss'] = 'WIN'
                    trade_data['balance_after'] = self.get_balance()
                    
                    self.log_trade(trade_data)
                    self.session_wins += 1
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    return "take_profit"
            
            # Check stop loss
            stop_triggered = False
            stop_reason = ""
            
            if current_bid <= stop_loss_price:
                stop_triggered = True
                stop_reason = "STOP_LOSS"
            elif TRAILING_STOP and current_bid <= trailing_stop_price:
                stop_triggered = True
                stop_reason = "TRAILING_STOP"
            
            if stop_triggered:
                print(f"\n\n🛑 {stop_reason} HIT @ ${current_bid:.2f} ({current_pnl_pct:+.1f}%)!")
                
                exit_id = self.force_sell(token_to_buy, current_bid, actual_shares)
                
                if exit_id:
                    exit_price = current_bid
                    pnl = (exit_price - entry_price) * actual_shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    trade_data['exit_reason'] = stop_reason
                    trade_data['exit_price'] = exit_price
                    trade_data['highest_price'] = highest_bid
                    trade_data['gross_pnl'] = pnl
                    trade_data['pnl_percent'] = pnl_pct
                    trade_data['win_loss'] = 'LOSS' if pnl < 0 else 'WIN'
                    trade_data['balance_after'] = self.get_balance()
                    
                    self.log_trade(trade_data)
                    
                    if pnl < 0:
                        self.session_losses += 1
                    else:
                        self.session_wins += 1
                    
                    self.session_trades += 1
                    self.traded_markets.add(slug)
                    
                    print(f"💰 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                    print(f"📊 Peak: ${highest_bid:.2f}")
                    return stop_reason.lower()
    
    def run(self):
        """Main bot loop"""
        print(f"\n🚀 ML Trading Bot Running...")
        print(f"\n📊 STRATEGY PARAMETERS:")
        print(f"   ML Model: {self.ml_predictor.best_model_name}")
        print(f"   Confidence Threshold: {ML_CONFIDENCE_THRESHOLD:.0%}")
        print(f"   Data Collection: {ML_DATA_COLLECTION_TIME}s (7 minutes)")
        print(f"   Position Size: {POSITION_SIZE} shares")
        print(f"   Take Profit: +{TAKE_PROFIT_PERCENT*100:.0f}% from entry")
        print(f"   Stop Loss: -{STOP_LOSS_PERCENT*100:.0f}% from entry")
        if TRAILING_STOP:
            print(f"   Trailing Stop: {TRAILING_STOP_PERCENT*100:.0f}% from peak")
        print(f"\n💰 Starting Balance: ${self.starting_balance:.2f}\n")
        
        current_market = None
        
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                current_timestamp = int(now_utc.timestamp())
                
                market_timestamp = (current_timestamp // 900) * 900
                expected_slug = f"btc-updown-15m-{market_timestamp}"
                
                if not current_market or current_market['slug'] != expected_slug:
                    print(f"\n🔍 Looking for: {expected_slug}")
                    current_market = self.get_market_from_slug(expected_slug)
                    
                    if current_market:
                        market_end = market_timestamp + 900
                        time_left = market_end - current_timestamp
                        print(f"✅ Found: {current_market['title']}")
                        print(f"   Time Remaining: {time_left//60}m {time_left%60}s")
                        
                        # Cancel old orders
                        try:
                            self.client.cancel_all()
                            time.sleep(1)
                        except:
                            pass
                    else:
                        next_market_time = ((current_timestamp // 900) + 1) * 900
                        wait_time = next_market_time - current_timestamp
                        print(f"⏳ Waiting {wait_time}s for next market")
                        time.sleep(min(wait_time, 60))
                        continue
                
                status = self.execute_ml_strategy(current_market, market_timestamp)
                
                # Print session stats
                if status in ["take_profit", "stop_loss", "trailing_stop", "market_settled_win", "market_settled_loss"]:
                    current_balance = self.get_balance()
                    session_pnl = current_balance - self.starting_balance
                    win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                    
                    print(f"\n📊 SESSION STATS:")
                    print(f"   Trades: {self.session_trades} | Wins: {self.session_wins} | Losses: {self.session_losses}")
                    print(f"   Win Rate: {win_rate:.1f}%")
                    print(f"   Balance: ${self.starting_balance:.2f} → ${current_balance:.2f}")
                    print(f"   Session P&L: ${session_pnl:+.2f}\n")
                    
                    time.sleep(5)
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\n\n🛑 Bot stopped by user")
                current_balance = self.get_balance()
                session_pnl = current_balance - self.starting_balance
                win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
                
                print(f"\n📊 FINAL STATS:")
                print(f"   Trades: {self.session_trades}")
                print(f"   Wins: {self.session_wins} | Losses: {self.session_losses}")
                print(f"   Win Rate: {win_rate:.1f}%")
                print(f"   Starting: ${self.starting_balance:.2f}")
                print(f"   Ending: ${current_balance:.2f}")
                print(f"   Total P&L: ${session_pnl:+.2f}")
                break
                
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)


if __name__ == "__main__":
    bot = MLTradingBot()
    bot.run()
