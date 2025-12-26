import pandas as pd
import numpy as np
import pickle
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------
TRAINING_FILE = "btc_15min_price_data.csv"
MODEL_OUTPUT = "polymarket_predictor.pkl"

def load_and_validate_data(filepath):
    """Loads CSV, converts Dates to Seconds, and validates prices"""
    print(f"📂 Loading data from {filepath}...")
    df = pd.read_csv(filepath)
    
    # 1. Map YOUR CSV headers to INTERNAL names
    # Update the Left Side keys to match your CSV's actual column names
    col_map = {
        'timestamp': 'timestamp',      
        'yes_best_bid': 'yes_price',   
        'no_best_bid': 'no_price'      
    }
    
    # Handle timestamp_utc if present (standard in your sample)
    if 'timestamp_utc' in df.columns:
        col_map['timestamp_utc'] = 'timestamp'
        
    df = df.rename(columns=col_map)
    
    # 2. CRITICAL FIX: Convert Timestamp String to Unix Seconds
    try:
        # First, convert to datetime objects
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        # Then, convert to seconds (integers) so we can do math
        df['timestamp'] = df['timestamp'].astype('int64') // 10**9
        print("✅ Converted timestamps to numeric seconds.")
    except Exception as e:
        print(f"⚠️ Timestamp conversion error: {e}")
        # Fallback: Maybe it's already in seconds?
        if df['timestamp'].dtype in [np.int64, np.float64]:
             print("   (Timestamps appear to be numeric already, continuing...)")
        else:
             print("   Ensure your timestamp column contains valid dates.")
             exit()
    
    # 3. VALIDATION CHECK
    max_price = df[['yes_price', 'no_price']].max().max()
    if max_price > 1.1:
        print("\n❌ CRITICAL ERROR: Data Validation Failed!")
        print(f"   Max price found: ${max_price:,.2f}")
        print("   The model expects POLYMARKET SHARE PRICES ($0.00 - $1.00).")
        print("   You seem to be training on BITCOIN SPOT PRICES (e.g. $90,000).")
        exit()
        
    print("✅ Data validation passed: Prices are in $0-$1 range.")
    return df

def extract_features(df_segment):
    """
    Extracts features from a 7-minute window of a market.
    """
    features = {}
    
    # Ensure sorted by time
    df = df_segment.sort_values('timestamp')
    
    # FIX: Handle missing values (NaNs) in source data
    # If a price is missing, use the previous known price. If all missing, default to 0.5.
    df['yes_price'] = df['yes_price'].ffill().bfill().fillna(0.5)
    df['no_price'] = df['no_price'].ffill().bfill().fillna(0.5)
    
    # Calculate simple stats
    yes_prices = df['yes_price'].values
    no_prices = df['no_price'].values
    
    features['yes_price_mean'] = np.mean(yes_prices)
    features['yes_price_std'] = np.std(yes_prices)
    features['yes_price_min'] = np.min(yes_prices) if len(yes_prices) > 0 else 0
    features['yes_price_max'] = np.max(yes_prices) if len(yes_prices) > 0 else 0
    features['yes_price_first'] = yes_prices[0] if len(yes_prices) > 0 else 0
    features['yes_price_final'] = yes_prices[-1] if len(yes_prices) > 0 else 0
    features['yes_price_change'] = features['yes_price_final'] - features['yes_price_first']
    
    features['no_price_mean'] = np.mean(no_prices)
    features['no_price_std'] = np.std(no_prices)
    
    # Momentum (Price velocity)
    if len(yes_prices) > 1:
        yes_momentum = np.diff(yes_prices)
        features['yes_momentum_mean'] = np.mean(yes_momentum)
    else:
        features['yes_momentum_mean'] = 0
        
    # Volatility
    features['yes_volatility'] = 0
    if features['yes_price_mean'] > 0:
         features['yes_volatility'] = features['yes_price_std'] / (features['yes_price_mean'] + 1e-6)

    # FINAL SAFETY CHECK: Replace any remaining NaNs with 0
    for k, v in features.items():
        if np.isnan(v) or np.isinf(v):
            features[k] = 0.0

    return features

