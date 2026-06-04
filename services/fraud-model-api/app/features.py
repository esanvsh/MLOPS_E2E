import numpy as np

from .schemas import TransactionFeatureInput

PAYMENT_METHOD_ENCODING = {
    "credit_card": 0,
    "debit_card": 1,
    "upi": 2,
    "neft": 3,
    "imps": 4,
}


def _scaler_mean(preprocessor, feature_name: str) -> float:
    """Return the training-set mean for a feature so it scales to 0 (neutral)."""
    names = list(preprocessor.feature_names_in_)
    if feature_name in names:
        return float(preprocessor.mean_[names.index(feature_name)])
    return 0.0


def build_feature_vector(
    input: TransactionFeatureInput, feature_names: list[str], preprocessor=None
) -> np.ndarray:
    amount_log = np.log1p(input.amount)
    is_late_night = 1 if input.hour >= 23 or input.hour <= 4 else 0

    # For features unavailable at inference time, use the scaler mean so they
    # transform to 0 (neutral) rather than an arbitrary out-of-distribution value.
    def neutral(name: str) -> float:
        return _scaler_mean(preprocessor, name) if preprocessor is not None else 0.0

    feature_map: dict[str, float] = {
        "amount_log": amount_log,
        "is_round_amount": 1 if input.amount % 100 == 0 else 0,
        "amount_bin": min(4, int(input.amount / 500)),
        "hour_of_day": input.hour,
        "day_of_week": input.day_of_week,
        "is_late_night": is_late_night,
        "is_weekend": 1 if input.day_of_week >= 5 else 0,
        "days_since_start": neutral("days_since_start"),
        "user_txn_count_cumulative": input.user_txn_count_24h,
        "card4_encoded": neutral("card4_encoded"),
        "card6_encoded": neutral("card6_encoded"),
        "addr_mismatch": input.addr_mismatch,
        "email_domain_risk": input.email_domain_risk,
        "P_emaildomain_risk_score": input.email_domain_risk / 2.0,
        "is_high_risk_category": input.is_high_risk_category,
        "amount_x_hour": amount_log * input.hour,
        "amount_x_velocity": amount_log * min(input.user_txn_count_24h, 20),
        "new_device_x_late_night": input.is_new_device * is_late_night,
        "V_pca_1": neutral("V_pca_1"),
        "V_pca_2": neutral("V_pca_2"),
        "V_pca_3": neutral("V_pca_3"),
        "V_pca_4": neutral("V_pca_4"),
        "V_pca_5": neutral("V_pca_5"),
        "payment_channel_encoded": PAYMENT_METHOD_ENCODING.get(input.payment_method, 0),
        "city_tier": neutral("city_tier"),
        "upi_collect_request": 0,
        "is_new_device": input.is_new_device,
        "is_new_merchant_for_user": input.is_high_risk_category,
        "C1": neutral("C1"),
        "C2": neutral("C2"),
        "C6": neutral("C6"),
        "C11": neutral("C11"),
        "C13": neutral("C13"),
        "C14": neutral("C14"),
        "D1": neutral("D1"),
        "D4": neutral("D4"),
        "D10": neutral("D10"),
        "D15": neutral("D15"),
        "M4_encoded": neutral("M4_encoded"),
        "M6_encoded": neutral("M6_encoded"),
    }

    return np.array([[feature_map.get(f, 0) for f in feature_names]])


def apply_preprocessing(
    X: np.ndarray, preprocessor, feature_names: list[str]
) -> np.ndarray:
    X_out = X.copy().astype(float)
    feature_idx = {f: i for i, f in enumerate(feature_names)}
    scaler_names = list(preprocessor.feature_names_in_)

    # Build scaler input in the exact order the scaler was fit on.
    # Missing features get their training mean so they scale to 0 (neutral).
    X_scaler = np.zeros((X_out.shape[0], len(scaler_names)))
    for i, f in enumerate(scaler_names):
        if f in feature_idx:
            X_scaler[:, i] = X_out[:, feature_idx[f]]
        else:
            X_scaler[:, i] = preprocessor.mean_[i]

    X_transformed = preprocessor.transform(X_scaler)

    for i, f in enumerate(scaler_names):
        if f in feature_idx:
            X_out[:, feature_idx[f]] = X_transformed[:, i]

    return X_out


def classify_risk(probability: float) -> str:
    if probability >= 0.70:
        return "HIGH"
    if probability >= 0.35:
        return "MEDIUM"
    return "LOW"
