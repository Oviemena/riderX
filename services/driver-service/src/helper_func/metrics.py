from prometheus_client import Gauge, Histogram

driver_availability = Gauge('driver_availability', 'Number of available drivers', ['region'])
request_duration = Histogram('request_duration_seconds', 'Request processing time', ['endpoint'])