def determine_winner(full_market_df):
    """
    Looks at the END of the 15-min market to see who won.
    Logic: If YES price > 0.95 at end, YES won.
    """
    if len(full_market_df) == 0:
        return None
        
    last_row = full_market_df.iloc[-1]
    
    # Check if market resolved clearly
    if last_row['yes_price'] >= 0.95:
        return 1 # YES WON
    elif last_row['no_price'] >= 0.95:
        return 0 # NO WON
    
    # If price ended at 0.50 (unresolved), skip this data point
    return None

def prepare_training_data(df):
    """Slices huge CSV into 15-minute market events using Market ID"""
    X = []
    y = []
    feature_names = []
    
    # FIX: Group by 'market_slug' to correctly separate back-to-back markets
    if 'market_slug' in df.columns:
        print("🔹 Segmentation: Using 'market_slug' (Most Accurate)")
        grouped = df.groupby('market_slug')
    elif 'market_title' in df.columns:
        print("🔹 Segmentation: Using 'market_title' (Fallback)")
        grouped = df.groupby('market_title')
    else:
        print("⚠️ Segmentation: Using time gaps (Least Accurate)")
        df['time_diff'] = df['timestamp'].diff()
        # Create arbitrary IDs based on time gaps > 5 mins
        df['group_id'] = (df['time_diff'] > 300).cumsum()
        grouped = df.groupby('group_id')
    
    print(f"🔄 Found {len(grouped)} unique market segments.")
    
    # Debug counters
    skipped_short = 0
    skipped_no_winner = 0
    skipped_no_data = 0
    valid_count = 0
    
    for name, market_df in grouped:
        # Ensure time sorted
        market_df = market_df.sort_values('timestamp')
        
        # Filter 1: Duration Check (Must be at least 10 mins / 600s)
        duration = market_df['timestamp'].max() - market_df['timestamp'].min()
        if duration < 600: 
            skipped_short += 1
            continue
            
        # Filter 2: Determine Winner (Ground Truth) using FULL data
        winner = determine_winner(market_df)
        if winner is None:
            skipped_no_winner += 1
            continue
            
        # Filter 3: Extract Features from first 7.5 mins (450s)
        start_time = market_df['timestamp'].min()
        input_df = market_df[market_df['timestamp'] <= start_time + 450]
        
        if len(input_df) < 10:
            skipped_no_data += 1
            continue
            
        features = extract_features(input_df)
        
        X.append(list(features.values()))
        y.append(winner)
        valid_count += 1
        
        # Save feature names from first valid iteration
        if len(X) == 1:
            feature_names = list(features.keys())

    # CRITICAL SAFETY CHECK
    if len(X) == 0:
        print("\n❌ ERROR: No valid training data found.")
        print("   Diagnostics:")
        print(f"   - Total Groups Found:      {len(grouped)}")
        print(f"   - Skipped (Too Short):     {skipped_short}")
        print(f"   - Skipped (No Winner):     {skipped_no_winner}")
        print(f"   - Skipped (Not Enough Data): {skipped_no_data}")
        print("\n   > Solution: Ensure your CSV has COMPLETE, resolved markets.")
        exit()

    # Convert to numpy arrays
    X_array = np.array(X)
    y_array = np.array(y)
    
    # FINAL SANITIZATION: Force all NaNs to 0.0 to prevent crash
    X_array = np.nan_to_num(X_array, nan=0.0, posinf=0.0, neginf=0.0)
    
    return X_array, y_array, feature_names

# ---------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------
if __name__ == "__main__":
    # 1. Load Data
    df = load_and_validate_data(TRAINING_FILE)
    
    # 2. Prepare Data
    X, y, feature_names = prepare_training_data(df)
    print(f"📊 Valid Training Samples: {len(X)}")
    print(f"   YES Wins: {sum(y)} | NO Wins: {len(y) - sum(y)}")
    
    # 3. Train Model
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    model = LogisticRegression(class_weight='balanced', random_state=42)
    model.fit(X_scaled, y)
    
    # 4. Evaluate
    accuracy = model.score(X_scaled, y)
    print(f"🏆 Model Accuracy: {accuracy:.2%}")
    
    # 5. Save Model
    print(f"💾 Saving model to {MODEL_OUTPUT}...")
    with open(MODEL_OUTPUT, 'wb') as f:
        pickle.dump({
            'models': {'Logistic Regression': model},
            'scaler': scaler,
            'feature_names': feature_names,
            'best_model_name': 'Logistic Regression'
        }, f)
    
    print("✅ Done! Transfer this .pkl file to your trading folder.")