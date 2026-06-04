import matplotlib
matplotlib.use('Agg')  # Required for WSL2 — no display

import argparse
import io
import json
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path

import boto3
import mlflow
import mlflow.xgboost
import numpy as np
import pandas as pd
import seaborn as sns
import shap
import xgboost as xgb
from dotenv import load_dotenv
from matplotlib import pyplot as plt
from mlflow.tracking import MlflowClient
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)

load_dotenv()

# ── paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
DATA_LOCAL = ROOT / "ml" / "data" / "processed"

# ── env ────────────────────────────────────────────────────────────────────────
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET_PROCESSED", "payshield-processed")
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5050")

# ── promotion thresholds ───────────────────────────────────────────────────────
PROMOTE_RECALL = 0.80
PROMOTE_F1 = 0.75
PROMOTE_ROC_AUC = 0.92

# optimal-threshold search bounds
OPT_MIN_RECALL = 0.80
OPT_MIN_PRECISION = 0.60
DEFAULT_THRESHOLD = 0.35


# ── S3 helpers ─────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
    )


def _read_parquet(s3, bucket: str, key: str) -> pd.DataFrame:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return pd.read_parquet(io.BytesIO(obj["Body"].read()), engine="pyarrow")


def _put(s3, data: bytes, bucket: str, key: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=data)


# ── plot helpers ───────────────────────────────────────────────────────────────

def _save(fig: plt.Figure, path: str) -> None:
    fig.savefig(path, bbox_inches="tight", dpi=120)
    plt.close(fig)


def plot_confusion_matrix(cm: np.ndarray) -> str:
    fig, ax = plt.subplots(figsize=(6, 5))
    sns.heatmap(
        cm, annot=True, fmt="d", cmap="Blues", ax=ax,
        xticklabels=["Genuine", "Fraud"],
        yticklabels=["Genuine", "Fraud"],
    )
    ax.set_xlabel("Predicted")
    ax.set_ylabel("Actual")
    ax.set_title("Confusion Matrix (test set)")
    path = "/tmp/confusion_matrix.png"
    _save(fig, path)
    return path


def plot_pr_curve(recalls, precisions, pr_auc: float, threshold: float,
                  test_recall: float, test_precision: float) -> str:
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(recalls, precisions, color="steelblue", lw=2, label=f"PR AUC = {pr_auc:.3f}")
    ax.scatter([test_recall], [test_precision], color="orange", zorder=5,
               label=f"threshold = {threshold:.3f}")
    ax.axvline(x=OPT_MIN_RECALL, color="gray", linestyle=":", alpha=0.6, label=f"recall target {OPT_MIN_RECALL}")
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title("Precision-Recall Curve (test set)")
    ax.legend()
    ax.grid(alpha=0.3)
    path = "/tmp/pr_curve.png"
    _save(fig, path)
    return path


def plot_roc_curve(fpr, tpr, roc_auc: float) -> str:
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(fpr, tpr, color="darkorange", lw=2, label=f"ROC AUC = {roc_auc:.3f}")
    ax.plot([0, 1], [0, 1], color="gray", linestyle="--", lw=1)
    ax.set_xlabel("False Positive Rate")
    ax.set_ylabel("True Positive Rate")
    ax.set_title("ROC Curve (test set)")
    ax.legend()
    ax.grid(alpha=0.3)
    path = "/tmp/roc_curve.png"
    _save(fig, path)
    return path


def plot_shap_summary(model: xgb.XGBClassifier, X_sample: np.ndarray,
                      feature_cols: list[str]) -> str:
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_sample)
    shap.summary_plot(shap_values, X_sample, feature_names=feature_cols, show=False)
    path = "/tmp/shap_summary.png"
    plt.savefig(path, bbox_inches="tight", dpi=120)
    plt.close()
    return path


