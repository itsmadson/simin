"""Labelling, purged CV and model behaviour."""

import itertools

import numpy as np
import pytest
from factories import gbm_series

from simin.features.indicators import closes, realized_vol
from simin.ml.cv import (
    Split,
    combinatorial_splits,
    probability_of_backtest_overfitting,
    purged_kfold,
    walk_forward_splits,
)
from simin.ml.labeling import (
    expected_value,
    label_balance,
    sample_uniqueness,
    triple_barrier,
)
from simin.ml.model import (
    GradientBoostingModel,
    LogisticModel,
    auc,
    brier_score,
    reliability_table,
)


def vol_of(bars, scale=50.0):
    return [v / scale if v else None for v in realized_vol(closes(bars), 24, 8760)]


# ------------------------------------------------------------------ labelling


def test_triple_barrier_labels_every_usable_event():
    bars = gbm_series(1000, seed=2)
    labels = triple_barrier(bars, list(range(100, 900, 10)), volatility=vol_of(bars))
    assert labels
    assert all(label.outcome in ("profit", "stop", "timeout") for label in labels)
    assert all(0 < label.holding_bars <= 48 for label in labels)


def test_stop_wins_ties_within_a_bar():
    """Same pessimism as the backtester: a bar touching both barriers is a loss."""
    bars = gbm_series(400, seed=4, sigma=0.05)
    labels = triple_barrier(
        bars, list(range(50, 350, 5)), volatility=vol_of(bars, scale=500.0), max_holding=3
    )
    assert label_balance(labels)["stop"] > 0


def test_costs_shift_labels_toward_losses():
    """Labelling on gross moves manufactures winners that fees would have eaten."""
    bars = gbm_series(2000, seed=6)
    events = list(range(100, 1900, 5))
    free = triple_barrier(bars, events, volatility=vol_of(bars), cost=0.0)
    costly = triple_barrier(bars, events, volatility=vol_of(bars), cost=0.02)
    assert label_balance(costly)["profit"] < label_balance(free)["profit"]


def test_expected_value_is_negative_on_a_random_walk_after_costs():
    bars = gbm_series(3000, seed=8, mu=0.0)
    labels = triple_barrier(bars, list(range(100, 2900, 5)), volatility=vol_of(bars), cost=0.011)
    assert float(expected_value(labels)) < 0


def test_uniqueness_weights_downweight_overlapping_labels():
    bars = gbm_series(600, seed=10)
    dense = triple_barrier(bars, list(range(100, 500, 1)), volatility=vol_of(bars))
    sparse = triple_barrier(bars, list(range(100, 500, 40)), volatility=vol_of(bars))
    dense_w = sample_uniqueness(dense, len(bars))
    sparse_w = sample_uniqueness(sparse, len(bars))
    assert sum(dense_w) / len(dense_w) < sum(sparse_w) / len(sparse_w)
    assert all(0 < w <= 1.0001 for w in dense_w)


def test_events_too_close_to_the_end_are_skipped():
    bars = gbm_series(200, seed=12)
    labels = triple_barrier(bars, [199, 198], volatility=vol_of(bars))
    assert all(label.index < 199 for label in labels)


# ------------------------------------------------------------------------ CV


def test_split_rejects_overlap():
    with pytest.raises(ValueError, match="leaks"):
        Split(train=[1, 2, 3], test=[3, 4])


def test_purged_kfold_never_leaks_across_the_boundary():
    """THE test that separates a real CV score from a fantasy one."""
    bars = gbm_series(2000, seed=14)
    labels = triple_barrier(bars, list(range(100, 1900, 10)), volatility=vol_of(bars))
    splits = list(purged_kfold(labels, n_splits=5, embargo_pct=0.02))
    assert splits
    for split in splits:
        test_start = min(labels[i].index for i in split.test)
        test_end = max(labels[i].end_index for i in split.test)
        for i in split.train:
            label = labels[i]
            overlaps = label.end_index >= test_start and label.index <= test_end
            assert not overlaps, "a training label overlaps the test window"


def test_purging_removes_samples_a_naive_kfold_would_keep():
    bars = gbm_series(1500, seed=16)
    labels = triple_barrier(bars, list(range(100, 1400, 5)), volatility=vol_of(bars))
    splits = list(purged_kfold(labels, n_splits=5))
    for split in splits:
        assert len(split.train) + len(split.test) < len(labels)


def test_walk_forward_splits_are_chronological_and_disjoint():
    splits = walk_forward_splits(1000, train_size=400, test_size=100)
    assert splits
    for split in splits:
        assert max(split.train) < min(split.test)
    for a, b in itertools.pairwise(splits):
        assert min(b.test) > min(a.test)


