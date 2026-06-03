#!/bin/bash
set -euo pipefail

MINIO_ENDPOINT="http://localhost:9000"
MINIO_USER="minioadmin"
MINIO_PASS="minioadmin123"
ALIAS="local"

BUCKETS=(
    payshield-raw-data
    payshield-processed-data
    payshield-model-artifacts
    payshield-drift-reports
)

# ── Install mc if missing ─────────────────────────────────────────────────────
if ! command -v mc &>/dev/null; then
    echo "Installing MinIO client (mc)..."
    curl -sLO https://dl.min.io/client/mc/release/linux-amd64/mc
    chmod +x mc
    if sudo -n mv mc /usr/local/bin/mc 2>/dev/null; then
        echo "✅  mc installed to /usr/local/bin/mc"
    else
        mkdir -p "$HOME/.local/bin"
        mv mc "$HOME/.local/bin/mc"
        export PATH="$HOME/.local/bin:$PATH"
        echo "✅  mc installed to ~/.local/bin/mc (sudo unavailable)"
    fi
    echo "✅  mc installed: $(mc --version | head -1)"
else
    echo "✅  mc already installed: $(mc --version | head -1)"
fi

# ── Configure alias ───────────────────────────────────────────────────────────
mc alias set "$ALIAS" "$MINIO_ENDPOINT" "$MINIO_USER" "$MINIO_PASS" --insecure \
    >/dev/null 2>&1
echo "✅  alias '$ALIAS' → $MINIO_ENDPOINT"

# ── Create buckets (idempotent) ───────────────────────────────────────────────
for bucket in "${BUCKETS[@]}"; do
    if mc ls "${ALIAS}/${bucket}" &>/dev/null; then
        echo "⚠️   s3://$bucket already exists — skipping"
    else
        mc mb "${ALIAS}/${bucket}" --insecure >/dev/null
        echo "✅  s3://$bucket created"
    fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Buckets in MinIO local:"
mc ls "$ALIAS" --insecure 2>/dev/null | awk '{print "  •", $NF}'