def plot_feature_importance(model: xgb.XGBClassifier) -> str:
    fig, ax = plt.subplots(figsize=(10, 8))
    xgb.plot_importance(model, ax=ax, max_num_features=20, title="Feature Importance (gain)")
    path = "/tmp/feature_importance.png"
    _save(fig, path)
    return path


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="PayShield AI — XGBoost Training Pipeline")
    parser.add_argument("--dataset-version", default="latest")
    parser.add_argument("--experiment-name", default="payshield-fraud-detection")
    parser.add_argument("--model-name", default="fraud-risk-model")
    parser.add_argument("--local", action="store_true", help="Read/write local ml/data/processed/ instead of MinIO")
    args = parser.parse_args()

    # ── MLflow S3 artifact client config ──────────────────────────────────────
    # The MLflow server stores artifacts in MinIO (s3://payshield-mlflow/).
    # The client must also talk to MinIO, not AWS, so we set these env vars
    # before any mlflow call that touches artifacts.
    os.environ["MLFLOW_S3_ENDPOINT_URL"] = S3_ENDPOINT_URL
    os.environ["AWS_ACCESS_KEY_ID"] = MINIO_ACCESS_KEY
    os.environ["AWS_SECRET_ACCESS_KEY"] = MINIO_SECRET_KEY

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

    # Ensure a clean experiment pointing at MinIO.
    # search_experiments(view_type="ALL") finds soft-deleted experiments too —
    # they still block the name in the UNIQUE constraint.
    # MLflow 2.11 returns HTTP 500 when rename is called on a deleted experiment,
    # so we restore it first (making it active), rename, then delete again.
    ml_client_setup = MlflowClient()
    stale = ml_client_setup.search_experiments(
        view_type="ALL",
        filter_string=f"name = '{args.experiment_name}'",
    )
    for exp in stale:
        if exp.artifact_location.startswith("s3://"):
            continue  # already points at MinIO — leave it alone
        freed_name = f"__archived_{exp.experiment_id}_{args.experiment_name}"
        print(f"  Freeing stale experiment {exp.experiment_id} (lifecycle={exp.lifecycle_stage}) → '{freed_name}'")
        if exp.lifecycle_stage == "deleted":
            ml_client_setup.restore_experiment(exp.experiment_id)
        ml_client_setup.rename_experiment(exp.experiment_id, freed_name)
        ml_client_setup.delete_experiment(exp.experiment_id)

    # Create fresh experiment with MinIO artifact root if it still doesn't exist.
    if not ml_client_setup.get_experiment_by_name(args.experiment_name):
        artifact_root = f"s3://payshield-mlflow/{args.experiment_name}"
        ml_client_setup.create_experiment(args.experiment_name, artifact_location=artifact_root)
        print(f"  Created experiment '{args.experiment_name}'  artifact_root={artifact_root}")
    mlflow.set_experiment(args.experiment_name)

    # ── Step 1: Load features ──────────────────────────────────────────────────
    print("\n[Step 1] Loading feature parquet files...")
    if args.local:
        train_df = pd.read_parquet(DATA_LOCAL / "train.parquet", engine="pyarrow")
        val_df = pd.read_parquet(DATA_LOCAL / "val.parquet", engine="pyarrow")
        test_df = pd.read_parquet(DATA_LOCAL / "test.parquet", engine="pyarrow")
        feature_schema = json.loads((DATA_LOCAL / "feature_schema.json").read_text())
        with open(DATA_LOCAL / "preprocessor.pkl", "rb") as fh:
            scaler = pickle.load(fh)
    else:
        client_s3 = _s3()
        train_df = _read_parquet(client_s3, S3_BUCKET, "features/train.parquet")
        val_df = _read_parquet(client_s3, S3_BUCKET, "features/val.parquet")
        test_df = _read_parquet(client_s3, S3_BUCKET, "features/test.parquet")
        raw = client_s3.get_object(Bucket=S3_BUCKET, Key="features/feature_schema.json")
        feature_schema = json.loads(raw["Body"].read())
        raw_pkl = client_s3.get_object(Bucket=S3_BUCKET, Key="features/preprocessor.pkl")
        scaler = pickle.loads(raw_pkl["Body"].read())

    feature_cols: list[str] = feature_schema["features"]
    X_train = train_df[feature_cols].values.astype(np.float32)
    y_train = train_df["label"].values.astype(np.int8)
    X_val = val_df[feature_cols].values.astype(np.float32)
    y_val = val_df["label"].values.astype(np.int8)
    X_test = test_df[feature_cols].values.astype(np.float32)
    y_test = test_df["label"].values.astype(np.int8)

    print(f"  train : {X_train.shape}  fraud={y_train.mean():.2%}")
    print(f"  val   : {X_val.shape}  fraud={y_val.mean():.2%}")
    print(f"  test  : {X_test.shape}  fraud={y_test.mean():.2%}")

    # ── Step 2: scale_pos_weight ───────────────────────────────────────────────
    print("\n[Step 2] Computing scale_pos_weight...")
    neg = int((y_train == 0).sum())
    pos = int((y_train == 1).sum())
    scale_pos_weight = neg / pos
    print(f"  neg={neg:,}  pos={pos:,}  scale_pos_weight={scale_pos_weight:.2f}")

    # ── Step 3: MLflow setup ───────────────────────────────────────────────────
    print(f"\n[Step 3] MLflow: {MLFLOW_TRACKING_URI}  experiment={args.experiment_name}")

    params: dict = dict(
        n_estimators=500,
        max_depth=6,
        min_child_weight=10,
        subsample=0.8,
        colsample_bytree=0.8,
        learning_rate=0.05,
        gamma=1.0,
        scale_pos_weight=scale_pos_weight,
        reg_alpha=0.1,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="aucpr",
        tree_method="hist",
        random_state=42,
        early_stopping_rounds=50,
        n_jobs=-1,
    )

    with mlflow.start_run() as run:
        run_id = run.info.run_id
        print(f"\n[Step 4] Training XGBoost  (run_id={run_id})...")

        # ── Step 4: Train ──────────────────────────────────────────────────────
        model = xgb.XGBClassifier(**params)
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=50,
        )
        best_iter = model.best_iteration
        print(f"  Best iteration: {best_iter}")

        # ── Step 5: Optimal threshold ──────────────────────────────────────────
        print("\n[Step 5] Finding optimal threshold on val set...")
        y_prob_val = model.predict_proba(X_val)[:, 1]
        precisions_v, recalls_v, thresholds_v = precision_recall_curve(y_val, y_prob_val)

        candidates = [
            (float(t), float(p), float(r))
            for t, p, r in zip(thresholds_v, precisions_v[:-1], recalls_v[:-1])
            if r >= OPT_MIN_RECALL and p >= OPT_MIN_PRECISION
        ]
        if candidates:
            best = max(candidates, key=lambda x: 2 * x[1] * x[2] / (x[1] + x[2] + 1e-9))
            threshold = best[0]
            print(f"  threshold={threshold:.4f}  recall={best[2]:.3f}  precision={best[1]:.3f}")
        else:
            threshold = DEFAULT_THRESHOLD
            print(f"  No candidate met recall≥{OPT_MIN_RECALL} & precision≥{OPT_MIN_PRECISION} — default {threshold}")

        # ── Step 6: Evaluate on test set ───────────────────────────────────────
        print("\n[Step 6] Evaluating on test set...")
        y_prob_test = model.predict_proba(X_test)[:, 1]
        y_pred_test = (y_prob_test >= threshold).astype(np.int8)

        test_recall = float(recall_score(y_test, y_pred_test))
        test_precision = float(precision_score(y_test, y_pred_test, zero_division=0))
        test_f1 = float(f1_score(y_test, y_pred_test, zero_division=0))
        test_roc_auc = float(roc_auc_score(y_test, y_prob_test))
        test_pr_auc = float(average_precision_score(y_test, y_prob_test))
        cm = confusion_matrix(y_test, y_pred_test)
        fn_count = int(cm[1, 0]) if cm.shape == (2, 2) else 0

        print(f"  recall:          {test_recall:.4f}")
        print(f"  precision:       {test_precision:.4f}")
        print(f"  f1_score:        {test_f1:.4f}")
        print(f"  roc_auc:         {test_roc_auc:.4f}")
        print(f"  pr_auc:          {test_pr_auc:.4f}")
        print(f"  false_negatives: {fn_count:,}")

        # ── Step 7: Plots ──────────────────────────────────────────────────────
        print("\n[Step 7] Generating plots...")
        fpr, tpr, _ = roc_curve(y_test, y_prob_test)
        precisions_t, recalls_t, _ = precision_recall_curve(y_test, y_prob_test)

        plots = [
            plot_confusion_matrix(cm),
            plot_pr_curve(recalls_t, precisions_t, test_pr_auc, threshold, test_recall, test_precision),
            plot_roc_curve(fpr, tpr, test_roc_auc),
            plot_shap_summary(model, X_test[:500], feature_cols),
            plot_feature_importance(model),
        ]
        for p in plots:
            print(f"  Saved {p}")

        # ── Step 8: Log to MLflow ──────────────────────────────────────────────
        print("\n[Step 8] Logging to MLflow...")
        mlflow.log_params({
            "n_estimators": params["n_estimators"],
            "max_depth": params["max_depth"],
            "min_child_weight": params["min_child_weight"],
            "subsample": params["subsample"],
            "colsample_bytree": params["colsample_bytree"],
            "learning_rate": params["learning_rate"],
            "gamma": params["gamma"],
            "scale_pos_weight": round(scale_pos_weight, 4),
            "reg_alpha": params["reg_alpha"],
            "reg_lambda": params["reg_lambda"],
            "tree_method": params["tree_method"],
            "threshold": round(threshold, 4),
            "best_iteration": best_iter,
            "dataset_version": args.dataset_version,
        })
        mlflow.log_metrics({
            "test_recall": test_recall,
            "test_precision": test_precision,
            "test_f1": test_f1,
            "test_roc_auc": test_roc_auc,
            "test_pr_auc": test_pr_auc,
            "test_false_negatives": float(fn_count),
            "train_fraud_rate": float(y_train.mean()),
            "val_fraud_rate": float(y_val.mean()),
            "test_fraud_rate": float(y_test.mean()),
        })
        for p in plots:
            mlflow.log_artifact(p, artifact_path="plots")

        mlflow.log_artifact("/tmp/preprocessor.pkl", artifact_path="artifacts")

        schema_tmp = "/tmp/feature_schema.json"
        with open(schema_tmp, "w") as fh:
            json.dump(feature_schema, fh, indent=2)
        mlflow.log_artifact(schema_tmp, artifact_path="artifacts")

        mlflow.xgboost.log_model(model, "model", registered_model_name=args.model_name)
        print(f"  Registered model: {args.model_name}")

        # ── Step 9: Promotion ──────────────────────────────────────────────────
        print("\n[Step 9] Model promotion check...")
        ml_client = MlflowClient()
        versions = ml_client.search_model_versions(f"name='{args.model_name}'")
        model_version = str(max(int(v.version) for v in versions))

        promote = (
            test_recall >= PROMOTE_RECALL
            and test_f1 >= PROMOTE_F1
            and test_roc_auc >= PROMOTE_ROC_AUC
        )

        if promote:
            ml_client.transition_model_version_stage(
                name=args.model_name,
                version=model_version,
                stage="Production",
                archive_existing_versions=True,
            )
            promotion_status = "Production"
            print(f"  ✅ Promoted {args.model_name} v{model_version} → Production")
            print(f"     recall={test_recall:.3f} ≥ {PROMOTE_RECALL}  "
                  f"f1={test_f1:.3f} ≥ {PROMOTE_F1}  "
                  f"roc_auc={test_roc_auc:.3f} ≥ {PROMOTE_ROC_AUC}")
        else:
            promotion_status = "Staging"
            print(f"  ❌ Not promoted — criteria not met:")
            print(f"     recall:  {test_recall:.3f}  {'✓' if test_recall >= PROMOTE_RECALL else '✗'} (need ≥{PROMOTE_RECALL})")
            print(f"     f1:      {test_f1:.3f}  {'✓' if test_f1 >= PROMOTE_F1 else '✗'} (need ≥{PROMOTE_F1})")
            print(f"     roc_auc: {test_roc_auc:.3f}  {'✓' if test_roc_auc >= PROMOTE_ROC_AUC else '✗'} (need ≥{PROMOTE_ROC_AUC})")

        mlflow.log_param("promotion_status", promotion_status)

        # ── Step 10: current_model.txt ─────────────────────────────────────────
        print("\n[Step 10] Writing current_model.txt...")
        current_model = {
            "run_id": run_id,
            "model_name": args.model_name,
            "model_version": model_version,
            "promotion_status": promotion_status,
            "threshold": threshold,
            "metrics": {
                "recall": test_recall,
                "precision": test_precision,
                "f1": test_f1,
                "roc_auc": test_roc_auc,
                "pr_auc": test_pr_auc,
                "false_negatives": fn_count,
            },
            "dataset_version": args.dataset_version,
            "best_iteration": best_iter,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
        payload = json.dumps(current_model, indent=2).encode()

        if args.local:
            dst = DATA_LOCAL / "current_model.txt"
            dst.write_bytes(payload)
            print(f"  Written to {dst}")
        else:
            _put(_s3(), payload, S3_BUCKET, "models/current_model.txt")
            print(f"  Uploaded s3://{S3_BUCKET}/models/current_model.txt")

        # ── Summary ────────────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  Training complete")
        print(f"  Run ID:      {run_id}")
        print(f"  Model:       {args.model_name} v{model_version}")
        print(f"  Threshold:   {threshold:.4f}")
        print(f"  Recall:      {test_recall:.4f}")
        print(f"  F1:          {test_f1:.4f}")
        print(f"  ROC-AUC:     {test_roc_auc:.4f}")
        print(f"  PR-AUC:      {test_pr_auc:.4f}")
        print(f"  FN count:    {fn_count:,}")
        print(f"  Status:      {promotion_status}")
        print("=" * 60)


if __name__ == "__main__":
    main()
