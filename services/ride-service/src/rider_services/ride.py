from pathlib import Path
import sys
from time import time
import logging
from flask import Flask, request, jsonify
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


shared_utils_path = Path("/app/shared-utils")
sys.path.append(str(shared_utils_path))


from connections.connections import get_firebase_db, get_redis_connection
from haversine import haversine
from helper_func.helpers import monitor_request, cluster_ride_requests, find_nearest_driver,  calculate_surge_multiplier

import os
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from decouple import config


# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# JWT setup
JWT_SECRET_KEY=config("JWT_SECRET_KEY")
app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
jwt=JWTManager(app)


# Use shared connections
r = get_redis_connection()
firebase_db = get_firebase_db()


# Rate limiting setup
limiter = Limiter(get_remote_address, app=app, default_limits=["100 per minute"], storage_uri=f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', 6379)}")




@app.route('/')
def home():
    return "Ride Service is running!"

@app.route('/find-driver', methods=['POST'])
@monitor_request
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(Exception))
@jwt_required()
@limiter.limit("100 per minute")  # Rate limit to 100 requests per minute per IP
def find_driver_route():
    """Handle driver matching requests, supporting both solo and shared rides with retries and JWT auth.

    Returns:
        jsonify: JSON response with driver match details or error message.
    """
    user_id = get_jwt_identity()
    data = request.json
    rider_lat = data['rider_lat']
    rider_lon = data['rider_lon']
    shared = data.get('shared', False)
    region = data.get('region', 'nyc')
    ride_id = data.get('ride_id')  # For solo rides
    
    logger.info(f"User {user_id} requested a driver match for region {region}, shared: {shared}")

    if shared:
        clusters = cluster_ride_requests(region)
        for cluster in clusters:
            if any(r['lat'] == rider_lat and r['lon'] == rider_lon for r in cluster):
                # Validate that user_id matches at least one rider_id in the cluster
                if not any(r.get('rider_id') == user_id for r in cluster):
                    logger.warning(f"User {user_id} not authorized for this shared ride cluster")
                    return jsonify({"error": "Unauthorized: User not part of this ride cluster"}), 403
                
                rider_lats = [r['lat'] for r in cluster]
                rider_lons = [r['lon'] for r in cluster]
                ride_ids = [r['id'] for r in cluster]
                result = find_nearest_driver(rider_lats, rider_lons, region, shared=True)
                if isinstance(result, dict):
                    for ride_id in ride_ids:
                        firebase_db.child(f"ride_requests/{ride_id}").update({"driver_id": result["driver_id"], "status": "accepted"})
                    logger.info(f"User {user_id} matched with driver {result['driver_id']} for shared ride {ride_ids}")
                    return jsonify(result)
    else:
        # Validate user_id matches the rider_id for the solo ride
        ride_data = firebase_db.child(f"ride_requests/{ride_id}").get()
        if not ride_data or ride_data.get('rider_id') != user_id:
            logger.warning(f"User {user_id} not authorized for ride {ride_id}")
            return jsonify({"error": "Unauthorized: User does not own this ride"}), 403
        
        result = find_nearest_driver([rider_lat], [rider_lon], region)
        if isinstance(result, dict):
            firebase_db.child(f"ride_requests/{ride_id}").update({"driver_id": result["driver_id"], "status": "accepted"})
            logger.info(f"User {user_id} matched with driver {result['driver_id']} for solo ride {ride_id}")
            return jsonify(result)
    
    return jsonify(result)

@app.route('/record-ride-completion', methods=['POST'])
@monitor_request
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(Exception))
@jwt_required()
@limiter.limit("100 per minute")
async def record_ride_completion_route():
    """Record completion of one or more rides, calculating fares and earnings with retries and JWT auth.

    Returns:
        jsonify: JSON response confirming ride completion.
    """
    user_id = get_jwt_identity()
    data = request.json
    ride_ids = data['ride_ids']
    driver_id = data['driver_id']
    rider_data = data['rider_data']
    driver_lat = data['driver_lat']
    driver_lon = data['driver_lon']
    region = data.get('region', 'nyc')
    
    # Validate that user_id matches the driver_id
    if user_id != driver_id:
        logger.warning(f"User {user_id} not authorized to complete ride for driver {driver_id}")
        return jsonify({"error": "Unauthorized: User does not match driver"}), 403

    total_distance = 0
    for rider in rider_data:
        total_distance += haversine(rider['lat'], rider['lon'], driver_lat, driver_lon)
    surge = calculate_surge_multiplier(rider_data[0]['lat'], rider_data[0]['lon'], region)
    fare = (5.0 + total_distance * 2.0) * surge / len(rider_data)
    driver_earnings = fare * len(rider_data) * 0.75

    r.incr(f"driver:{driver_id}:ride_count", len(rider_data))
    firebase_db.child(f"drivers/{driver_id}/earnings").push({
        "ride_ids": ride_ids,
        "amount": driver_earnings,
        "timestamp": time(),
        "surge": surge,
        "distance_km": total_distance,
    })
    for ride_id, rider in zip(ride_ids, rider_data):
        firebase_db.child(f"ride_requests/{ride_id}").update({
            "fare": fare,
            "surge": surge,
            "distance_km": total_distance / len(rider_data),
            "status": "completed",
        })
    logger.info(f"Driver {user_id} completed ride {ride_ids}: Total Fare ${fare * len(rider_data):.2f}, Earnings ${driver_earnings:.2f}")
    return jsonify({"message": "Ride completed"}), 200

