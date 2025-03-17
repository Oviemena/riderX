import logging
from functools import wraps
from pathlib import Path
import sys
import redis
import requests
import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from time import time
import pickle
from decouple import config


from prometheus_client import start_http_server
from sklearn.cluster import KMeans
import xgboost as xgb

shared_utils_path = Path("/app/shared-utils")
sys.path.append(str(shared_utils_path))


from connections.connections import get_firebase_db, get_redis_connection
from haversine import haversine
from helper_func.metrics import match_latency, surge_multiplier_gauge, request_duration, ml_prediction_latency, match_quality, pooling_efficiency, shared_ride_count


# Use shared connections
r = get_redis_connection()
firebase_db = get_firebase_db()

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# GOOGLE maps api setup
GOOGLE_MAPS_API_KEY = config('GOOGLE_MAPS_API_KEY')

# Prometheus metrics
start_http_server(8002)
logger.info("Ride Service metrics on port 8002")


# Deserializing the model
with open('xgboost_model.pkl', 'rb') as f:
    xgb_model = pickle.load(f)
logger.info("XGBoost model loaded")



def get_traffic_eta(rider_lat, rider_lon, driver_lat, driver_lon, timestamp=None):
    """Fetch traffic-adjusted ETA from Google Maps API or fallback to Haversine-based estimate.

    Args:
        rider_lat (float): Rider's latitude.
        rider_lon (float): Rider's longitude.
        driver_lat (float): Driver's latitude.
        driver_lon (float): Driver's longitude.
        timestamp (int, optional): Unix timestamp for historical ETA. Defaults to None.

    Returns:
        float: Estimated time of arrival in minutes.
    """
    url = f"https://maps.googleapis.com/maps/api/directions/json?origin={rider_lat},{rider_lon}&destination={driver_lat},{driver_lon}&key={GOOGLE_MAPS_API_KEY}&departure_time=now"
    response = requests.get(url)
    if response.status_code == 200 and response.json()['status'] == 'OK':
        return response.json()['routes'][0]['legs'][0]['duration_in_traffic']['value'] / 60
    return haversine(rider_lat, rider_lon, driver_lat, driver_lon) / 40 * 60

def get_pooled_route_eta(rider_coords, driver_lat, driver_lon):
    """Calculate ETA for a pooled route with multiple rider pickups using Google Maps Directions API.

    Args:
        rider_coords (list): List of [lat, lon] pairs for riders.
        driver_lat (float): Driver's latitude.
        driver_lon (float): Driver's longitude.

    Returns:
        float: Total ETA in minutes for the optimized route.
    """
    origin = f"{driver_lat},{driver_lon}"
    waypoints = "|".join([f"{lat},{lon}" for lat, lon in rider_coords])
    url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={origin}&waypoints={waypoints}&key={GOOGLE_MAPS_API_KEY}&departure_time=now"
    response = requests.get(url)
    if response.status_code == 200 and response.json()['status'] == 'OK':
        total_duration = sum(leg['duration_in_traffic']['value'] for leg in response.json()['routes'][0]['legs']) / 60
        return total_duration
    # Fallback to sum of individual ETAs
    return sum(get_traffic_eta(lat, lon, driver_lat, driver_lon) for lat, lon in rider_coords)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(redis.exceptions.RedisClusterException))
