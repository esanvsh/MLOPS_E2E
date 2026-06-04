from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class TransactionFeatureInput(BaseModel):
    transaction_id: str = ""
    amount: float = Field(..., gt=0)
    merchant_category: str = "other"
    country: str = "IN"
    hour: int = Field(12, ge=0, le=23)
    day_of_week: int = Field(0, ge=0, le=6)
    payment_method: str = "upi"
    failed_attempts: int = 0
    is_new_device: int = 0
    user_txn_count_24h: int = 1
    addr_mismatch: int = 0
    email_domain_risk: int = 0
    is_high_risk_category: int = 0
    device_id: str = ""
    user_id: str = ""
    model_config = ConfigDict(extra="allow")


class FraudPredictionResponse(BaseModel):
    transaction_id: str
    prediction: int
    fraud_probability: float
    risk_level: Literal["HIGH", "MEDIUM", "LOW"]
    model_version: str
    threshold_used: float
    latency_ms: float


class ModelInfoResponse(BaseModel):
    model_name: str
    model_version: str
    stage: str
    loaded_at: str
    feature_count: int


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_version: str
    redis_connected: bool
    timestamp: str
