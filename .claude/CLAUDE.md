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

## Dataset
- Kaggle IEEE-CIS Fraud Detection (590K rows)
- Location: ml/data/ (gitignored)
- isFraud: 0=genuine, 1=fraud (~3.5% fraud rate)

## Docker Platform Notes
- All services: use standard linux/amd64 images (x86_64 WSL2)
- No --platform flags needed (unlike Mac M3 which needs arm64/amd64 switching)
- Kafka/Zookeeper: confluentinc images work natively on x86_64