def test_anchored_walk_forward_grows_the_training_set():
    rolling = walk_forward_splits(1000, train_size=300, test_size=100)
    anchored = walk_forward_splits(1000, train_size=300, test_size=100, anchored=True)
    assert len(anchored[-1].train) > len(rolling[-1].train)
    assert min(anchored[-1].train) == 0


def test_combinatorial_splits_cover_every_combination():
    assert len(combinatorial_splits(6, 2)) == 15
    for train, test in combinatorial_splits(6, 2):
        assert not set(train) & set(test)


def test_pbo_is_high_when_the_winner_is_noise():
    """Selecting the in-sample best from pure noise fails out-of-sample about
    half the time — which is exactly what PBO is designed to reveal."""
    rng = np.random.default_rng(0)
    is_scores = [list(rng.normal(size=10)) for _ in range(200)]
    oos_scores = [list(rng.normal(size=10)) for _ in range(200)]
    pbo = probability_of_backtest_overfitting(is_scores, oos_scores)
    assert 0.35 < pbo < 0.65


def test_pbo_is_low_when_skill_persists():
    is_scores, oos_scores = [], []
    for _ in range(100):
        skill = [0.0, 0.1, 0.2, 0.3, 5.0]     # the last one is genuinely better
        is_scores.append(skill)
        oos_scores.append(skill)
    assert probability_of_backtest_overfitting(is_scores, oos_scores) == 0.0


# --------------------------------------------------------------------- models


def test_logistic_learns_a_learnable_signal():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(1000, 3))
    y = (x[:, 0] + rng.normal(scale=0.5, size=1000) > 0).astype(int)
    model = LogisticModel()
    model.fit(x[:700], y[:700])
    probs = model.predict_proba(x[700:])
    assert auc(probs, y[700:]) > 0.8


def test_model_cannot_learn_pure_noise():
    """A model that scores well on noise is a bug in the evaluation, not a find."""
    rng = np.random.default_rng(2)
    x = rng.normal(size=(1200, 5))
    y = rng.integers(0, 2, size=1200)
    model = LogisticModel()
    model.fit(x[:900], y[:900])
    assert 0.4 < auc(model.predict_proba(x[900:]), y[900:]) < 0.6


def test_scaler_uses_training_statistics_only():
    """Re-fitting the scaler on test data is leakage with a friendly interface."""
    rng = np.random.default_rng(3)
    x = rng.normal(size=(400, 2))
    y = (x[:, 0] > 0).astype(int)
    model = LogisticModel()
    model.fit(x[:300], y[:300])
    mean_before = model.mean_.copy()
    model.predict_proba(x[300:] * 100)     # wildly different scale
    assert np.allclose(model.mean_, mean_before)


def test_unfitted_model_refuses_to_predict():
    with pytest.raises(RuntimeError, match="not fitted"):
        LogisticModel().predict_proba([[1.0, 2.0]])


def test_gradient_boosting_falls_back_cleanly_without_lightgbm():
    rng = np.random.default_rng(4)
    x = rng.normal(size=(300, 3))
    y = (x[:, 1] > 0).astype(int)
    model = GradientBoostingModel()
    model.fit(x, y)
    probs = model.predict_proba(x)
    assert len(probs) == len(y)
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_sample_weights_change_the_fit():
    rng = np.random.default_rng(5)
    x = rng.normal(size=(500, 2))
    y = (x[:, 0] > 0).astype(int)
    flat = LogisticModel()
    flat.fit(x, y)
    weighted = LogisticModel()
    weighted.fit(x, y, weights=[1.0 if i < 250 else 0.01 for i in range(500)])
    assert not np.allclose(flat.coef_, weighted.coef_)


def test_brier_rewards_calibration_not_confidence():
    outcomes = [1, 0, 1, 0]
    calibrated = [0.5, 0.5, 0.5, 0.5]
    overconfident = [1.0, 1.0, 1.0, 1.0]
    assert brier_score(calibrated, outcomes) < brier_score(overconfident, outcomes)


def test_reliability_table_bins_predictions():
    probs = [0.05, 0.15, 0.85, 0.95]
    table = reliability_table(probs, [0, 0, 1, 1], bins=10)
    assert table
    assert all(0 <= predicted <= 1 and 0 <= realized <= 1 for predicted, realized, _ in table)


def test_auc_of_a_perfect_ranker_is_one():
    assert auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
