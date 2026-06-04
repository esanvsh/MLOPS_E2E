# PayShield AI
## Production MLOps Platform | WSL2 + Docker Desktop | Phase-by-Phase


## 🖥️ Your Stack (Windows + WSL2)

```
Windows 11/10 (host OS)
├── WSL2 (Ubuntu 22.04)          ← ALL development happens here
│   ├── pyenv                    → Python version manager
│   ├── Python 3.11              → inside project .venv ONLY
│   ├── nvm                      → Node version manager
│   ├── Node.js 20               → project-local
│   ├── git                      → version control
│   └── aws / kubectl / helm / terraform
│
├── Docker Desktop for Windows   ← uses WSL2 backend
│   └── WSL2 integration enabled → containers accessible from WSL2
│
└── VS Code (Windows app)
    └── Remote WSL extension     → edits files inside WSL2 transparently
        └── Claude Code extension → AI pair programmer
```

## ⚡ Quick Start (After Setup)

```bash
# From WSL2 terminal:
cd ~/projects/payshield-ai
make up           # start all Docker services
make health       # check all services healthy
make ml-train     # train fraud model
make down         # stop everything
```


## 🌐 Local URLs (All Open in Windows Browser)

| Service | URL | Login |
|---------|-----|-------|
| React Frontend | http://localhost:5173 | register new |
| Backend BFF Swagger | http://localhost:8001/docs | — |
| Auth Service Swagger | http://localhost:8002/docs | — |
| Transaction Swagger | http://localhost:8003/docs | — |
| Fraud Model API Swagger | http://localhost:8004/docs | — |
| MLflow UI | http://localhost:5050 | — |
| MinIO Console | http://localhost:9001 | minioadmin / minioadmin123 |
| DynamoDB Admin | http://localhost:8001 | — |
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3001 | admin / admin123 |
| Alertmanager | http://localhost:9093 | — |

---

### PHASE 0: WSL2 + Ubuntu + Docker Desktop + VS Code

### Phase 1 — Auth Service + React Frontend
##### JWT + Redis + DynamoDB Local | Runs on Your Windows Laptop

### Phase 2 — Transaction Service + Kafka
###### DynamoDB + Event Streaming | Local Test

### Phase 3 — ML Training Pipeline
###### Kaggle Data → XGBoost → MLflow | Runs on Your Windows Laptop

### Phase 4 — Fraud Model API
###### FastAPI + XGBoost Model Serving | Local Test

### Phase 5 + 6 — Full Stack Integration + Monitoring
###### All 13 Services + Prometheus + Grafana | docker-compose

### Phase 7 — Drift Detection + Retraining
###### Evidently AI | Automated Retraining Trigger

### Phase 8+9 — AWS Infrastructure + EKS Deployment
###### Terraform + Helm + ArgoCD