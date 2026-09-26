"""Local immutable research storage and scikit-learn adapter."""

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from invest_lab.domain.research import canonical, content_hash, coverage, timestamp


class SQLiteResearchRepository:
    def __init__(self, db_path: str | Path, artifact_dir: str | Path | None = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = (
            Path(artifact_dir) if artifact_dir else self.path.parent / "research_artifacts"
        )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            for table in ("datasets", "experiments", "models"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS research_{table} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self, table, value, *, mutable=False):
        with self.connect() as db:
            existing = db.execute(
                f"SELECT payload FROM research_{table} WHERE id=?", (value["id"],)
            ).fetchone()
            if existing and not mutable:
                return json.loads(existing[0])
            db.execute(
                f"INSERT INTO research_{table} VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (value["id"], canonical(value), value["created_at"]),
            )
        return value

    def _get(self, table, identifier):
        with self.connect() as db:
            row = db.execute(
                f"SELECT payload FROM research_{table} WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise LookupError("연구 산출물을 찾을 수 없습니다.")
        return json.loads(row[0])

    def _list(self, table):
        with self.connect() as db:
            rows = db.execute(
                f"SELECT payload FROM research_{table} ORDER BY created_at DESC,id"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_dataset(self, dataset):
        digest = content_hash(dataset)
        value = {
            "id": digest[:24],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "hash": digest,
            "coverage": coverage(dataset),
            "data": dataset,
        }
        value = self._save("datasets", value)
        directory = self.artifact_dir / value["id"]
        directory.mkdir(exist_ok=True)
        destination = directory / "dataset.json"
        if not destination.exists():
            temporary = directory / (uuid4().hex + ".tmp")
            temporary.write_text(canonical(value), encoding="utf-8")
            temporary.replace(destination)
        return self._summary(value)

    def get_dataset(self, dataset_id):
        return self._get("datasets", dataset_id)

    def _summary(self, value):
        return {
            "id": value["id"],
            "created_at": value["created_at"],
            "hash": value["hash"],
            "name": value["data"]["name"],
            "source_kind": value["data"]["source_kind"],
            "coverage": value["coverage"],
            "start_session": value["data"]["sessions"][0],
            "end_session": value["data"]["sessions"][-1],
        }

    def list_datasets(self):
        return [self._summary(r) for r in self._list("datasets")]

    def save_experiment(self, experiment):
        return self._save("experiments", experiment)

    def get_experiment(self, experiment_id):
        return self._get("experiments", experiment_id)

    def list_experiments(self):
        return [{k: v for k, v in e.items() if k != "result"} for e in self._list("experiments")]

    def save_model(self, model):
        return self._save("models", model, mutable=True)

    def list_models(self):
        return self._list("models")

    def export_dataset(self, dataset_id):
        import pyarrow as pa
        import pyarrow.parquet as pq

        stored = self.get_dataset(dataset_id)
        directory = self.artifact_dir / dataset_id
        manifest = {
            "dataset_id": dataset_id,
            "dataset_hash": stored["hash"],
            "tables": {},
            "coverage": stored["coverage"],
            "source_kind": stored["data"]["source_kind"],
        }
        for name in (
            "bars",
            "financial_facts",
            "macro",
            "actions",
            "universe",
            "etf_metadata",
            "benchmarks",
        ):
            rows = stored["data"][name]
            if rows:
                path = directory / (name + ".parquet")
                if not path.exists():
                    temporary = directory / (uuid4().hex + ".tmp")
                    pq.write_table(pa.Table.from_pylist(rows), temporary)
                    temporary.replace(path)
                import hashlib

                manifest["tables"][name] = {
                    "file": path.name,
                    "rows": len(rows),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
        (directory / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
        return manifest


TECHNICAL_COLUMNS = [
    "return7",
    "return30",
    "return90",
    "rsi14",
    "atr_pct",
    "pullback_atr",
    "relative63",
    "volume_ratio",
    "volatility20",
]
FINANCIAL_COLUMNS = [
    "earnings_yield",
    "book_yield",
    "roe",
    "operating_margin",
    "cashflow_yield",
    "debt_to_equity",
]
MACRO_COLUMNS = [
    "VIXCLS",
    "VXNCLS",
    "DEXKOUS_change",
    "yield_curve",
    "BAMLH0A0HYM2",
    "breadth",
    "vix_atr_interaction",
]


def purged_date_split(rows, test_dates, *, calibration_dates=15, min_train_dates=20):
    """Date-group split with mature labels, including calibration -> test purge."""
    if not test_dates:
        raise ValueError("평가 날짜가 없습니다.")
    first_test = min(test_dates)
    earlier_dates = sorted({r["session"] for r in rows if r["session"] < first_test})
    if len(earlier_dates) <= calibration_dates + min_train_dates:
        raise ValueError("purge/calibration 이전 학습 날짜가 부족합니다.")
    calibration_sessions = set(earlier_dates[-calibration_dates:])
    calibration_start = min(calibration_sessions)
    train_cutoff = timestamp(calibration_start + "T00:00:00+00:00")
    test_cutoff = timestamp(first_test + "T00:00:00+00:00")
    train = [
        r
        for r in rows
        if r["session"] < calibration_start
        and r["label_end"] < calibration_start
        and timestamp(r["label_available_at"]) < train_cutoff
    ]
    calibration = [
        r
        for r in rows
        if r["session"] in calibration_sessions
        and r["label_end"] < first_test
        and timestamp(r["label_available_at"]) < test_cutoff
    ]
    test = [r for r in rows if r["session"] in test_dates]
    if len({r["session"] for r in train}) < min_train_dates or not calibration or not test:
        raise ValueError("레이블 만기 purge 후 학습/보정/평가 표본이 부족합니다.")
    return train, calibration, test


class SklearnResearchLearner:
    def train(self, rows, parameters):
        import numpy as np
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.metrics import (
            average_precision_score,
            brier_score_loss,
            log_loss,
            mean_squared_error,
            roc_auc_score,
        )
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        model_type = parameters.get("model_type", "ridge")
        if model_type not in {"ridge", "logistic"}:
            raise ValueError("model_type은 ridge/logistic입니다.")
        ablation = parameters.get("ablation", "technical")
        if ablation not in {"technical", "financial", "macro", "all"}:
            raise ValueError("ablation은 technical/financial/macro/all입니다.")
        columns = list(TECHNICAL_COLUMNS)
        if ablation in {"financial", "all"}:
            columns += FINANCIAL_COLUMNS
        if ablation in {"macro", "all"}:
            columns += MACRO_COLUMNS
        dates = sorted({r["session"] for r in rows})
        holdout_count = int(parameters.get("holdout_dates", max(20, len(dates) // 6)))
        fold_count = int(parameters.get("folds", 3))
        cal_count = int(parameters.get("calibration_dates", 20))
        if holdout_count < 5 or fold_count < 1 or cal_count < 5:
            raise ValueError("시간 분할 크기가 너무 작습니다.")
        if len(dates) < holdout_count + cal_count + 50:
            raise ValueError("시간순 검증을 위한 실제 날짜 수가 부족합니다.")
        development = dates[:-holdout_count]
        holdout = dates[-holdout_count:]
        validation_size = max(5, min(20, (len(development) - cal_count - 40) // fold_count))
        windows = [
            development[
                len(development) - (fold_count - i) * validation_size : len(development)
                - (fold_count - i - 1) * validation_size
            ]
            for i in range(fold_count)
        ]

        def matrix(items):
            return np.array(
                [
                    [
                        r["features"].get(c) if r["features"].get(c) is not None else np.nan
                        for c in columns
                    ]
                    for r in items
                ],
                dtype=float,
            )

        def targets(items):
            a = np.array([r["target"] for r in items])
            return (a > 0).astype(int) if model_type == "logistic" else a

        def fit(train, calibration, strength):
            estimator = (
                Ridge(alpha=strength)
                if model_type == "ridge"
                else LogisticRegression(
                    C=strength, random_state=int(parameters.get("seed", 42)), max_iter=1000
                )
            )
            y = targets(train)
            if model_type == "logistic" and len(set(y)) < 2:
                raise ValueError("로지스틱 학습에 두 클래스가 필요합니다.")
            pipe = Pipeline(
                [
                    ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                    ("scale", StandardScaler()),
                    ("model", estimator),
                ]
            )
            pipe.fit(matrix(train), y)
            calibrator = None
            if model_type == "logistic":
                cy = targets(calibration)
                if len(set(cy)) < 2:
                    raise ValueError("확률 보정 구간에 두 클래스가 필요합니다.")
                calibrator = LogisticRegression(
                    C=1.0, random_state=int(parameters.get("seed", 42)), max_iter=1000
                )
                calibrator.fit(pipe.decision_function(matrix(calibration)).reshape(-1, 1), cy)
            return pipe, calibrator

        def predictions(pipe, calibrator, items):
            x = matrix(items)
            return (
                calibrator.predict_proba(pipe.decision_function(x).reshape(-1, 1))[:, 1]
                if calibrator
                else pipe.predict(x)
            )

        def metrics(items, prediction):
            y = targets(items)
            by_date = {}
            for r, p in zip(items, prediction):
                by_date.setdefault(r["session"], []).append((float(p), r["target"], r["symbol"]))
            ics, top_returns = [], []
            for values in by_date.values():
                ranked = sorted(values, key=lambda v: (-v[0], v[2]))
                top_returns.append(sum(v[1] for v in ranked[:10]) / min(10, len(ranked)))
                if (
                    len(values) > 2
                    and len({v[0] for v in values}) > 1
                    and len({v[1] for v in values}) > 1
                ):
                    # Average ranks handle ties without a pandas/scipy domain dependency.
                    def ranks(vals):
                        return [
                            sum(v < x for v in vals) + (sum(v == x for v in vals) - 1) / 2
                            for x in vals
                        ]

                    ics.append(
                        float(
                            np.corrcoef(
                                ranks([v[0] for v in values]), ranks([v[1] for v in values])
                            )[0, 1]
                        )
                    )
            result = {
                "samples": len(items),
                "decision_dates": len(by_date),
                "rank_ic": float(np.mean(ics)) if ics else None,
                "top10_mean_label_return": float(np.mean(top_returns)),
                "mse": float(mean_squared_error(y, prediction)),
                "label_returns_are_not_a_portfolio_backtest": True,
            }
            if model_type == "logistic":
                result.update(
                    {
                        "brier": float(brier_score_loss(y, prediction)),
                        "log_loss": float(log_loss(y, prediction, labels=[0, 1])),
                        "roc_auc": float(roc_auc_score(y, prediction))
                        if len(set(y)) == 2
                        else None,
                        "average_precision": float(average_precision_score(y, prediction))
                        if y.sum()
                        else None,
                    }
                )
                bins = []
                for low in np.linspace(0, 0.8, 5):
                    mask = (prediction >= low) & (
                        prediction <= low + 0.2 if low >= 0.8 else prediction < low + 0.2
                    )
                    if mask.any():
                        bins.append(
                            {
                                "count": int(mask.sum()),
                                "predicted": float(prediction[mask].mean()),
                                "observed": float(y[mask].mean()),
                            }
                        )
                result["calibration_bins"] = bins
            # Date-block bootstrap: resample blocks of decision dates, never individual stock rows.
            rng = np.random.default_rng(int(parameters.get("seed", 42)))
            blocks = [top_returns[i : i + 5] for i in range(0, len(top_returns), 5)]
            estimates = [
                np.mean([v for j in rng.integers(0, len(blocks), len(blocks)) for v in blocks[j]])
                for _ in range(100)
            ]
            result["date_block_interval95"] = [
                float(x) for x in np.quantile(estimates, [0.025, 0.975])
            ]
            return result

        trials = []
        for strength in (0.1, 1.0, 10.0):
            reports = []
            for window in windows:
                train, calibration, validation = purged_date_split(
                    rows, window, calibration_dates=cal_count
                )
                pipe, calibrator = fit(train, calibration, strength)
                result = metrics(validation, predictions(pipe, calibrator, validation))
                reports.append(
                    {
                        "train_end": max(r["session"] for r in train),
                        "train_label_end": max(r["label_end"] for r in train),
                        "calibration_start": min(r["session"] for r in calibration),
                        "calibration_label_end": max(r["label_end"] for r in calibration),
                        "test_start": window[0],
                        "test_end": window[-1],
                        "metrics": result,
                    }
                )
            criterion = "log_loss" if model_type == "logistic" else "mse"
            trials.append(
                {
                    "strength": strength,
                    "score": sum(r["metrics"][criterion] for r in reports) / len(reports),
                    "folds": reports,
                }
            )
        chosen = min(trials, key=lambda t: (t["score"], t["strength"]))
        train, calibration, test = purged_date_split(rows, holdout, calibration_dates=cal_count)
        pipe, calibrator = fit(train, calibration, chosen["strength"])
        prediction = predictions(pipe, calibrator, test)
        model = pipe.named_steps["model"]
        artifact = {
            "model_type": model_type,
            "ablation": ablation,
            "feature_names": columns,
            "imputation": pipe.named_steps["impute"].statistics_.tolist(),
            "mean": pipe.named_steps["scale"].mean_.tolist(),
            "scale": pipe.named_steps["scale"].scale_.tolist(),
            "coefficients": np.asarray(model.coef_).reshape(-1).tolist(),
            "intercept": float(np.asarray(model.intercept_).reshape(-1)[0]),
            "calibration": {
                "coefficient": float(calibrator.coef_[0, 0]),
                "intercept": float(calibrator.intercept_[0]),
            }
            if calibrator
            else None,
            "fit_cutoff": min(holdout) + "T00:00:00+00:00",
            "train_start": min(r["session"] for r in train),
            "train_end": max(r["session"] for r in train),
            "calibration_end": max(r["session"] for r in calibration),
            "max_training_label_available_at": max(
                (r["label_available_at"] for r in train + calibration), key=timestamp
            ),
            "holdout_start": holdout[0],
            "holdout_end": holdout[-1],
            "seed": int(parameters.get("seed", 42)),
            "strength": chosen["strength"],
            "prediction_contract": "positive_cost_adjusted_benchmark_excess_return_probability"
            if model_type == "logistic"
            else "cost_adjusted_benchmark_excess_return",
        }
        baseline = np.full(len(test), targets(train).mean())
        feature_coverage = {
            c: sum(r["features"].get(c) is not None for r in train) / len(train) for c in columns
        }
        evaluation = metrics(test, prediction)
        baseline_evaluation = metrics(test, baseline)
        criterion = "log_loss" if model_type == "logistic" else "mse"
        return {
            "artifact": artifact,
            "validation_trials": trials,
            "holdout": evaluation,
            "constant_baseline_holdout": metrics(test, baseline),
            "feature_coverage": feature_coverage,
            "evaluation_complete": True,
            "outperforms_constant_baseline": evaluation[criterion] < baseline_evaluation[criterion],
            "predictions": [
                {
                    "session": r["session"],
                    "symbol": r["symbol"],
                    "prediction": float(p),
                    "target": r["target"],
                    "label_end": r["label_end"],
                }
                for r, p in zip(test, prediction)
            ],
            "selection": "candidate_only_manual_review_required",
            "final_test_used_for_selection": False,
        }

    def predict(self, artifact, features):
        count = len(artifact["feature_names"])
        for name in ("imputation", "mean", "scale", "coefficients"):
            if len(artifact[name]) != count or any(
                isinstance(x, bool) or not math.isfinite(float(x)) for x in artifact[name]
            ):
                raise ValueError("모델 전처리/계수 산출물 형식이 올바르지 않습니다.")
        if any(s <= 0 for s in artifact["scale"]) or not math.isfinite(artifact["intercept"]):
            raise ValueError("모델 scale/intercept가 올바르지 않습니다.")
        result = []
        for row in features:
            values = [
                row.get(c) if row.get(c) is not None else artifact["imputation"][i]
                for i, c in enumerate(artifact["feature_names"])
            ]
            if any(isinstance(x, bool) or not math.isfinite(float(x)) for x in values):
                raise ValueError("모델 추론 피처에는 유한한 숫자가 필요합니다.")
            raw = artifact["intercept"] + sum(
                (float(x) - m) / s * c
                for x, m, s, c in zip(
                    values, artifact["mean"], artifact["scale"], artifact["coefficients"]
                )
            )
            if artifact["calibration"]:
                raw = (
                    raw * artifact["calibration"]["coefficient"]
                    + artifact["calibration"]["intercept"]
                )
                raw = 1 / (1 + math.exp(-max(-700, min(700, raw))))
            result.append(raw)
        return result


def build_research_service(db_path, artifact_dir=None):
    from invest_lab.application.research import ResearchService

    return ResearchService(
        SQLiteResearchRepository(db_path, artifact_dir), SklearnResearchLearner()
    )
