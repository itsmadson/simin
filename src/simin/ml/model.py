"""Models for the meta-labelling layer.

Two implementations behind one interface:

* ``LogisticModel`` — L2-regularised logistic regression, pure NumPy, always
  available. It is the benchmark the fancy model must beat. If gradient boosting
  cannot beat well-regularised logistic regression out-of-sample, the features
  are noise and no amount of model capacity will fix that.
* ``GradientBoostingModel`` — LightGBM when installed, with conservative
  regularisation defaults suited to a few thousand noisy samples.

Both are probability models: they output P(the primary signal pays off), which
the risk engine consumes as confidence. Neither is allowed to place an order.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


class ProbabilityModel(Protocol):
    def fit(
        self, x: Sequence[Sequence[float]], y: Sequence[int], weights: Sequence[float] | None = None
    ) -> None: ...
    def predict_proba(self, x: Sequence[Sequence[float]]) -> list[float]: ...


@dataclass(slots=True)
class LogisticModel:
    """Logistic regression by gradient descent with sample weights.

    Features are standardised using **training statistics only**, and those
    statistics are stored: re-fitting the scaler on test data is textbook leakage
    and would quietly inflate every score downstream.
    """

    l2: float = 1.0
    learning_rate: float = 0.1
    epochs: int = 400
    coef_: np.ndarray | None = None
    intercept_: float = 0.0
    mean_: np.ndarray | None = None
    scale_: np.ndarray | None = None

    def _standardize(self, x: np.ndarray, *, fit: bool) -> np.ndarray:
        if fit:
            self.mean_ = x.mean(axis=0)
            scale = x.std(axis=0)
            scale[scale == 0] = 1.0
            self.scale_ = scale
        if self.mean_ is None or self.scale_ is None:
            raise RuntimeError("model is not fitted")
        standardized: np.ndarray = (x - self.mean_) / self.scale_
        return standardized

    def fit(
        self, x: Sequence[Sequence[float]], y: Sequence[int], weights: Sequence[float] | None = None
    ) -> None:
        xa = np.asarray(x, dtype=float)
        ya = np.asarray(y, dtype=float)
        if xa.ndim != 2 or len(xa) != len(ya):
            raise ValueError("x must be 2-D and align with y")
        w = np.asarray(weights, dtype=float) if weights is not None else np.ones(len(ya))
        w = w / w.sum() * len(w)
        xs = self._standardize(xa, fit=True)
        n_features = xs.shape[1]
        coef = np.zeros(n_features)
        intercept = 0.0
        for _ in range(self.epochs):
            z = xs @ coef + intercept
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            error = (p - ya) * w
            grad = xs.T @ error / len(ya) + self.l2 * coef / len(ya)
            coef -= self.learning_rate * grad
            intercept -= self.learning_rate * error.mean()
        self.coef_ = coef
        self.intercept_ = float(intercept)

    def predict_proba(self, x: Sequence[Sequence[float]]) -> list[float]:
        if self.coef_ is None:
            raise RuntimeError("model is not fitted")
        xs = self._standardize(np.asarray(x, dtype=float), fit=False)
        z = xs @ self.coef_ + self.intercept_
        return [float(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, v))))) for v in z]


@dataclass(slots=True)
class GradientBoostingModel:
    """LightGBM wrapper with defaults tuned for small, noisy financial samples.

    Shallow trees, heavy subsampling and a minimum leaf size: capacity is the
    enemy here, not the goal. Falls back to logistic regression when LightGBM is
    not installed, so the pipeline never silently changes shape.
    """

    n_estimators: int = 200
    learning_rate: float = 0.03
    num_leaves: int = 7
    min_child_samples: int = 50
    subsample: float = 0.7
    colsample: float = 0.7
    seed: int = 7
    _impl: object | None = field(default=None, repr=False)
    _fallback: LogisticModel | None = field(default=None, repr=False)

    @property
    def is_fallback(self) -> bool:
        return self._fallback is not None

    def fit(
        self, x: Sequence[Sequence[float]], y: Sequence[int], weights: Sequence[float] | None = None
    ) -> None:
        try:
            import lightgbm as lgb
        except ImportError:
            self._fallback = LogisticModel()
            self._fallback.fit(x, y, weights)
            return
        self._impl = lgb.LGBMClassifier(
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples,
            subsample=self.subsample,
            subsample_freq=1,
            colsample_bytree=self.colsample,
            random_state=self.seed,
            verbosity=-1,
        )
        self._impl.fit(np.asarray(x), np.asarray(y), sample_weight=weights)  # type: ignore[attr-defined]

    def predict_proba(self, x: Sequence[Sequence[float]]) -> list[float]:
        if self._fallback is not None:
            return self._fallback.predict_proba(x)
        if self._impl is None:
            raise RuntimeError("model is not fitted")
        probs = self._impl.predict_proba(np.asarray(x))  # type: ignore[attr-defined]
        return [float(p[1]) for p in probs]


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of the probabilities. Calibration, not ranking.

    A model used for position sizing must be *calibrated*: when it says 60%, it
    has to be right 60% of the time. AUC says nothing about that, which is why
    a strategy sized off an uncalibrated score bets confidently on noise.
    """
    if not probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes, strict=False)) / len(
        probabilities
    )


def auc(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Rank-based AUC via the Mann-Whitney statistic."""
    pos = [p for p, o in zip(probabilities, outcomes, strict=False) if o == 1]
    neg = [p for p, o in zip(probabilities, outcomes, strict=False) if o == 0]
    if not pos or not neg:
        return 0.5
    ordered = sorted(zip(probabilities, outcomes, strict=False))
    ranks: dict[int, float] = {}
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    rank_sum = sum(ranks[k] for k, (_, o) in enumerate(ordered) if o == 1)
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def reliability_table(
    probabilities: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> list[tuple[float, float, int]]:
    """Predicted vs realised frequency per probability bucket.

    Shipped to the dashboard so an overconfident model is visible rather than
    merely statistically detectable.
    """
    table: list[tuple[float, float, int]] = []
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        bucket = [
            o
            for p, o in zip(probabilities, outcomes, strict=False)
            if low <= p < high or (b == bins - 1 and p == 1.0)
        ]
        if bucket:
            table.append(((low + high) / 2, sum(bucket) / len(bucket), len(bucket)))
    return table
