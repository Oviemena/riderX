import logging
from pathlib import Path
import sys
from time import sleep, time

from helper_func.helpers import seed_drivers, simulate_rides

# Add shared utils to Python path
# shared_utils_path = Path(__file__).parent.parent.parent.parent / 'services' / 'shared-utils'
shared_utils_path = Path("/app/shared-utils")
sys.path.append(str(shared_utils_path))

from connections.connections import get_firebase_db, get_redis_connection
from driver_service.driver import app

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)




def initialize_service():
    """Initialize service with Redis connection."""
    max_retries = 5
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            redis_conn = get_redis_connection()
            # Test connection with simple operation
            redis_conn.set("test_key", "test_value")
            redis_conn.get("test_key")
            logger.info("Service initialization successful")
            return True
        except Exception as e:
            logger.error(f"Service initialization attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                sleep(retry_delay)
    
    return False

if __name__ == "__main__":
    if not initialize_service():
        logger.error("Service initialization failed")
        exit(1)
    logger.info("Starting driver service")
    r = get_redis_connection()  # Fresh connection
    firebase_db = get_firebase_db()
    seed_drivers(r, "nyc", 20)  # Initial seeding for testing
    drivers = firebase_db.child("drivers").get()
    simulate_rides(firebase_db, drivers)
    firebase_db.child("drivers_seeded").set({"status": "complete", "count": 20, "timestamp": time()})
    logger.info("Driver seeding and ride simulation signaled complete")
    app.run(host='0.0.0.0', port=5001,debug=False)