def calculate_surge_multiplier(rider_lat, rider_lon, region="nyc", max_radius_km=10):
    """Calculate surge multiplier based on driver availability near the rider with retries.

    Args:
        rider_lat (float): Rider's latitude.
        rider_lon (float): Rider's longitude.
        region (str, optional): Region identifier. Defaults to "nyc".
        max_radius_km (int, optional): Search radius in kilometers. Defaults to 10.

    Returns:
        float: Surge multiplier (1.0 to 2.5).
    """
    driver_key = f"drivers:{region}"
    nearby_drivers = r.geosearch(driver_key, longitude=rider_lon, latitude=rider_lat, radius=max_radius_km * 1000, unit="m")
    available_drivers = sum(1 for d in nearby_drivers if r.get(f"driver:{d}:available") == "1")
    base_demand = 10
    surge = max(1.0, min(2.5, base_demand / max(available_drivers, 1)))
    surge_multiplier_gauge.labels(region).set(surge)
    return surge

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(Exception))
def cluster_ride_requests(region="nyc", max_radius_km=1):
    """Cluster pending ride requests for pooling using KMeans with retries.

    Args:
        region (str, optional): Region to filter rides. Defaults to "nyc".
        max_radius_km (int, optional): Maximum radius for clustering consideration. Defaults to 1.

    Returns:
        list: List of clusters, each containing ride dictionaries with 'id', 'lat', and 'lon'.
    """
    pending_rides = firebase_db.child('ride_requests').order_by_child('status').equal_to('pending').get()
    if not pending_rides:
        return []

    ride_coords = []
    ride_data = []
    for ride_id, ride in pending_rides.items():
        if ride.get('region') == region and ride.get('shared', False):
            ride_coords.append([ride['rider_lat'], ride['rider_lon']])
            ride_data.append({'id': ride_id, 'lat': ride['rider_lat'], 'lon': ride['rider_lon']})

    if len(ride_coords) < 2:
        return [ride_data]

    kmeans = KMeans(n_clusters=min(4, len(ride_coords)), random_state=42)
    labels = kmeans.fit_predict(np.array(ride_coords))
    clusters = {}
    for label, ride in zip(labels, ride_data):
        clusters.setdefault(label, []).append(ride)
    return list(clusters.values())

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(redis.exceptions.RedisClusterException))
def find_nearest_driver(rider_lats, rider_lons, region="nyc", max_radius_km=10, shared=False):
    """Find the best driver for one or more riders using XGBoost-based matching with retries.

    Args:
        rider_lats (list): List of rider latitudes.
        rider_lons (list): List of rider longitudes.
        region (str, optional): Region identifier. Defaults to "nyc".
        max_radius_km (int, optional): Search radius in kilometers. Defaults to 10.
        shared (bool, optional): Whether this is a shared ride. Defaults to False.

    Returns:
        dict or str: Driver match details or error message if no driver found.
    """
    start_time = time()
    driver_key = f"drivers:{region}"
    
    centroid_lat = np.mean(rider_lats) if shared else rider_lats[0]
    centroid_lon = np.mean(rider_lons) if shared else rider_lons[0]
    surge = calculate_surge_multiplier(centroid_lat, centroid_lon, region)
    
    nearby = r.geosearch(driver_key, longitude=centroid_lon, latitude=centroid_lat, radius=max_radius_km * 1000, unit="m")
    if not nearby:
        return f"No available drivers within {max_radius_km} km"

    rider_coords = list(zip(rider_lats, rider_lons))
    driver_ids = [d for d in nearby]
    driver_data = []
    for driver_id in driver_ids:
        if r.get(f"driver:{driver_id}:available") == "1":
            pos = r.geopos(driver_key, driver_id)[0]
            lat, lon = pos[1], pos[0]
            eta = get_pooled_route_eta(rider_coords, lat, lon) if shared else get_traffic_eta(rider_lats[0], rider_lons[0], lat, lon)
            rating = float(r.get(f"driver:{driver_id}:rating") or 4.0)
            ride_count = float(r.get(f"driver:{driver_id}:ride_count") or 0)
            response_time = float(r.get(f"driver:{driver_id}:avg_response_time") or 5.0)
            driver_data.append({
                "id": driver_id,
                "lat": lat,
                "lon": lon,
                "eta": eta,
                "rating": rating,
                "ride_count": ride_count,
                "response_time": response_time
            })
    
    if not driver_data:
        return f"No available drivers within {max_radius_km} km"

    features = np.array([[d["eta"], d["rating"], d["ride_count"], d["response_time"]] for d in driver_data])
    dmatrix = xgb.DMatrix(features)

    pred_start = time()
    quality_scores = xgb_model.predict(dmatrix)
    pred_latency = time() - pred_start
    ml_prediction_latency.labels(region).observe(pred_latency)

    best_driver_idx = np.argmax(quality_scores)
    best_driver = driver_data[best_driver_idx]
    min_distance = haversine(centroid_lat, centroid_lon, best_driver["lat"], best_driver["lon"])

    latency = time() - start_time
    match_latency.labels(region).observe(latency)
    match_quality.labels(region).set(quality_scores[best_driver_idx])
    if shared:
        pooling_efficiency.labels(region).set(len(rider_lats))
        shared_ride_count.labels(region).inc()

    return {
        "driver_id": best_driver["id"],
        "distance_km": min_distance,
        "surge_multiplier": surge,
        "eta_minutes": best_driver["eta"],
        "match_quality": float(quality_scores[best_driver_idx]),
        "rider_count": len(rider_lats) if shared else 1
    }

def monitor_request(f):
    """Decorator to monitor request duration for Prometheus.

    Args:
        f (function): The function to decorate.

    Returns:
        function: Decorated function with timing metrics.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time()
        result = f(*args, **kwargs)
        duration = time() - start_time
        request_duration.labels(f.__name__).observe(duration)
        return result
    return decorated_function