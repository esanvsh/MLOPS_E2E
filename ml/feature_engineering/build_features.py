import matplotlib
matplotlib.use('Agg')  # Required for WSL2 — no display available

import argparse
import io
import json
import os
import pickle
import sys
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from sklearn.decomposition import PCA
from sklearn.preprocessing import LabelEncoder, StandardScaler
from tqdm import tqdm

load_dotenv()

# ── paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = ROOT / "ml" / "data" / "raw"
DATA_LOCAL = ROOT / "ml" / "data" / "processed"

TRANSACTION_FILE = DATA_RAW / "train_transaction.csv"
IDENTITY_FILE = DATA_RAW / "train_identity.csv"

# ── MinIO / S3 config ──────────────────────────────────────────────────────────
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET_PROCESSED", "payshield-processed")

# ── domain risk lists ──────────────────────────────────────────────────────────
HIGH_RISK_DOMAINS = {"protonmail.com", "guerrillamail.com", "mailinator.com", "throwam.com", "spam.la"}
NORMAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "aol.com", "comcast.net", "msn.com", "att.net",
}

# ── final feature list (37 cols + label) ──────────────────────────────────────
FINAL_FEATURES = [
    # Amount (3)
    "amount_log", "is_round_amount", "amount_bin",
    # Time (5)
    "hour_of_day", "day_of_week", "is_late_night", "is_weekend", "days_since_start",
    # Velocity (1)
    "user_txn_count_cumulative",
    # Card (2)
    "card4_encoded", "card6_encoded",
    # Address (1)
    "addr_mismatch",
    # Email (1)
    "email_domain_risk",
    # Category (1)
    "is_high_risk_category",
    # Cross features (3)
    "amount_x_hour", "amount_x_velocity", "new_device_x_late_night",
    # Synthetic / forward-compat (5)
    "payment_channel_encoded", "city_tier", "upi_collect_request",
    "is_new_device", "is_new_merchant_for_user",
    # C-features (6)
    "C1", "C2", "C6", "C11", "C13", "C14",
    # D-features (4)
    "D1", "D4", "D10", "D15",
    # M-features encoded (2)
    "M4_encoded", "M6_encoded",
    # PCA components (5)
    "V_pca_1", "V_pca_2", "V_pca_3", "V_pca_4", "V_pca_5",
]


# ── S3 helpers ─────────────────────────────────────────────────────────────────

def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
    )


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        s3.create_bucket(Bucket=bucket)
        print(f"  Created bucket: {bucket}")


def upload_parquet(s3, df: pd.DataFrame, bucket: str, key: str) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


def upload_json(s3, data: dict, bucket: str, key: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data, indent=2).encode())


# ── email domain risk ──────────────────────────────────────────────────────────

def _domain_risk(domain) -> int:
    if pd.isna(domain):
        return 1  # unknown
    d = str(domain).lower().strip()
    if d in HIGH_RISK_DOMAINS:
        return 2
    if d in NORMAL_DOMAINS:
        return 0
    return 1


