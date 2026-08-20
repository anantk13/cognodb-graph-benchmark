"""Latency statistics.

Percentiles are computed by explicit order statistics rather than by calling a
library function, for two reasons:

1.  A reader can audit exactly which sample was reported. Interpolating
    percentile implementations differ between libraries; a rank is unambiguous.
2.  The same order statistics give a distribution-free confidence interval for
    the percentile, which is what makes a p95 defensible rather than decorative.

Nothing here assumes the latency distribution has any particular shape. Latency
is routinely multimodal, so mean and standard deviation describe no real
request; see Gregg, "What the Mean Really Means".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 1.96 standard normal deviates -> a two-sided 95% interval.
_Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Percentile:
    """A percentile together with the interval we are entitled to claim for it."""

    quantile: float
    """The quantile requested, e.g. 0.95."""

    value: float
    """The observed sample at the reported rank."""

    ci_low: float | None
    """Lower bound of the distribution-free 95% CI, or None if n is too small."""

    ci_high: float | None
    """Upper bound of the distribution-free 95% CI, or None if n is too small."""

    rank: int
    """1-based rank of `value` within the sorted samples."""

    n: int
    """Number of samples the percentile was computed from."""

    @property
    def ci_width(self) -> float | None:
        """Width of the confidence interval in the same unit as `value`."""
        if self.ci_low is None or self.ci_high is None:
            return None
        return self.ci_high - self.ci_low

    @property
    def is_bounded(self) -> bool:
        """True when n was large enough for the CI to be bounded on both sides.

        A p95 from 40 samples has an upper bound that runs off the end of the
        data. Reporting it without saying so is how benchmarks overclaim.
        """
        return self.ci_low is not None and self.ci_high is not None


def _ci_ranks(n: int, quantile: float) -> tuple[int, int]:
    """1-based order-statistic ranks bounding a 95% CI for `quantile`.

    The distribution-free interval for a population quantile is the pair of
    observations at ranks

        lower = n*q - 1.96*sqrt(n*q*(1-q))
        upper = n*q + 1.96*sqrt(n*q*(1-q)) + 1

    (Bland, "An Introduction to Medical Statistics", confidence intervals for a
    population quantile). The trailing +1 on the upper rank is part of the
    formula, not an off-by-one; it is what makes the interval's coverage at
    least 95% rather than slightly under.

    Both `percentile` and `minimum_samples_for` route through this function so
    that the n they require and the n they can actually deliver cannot drift
    apart.
    """
    spread = _Z_95 * math.sqrt(n * quantile * (1.0 - quantile))
    lower = math.floor(n * quantile - spread)
    upper = math.ceil(n * quantile + spread) + 1
    return lower, upper


def minimum_samples_for(quantile: float) -> int:
    """Smallest n whose 95% CI for `quantile` is bounded on both sides.

    Below this n the upper bound runs off the end of the sample: the data
    contains no observation large enough to close the interval, so any p95
    quoted from it is a point with no defensible error bar. Solving for it
    numerically avoids hand-waving about whether "100 iterations is probably
    fine".

    Returns 110 for p95 and 563 for p99.

    Worth stating plainly in the report: the commonly suggested "at least 100
    iterations per read workload" is ten samples short of a bounded p95
    interval. At n=100 the upper bound does not exist, so a p95 quoted from a
    hundred iterations has no defensible error bar in either direction. This
    harness runs 1000 per workload, where the interval spans ranks 935 to 964 --
    bounded, and tight enough to be worth printing.
    """
    if not 0.0 < quantile < 1.0:
        raise ValueError(f"quantile must be in (0, 1), got {quantile}")
    n = 1
    while True:
        lower, upper = _ci_ranks(n, quantile)
        if lower >= 1 and upper <= n:
            return n
        n += 1


def percentile(samples: list[float], quantile: float) -> Percentile:
    """Compute `quantile` of `samples` by order statistics, with a 95% CI.

    `samples` need not be sorted. The nearest-rank convention is used: the
    reported value is a real observation, never an interpolation between two.
    """
    if not samples:
        raise ValueError("cannot compute a percentile of zero samples")
    if not 0.0 < quantile < 1.0:
        raise ValueError(f"quantile must be in (0, 1), got {quantile}")

    ordered = sorted(samples)
    n = len(ordered)

    # Nearest-rank: smallest rank r such that at least q of the data is <= x[r].
    rank = max(1, math.ceil(quantile * n))
    value = ordered[rank - 1]

    # Distribution-free CI for a quantile, via the binomial order statistics.
    lower_rank, upper_rank = _ci_ranks(n, quantile)

    ci_low = ordered[lower_rank - 1] if 1 <= lower_rank <= n else None
    ci_high = ordered[upper_rank - 1] if 1 <= upper_rank <= n else None

    return Percentile(
        quantile=quantile,
        value=value,
        ci_low=ci_low,
        ci_high=ci_high,
        rank=rank,
        n=n,
    )


@dataclass(frozen=True)
class Summary:
    """Everything we are willing to publish about one set of latency samples.

    p99 is deliberately absent. This harness drives targets with a closed-loop
    client -- the next request is issued only after the previous one returns --
    which under-samples stalls. The distortion is roughly 1x at p50 and 1.5x at
    p95, but 20x or worse at p99. Publishing a closed-loop p99 would be
    dishonest, so `Summary` gives callers no way to do it. The mixed-workload
    runner uses an open-loop generator and reports separately.
    """

    n: int
    mean: float
    stdev: float
    minimum: float
    maximum: float
    p50: Percentile
    p95: Percentile

    def as_dict(self) -> dict:
        """Flatten for JSON serialisation into the results file."""
        return {
            "n": self.n,
            "mean_ms": self.mean,
            "stdev_ms": self.stdev,
            "min_ms": self.minimum,
            "max_ms": self.maximum,
            "p50_ms": self.p50.value,
            "p50_ci_low_ms": self.p50.ci_low,
            "p50_ci_high_ms": self.p50.ci_high,
            "p95_ms": self.p95.value,
            "p95_ci_low_ms": self.p95.ci_low,
            "p95_ci_high_ms": self.p95.ci_high,
            "p95_ci_bounded": self.p95.is_bounded,
        }


def summarise(samples: list[float]) -> Summary:
    """Reduce raw per-iteration durations to the figures we publish.

    Mean and standard deviation are included despite being poor descriptions of
    latency, because omitting them is its own form of cherry-picking: one
    published vendor benchmark reported p99 alone, and the competitor it named
    had in fact won the mean, p50, p90 and p95. Publish the whole picture.
    """
    if not samples:
        raise ValueError("cannot summarise zero samples")

    n = len(samples)
    mean = sum(samples) / n
    variance = sum((s - mean) ** 2 for s in samples) / n if n > 1 else 0.0

    return Summary(
        n=n,
        mean=mean,
        stdev=math.sqrt(variance),
        minimum=min(samples),
        maximum=max(samples),
        p50=percentile(samples, 0.50),
        p95=percentile(samples, 0.95),
    )
