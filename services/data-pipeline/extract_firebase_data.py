from pathlib import Path
import sys
import pandas as pd
import logging
import requests
from time import time
from decouple import config
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from functools import lru_cache

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import directly - the PYTHONPATH is set to /app in the Dockerfile
try:
    from shared_utils.connections.connections import get_firebase_db
    from shared_utils.haversine.haversine import haversine
    logger.info("Successfully imported shared_utils")
except ModuleNotFoundError as e:
    logger.error(f"Failed to import shared_utils: {e}")
    # Try alternate path resolution as fallback
    shared_utils_path = Path(__file__).parent.parent.parent / "shared-utils"
    sys.path.append(str(shared_utils_path))
    logger.info(f"Added {shared_utils_path} to sys.path")
    from shared_utils.connections.connections import get_firebase_db
    from shared_utils.haversine.haversine import haversine
# Constants
GOOGLE_MAPS_API_KEY = config("GOOGLE_MAPS_API_KEY")
DEFAULT_RATING = 4.0
DEFAULT_RESPONSE_TIME = 5.0
MAX_WORKERS = 10
CACHE_SIZE = 128

@lru_cache(maxsize=CACHE_SIZE)
def get_traffic_eta(rider_lat: float, rider_lon: float, driver_lat: float, driver_lon: float, timestamp: Optional[int] = None) -> float:
    """
    Fetch traffic-adjusted ETA with caching or fallback to Haversine.
    
    Args:
        rider_lat: Rider's latitude
        rider_lon: Rider's longitude
        driver_lat: Driver's latitude
        driver_lon: Driver's longitude
        timestamp: Optional timestamp for the request
    
    Returns:
        float: Estimated time of arrival in minutes
    """
    try:
        url = (
            f"https://maps.googleapis.com/maps/api/directions/json"
            f"?origin={rider_lat},{rider_lon}"
            f"&destination={driver_lat},{driver_lon}"
            f"&key={GOOGLE_MAPS_API_KEY}&departure_time=now"
        )
        response = requests.get(url, timeout=5)
        if response.status_code == 200 and response.json()['status'] == 'OK':
            return response.json()['routes'][0]['legs'][0]['duration_in_traffic']['value'] / 60
    except (requests.RequestException, KeyError, IndexError) as e:
        logger.warning(f"Failed to get Google Maps ETA: {str(e)}")
    
    return haversine(rider_lat, rider_lon, driver_lat, driver_lon) / 40 * 60

def process_ride(ride_data: Tuple[str, Dict]) -> Optional[Dict]:
    """
    Process individual ride data.
    
    Args:
        ride_data: Tuple of (ride_id, ride_dict)
    
    Returns:
        Optional[Dict]: Processed ride data or None if invalid
    """
    ride_id, ride = ride_data
    if not (ride.get('status') == 'completed' and 'driver_id' in ride):
        return None

    driver_id = ride['driver_id']
    driver = drivers.get(driver_id, {})

    coordinates = [
        ride.get('rider_lat'),
        ride.get('rider_lon'),
        ride.get('driver_lat'),
        ride.get('driver_lon')
    ]
    
    if not all(coordinates):
        return None

    eta = get_traffic_eta(*coordinates, ride.get('timestamp'))
    request_time = ride.get('request_time')
    accept_time = ride.get('accept_time')

    return {
        'eta_minutes': eta,
        'rating': driver.get('rating', DEFAULT_RATING),
        'ride_count': len(driver.get('earnings', {})),
        'response_time': (accept_time - request_time) / 60 if all([request_time, accept_time]) else DEFAULT_RESPONSE_TIME,
        'match_quality': ride.get('rating', DEFAULT_RATING) / 5.0,
        'shared': ride.get('shared', False),
        'rider_count': ride.get('rider_count', 1),
        'ride_id': ride_id,
        'driver_id': driver_id
    }

def extract_data() -> pd.DataFrame:
    """
    Extract ride and driver data from Firebase using parallel processing.
    
    Returns:
        pd.DataFrame: DataFrame with extracted features
    """
    logger.info("Extracting data from Firebase...")
    global drivers  # Make drivers accessible to process_ride
    
    rides = firebase_db.child('ride_requests').get() or {}
    drivers = firebase_db.child('drivers').get() or {}

    if not rides or not drivers:
        logger.warning("Missing ride or driver data in Firebase")
        return pd.DataFrame()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(filter(None, executor.map(process_ride, rides.items())))

    df = pd.DataFrame(results)
    logger.info(f"Extracted {len(df)} completed rides")
    return df

def save_to_csv(df: pd.DataFrame, filename: str = 'training_data.csv') -> None:
    """
    Save extracted data to CSV with error handling.
    
    Args:
        df: DataFrame to save
        filename: Output filename
    """
    try:
        df.to_csv(filename, index=False)
        logger.info(f"Saved training data to {filename}")
    except Exception as e:
        logger.error(f"Failed to save CSV: {str(e)}")

def run_pipeline() -> None:
    """Run the complete data extraction pipeline with timing."""
    start_time = time()
    
    try:
        df = extract_data()
        if df.empty:
            logger.warning("No data extracted; check Firebase setup or ride completion data")
            return
        
        save_to_csv(df)
        duration = time() - start_time
        logger.info(f"Pipeline completed in {duration:.2f} seconds")
    
    except Exception as e:
        logger.error(f"Pipeline failed: {str(e)}")
        raise

if __name__ == "__main__":
    run_pipeline()