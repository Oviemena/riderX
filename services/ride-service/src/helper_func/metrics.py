from prometheus_client import  Gauge, Counter, Histogram



match_latency = Histogram('match_latency_seconds', 'Time to match a driver', ['region'])
surge_multiplier_gauge = Gauge('surge_multiplier', 'Current surge multiplier', ['region'])
request_duration = Histogram('request_duration_seconds', 'Request processing time', ['endpoint'])
ml_prediction_latency = Histogram('ml_prediction_latency_seconds', 'Time for ML prediction', ['region'])
match_quality = Gauge('match_quality', 'Predicted match quality score', ['region'])
pooling_efficiency = Gauge('pooling_efficiency', 'Average riders per pooled ride', ['region'])
shared_ride_count = Counter('shared_ride_count', 'Number of shared rides', ['region'])
