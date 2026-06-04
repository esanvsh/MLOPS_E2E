#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# docker compose must be run from the project root
cd "$PROJECT_DIR"

# ── Helpers ───────────────────────────────────────────────────────────────────

wait_for_http() {
    local name="$1" url="$2" max="${3:-60}"
    local elapsed=0
    printf "Waiting for %s" "$name"
    while ! curl -sf -o /dev/null "$url" 2>/dev/null; do
        if [[ $elapsed -ge $max ]]; then
            printf " ❌ timed out after %ds\n" "$max" >&2
            exit 1
        fi
        printf "."
        sleep 2
        elapsed=$((elapsed + 2))
    done
    printf " ✅ ready (%ds)\n" "$elapsed"
}

wait_for_kafka() {
    local max="${1:-90}"
    local elapsed=0
    printf "Waiting for Kafka"
    while ! docker compose exec -T kafka \
            kafka-broker-api-versions --bootstrap-server localhost:9092 &>/dev/null; do
        if [[ $elapsed -ge $max ]]; then
            printf " ❌ timed out after %ds\n" "$max" >&2
            exit 1
        fi
        printf "."
        sleep 3
        elapsed=$((elapsed + 3))
    done
    printf " ✅ ready (%ds)\n" "$elapsed"
}

http_up() {
    curl -sf -o /dev/null "$1" 2>/dev/null && echo "✅  UP" || echo "❌  DOWN"
}

tcp_up() {
    (echo > /dev/tcp/localhost/"$1") 2>/dev/null && echo "✅  UP" || echo "❌  DOWN"
}

status_row() {
    printf "  %-22s %-34s %s\n" "$1" "$2" "$3"
}

# ── Step 1: DynamoDB ──────────────────────────────────────────────────────────

echo ""
echo "── Step 1: DynamoDB ─────────────────────────────────────────────────────────"
wait_for_http "DynamoDB" "http://localhost:8000" 90
bash "$SCRIPT_DIR/setup-local-dynamodb.sh"

# ── Step 2: MinIO ─────────────────────────────────────────────────────────────

echo ""
echo "── Step 2: MinIO ────────────────────────────────────────────────────────────"
wait_for_http "MinIO" "http://localhost:9000/minio/health/live" 60
bash "$SCRIPT_DIR/setup-local-minio.sh"

# ── Step 3: Kafka ─────────────────────────────────────────────────────────────

echo ""
echo "── Step 3: Kafka ────────────────────────────────────────────────────────────"
wait_for_kafka 90
bash "$SCRIPT_DIR/setup-kafka-topics.sh"

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ── Local Stack Setup Complete ──"
echo "════════════════════════════════════════════════════════════════"
echo ""

status_row "Service" "URL" "Status"
printf "  %s\n" "──────────────────────────────────────────────────────────────"

# Infrastructure
status_row "DynamoDB Local"     "http://localhost:8000"           "$(http_up http://localhost:8000)"
status_row "MinIO API"          "http://localhost:9000"           "$(http_up http://localhost:9000/minio/health/live)"
status_row "MinIO Console"      "http://localhost:9001"           "$(tcp_up 9001)"
status_row "Redis"              "localhost:6379"                  "$(tcp_up 6379)"
status_row "Kafka"              "localhost:9092"                  "$(tcp_up 9092)"
status_row "MLflow"             "http://localhost:5050"           "$(http_up http://localhost:5050/health)"
status_row "Prometheus"         "http://localhost:9090"           "$(http_up http://localhost:9090/-/healthy)"
status_row "Grafana"            "http://localhost:3001"           "$(http_up http://localhost:3001/api/health)"

echo ""

# Application services
status_row "Auth Service"       "http://localhost:8002/health"    "$(http_up http://localhost:8002/health)"
status_row "Backend BFF"        "http://localhost:8001/health"    "$(http_up http://localhost:8001/health)"
status_row "Transaction Svc"    "http://localhost:8003/health"    "$(http_up http://localhost:8003/health)"
status_row "Feature Service"    "http://localhost:8005/health"    "$(http_up http://localhost:8005/health)"
status_row "Frontend"           "http://localhost:5173"           "$(http_up http://localhost:5173)"

echo ""
