#!/bin/bash
set -euo pipefail

export AWS_ACCESS_KEY_ID=local
export AWS_SECRET_ACCESS_KEY=local
export AWS_DEFAULT_REGION=us-east-1

ENDPOINT="${DYNAMODB_ENDPOINT:-http://localhost:8000}"

# Run create-table; print ✅ on success, ⚠️ if already exists, exit 1 on other errors.
create_table() {
    local table_name="$1"
    shift
    local output
    if output=$(aws dynamodb create-table \
        --endpoint-url "$ENDPOINT" \
        --table-name "$table_name" \
        "$@" 2>&1); then
        echo "✅  $table_name created"
    elif echo "$output" | grep -q "ResourceInUseException"; then
        echo "⚠️   $table_name already exists — skipping"
    else
        echo "❌  $table_name failed: $output" >&2
        exit 1
    fi
}

# ── payshield-users-dev ───────────────────────────────────────────────────────
create_table "payshield-users-dev" \
    --billing-mode PAY_PER_REQUEST \
    --attribute-definitions \
        AttributeName=user_id,AttributeType=S \
        AttributeName=email,AttributeType=S \
    --key-schema \
        AttributeName=user_id,KeyType=HASH \
    --global-secondary-indexes '[
        {
            "IndexName": "email-index",
            "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"}
        }
    ]'

# ── payshield-transactions-dev ────────────────────────────────────────────────
create_table "payshield-transactions-dev" \
    --billing-mode PAY_PER_REQUEST \
    --attribute-definitions \
        AttributeName=transaction_id,AttributeType=S \
        AttributeName=user_id,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
        AttributeName=risk_level,AttributeType=S \
    --key-schema \
        AttributeName=transaction_id,KeyType=HASH \
    --global-secondary-indexes '[
        {
            "IndexName": "user-id-index",
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"}
            ],
            "Projection": {"ProjectionType": "ALL"}
        },
        {
            "IndexName": "risk-level-index",
            "KeySchema": [
                {"AttributeName": "risk_level", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"}
            ],
            "Projection": {"ProjectionType": "ALL"}
        }
    ]'

# ── payshield-model-predictions-dev ──────────────────────────────────────────
create_table "payshield-model-predictions-dev" \
    --billing-mode PAY_PER_REQUEST \
    --attribute-definitions \
        AttributeName=prediction_id,AttributeType=S \
        AttributeName=model_version,AttributeType=S \
    --key-schema \
        AttributeName=prediction_id,KeyType=HASH \
    --global-secondary-indexes '[
        {
            "IndexName": "model-version-index",
            "KeySchema": [{"AttributeName": "model_version", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"}
        }
    ]'

echo ""
echo "Done. Tables in DynamoDB local:"
aws dynamodb list-tables --endpoint-url "$ENDPOINT" \
    --query 'TableNames[*]' --output text | tr '\t' '\n' | sed 's/^/  • /'