# ── pipeline ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="PayShield AI — Feature Engineering Pipeline")
    parser.add_argument("--local", action="store_true", help="Read/write local ml/data/ instead of MinIO")
    args = parser.parse_args()

    bar = tqdm(total=10, desc="Pipeline", unit="step", ncols=80, file=sys.stdout)

    # ── Step 1: Load CSVs ──────────────────────────────────────────────────────
    bar.set_description("Step 1/10: Loading CSVs")
    tqdm.write("\n[Step 1] Loading CSVs...")
    tx = pd.read_csv(TRANSACTION_FILE)
    identity = pd.read_csv(IDENTITY_FILE)
    tqdm.write(f"  train_transaction: {tx.shape}  {tx.memory_usage(deep=True).sum() / 1e6:.1f} MB")
    tqdm.write(f"  train_identity:    {identity.shape}  {identity.memory_usage(deep=True).sum() / 1e6:.1f} MB")
    bar.update(1)

    # ── Step 2: LEFT JOIN ──────────────────────────────────────────────────────
    bar.set_description("Step 2/10: Merging")
    tqdm.write("\n[Step 2] LEFT JOIN on TransactionID...")
    df = tx.merge(identity, on="TransactionID", how="left")
    tqdm.write(f"  Merged shape: {df.shape}")
    del tx, identity
    bar.update(1)

    # ── Step 3: Drop columns > 80% null ───────────────────────────────────────
    bar.set_description("Step 3/10: Dropping sparse cols")
    tqdm.write("\n[Step 3] Dropping columns with > 80% nulls...")
    null_frac = df.isnull().mean()
    drop_cols = null_frac[null_frac > 0.80].index.tolist()
    df.drop(columns=drop_cols, inplace=True)
    tqdm.write(f"  Dropped {len(drop_cols)} columns — {df.shape[1]} remaining")
    bar.update(1)

    # ── Step 4: PCA on V-columns ───────────────────────────────────────────────
    bar.set_description("Step 4/10: PCA on V-cols")
    tqdm.write("\n[Step 4] PCA on V1–V339 → 5 components...")
    v_cols = [c for c in df.columns if c.startswith("V") and c[1:].isdigit()]
    tqdm.write(f"  V-columns surviving null drop: {len(v_cols)}")
    if v_cols:
        v_data = df[v_cols].fillna(0).astype(np.float32).values
        pca = PCA(n_components=5, random_state=42)
        v_pca = pca.fit_transform(v_data)
        explained = pca.explained_variance_ratio_.cumsum()[-1]
        for i in range(5):
            df[f"V_pca_{i + 1}"] = v_pca[:, i].astype(np.float32)
        tqdm.write(f"  Cumulative explained variance (5 components): {explained:.1%}")
        df.drop(columns=v_cols, inplace=True)
    else:
        for i in range(5):
            df[f"V_pca_{i + 1}"] = np.float32(0.0)
        tqdm.write("  No V-columns found — PCA components set to 0")
    bar.update(1)

    # ── Step 5: Feature engineering ───────────────────────────────────────────
    bar.set_description("Step 5/10: Engineering features")
    tqdm.write("\n[Step 5] Engineering features...")

    # Amount
    df["amount_log"] = np.log1p(df["TransactionAmt"]).astype(np.float32)
    df["is_round_amount"] = (df["TransactionAmt"] % 1 == 0).astype(np.int8)
    df["amount_bin"] = (
        pd.qcut(df["TransactionAmt"], q=5, labels=False, duplicates="drop")
        .fillna(0)
        .astype(np.int8)
    )

    # Time — TransactionDT is seconds elapsed from a dataset reference point
    ref_dt = int(df["TransactionDT"].min())
    df["days_since_start"] = ((df["TransactionDT"] - ref_dt) / 86400).astype(np.float32)
    df["hour_of_day"] = ((df["TransactionDT"] // 3600) % 24).astype(np.int8)
    df["day_of_week"] = ((df["TransactionDT"] // 86400) % 7).astype(np.int8)
    df["is_late_night"] = ((df["hour_of_day"] >= 22) | (df["hour_of_day"] <= 5)).astype(np.int8)
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(np.int8)

    # Velocity — sort first, then expanding cumcount per card1
    df.sort_values("TransactionDT", inplace=True, ignore_index=True)
    df["user_txn_count_cumulative"] = df.groupby("card1").cumcount().astype(np.int32)

    # Card encoding
    for src, dst in [("card4", "card4_encoded"), ("card6", "card6_encoded")]:
        if src in df.columns:
            le = LabelEncoder()
            df[dst] = le.fit_transform(df[src].fillna("unknown").astype(str)).astype(np.int16)
        else:
            df[dst] = np.int16(0)

    # Address mismatch
    if "addr1" in df.columns and "addr2" in df.columns:
        df["addr_mismatch"] = (df["addr1"].fillna(-1) != df["addr2"].fillna(-2)).astype(np.int8)
    else:
        df["addr_mismatch"] = np.int8(0)

    # Email domain risk (purchaser domain)
    email_col = next((c for c in ("P_emaildomain", "R_emaildomain") if c in df.columns), None)
    if email_col:
        df["email_domain_risk"] = df[email_col].map(_domain_risk).astype(np.int8)
    else:
        df["email_domain_risk"] = np.int8(1)

    # Category
    if "ProductCD" in df.columns:
        df["is_high_risk_category"] = df["ProductCD"].isin(["H", "C"]).astype(np.int8)
    else:
        df["is_high_risk_category"] = np.int8(0)

    # Cross features
    df["amount_x_hour"] = (df["amount_log"] * df["hour_of_day"]).astype(np.float32)
    df["amount_x_velocity"] = (df["amount_log"] * df["user_txn_count_cumulative"]).astype(np.float32)
    df["new_device_x_late_night"] = (df["addr_mismatch"] * df["is_late_night"]).astype(np.int8)

    # Synthetic (forward-compatible with live feature service)
    df["payment_channel_encoded"] = np.int8(0)
    df["city_tier"] = np.int8(1)
    df["upi_collect_request"] = np.int8(0)
    df["is_new_device"] = df["addr_mismatch"]
    df["is_new_merchant_for_user"] = df["is_high_risk_category"]

    # M-columns
    for src, dst in [("M4", "M4_encoded"), ("M6", "M6_encoded")]:
        if src in df.columns:
            le = LabelEncoder()
            df[dst] = le.fit_transform(df[src].fillna("unknown").astype(str)).astype(np.int8)
        else:
            df[dst] = np.int8(0)

    # C / D columns — fill nulls, cast to float32
    for col in ["C1", "C2", "C6", "C11", "C13", "C14", "D1", "D4", "D10", "D15"]:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype(np.float32)
        else:
            df[col] = np.float32(0.0)

    tqdm.write("  Feature engineering complete")
    bar.update(1)

    # ── Step 6: Select final features ─────────────────────────────────────────
    bar.set_description("Step 6/10: Selecting features")
    tqdm.write("\n[Step 6] Selecting final feature columns...")
    available = [c for c in FINAL_FEATURES if c in df.columns]
    missing_feats = [c for c in FINAL_FEATURES if c not in df.columns]
    if missing_feats:
        tqdm.write(f"  Warning: {len(missing_feats)} requested features absent: {missing_feats}")

    df_final = df[available + ["isFraud"]].copy()
    df_final.rename(columns={"isFraud": "label"}, inplace=True)
    tqdm.write(f"  Final dataset: {df_final.shape}  ({len(available)} features + label)")
    bar.update(1)

    # ── Step 7: Time-based split ───────────────────────────────────────────────
    bar.set_description("Step 7/10: Splitting")
    tqdm.write("\n[Step 7] Time-based split (sorted by TransactionDT)...")
    n = len(df_final)
    n_train = int(n * 0.80)
    n_val = int(n * 0.10)

    train = df_final.iloc[:n_train].copy()
    val = df_final.iloc[n_train: n_train + n_val].copy()
    test = df_final.iloc[n_train + n_val:].copy()
    reference = train.sample(10_000, random_state=42)

    tqdm.write(f"  train:     {len(train):,} rows  (fraud rate {train['label'].mean():.2%})")
    tqdm.write(f"  val:       {len(val):,} rows  (fraud rate {val['label'].mean():.2%})")
    tqdm.write(f"  test:      {len(test):,} rows  (fraud rate {test['label'].mean():.2%})")
    tqdm.write(f"  reference: {len(reference):,} rows")
    bar.update(1)

    # ── Step 8: StandardScaler ─────────────────────────────────────────────────
    bar.set_description("Step 8/10: Scaling")
    tqdm.write("\n[Step 8] StandardScaler on float columns (fit on train only)...")
    float_cols = [c for c in available if train[c].dtype in (np.float32, np.float64)]
    scaler = StandardScaler()
    train[float_cols] = scaler.fit_transform(train[float_cols]).astype(np.float32)
    val[float_cols] = scaler.transform(val[float_cols]).astype(np.float32)
    test[float_cols] = scaler.transform(test[float_cols]).astype(np.float32)
    reference[float_cols] = scaler.transform(reference[float_cols]).astype(np.float32)

    with open("/tmp/preprocessor.pkl", "wb") as f:
        pickle.dump(scaler, f)
    tqdm.write(f"  Scaled {len(float_cols)} float columns")
    tqdm.write("  Scaler saved to /tmp/preprocessor.pkl")
    bar.update(1)

    # ── Step 9: Write outputs ──────────────────────────────────────────────────
    bar.set_description("Step 9/10: Writing outputs")
    tqdm.write("\n[Step 9] Writing parquet files...")

    feature_schema = {
        "features": available,
        "label": "label",
        "float_cols": float_cols,
        "n_features": len(available),
        "splits": {
            "train": len(train),
            "val": len(val),
            "test": len(test),
            "reference": len(reference),
        },
    }

    splits: dict[str, pd.DataFrame] = {
        "train": train,
        "val": val,
        "test": test,
        "reference": reference,
    }

    if args.local:
        DATA_LOCAL.mkdir(parents=True, exist_ok=True)
        for name, split_df in tqdm(splits.items(), desc="  Writing", leave=False, ncols=60):
            out_path = DATA_LOCAL / f"{name}.parquet"
            split_df.to_parquet(out_path, index=False, engine="pyarrow")
            tqdm.write(f"  Wrote {out_path}  ({len(split_df):,} rows)")
        schema_path = DATA_LOCAL / "feature_schema.json"
        schema_path.write_text(json.dumps(feature_schema, indent=2))
        tqdm.write(f"  Wrote {schema_path}")
        scaler_dst = DATA_LOCAL / "preprocessor.pkl"
        with open(scaler_dst, "wb") as f:
            pickle.dump(scaler, f)
        tqdm.write(f"  Wrote {scaler_dst}")
    else:
        s3 = get_s3_client()
        ensure_bucket(s3, S3_BUCKET)
        for name, split_df in tqdm(splits.items(), desc="  Uploading", leave=False, ncols=60):
            key = f"features/{name}.parquet"
            upload_parquet(s3, split_df, S3_BUCKET, key)
            tqdm.write(f"  Uploaded s3://{S3_BUCKET}/{key}  ({len(split_df):,} rows)")
        upload_json(s3, feature_schema, S3_BUCKET, "features/feature_schema.json")
        tqdm.write(f"  Uploaded s3://{S3_BUCKET}/features/feature_schema.json")
        with open("/tmp/preprocessor.pkl", "rb") as f:
            s3.put_object(Bucket=S3_BUCKET, Key="features/preprocessor.pkl", Body=f.read())
        tqdm.write(f"  Uploaded s3://{S3_BUCKET}/features/preprocessor.pkl")

    bar.update(1)

    # ── Step 10: Summary ───────────────────────────────────────────────────────
    bar.set_description("Step 10/10: Done")
    dest = str(DATA_LOCAL) if args.local else f"s3://{S3_BUCKET}/features/"
    tqdm.write("\n[Step 10] Summary")
    tqdm.write("=" * 60)
    tqdm.write(f"  Features:          {len(available)}")
    tqdm.write(f"  Train:             {len(train):,} rows  (fraud {train['label'].mean():.2%})")
    tqdm.write(f"  Val:               {len(val):,} rows  (fraud {val['label'].mean():.2%})")
    tqdm.write(f"  Test:              {len(test):,} rows  (fraud {test['label'].mean():.2%})")
    tqdm.write(f"  Reference:         {len(reference):,} rows")
    tqdm.write(f"  Float cols scaled: {len(float_cols)}")
    tqdm.write(f"  Scaler:            /tmp/preprocessor.pkl")
    tqdm.write(f"  Output:            {dest}")
    tqdm.write("=" * 60)
    tqdm.write("  Pipeline complete.")
    bar.update(1)
    bar.close()


if __name__ == "__main__":
    main()
