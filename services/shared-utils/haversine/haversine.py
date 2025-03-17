from math import radians, sin, cos, sqrt, atan2

def haversine(lat1, lon1, lat2, lon2):
    """Calculate the great-circle distance between two points on Earth using Haversine formula.

    Args:
        lat1 (float): Latitude of first point in degrees.
        lon1 (float): Longitude of first point in degrees.
        lat2 (float): Latitude of second point in degrees.
        lon2 (float): Longitude of second point in degrees.

    Returns:
        float: Distance in kilometers.
    """
    R = 6371  # Earth's radius in kilometers
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c