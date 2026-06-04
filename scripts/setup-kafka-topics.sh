#!/bin/bash
set -euo pipefail

TOPICS=(
    transactions.raw
    transactions.validated
    transactions.features
    transactions.scored
    transactions.failed
    model.drift.detected
    alerts.fraud.highrisk
)

create_topic() {
    local topic="$1"
    local output
    if output=$(docker compose exec kafka kafka-topics \
        --bootstrap-server localhost:9092 \
        --create --if-not-exists \
        --topic "$topic" \
        --partitions 1 \
        --replication-factor 1 2>&1); then
        echo "✅  $topic"
    else
        echo "❌  $topic failed: $output" >&2
        exit 1
    fi
}

echo "Creating Kafka topics..."
echo ""

for topic in "${TOPICS[@]}"; do
    create_topic "$topic"
done

echo ""
echo "Topics in broker:"
docker compose exec kafka kafka-topics \
    --bootstrap-server localhost:9092 \
    --list | sort | sed 's/^/  • /'
