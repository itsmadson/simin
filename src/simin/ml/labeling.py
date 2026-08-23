"""Triple-barrier labelling and sample weighting.

Predicting the next bar's return is the standard approach and it is close to
useless: the signal-to-noise ratio is so low that an MSE-optimal model learns to
predict zero. The triple-barrier method instead labels each *event* by which
barrier it reaches first — profit target, stop, or a time limit — so the target
matches how the strategy actually exits.

Barriers are scaled by volatility, not fixed percentages: a 2% target is a
routine hour for one asset and a month for another.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from simin.types import Bar


@dataclass(frozen=True, slots=True)
class Label:
    """Outcome of one event.

    ``value`` is 1 when the profit barrier was touched first, 0 otherwise. For
    meta-labelling this reads as "was taking this signal the right call?".
    """

    index: int
    ts: datetime
    value: int
    outcome: str  # "profit" | "stop" | "timeout"
    holding_bars: int
    realized_return: float
    end_index: int


def triple_barrier(
    bars: Sequence[Bar],
    event_indices: Sequence[int],
    *,
    volatility: Sequence[float | None],
    profit_multiple: float = 2.0,
    stop_multiple: float = 1.0,
    max_holding: int = 48,
    cost: float = 0.0,
    direction: int = 1,
) -> list[Label]:
    """Label each event by the first barrier its path touches.

    ``cost`` is subtracted from the profit barrier and added to the stop, so a
    "win" means a win *after fees and spread*. Labelling on gross moves and
    hoping the model learns costs later is how a promising classifier becomes an
    unprofitable strategy.

    Barrier checks are pessimistic in the same way as the backtester: if a bar's
    range touches both barriers, the stop counts.
    """
    labels: list[Label] = []
    n = len(bars)
    for idx in event_indices:
        if idx >= n - 1:
            continue
        vol = volatility[idx] if idx < len(volatility) else None
        if vol is None or vol <= 0:
            continue
        entry = float(bars[idx].close)
        # Cost makes the profit barrier harder to reach AND the stop easier: the
        # loss budget is spent on fees before the price has moved at all. Pushing
        # the stop further away instead (the intuitive sign) would make an
        # expensive venue look *safer*, which is backwards.
        if direction > 0:
            up = entry * (1 + vol * profit_multiple + cost)
            down = entry * (1 - vol * stop_multiple + cost)
        else:
            up = entry * (1 - vol * profit_multiple - cost)
            down = entry * (1 + vol * stop_multiple - cost)
        end = min(n - 1, idx + max_holding)
        outcome, hit_index = "timeout", end
        for j in range(idx + 1, end + 1):
            high, low = float(bars[j].high), float(bars[j].low)
            hit_stop = low <= down if direction > 0 else high >= down
            hit_profit = high >= up if direction > 0 else low <= up
            if hit_stop:            # checked first, on purpose
                outcome, hit_index = "stop", j
                break
            if hit_profit:
                outcome, hit_index = "profit", j
                break
        exit_price = float(bars[hit_index].close)
        realized = (exit_price / entry - 1.0) * direction - cost
        labels.append(
            Label(
                index=idx,
                ts=bars[idx].ts,
                value=1 if outcome == "profit" else 0,
                outcome=outcome,
                holding_bars=hit_index - idx,
                realized_return=realized,
                end_index=hit_index,
            )
        )
    return labels


def sample_uniqueness(labels: Sequence[Label], n_bars: int) -> list[float]:
    """Weight each label by how little its holding period overlaps with others.

    Overlapping labels are not independent observations: three events that all
    resolve over the same afternoon carry roughly one afternoon of information.
    Training as if they were three doubles down on whatever that afternoon did,
    and is a large, silent source of overfitting.
    """
    concurrency = [0] * (n_bars + 1)
    for label in labels:
        for i in range(label.index, min(label.end_index + 1, n_bars)):
            concurrency[i] += 1
    weights: list[float] = []
    for label in labels:
        span = range(label.index, min(label.end_index + 1, n_bars))
        counts = [concurrency[i] for i in span if concurrency[i] > 0]
        weights.append(sum(1.0 / c for c in counts) / len(counts) if counts else 1.0)
    return weights


def meta_labels(
    bars: Sequence[Bar],
    signals: Sequence[int],
    *,
    volatility: Sequence[float | None],
    cost: float,
    **kwargs: object,
) -> list[Label]:
    """Labels for the meta-model: did the primary strategy's signal pay off?

    The primary model keeps sole responsibility for direction. The meta-model
    only decides whether to act and how large — a far easier question, on a
    balanced target, and one where being wrong costs a skipped trade rather than
    a reversed one.
    """
    return triple_barrier(bars, signals, volatility=volatility, cost=cost, **kwargs)  # type: ignore[arg-type]


def barrier_returns(labels: Sequence[Label]) -> list[float]:
    return [label.realized_return for label in labels]


def label_balance(labels: Sequence[Label]) -> dict[str, float]:
    if not labels:
        return {"profit": 0.0, "stop": 0.0, "timeout": 0.0}
    total = len(labels)
    return {
        outcome: sum(1 for label in labels if label.outcome == outcome) / total
        for outcome in ("profit", "stop", "timeout")
    }


def expected_value(labels: Sequence[Label], cost: float = 0.0) -> Decimal:
    """Average net return per event. Negative means the signal is not worth taking."""
    if not labels:
        return Decimal(0)
    mean = sum(label.realized_return for label in labels) / len(labels)
    return Decimal(str(round(mean - cost, 8)))
