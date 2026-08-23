"""Cross-validation for time series with overlapping labels.

Plain K-fold on financial data leaks in two directions: a training fold can sit
*after* its test fold, and labels that span the fold boundary appear in both. The
result is a model that scores beautifully and fails immediately.

Purging removes training samples whose label windows overlap the test set;
embargoing additionally drops samples immediately after it, because serial
correlation makes the bars just past the boundary nearly the same observation.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from simin.ml.labeling import Label


@dataclass(frozen=True, slots=True)
class Split:
    train: list[int]
    test: list[int]

    def __post_init__(self) -> None:
        if set(self.train) & set(self.test):
            raise ValueError("train and test overlap — this split leaks")


def purged_kfold(
    labels: Sequence[Label], n_splits: int = 5, embargo_pct: float = 0.01
) -> Iterator[Split]:
    """Contiguous test folds, with purge and embargo applied to the training set."""
    if n_splits < 2:
        raise ValueError("need at least 2 splits")
    n = len(labels)
    if n < n_splits:
        return
    fold_size = n // n_splits
    embargo = int(n * embargo_pct)
    for k in range(n_splits):
        start = k * fold_size
        stop = n if k == n_splits - 1 else (k + 1) * fold_size
        test = list(range(start, stop))
        test_start_bar = labels[start].index
        test_end_bar = labels[stop - 1].end_index

        train: list[int] = []
        for i, label in enumerate(labels):
            if start <= i < stop:
                continue
            # purge: any label whose window overlaps the test window is out
            if label.end_index >= test_start_bar and label.index <= test_end_bar:
                continue
            # embargo: drop the samples immediately following the test window
            if 0 < label.index - test_end_bar <= embargo:
                continue
            train.append(i)
        if train and test:
            yield Split(train=train, test=test)


def walk_forward_splits(
    n: int, *, train_size: int, test_size: int, step: int | None = None, anchored: bool = False
) -> list[Split]:
    """Rolling (or anchored) train/test windows in chronological order.

    This is the shape of the real question: *would this have worked on data that
    did not exist when the parameters were chosen?*
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("window sizes must be positive")
    step = step or test_size
    splits: list[Split] = []
    start = 0
    while start + train_size + test_size <= n:
        train_start = 0 if anchored else start
        train = list(range(train_start, start + train_size))
        test = list(range(start + train_size, start + train_size + test_size))
        splits.append(Split(train=train, test=test))
        start += step
    return splits


def combinatorial_splits(
    n_groups: int, n_test_groups: int = 2
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """All ways to choose test groups out of N — the basis of the PBO estimate.

    Bailey et al. compute the probability of backtest overfitting by checking how
    often the configuration that ranked best in-sample lands below the median
    out-of-sample across many such recombinations.
    """
    groups = tuple(range(n_groups))
    out: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for test in itertools.combinations(groups, n_test_groups):
        train = tuple(g for g in groups if g not in test)
        out.append((train, test))
    return out


def probability_of_backtest_overfitting(
    in_sample_ranks: Sequence[Sequence[float]],
    out_of_sample_ranks: Sequence[Sequence[float]],
) -> float:
    """Fraction of recombinations where the in-sample winner underperforms the
    out-of-sample median.

    A PBO above ~0.5 means the selection procedure is worse than picking at
    random — the "best" configuration is reliably the one that fit the noise best.
    """
    if not in_sample_ranks:
        return 1.0
    failures = 0
    total = 0
    for is_scores, oos_scores in zip(in_sample_ranks, out_of_sample_ranks, strict=False):
        if not is_scores or len(is_scores) != len(oos_scores):
            continue
        best = max(range(len(is_scores)), key=lambda i: is_scores[i])
        ordered = sorted(oos_scores)
        median = ordered[len(ordered) // 2]
        if oos_scores[best] < median:
            failures += 1
        total += 1
    return failures / total if total else 1.0
