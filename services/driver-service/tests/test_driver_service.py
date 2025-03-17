import pytest
from driver import app, haversine, r
from unittest.mock import patch, MagicMock

@pytest.fixture
def client():
    """Fixture to create a test client for the Flask app."""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_haversine():
    """Test the haversine distance calculation."""
    distance = haversine(40.7128, -74.0060, 40.7300, -73.9350)
    assert isinstance(distance, float)
    assert 6.0 < distance < 7.0  # Approx distance between NYC points

@patch('driver.r')
@patch('driver.firebase_db')
def test_seed_drivers_route(mock_firebase, mock_redis, client):
    """Test the seed-drivers endpoint."""
    mock_redis.geoadd.return_value = None
    mock_redis.set.return_value = None
    mock_redis.pipeline.return_value = MagicMock(execute=lambda: None)
    mock_firebase.child.return_value.set.return_value = None
    
    response = client.post('/seed-drivers', 
                         json={'region': 'nyc', 'count': 3},
                         headers={'Authorization': 'Bearer fake-token'})
    assert response.status_code == 202
    assert response.json == {"message": "Seeding drivers for nyc started"}

@patch('driver.r')
@patch('driver.firebase_db')
def test_update_driver_route(mock_firebase, mock_redis, client):
    """Test the update-driver endpoint."""
    mock_redis.geoadd.return_value = None
    mock_redis.set.return_value = None
    mock_redis.keys.return_value = [b"driver:driver1:available"]
    mock_redis.get.return_value = "1"
    mock_firebase.child.return_value.update.return_value = None
    
    response = client.post('/update-driver', 
                         json={
                             'driver_id': 'driver1',
                             'lat': 40.7128,
                             'lon': -74.0060,
                             'available': True,
                             'region': 'nyc'
                         },
                         headers={'Authorization': 'Bearer fake-token'})
    assert response.status_code == 200
    assert response.json == {"message": "Driver driver1 updated"}