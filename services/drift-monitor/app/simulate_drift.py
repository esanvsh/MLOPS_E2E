import matplotlib
matplotlib.use('Agg')

import io
import os
from datetime import datetime, timezone

import boto3
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

S3_ENDPOINT_URL     = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS_KEY    = os.getenv("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY    = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
S3_BUCKET_PROCESSED = os.getenv("S3_BUCKET_PROCESSED", "payshield-processed-data")

REFERENCE_KEY = "features/reference.parquet"
TODAY         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUTPUT_KEY    = f"inference/live_features_{TODAY}.parquet"

SAMPLE_SIZE = 2_000
RNG_SEED    = 42


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )


def main() -> None:
    s3  = _s3()
    rng = np.random.default_rng(RNG_SEED)

    print("Loading reference data from MinIO…")
    try:
        obj = s3.get_object(Bucket=S3_BUCKET_PROCESSED, Key=REFERENCE_KEY)
        reference = pd.read_parquet(io.BytesIO(obj["Body"].read()))
    except ClientError as exc:
        print(f"✗ Could not load reference: {exc}")
        raise SystemExit(1)

    df = reference.sample(min(SAMPLE_SIZE, len(reference)), random_state=RNG_SEED).copy()
    print(f"Sampled {len(df):,} rows from {len(reference):,} reference rows.\n")

    changes: list[str] = []

    if "amount_log" in df.columns:
        df["amount_log"] = df["amount_log"] * 1.8
        changes.append("amount_log × 1.8  (transaction amounts shifted significantly higher)")

    if "is_late_night" in df.columns:
        mask = rng.random(len(df)) < 0.80
        df["is_late_night"] = mask.astype(df["is_late_night"].dtype)
        pct = mask.mean() * 100
        changes.append(f"is_late_night = 1 for {pct:.0f}% of rows  (night-time activity spike)")

    if "user_txn_count_cumulative" in df.columns:
        df["user_txn_count_cumulative"] = rng.integers(15, 51, size=len(df)).astype(
            df["user_txn_count_cumulative"].dtype
        )
        changes.append("user_txn_count_cumulative → uniform [15, 50]  (velocity spike)")

    if "is_new_device" in df.columns:
        mask = rng.random(len(df)) < 0.70
        df["is_new_device"] = mask.astype(df["is_new_device"].dtype)
        pct = mask.mean() * 100
        changes.append(f"is_new_device = 1 for {pct:.0f}% of rows  (unusual device churn)")

    if "hour_of_day" in df.columns:
        # Late-night hours: 22, 23, 0, 1, 2, 3, 4
        late_hours = np.array([22, 23, 0, 1, 2, 3, 4])
        df["hour_of_day"] = rng.choice(late_hours, size=len(df)).astype(
            df["hour_of_day"].dtype
        )
        changes.append("hour_of_day → concentrated in 22–04  (late-night shift)")

    print("Changes applied:")
    for change in changes:
        print(f"  • {change}")

    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)

    print(f"\nUploading to s3://{S3_BUCKET_PROCESSED}/{OUTPUT_KEY} …")
    s3.put_object(
        Bucket=S3_BUCKET_PROCESSED,
        Key=OUTPUT_KEY,
        Body=buf.getvalue(),
        ContentType="application/octet-stream",
    )
    print(f"Saved {len(df):,} rows  ({buf.tell():,} bytes)")
    print("\n✅ Run 'make drift-check' to detect this drift")


if __name__ == "__main__":
    main()
