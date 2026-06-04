# PayShield AI — MLOps Fraud Detection Platform

## Project Overview
Real-time fraud detection platform on AWS EKS.
Binary classification: 0=genuine, 1=fraud.
Primary model: XGBoost with threshold=0.35 (maximize recall).

## Development Environment
- OS: Windows 11/10 with WSL2 (Ubuntu 22.04)
- All development inside WSL2 — Linux paths and commands only
- Python 3.11.9 in .venv (NEVER use system python or sudo pip)
- Node.js 20 via nvm
- Docker Desktop with WSL2 backend

## Tech Stack
- Frontend: React + Vite + Tailwind CSS
- Backend: FastAPI (Python 3.11)
- Auth: JWT + Redis sessions
- Database: DynamoDB (local via dynamodb-local, AWS in prod)
- Streaming: Kafka (local via Docker, Amazon MSK in prod)
- Cache: Redis
- ML: XGBoost, scikit-learn, MLflow tracking
- Storage: MinIO locally, S3 in prod
- Monitoring: Prometheus + Grafana + Loki
- Drift: Evidently AI
- Container: Docker + docker-compose locally, EKS in prod
- IaC: Terraform
- GitOps: ArgoCD + Helm

## Code Standards
- Python: PEP 8, type hints, pydantic for validation
- All services expose GET /health and GET /metrics
- Structured JSON logging in every service
- FastAPI uses lifespan context manager (not deprecated @app.on_event)
- Use async/await for all I/O
- Environment variables via python-dotenv / os.environ
- Never hardcode secrets
- Shell scripts use bash (#!/bin/bash), not PowerShell

## File Naming
- Python files: snake_case
- React components: PascalCase
- Services: services/{service-name}/app/main.py

## Local Ports
- frontend:             5173
- backend-bff:          8001
- auth-service:         8002
- transaction-service:  8003
- fraud-model-api:      8004
- feature-service:      8005
- drift-monitor:        8006
- mlflow:               5050
- dynamodb-local:       8000
- redis:                6379
- kafka:                9092
- minio:                9000 (API), 9001 (console)
- prometheus:           9090
- grafana:              3001
- loki:                 3100

## Key Business Rules
- Fraud probability >= 0.70 → HIGH risk (auto-block)
- Fraud probability 0.35-0.70 → MEDIUM risk (review)
- Fraud probability < 0.35 → LOW risk (allow)
- Model must have recall >= 0.80 to be promoted to Production
- Drift check mandatory before retraining
- Transaction status on create: fraud_prediction="PENDING", risk_level="UNKNOWN", status="PROCESSING"
- Feature service Kafka consumer group: feature-service-group

## Dataset
- Kaggle IEEE-CIS (590,540 rows, 3.51% fraud)
- Features: 39 engineered (see ml/feature_engineering/build_features.py)
- Feature schema: s3://payshield-processed-data/features/feature_schema.json
- Model: XGBoost v2 in MLflow Staging (fraud-risk-model)

## Docker Platform Notes
- All services: use standard linux/amd64 images (x86_64 WSL2)
- No --platform flags needed (unlike Mac M3 which needs arm64/amd64 switching)
- Kafka/Zookeeper: confluentinc images work natively on x86_64

---

### Phase 2 — Transaction Service + Kafka ✅ COMPLETE
Completed: 2026-06-04

New services built:
- transaction-service (port 8003):
  POST /transactions — stores in DynamoDB + publishes to Kafka
  GET /transactions — user's history via GSI (user-id-index)
  GET /transactions/fraud — HIGH/MEDIUM risk list via GSI (risk-level-index, ADMIN/ANALYST only)
  GET /transactions/{id} — single transaction by PK
  PATCH /transactions/{id}/prediction — updates fraud result (called by feature-service)
- feature-service (port 8005):
  Kafka consumer: transactions.raw → build features → call fraud model → PATCH transaction
  Redis velocity keys: vel:1h:{user_id}, vel:24h:{user_id}, devices:{user_id}
  Exposes GET /health and GET /metrics only (no business routes)

Kafka topics created (all partitions=1, replication=1 for local dev):
- transactions.raw
- transactions.validated
- transactions.features
- transactions.scored
- transactions.failed
- model.drift.detected
- alerts.fraud.highrisk

Transaction flow:
User submits → BFF (auth check) → transaction-service (DynamoDB + Kafka)
→ feature-service (Kafka consumer) → builds features + Redis velocity
→ fraud-model-api (Phase 4 ✅)
→ PATCH transaction-service → DynamoDB updated

Frontend pages added:
- NewTransaction (/transactions/new): payment form with polling
- TransactionHistory (/transactions): risk-level badges, auto-refresh 10s
- Layout sidebar with navigation

Transaction status lifecycle:
PROCESSING (on create) → prediction scored → risk_level = HIGH/MEDIUM/LOW

Tests: not yet written for Phase 2 services.

---

### Phase 3 — ML Training Pipeline ✅ COMPLETE
Completed: 2026-06-04

Dataset:
- Kaggle IEEE-CIS Fraud Detection (590,540 transactions)
- Located at: ml/data/raw/ (gitignored)
- Fraud rate: ~3.51%
- Train/Val/Test split: 80/10/10 (time-based, NOT random — by TransactionDT)

Feature engineering:
- Script: ml/feature_engineering/build_features.py
- Input: raw CSVs → Output: parquet in MinIO (payshield-processed-data/features/)
- Feature count: 39 features
- PCA on V1-V339 (292 surviving cols → 5 components: V_pca_1 to V_pca_5, 98.2% variance)
- Scaler: StandardScaler saved as preprocessor.pkl (fit on train only)
- Reference data: 10,000 row sample for drift baseline

Trained model:
- Algorithm: XGBoost (tree_method="hist")
- scale_pos_weight: 27.46 (computed from class imbalance)
- Optimal threshold: 0.35 (default — no val candidate met recall≥0.80 & precision≥0.60)
- Registered name: fraud-risk-model
- Current version: 2 (Staging — promotion criteria not yet met)
- MLflow experiment: payshield-fraud-detection

Model metrics (v2, test set):
- Recall:          0.756
- Precision:       0.171
- F1 Score:        0.279
- ROC AUC:         0.887
- PR AUC:          0.474
- False Negatives: 539

Top features by SHAP importance:
1. amount_log
2. V_pca_1
3. is_late_night
4. amount_x_hour
5. C1 / C14

MLflow artifacts (per run):
- confusion_matrix.png, pr_curve.png, roc_curve.png
- shap_summary.png, feature_importance.png
- preprocessor.pkl, feature_schema.json

Promotion gate (not yet passed):
- recall ≥ 0.80, f1 ≥ 0.75, roc_auc ≥ 0.92
- Requires richer features (Phase 4: device fingerprinting, merchant history)

Tests: ml/tests/ — not yet written for Phase 3.

---

### Phase 4 — Fraud Model API ✅ COMPLETE
Completed: 2026-06-04

New service:
- fraud-model-api (port 8004):
  POST /predict → XGBoost inference, returns fraud_probability + risk_level
  GET /health → model loaded status + Redis status
  GET /model/info → version, stage, loaded_at, feature_count
  POST /model/reload → hot-swap model without restart (requires X-Reload-Key)
  GET /metrics → Prometheus metrics

Model loading:
- Loads from MLflow at startup: models:/fraud-risk-model/Production
- Downloads preprocessor.pkl and feature_schema.json from MLflow artifacts
- Model load time: ~15-25 seconds on startup
- Caches risk score in Redis: risk_cache:{transaction_id} TTL=300s

Prediction logic:
- Feature vector: 37 features in exact training order
- Real-time velocity from Redis (vel:1h, vel:24h, devices sets)
- Classification threshold: 0.35 (from MODEL_THRESHOLD env var)
- Risk classification: >= 0.70 → HIGH, >= 0.35 → MEDIUM, < 0.35 → LOW
- Inference latency: ~3-10ms (XGBoost hist)
- End-to-end latency (BFF → feature → model): ~100-150ms

Prometheus metrics tracked:
- fraud_predictions_total{risk_level}
- model_prediction_latency_ms (histogram)
- high_risk_transactions_total
- model_loaded_status (gauge)
- fraud_amount_saved_total (INR)
- prediction_confidence_histogram

Response headers: X-Model-Version, X-Prediction-Latency

FULL PIPELINE NOW WORKS:
Transaction submitted → Kafka → Feature service → Fraud model API → DynamoDB updated
Frontend shows: HIGH/MEDIUM/LOW risk with probability

Tests: services/fraud-model-api/tests/test_model_api.py — ALL PASS
