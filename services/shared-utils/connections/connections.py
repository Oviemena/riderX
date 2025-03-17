import firebase_admin
from firebase_admin import credentials, db
from rediscluster import RedisCluster
from pathlib import Path
import logging
from decouple import config

from tenacity import sleep

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Redis Cluster connection (singleton-like)
_redis_conn = None
_firebase_conn = None

def get_redis_connection(max_retries=5, retry_delay=2):
    """Get or initialize a Redis Cluster connection with retries.
    
    Args:
        max_retries (int): Maximum number of connection attempts
        retry_delay (int): Seconds to wait between retries
    
    Returns:
        RedisCluster: A Redis Cluster client instance
    """
    global _redis_conn
    
    if _redis_conn is not None:
        return _redis_conn

    redis_host = config("REDIS_HOST")
    redis_port = int(config("REDIS_PORT"))
    startup_nodes = [
        {"host": redis_host, "port": redis_port},
    ]

    for attempt in range(max_retries):
        try:
            _redis_conn = RedisCluster(
                startup_nodes=startup_nodes,
                decode_responses=True,
                skip_full_coverage_check=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True
            )
            # Verify connection
            _redis_conn.ping()
            logger.info(f"Redis Cluster connection established on attempt {attempt + 1}")
            return _redis_conn
        except Exception as e:
            logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                sleep(retry_delay)
    
    raise Exception("Failed to connect to Redis Cluster after maximum retries")


def get_firebase_db():
    """Get or initialize Firebase database connection."""
    global _firebase_conn
    if _firebase_conn is None:
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate('/app/shared-utils/credentials/riderx-4f28f-firebase-adminsdk-fbsvc-dfb7d4d54e.json')
                firebase_admin.initialize_app(cred, {
                    'databaseURL': config('FIREBASE_DATABASE_URL')
                })
            _firebase_conn = db.reference()
            logger.info("Firebase connection established")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            raise
    return _firebase_conn