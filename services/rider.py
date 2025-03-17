import psycopg2.pool
import redis
import psycopg2
import numpy as np
from math import radians, sin, cos, sqrt, atan2
from time import time, sleep 
from threading import Thread
import logging
import osmnx as ox
# import pika
import asyncio
import aio_pika
import cProfile
import firebase_admin
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from circuitbreaker import circuit
from aiohttp import web, WSMsgType

from prometheus_client import start_http_server, Gauge
from shapely.geometry import Polygon, Point
from firebase_admin import credentials, db, firestore, messaging, auth
import pandas as pd

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Prometheus metrics
match_latency = Gauge('match_latency_seconds', 'Time to match a driver', ['region'])
sync_delay = Gauge('sync_delay_seconds', 'Time to sync an update to PostgreSQL', ['region'])

# Connect to Redis
redis_pool = redis.ConnectionPool(
  host='gusc1-concise-mudfish-31044.upstash.io',
  port=31044,
  password='41e4dd927edd42329833ab96eff9d193',
  ssl=True
)

# PostgreSQL connection
pg_pool = psycopg2.pool.SimpleConnectionPool(1,20,
    dbname="riderx",
    user="postgres",
    password="yourpassword",  # Replace with your password
    host="localhost",
    port="5432"
)

# Context manager for getting PostgreSQL connections from pool
@contextmanager
def get_pg_connection():
    conn = pg_pool.getconn()
    try:
        yield conn
    finally:
        pg_pool.putconn(conn)



