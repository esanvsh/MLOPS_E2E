import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for WSL2

import argparse
import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
TRANSACTION_FILE = DATA_DIR / "train_transaction.csv"
IDENTITY_FILE = DATA_DIR / "train_identity.csv"

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

_all_passed: list[bool] = []


def report(label: str, passed: bool, detail: str = "") -> None:
    status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    _all_passed.append(passed)


def check_files_exist() -> bool:
    tx_ok = TRANSACTION_FILE.exists()
    id_ok = IDENTITY_FILE.exists()
    report("train_transaction.csv exists", tx_ok, str(TRANSACTION_FILE) if not tx_ok else "")
    report("train_identity.csv exists", id_ok, str(IDENTITY_FILE) if not id_ok else "")
    return tx_ok and id_ok


def run_full_checks() -> None:
    tx = pd.read_csv(TRANSACTION_FILE)
    identity = pd.read_csv(IDENTITY_FILE)

    # Row counts
    report(
        "transaction row count > 500,000",
        len(tx) > 500_000,
        f"{len(tx):,} rows",
    )
    report(
        "identity row count > 100,000",
        len(identity) > 100_000,
        f"{len(identity):,} rows",
    )

    # Required columns
    required = ["TransactionID", "isFraud", "TransactionAmt", "TransactionDT"]
    missing = [c for c in required if c not in tx.columns]
    report(
        "required columns present",
        len(missing) == 0,
        f"missing: {missing}" if missing else ", ".join(required),
    )

    if "isFraud" in tx.columns:
        unique_labels = set(tx["isFraud"].dropna().unique())
        report(
            "isFraud values are only 0 and 1",
            unique_labels <= {0, 1},
            f"found: {unique_labels}" if not unique_labels <= {0, 1} else "0 and 1 only",
        )

        fraud_rate = tx["isFraud"].mean()
        report(
            "fraud rate between 1% and 10%",
            0.01 <= fraud_rate <= 0.10,
            f"{fraud_rate:.2%}",
        )

    if "TransactionAmt" in tx.columns:
        neg_count = int((tx["TransactionAmt"] < 0).sum())
        report(
            "no negative transaction amounts",
            neg_count == 0,
            f"{neg_count} negative values found" if neg_count else "all amounts >= 0",
        )

    if "TransactionDT" in tx.columns:
        is_numeric = pd.api.types.is_numeric_dtype(tx["TransactionDT"])
        report(
            "TransactionDT is numeric",
            is_numeric,
            str(tx["TransactionDT"].dtype),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate IEEE-CIS fraud dataset.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check file existence only; skip loading data.",
    )
    args = parser.parse_args()

    print("\nPayShield AI — Data Validation")
    print("=" * 40)

    files_ok = check_files_exist()

    if not args.dry_run:
        if not files_ok:
            print(f"\n{RED}Skipping data checks — files not found.{RESET}\n")
        else:
            run_full_checks()

    total = len(_all_passed)
    passed = sum(_all_passed)
    failed = total - passed

    print("=" * 40)
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  {RED}({failed} failed){RESET}")
    else:
        print(f"  {GREEN}(all passed){RESET}")
    print()

    sys.exit(0 if all(_all_passed) else 1)


if __name__ == "__main__":
    main()
