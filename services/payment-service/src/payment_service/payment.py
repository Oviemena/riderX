import logging
from flask import Flask, request, jsonify
import requests
from prometheus_client import start_http_server, Counter, Histogram
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from connections.connections import get_firebase_db
from haversine import haversine  # Assuming shared-utils is installed
from decouple import config
from time import time
from functools import wraps

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# JWT setup
JWT_SECRET_KEY=config("JWT_SECRET_KEY")
app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
jwt=JWTManager(app)

# Rate limiting setup
limiter = Limiter(get_remote_address, app=app, default_limits=["100 per minute"])

# Firebase initialization
firebase_db = get_firebase_db()

# Paystack configuration
PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY")  # Replace with your Paystack secret key
PAYSTACK_API_URL = 'https://api.paystack.co/transaction'

# Prometheus metrics
payment_success = Counter('payment_success_total', 'Successful payments', ['region'])
payment_failure = Counter('payment_failure_total', 'Failed payments', ['region'])
request_duration = Histogram('request_duration_seconds', 'Request processing time', ['endpoint'])

start_http_server(8003)
logger.info("Payment Service metrics on port 8003")

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

@app.route('/create-payment-intent', methods=['POST'])
@monitor_request
@jwt_required()
@limiter.limit("100 per minute")
def create_payment_intent():
    """Create a Paystack transaction intent for payment.

    Returns:
        jsonify: JSON response with payment authorization URL and transaction details.
    """
    data = request.json
    rider_lat = data['rider_lat']
    rider_lon = data['rider_lon']
    driver_lat = data['driver_lat']
    driver_lon = data['driver_lon']
    region = data.get('region', 'nyc')
    ride_id = data.get('ride_id')

    # Calculate distance and fare
    distance_km = haversine(rider_lat, rider_lon, driver_lat, driver_lon)

    # Fetch surge from ride-service
    surge_response = requests.get(f"http://ride-service:5002/calculate-surge?rider_lat={rider_lat}&rider_lon={rider_lon}&region={region}")
    surge = surge_response.json()['surge'] if surge_response.status_code == 200 else 1.0

    fare = (5.0 + distance_km * 2.0) * surge
    amount = int(fare * 100)  # Paystack expects amount in kobo (100 kobo = 1 NGN)

    try:
        # Initialize Paystack transaction
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
            'Content-Type': 'application/json'
        }
        payload = {
            'email': get_jwt_identity(),  # Use JWT identity as email
            'amount': amount,
            'callback_url': config('PAYSTACK_CALLBACK_URL', 'http://localhost/payment/callback'),  # Replace with actual callback URL
            'metadata': {'ride_id': ride_id, 'surge': surge, 'distance_km': distance_km}
        }
        response = requests.post(f'{PAYSTACK_API_URL}/initialize', json=payload, headers=headers)
        response_data = response.json()

        if response.status_code == 200 and response_data['status']:
            payment_success.labels(region).inc()
            return jsonify({
                'authorization_url': response_data['data']['authorization_url'],
                'access_code': response_data['data']['access_code'],
                'reference': response_data['data']['reference'],
                'fare': fare,
                'surge': surge
            })
        else:
            raise Exception(response_data.get('message', 'Unknown error'))

    except Exception as e:
        payment_failure.labels(region).inc()
        logger.error(f"Payment intent creation failed: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5003, debug=True)