# Background task decorator
def background_task(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        thread = Thread(target=f, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper

def circuit_breaker(f):
    @circuit(failure_threshold=5, recovery_timeout=30)
    @wraps(f)
    async def wrapper(*args, **kwargs):
        return await f(*args, **kwargs)
    return wrapper

# Optimized seed drivers function with all suggested improvements
def seed_drivers(region="nyc", stress_test=False, background=False):
    if background:
        return _seed_drivers_background(region, stress_test)
    else:
        return _seed_drivers_sync(region, stress_test)

@background_task
def _seed_drivers_background(region, stress_test):
    _seed_drivers_sync(region, stress_test)
    logger.info(f"Background seeding completed for {region}")


# Haversine formula for distance (in km)
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth’s radius
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c


# Region centroids for dynamic failover
REGION_CENTROIDS = {
    "nyc": (40.7128, -74.0060),  # Manhattan
    "la": (34.0522, -118.2437)   # Los Angeles
}

# Generate random drivers for load testing

def generate_random_drivers(region, count=10000):
    bounds = {
        "nyc": {"lat_min": 40.5, "lat_max": 40.9, "lon_min": -74.3, "lon_max": -73.7},
        "la": {"lat_min": 33.7, "lat_max": 34.3, "lon_min": -118.7, "lon_max": -118.0}
    }

    latitudes = np.random.uniform(bounds[region]["lat_min"], bounds[region]["lat_max"], count)
    longitudes = np.random.uniform(bounds[region]["lon_min"], bounds[region]["lon_max"], count)
    availability = np.random.choice([True, False], count)

    drivers = [
        {"id": f"driver_{region}_{i}", "lat": lat, "lon": lon, "available": avail}
        for i, (lat, lon, avail) in enumerate(zip(latitudes, longitudes, availability))
    ]

    return drivers
# Use larger batches for Redis pipelining
BATCH_SIZE = 1000

def _seed_drivers_sync(region="nyc", stress_test=False):
    count = 10000 if stress_test else 3
    drivers = generate_random_drivers(region, count) if stress_test else {
        "nyc": [
            {"id": "driver1", "lat": 40.7128, "lon": -74.0060, "available": True},
            {"id": "driver2", "lat": 40.7300, "lon": -73.9350, "available": True},
            {"id": "driver3", "lat": 40.6782, "lon": -73.9442, "available": False},
        ],
        "la": [
            {"id": "driver4", "lat": 34.0522, "lon": -118.2437, "available": True},
            {"id": "driver5", "lat": 34.0407, "lon": -118.2468, "available": True},
            {"id": "driver6", "lat": 33.9416, "lon": -118.4085, "available": False},
        ]
    }.get(region, [])
   
    driver_key = f"drivers:{region}"
    table_name = f"drivers_{region}"
    
    # Process large datasets in chunks
    chunk_size = 1000
    driver_chunks = [drivers[i:i + chunk_size] for i in range(0, len(drivers), chunk_size)]
    
    # Get Redis connection from pool
    r_conn = redis.Redis(connection_pool=redis_pool)
    
    # Use ThreadPoolExecutor to process Redis and PostgreSQL operations concurrently
    with ThreadPoolExecutor(max_workers=2) as executor:
        # Submit Redis task
        redis_future = executor.submit(seed_redis, r_conn, driver_chunks, driver_key)
        
        # Submit PostgreSQL task
        pg_future = executor.submit(seed_postgresql, driver_chunks, table_name)
        
        # Wait for both tasks to complete
        redis_result = redis_future.result()
        pg_result = pg_future.result()
    
    logger.info(f"Seeded {len(drivers)} drivers for {region}")
    return len(drivers)

def seed_redis(r_conn, driver_chunks, driver_key):
    """Process Redis operations in chunks"""
    for chunk in driver_chunks:
        pipe = r_conn.pipeline(transaction=False)
        for driver in chunk:
            pipe.geoadd(driver_key, (driver["lon"], driver["lat"], driver["id"]))
            pipe.set(f"driver:{driver['id']}:available", "1" if driver["available"] else "0")
        pipe.execute()
    return True

def seed_postgresql(driver_chunks, table_name):
    """Process PostgreSQL operations in chunks"""
    with get_pg_connection() as conn:
        with conn.cursor() as cursor:
            for chunk in driver_chunks:
                if not chunk:
                    continue
                    
                pg_values = []
                pg_args = []
                
                for driver in chunk:
                    pg_args.extend([driver["id"], driver["lon"], driver["lat"], driver["available"]])
                    pg_values.append("(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)")
                
                batch_query = f"""
                    INSERT INTO {table_name} (id, location, available) 
                    VALUES {', '.join(pg_values)}
                    ON CONFLICT (id) DO UPDATE SET 
                    location = EXCLUDED.location, 
                    available = EXCLUDED.available
                """
                cursor.execute(batch_query, pg_args)
            conn.commit()
    return True

# Asynchronous function to sync RabbitMQ updates to PostgreSQL
async def sync_updates_to_postgis():
    connection = None
    try:
        while True:
            try:
                # Create an asynchronous RabbitMQ connection
                connection = await aio_pika.connect_robust(
                    host='localhost',
                    port=5672,
                    login='guest',
                    password='guest',
                    virtualhost='/'
                )
                channel = await connection.channel()
                queue = await channel.declare_queue('driverX_updates', durable=True)

                async with queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        try:
                            start_time = time()
                            async with message.process():
                                body = message.body.decode().split(",")
                                driver_id, lat, lon, available, region = body
                                table_name = f"drivers_{region}"

                                pg_pool.execute(
                                    f"INSERT INTO {table_name} (id, location, available) VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s) "
                                    "ON CONFLICT (id) DO UPDATE SET location = EXCLUDED.location, available = EXCLUDED.available",
                                    (driver_id, float(lon), float(lat), available == "1")
                                )
                                pg_pool.commit()
                                delay = time() - start_time
                                sync_delay.labels(region).set(delay)
                                logger.info(f"Synced update for {driver_id} in {region} to PostGIS ({delay:.3f}s)")
                        except Exception as e:
                            logger.error(f"Error processing message: {e}")
            except aio_pika.exceptions.AMQPConnectionError as e:
                logger.error(f"RabbitMQ connection error: {e}")
                await asyncio.sleep(5)  # Wait before reconnecting
            except Exception as e:
                logger.error(f"Unexpected error in sync thread: {e}")
                await asyncio.sleep(5)
    finally:
        if connection:
            await connection.close()
        
# Simulate a driver update
@circuit_breaker
async def update_driver_status(driver_id, lat, lon, available, region="nyc", max_retries=3):
    for attempt in range(max_retries):
        try:
           # Create an asynchronous RabbitMQ connection
            connection = await aio_pika.connect_robust(
                host='localhost',
                port=5672,
                login='guest',
                password='guest',
                virtualhost='/'
            )
            channel = await connection.channel()
            queue = await channel.declare_queue('driverX_updates', durable=True)

            driver_key = f"drivers:{region}"
            r.geoadd(driver_key, (lon, lat, driver_id))
            r.set(f"driver:{driver_id}:available", "1" if available else "0")
            message = aio_pika.Message(
                    body=f"{driver_id},{lat},{lon},{1 if available else 0},{region}".encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                )
            await channel.default_exchange.publish(message, routing_key='driverX_updates')
            logger.info(f"Queued update for {driver_id} in {region}")
            return True
        except aio_pika.exceptions.AMQPError as e:
            logger.warning(f"RabbitMQ publish attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1)  # Wait before retry

# Cache for hotspot results (rider lat/lon rounded to 0.01 degree ~1 km)
hotspot_cache = {}  # {key: (driver_id, timestamp)}
CACHE_TTL = 300  # 5 minutes

def check_hotspot_cache(rider_lat, rider_lon):
    key = f"{round(rider_lat, 2)}:{round(rider_lon, 2)}"
    if key in hotspot_cache:
        driver_id, timestamp = hotspot_cache[key]
        if time() - timestamp < CACHE_TTL:
            return driver_id
        del hotspot_cache[key]
    return None

def cache_hotspot(rider_lat, rider_lon, driver_id):
    key = f"{round(rider_lat, 2)}:{round(rider_lon, 2)}"
    hotspot_cache[key] = (driver_id, time())
# Find nearest driver with region failover
def find_nearest_driver(rider_lat, rider_lon, region="nyc", max_radius_km=10):
    start_time = time()
    driver_key = f"drivers:{region}"
    
    cached_driver = check_hotspot_cache(rider_lat, rider_lon)
    if cached_driver and r.get(f"driver:{cached_driver}:available") == b"1":
        latency = time() - start_time
        match_latency.labels(region).set(latency)
        logger.info(f"Cache hit! Time: {time() - start_time:.3f}s")
        return cached_driver

    regions_to_try = [region]
    failover_region = "la" if region == "nyc" else "nyc"
    failover_distance = haversine(rider_lat, rider_lon, *REGION_CENTROIDS[failover_region])
    if failover_distance <= 50:  # Only failover if within 50 km
        regions_to_try.append(failover_region)
    for try_region in regions_to_try:
        driver_key = f"drivers:{try_region}"
        radii = [1, 5, max_radius_km]
        for radius in radii:
            try:
                nearby = r.geosearch(driver_key, longitude=rider_lon, latitude=rider_lat, 
                                    radius=radius * 1000, unit="m")
                if nearby:
                    closest_driver = None
                    min_distance = float('inf')
                    
                    for driver_id in nearby:
                        if r.get(f"driver:{driver_id.decode()}:available") == b"1":
                            pos = r.geopos(driver_key, driver_id)[0]
                            distance = haversine(rider_lat, rider_lon, pos[1], pos[0])
                            if distance < min_distance:
                                min_distance = distance
                                closest_driver = driver_id.decode()
                    
                    if closest_driver:
                        cache_hotspot(rider_lat, rider_lon, closest_driver)
                        logger.info(f"Match found in {try_region} at {radius} km. Time: {time() - start_time:.3f}s")
                        return closest_driver
            except redis.RedisError:
                logger.warning(f"Redis failed for {try_region}, switching to PostgreSQL fallback")
                result = find_nearest_driver_postgis(rider_lat, rider_lon, try_region, max_radius_km)
                if "No available drivers" not in result:
                    return result
        if try_region != region:
            logger.info(f"No drivers in {region}, failed over to {try_region}")
    
    return f"No available drivers within {max_radius_km} km in any region"

# PostgreSQL/PostGIS fallback
def find_nearest_driver_postgis(rider_lat, rider_lon,region, max_radius_km):
    start_time = time()
    table_name = f"drivers_{region}"
    query = """
        SELECT id
        FROM {table_name}
        WHERE available = TRUE
        AND ST_DWithin(
            location,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            %s
        )
        ORDER BY ST_Distance(
            location,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
        )
        LIMIT 1
    """
    pg_pool.execute(query, (rider_lon, rider_lat, max_radius_km * 1000, rider_lon, rider_lat))
    result = pg_pool.fetchone()
    
    driver_id = result[0] if result else None
    if driver_id:
        latency = time() - start_time
        match_latency.labels(region).set(latency)
        logger.info(f"PostGIS fallback match found. Time: {time() - start_time:.3f}s")
        return driver_id
    return f"No available drivers within {max_radius_km} km in PostGIS"


if __name__ == "__main__":
    import sys
    
    try:
        # Start sync thread
        asyncio.run(sync_updates_to_postgis())
        
        # Check if --seed flag is provided
        if len(sys.argv) > 1 and sys.argv[1] == '--seed':
            logger.info("Starting driver seeding...")
            try:
                profiler = cProfile.Profile()
                profiler.enable()
                start_seed_time = time()
                seed_drivers("nyc", stress_test=True, background=True)
                seed_drivers("la", stress_test=True, background=True)
                profiler.disable()
                profiler.print_stats(sort='time')
                logger.info("Driver seeding completed.")
                end_seed_time=time() - start_seed_time()
                logger.info(f"seed started at {start_seed_time} and ended at {end_seed_time}")
                sys.exit(0)
            except Exception as e:
                logger.error(f"Failed to seed drivers: {e}")
                raise
        
        # Run tests
        logger.info("Starting driver tests...")
        
        # Test NYC matching
        try:
            # Create Redis connection
            r = redis.Redis(connection_pool=redis_pool)
            
            # Test NYC
            rider_lat, rider_lon = 40.7200, -73.9900  # Near Manhattan
            result = find_nearest_driver(rider_lat, rider_lon, "nyc")
            logger.info(f"NYC matched driver: {result}")
            
            result = find_nearest_driver(rider_lat, rider_lon, "nyc")
            logger.info(f"NYC matched driver (cache): {result}")
            
            logger.info("Updating a driver in NYC...")
            update_driver_status("driver_nyc_500", 40.7350, -73.9400, False, "nyc")
            
            result = find_nearest_driver(rider_lat, rider_lon, "nyc")
            logger.info(f"NYC matched driver after update: {result}")
            
            # Test LA
            rider_lat, rider_lon = 34.0500, -118.2450  # Near LA
            result = find_nearest_driver(rider_lat, rider_lon, "la")
            logger.info(f"LA matched driver: {result}")
            
            # Test failover (clear NYC drivers to force failover to LA)
            r.delete("drivers:nyc")
            result = find_nearest_driver(rider_lat, rider_lon, "nyc")
            logger.info(f"Failover match from NYC to LA: {result}")
            
            
            # Test PostGIS fallback (stop Redis)
            r.connection_pool.disconnect()
            result = find_nearest_driver(rider_lat, rider_lon, "la")
            logger.info(f"LA matched driver (fallback): {result}")
                            
        except Exception as e:
            logger.error(f"Test failed: {e}")
            raise
            
    except Exception as e:
        logger.error(f"Error in main: {str(e)}", exc_info=True)
        raise
    finally:
        try:
            if pg_pool:
                pg_pool.closeall()
                logger.info("PostgreSQL connections closed")
        except Exception as e:
            logger.error(f"Error closing PostgreSQL connections: {e}")