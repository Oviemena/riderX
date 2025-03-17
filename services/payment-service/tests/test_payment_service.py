import pytest
from payment import app, haversine
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

@patch('payment.requests.get')
@patch('payment.requests.post')
@patch('payment.firebase_db')
def test_create_payment_intent(mock_firebase, mock_post, mock_get, client):
    """Test the create-payment-intent endpoint."""
    # Mock surge response from ride-service
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {'surge': 1.5}
    
    # Mock Paystack response
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        'status': True,
        'data': {
            'authorization_url': 'https://paystack.com/pay/test',
            'access_code': 'test_access_code',
            'reference': 'test_ref'
        }
    }
    
    mock_firebase.child.return_value.update.return_value = None
    
    response = client.post('/create-payment-intent',
                          json={
                              'rider_lat': 40.7128,
                              'rider_lon': -74.0060,
                              'driver_lat': 40.7300,
                              'driver_lon': -73.9350,
                              'ride_id': 'ride123',
                              'region': 'nyc'
                          },
                          headers={'Authorization': 'Bearer fake-token'})
    assert response.status_code == 200
    assert 'authorization_url' in response.json
    assert response.json['authorization_url'] == 'https://paystack.com/pay/test'
    assert 'fare' in response.json