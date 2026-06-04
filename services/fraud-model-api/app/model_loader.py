import json
import os
import pickle
import tempfile
from datetime import datetime, timezone

import mlflow
import mlflow.xgboost
import structlog

logger = structlog.get_logger()

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5050")
MODEL_NAME = os.getenv("MODEL_NAME", "fraud-risk-model")
MODEL_STAGE = os.getenv("MODEL_STAGE", "Production")


class ModelLoader:
    def __init__(self) -> None:
        self.model = None
        self.preprocessor = None
        self.feature_names: list[str] = []
        self.model_version: str = ""
        self.model_name: str = MODEL_NAME
        self.loaded_at: str = ""

    def load_model(self) -> bool:
        try:
            mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
            client = mlflow.tracking.MlflowClient()

            versions = client.get_latest_versions(MODEL_NAME, stages=[MODEL_STAGE])
            if not versions:
                logger.warning("no_model_found_for_stage", model_name=MODEL_NAME, stage=MODEL_STAGE)
                return False

            version = versions[0]

            self.model = mlflow.xgboost.load_model(f"models:/{MODEL_NAME}/{MODEL_STAGE}")

            with tempfile.TemporaryDirectory() as tmpdir:
                preprocessor_path = client.download_artifacts(
                    version.run_id, "artifacts/preprocessor.pkl", tmpdir
                )
                with open(preprocessor_path, "rb") as f:
                    self.preprocessor = pickle.load(f)

                schema_path = client.download_artifacts(
                    version.run_id, "artifacts/feature_schema.json", tmpdir
                )
                with open(schema_path, "r") as f:
                    schema = json.load(f)
                    self.feature_names = schema if isinstance(schema, list) else schema.get("features", [])

            self.model_version = version.version
            self.loaded_at = datetime.now(timezone.utc).isoformat()

            logger.info(
                "model_loaded",
                model_name=MODEL_NAME,
                stage=MODEL_STAGE,
                version=self.model_version,
                feature_count=len(self.feature_names),
            )
            return True

        except Exception:
            logger.exception("model_load_failed", model_name=MODEL_NAME)
            self.model = None
            return False

    def reload_model(self) -> bool:
        return self.load_model()

    def is_loaded(self) -> bool:
        return self.model is not None


MODEL_LOADER = ModelLoader()
