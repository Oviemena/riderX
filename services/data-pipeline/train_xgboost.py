import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
import pickle
import logging
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_data(filename='training_data.csv'):
    """Load training data from CSV."""
    df = pd.read_csv(filename)
    logger.info(f"Loaded {len(df)} records from {filename}")
    return df

def train_model(df):
    """Train XGBoost model with pooling features."""
    X = df[['eta_minutes', 'rating', 'ride_count', 'response_time']]
    y = df['match_quality']

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dtest = xgb.DMatrix(X_test, label=y_test)
    params = {
        'objective': 'reg:squarederror',
        'max_depth': 5,
        'eta': 0.05,
        'subsample': 0.7,
        'colsample_bytree': 0.7
    }
    model = xgb.train(params, dtrain, num_boost_round=200, evals=[(dtest, 'eval')], 
                      early_stopping_rounds=20, verbose_eval=True)

    y_pred = model.predict(dtest)
    rmse = np.sqrt(((y_pred - y_test) ** 2).mean())
    logger.info(f"Test RMSE: {rmse:.4f}")

    return model

def save_model(model, filename='xgboost_model.pkl'):
    """Save trained model to file."""
    with open(filename, 'wb') as f:
        pickle.dump(model, f)
    logger.info(f"Model saved as {filename}")

def run_training():
    """Run the training pipeline."""
    df = load_data()
    if df.empty:
        logger.error("No data to train on; run extract_firebase_data.py first")
        return
    model = train_model(df)
    save_model(model)

if __name__ == "__main__":
    run_training()