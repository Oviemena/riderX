import json
from pathlib import Path
import sys
import numpy as np
import logging
from time import time

shared_utils_path = Path("/app/shared-utils")
sys.path.append(str(shared_utils_path))

from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from connections.connections import get_firebase_db, get_redis_connection
from haversine.haversine import haversine
from helper_func.metrics import driver_availability, request_duration

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rediscluster.exceptions import ClusterDownError

from prometheus_client import start_http_server

# Use shared connections
r = get_redis_connection()
firebase_db = get_firebase_db()


# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# # Prometheus metrics
# start_http_server(8001)
# logger.info("Driver Service metrics on port 8001")


def generate_random_drivers(region, count=10000):
    """Generate random driver data for seeding.

    Args:
        region (str): Region identifier (e.g., 'nyc').
        count (int): Number of drivers to generate.

    Returns:
        list: List of driver dictionaries.
    """
    bounds = {"nyc": {"lat_min": 40.5, "lat_max": 40.9, "lon_min": -74.3, "lon_max": -73.7}}
    latitudes = np.random.uniform(bounds[region]["lat_min"], bounds[region]["lat_max"], count)
    longitudes = np.random.uniform(bounds[region]["lon_min"], bounds[region]["lon_max"], count)
    availability = np.random.choice([True, False], count)
    ratings = np.random.uniform(3.0, 5.0, count)
    drivers = [
        {"id": f"driver_{region}_{i}", "lat": float(lat), "lon": float(lon), "available": bool(avail), "region": region, "rating": float(rating)}
        for i, (lat, lon, avail, rating) in enumerate(zip(latitudes, longitudes, availability, ratings))
    ]
    logger.info(f"Generated drivers: {drivers[:1]}")  # Log first driver for debugging
    return drivers
    
r = None
firebase_db = None

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ClusterDownError))
def seed_redis_and_firebase(r_conn, driver_chunks, driver_key):
    """Seed driver data into Redis and Firebase.

    Args:
        r_conn (RedisCluster): Redis connection object.
        driver_chunks (list): List of driver data chunks.
        driver_key (str): Redis key for geospatial data.
    """
    global r, firebase_db
    if r_conn is None:
        logger.error("Redis connection is None, initializing new connection")
        r_conn = get_redis_connection()
    if r is None:
        r = r_conn  # Sync global r with passed connection
        logger.info("Redis connection initialized")
    if firebase_db is None:
        firebase_db = get_firebase_db()
        logger.info("Firebase connection initialized in seed function")
    for chunk in driver_chunks:
        pipe = r_conn.pipeline(transaction=False)
        # Batch Firebase updates
        firebase_batch = {}
        for driver in chunk:
            logger.info(f"Processing driver {driver['id']}")
            lon, lat, name = driver["lon"], driver["lat"], driver["id"]
            if not all(isinstance(x, (int, float, str)) for x in [lon, lat, name]):
                logger.error(f"Invalid data for driver {name}: lon={lon}, lat={lat}")
                raise ValueError(f"Invalid GEOADD data for {name}")
            pipe.geoadd(driver_key, lon, lat, name)
            pipe.set(f"driver:{driver['id']}:available", "1" if driver["available"] else "0")
            pipe.set(f"driver:{driver['id']}:rating", driver["rating"])
            # Add to batch
            firebase_batch[f"drivers/{driver['id']}"] = {
                "lat": driver["lat"], "lon": driver["lon"], "available": driver["available"],
                "region": driver["region"], "rating": driver["rating"]
            }
        logger.info("Executing Redis pipeline")
        pipe.execute()
        try:
            start_time = time()
            firebase_db.update(firebase_batch)
            duration = time() - start_time
            logger.info(f"Batch updated {len(firebase_batch)} drivers to Firebase in {duration:.2f}s")
        except Exception as e:
            logger.error(f"Failed to seed driver {driver['id']} to Firebase: {str(e)}")
            raise
    available_count = sum(1 for d in r.keys(f"driver:*:available") if r.get(d) == "1")
    driver_availability.labels("nyc").set(available_count)

def seed_drivers(r_conn,region="nyc", count=20):
    """Seed drivers into Redis and Firebase.

    Args:
        region (str): Region identifier (default: 'nyc').
        count (int): Number of drivers to seed (default: 20).
    """
    drivers = generate_random_drivers(region, count)
    driver_key = f"drivers:{region}"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(seed_redis_and_firebase, r_conn, [drivers], driver_key)
        try:
            future.result()  # Wait for result and raise exceptions
        except Exception as e:
            logger.error(f"Seed drivers failed: {str(e)}")
            raise
    logger.info(f"Seeded {len(drivers)} drivers for {region} region")
    
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


def simulate_rides(firebase_db, drivers, count=20):
    """Simulate completed rides for training data."""
    ride_data = {}
    for i in range(count):
        driver_id = np.random.choice(list(drivers.keys()))
        driver = drivers[driver_id]
        rider_lat = np.random.uniform(40.5, 40.9)
        rider_lon = np.random.uniform(-74.3, -73.7)
        request_time = time() - np.random.uniform(600, 3600)  # 10-60 min ago
        accept_time = request_time + np.random.uniform(30, 300)  # 30s-5min response
        ride_data[f"ride_{i}"] = {
            "rider_lat": rider_lat,
            "rider_lon": rider_lon,
            "driver_lat": driver["lat"],
            "driver_lon": driver["lon"],
            "driver_id": driver_id,
            "status": "completed",
            "request_time": request_time,
            "accept_time": accept_time,
            "rating": np.random.uniform(3.5, 5.0),
            "shared": np.random.choice([True, False]),
            "rider_count": np.random.randint(1, 4) if "shared" else 1,
            "timestamp": time()
        }
    # Convert to JSON-compatible format
    serialized_data = json.loads(json.dumps(ride_data))
    firebase_db.child("ride_requests").update(serialized_data)
    logger.info(f"Simulated {count} completed rides")