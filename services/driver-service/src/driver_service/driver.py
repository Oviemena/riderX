from flask import Flask, request, jsonify
from flask_jwt_extended import JWTManager, jwt_required

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rediscluster.exceptions import RedisClusterException
from decouple import config


from connections.connections import get_firebase_db, get_redis_connection
from haversine.haversine import haversine
from helper_func.helpers import monitor_request, seed_drivers
from helper_func.metrics import driver_availability


app = Flask("driver_service")

# JWT setup
JWT_SECRET_KEY=config("JWT_SECRET_KEY")
app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
jwt=JWTManager(app)

# Use shared connections
r = get_redis_connection()
firebase_db = get_firebase_db()

@app.route('/')
def home():
    return "Driver Service is running!"

@app.route('/seed-drivers', methods=['POST'])
@monitor_request
# @jwt_required()  uncomment only when the frontend is connected
def seed_drivers_route():
    """Handle driver seeding requests.

    Returns:
        jsonify: JSON response confirming seeding started.
    """
    data = request.json
    region = data.get('region', 'nyc')
    count = data.get('count', 3)
    seed_drivers(region, count)
    return jsonify({"message": f"Seeding drivers for {region} started"}), 202

@app.route('/update-driver', methods=['POST'])
@monitor_request
# @jwt_required() uncomment only when the frontend is connected
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(RedisClusterException))
def update_driver_route():
    """Handle driver location update requests with retries.

    Returns:
        jsonify: JSON response confirming driver updated.
    """
    data = request.json
    driver_id = data['driver_id']
    lat = data['lat']
    lon = data['lon']
    available = data['available']
    region = data.get('region', 'nyc')
    driver_key = f"drivers:{region}"
    r.geoadd(driver_key, (lon, lat, driver_id))
    r.set(f"driver:{driver_id}:available", "1" if available else "0")
    firebase_db.child(f"drivers/{driver_id}").update({"lat": lat, "lon": lon, "available": available, "region": region})
    driver_availability.labels(region).set(sum(1 for d in r.keys(f"driver:*:available") if r.get(d) == "1"))
    return jsonify({"message": f"Driver {driver_id} updated"}), 200



