from prometheus_client import Counter, Histogram, Gauge, Info

PREDICTIONS_TOTAL = Counter(
    "fraud_predictions_total",
    "Total fraud predictions made",
    ["risk_level"]
)
PREDICTION_LATENCY = Histogram(
    "model_prediction_latency_ms",
    "Model prediction latency in milliseconds",
    buckets=[5, 10, 25, 50, 100, 150, 200, 500, 1000]
)
HIGH_RISK_TOTAL = Counter("high_risk_transactions_total", "HIGH risk transactions")
MODEL_LOADED = Gauge("model_loaded_status", "1 if model loaded, 0 otherwise")
MODEL_INFO = Info("fraud_model", "Loaded model information")
REQUEST_COUNT = Counter("http_requests_total", "HTTP requests", ["method", "endpoint", "status_code"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "HTTP request duration", ["endpoint"])
REDIS_ERRORS = Counter("redis_connection_errors_total", "Redis errors")
FRAUD_AMOUNT_SAVED = Counter("fraud_amount_saved_total", "Total amount saved by catching fraud (INR)")
MODEL_CONFIDENCE = Histogram(
    "prediction_confidence_histogram",
    "Distribution of fraud probability scores",
    buckets=[0.05, 0.10, 0.20, 0.35, 0.50, 0.70, 0.85, 0.95, 1.0]
)
